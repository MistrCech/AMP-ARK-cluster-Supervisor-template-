"""Most mezi chatem clusteru a jednim kanalem na Discordu.

Zamerne bez externi zavislosti (urllib), jako rcon.py, a bez Gateway - jen
REST API v10:
- hra -> Discord: POST /channels/{id}/messages; co se nahromadi, odejde
  jednou zpravou (rate limit kanalu je par zprav za sekundu);
- Discord -> hra: GET /channels/{id}/messages?after=<posledni> kazde 3 s.

Bot potrebuje v kanalu View Channel, Send Messages a Read Message History,
a v Developer Portalu (Bot -> Privileged Gateway Intents) Message Content
Intent - bez nej Discord vraci u cizich zprav prazdny text, i pres REST.
Public Key aplikace k nicemu z toho neni - slouzi jen k overovani podpisu
interakci (slash prikazy pres HTTP endpoint).

Token se nikdy nevypisuje. Chyby 401/403/404 se ohlasi jednou a most pak
zkousi jen jednou za 10 minut - Discord docasne banuje adresu po 10 000
neplatnych pozadavcich za 10 minut, a to se pri spatnem tokenu a pollingu
po 3 s nesmi ani priblizit.
"""
import http.client
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request

API = "https://discord.com/api/v10"
# Discord vyzaduje u botu tenhle tvar. Vychozi "Python-urllib/3.x" blokuje
# Cloudflare pred API.
USER_AGENT = "DiscordBot (https://github.com/MistrCech/ArkClusterAMP, 1.0)"
POLL_INTERVAL = 3.0
MAX_CONTENT = 2000
# Pri spatnem tokenu, chybejicim opravneni nebo kanalu.
FATAL_RETRY = 600.0
# Zprava bez nahledu odkazu - most nema zahlcovat kanal embedy.
SUPPRESS_EMBEDS = 1 << 2
# Typ zpravy: 0 = bezna, 19 = odpoved. Ostatni (pripnuti, pripojeni clena,
# boost...) do hry nepatri.
CHAT_TYPES = (0, 19)

_MD_SPECIAL = re.compile(r"([\\*_~`|<>\[\]()])")


def escape_markdown(text):
    """Text ze hry doslova - bez tucneho pisma, odkazu [x](url) a zminek <@id>."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def plain_text(message):
    """Obsah zpravy z Discordu jako jeden radek obycejneho textu."""
    content = message.get("content") or ""
    names = {str(user.get("id")): user.get("global_name") or user.get("username") or "?"
             for user in message.get("mentions") or []}
    content = re.sub(r"<@!?(\d+)>", lambda m: "@" + names.get(m.group(1), "?"), content)
    content = re.sub(r"<@&\d+>", "@role", content)
    content = re.sub(r"<#\d+>", "#kanal", content)
    content = re.sub(r"<a?:(\w+):\d+>", r":\1:", content)
    content = re.sub(r"<t:(-?\d+)(?::[tTdDfFRsS])?>",
                     lambda m: time.strftime("%d.%m. %H:%M", time.localtime(int(m.group(1)))),
                     content)
    content = re.sub(r"\*\*|__|~~|\|\||`", "", content)
    parts = [" ".join(content.split())]
    if message.get("attachments"):
        parts.append("[priloha]")
    if message.get("sticker_items"):
        parts.append("[nalepka]")
    return " ".join(p for p in parts if p)


class DiscordError(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.retry_after = retry_after


class DiscordBridge:
    """Dve vlakna: odesilani z fronty a polling kanalu (jen s inbound=True)."""

    def __init__(self, token, channel_id, on_message, log, stopping, inbound=True):
        self._token = token
        self.channel_id = str(channel_id)
        # on_message(jmeno, text) - zprava z Discordu pro hru.
        self.on_message = on_message
        self.log = log
        self.stopping = stopping
        self.inbound = inbound
        self.bot_id = None
        self._last_id = None
        self._queue = queue.Queue(maxsize=500)
        self._carry = None
        # (metoda, cesta bez query) -> monotonic, do kdy se na route nesmi.
        self._route_wait = {}
        self._failing = set()
        self._warned = set()

    def start(self):
        threading.Thread(target=self._send_loop, name="discord-send", daemon=True).start()
        if self.inbound:
            threading.Thread(target=self._poll_loop, name="discord-poll", daemon=True).start()

    def send(self, line):
        """Zaradi radek (uz s markdownem) k odeslani. Nikdy neblokuje."""
        try:
            self._queue.put_nowait(line[:MAX_CONTENT])
        except queue.Full:
            self._warn_once("full", "fronta pro Discord je plna - zpravy ze hry se zahazuji")

    # --- HTTP ---

    def _request(self, method, path, body=None, timeout=15):
        route = (method, path.split("?", 1)[0])
        wait = self._route_wait.get(route, 0) - time.monotonic()
        if wait > 0:
            self.stopping.wait(wait)
        headers = {"Authorization": f"Bot {self._token}", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(API + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                self._note_limits(route, response.headers)
                payload = response.read()
        except urllib.error.HTTPError as exc:
            self._note_limits(route, exc.headers)
            raw = exc.read()
            try:
                detail = json.loads(raw or b"{}")
            except ValueError:
                detail = {}
            retry_after = detail.get("retry_after")
            if retry_after is None and exc.headers is not None:
                retry_after = exc.headers.get("Retry-After")
            try:
                retry_after = float(retry_after) if retry_after is not None else None
            except ValueError:
                retry_after = None
            raise DiscordError(exc.code, detail.get("message") or exc.reason,
                               retry_after) from None
        return json.loads(payload) if payload else None

    def _note_limits(self, route, headers):
        if headers is None:
            return
        try:
            if headers.get("X-RateLimit-Remaining") == "0":
                reset_after = float(headers.get("X-RateLimit-Reset-After") or 1)
                self._route_wait[route] = time.monotonic() + reset_after
        except ValueError:
            pass

    def _retry_delay(self, what, exc, attempt):
        """Kolik pockat pred dalsim pokusem, nebo None = vzdat tenhle pozadavek."""
        if isinstance(exc, DiscordError):
            if exc.status == 429:
                return (exc.retry_after or 1.0) + 0.25
            if exc.status == 401:
                self._fail(what, "Discord odmita token bota (401) - zkontroluj "
                                 "Discord Bot Token v nastaveni instance")
                return FATAL_RETRY
            if exc.status == 403:
                self._fail(what, f"bot nema v kanalu opravneni (403: {exc}) - potrebuje "
                                 "View Channel, Send Messages a Read Message History")
                return FATAL_RETRY
            if exc.status == 404:
                self._fail(what, f"kanal {self.channel_id} nenalezen (404) - zkontroluj "
                                 "Discord Channel ID")
                return FATAL_RETRY
            if 400 <= exc.status < 500:
                # Vadny pozadavek (napr. prilis dlouha zprava) - opakovat nema smysl.
                self._fail(what, f"Discord odmitl pozadavek ({exc})")
                return None
        self._fail(what, f"Discord nedostupny ({exc}), zkousim dal")
        return min(5.0 * 2 ** attempt, 120.0)

    def _fail(self, what, message):
        if what not in self._failing:
            self._failing.add(what)
            self.log(message)

    def _ok(self, what):
        if what in self._failing:
            self._failing.discard(what)
            self.log("spojeni s Discordem zase funguje")

    def _warn_once(self, key, message):
        if key not in self._warned:
            self._warned.add(key)
            self.log(message)

    # --- hra -> Discord ---

    def _next_batch(self):
        """Radky, ktere se vejdou do jedne zpravy - prvni ceka na frontu."""
        first = self._carry
        self._carry = None
        if first is None:
            try:
                first = self._queue.get(timeout=1)
            except queue.Empty:
                return None
        batch, size = [first], len(first)
        while True:
            try:
                line = self._queue.get_nowait()
            except queue.Empty:
                break
            if size + 1 + len(line) > MAX_CONTENT:
                self._carry = line
                break
            batch.append(line)
            size += 1 + len(line)
        return "\n".join(batch)

    def _send_loop(self):
        while not self.stopping.is_set():
            content = self._next_batch()
            if content is None:
                continue
            attempt = 0
            while not self.stopping.is_set():
                try:
                    self._request("POST", f"/channels/{self.channel_id}/messages",
                                  {"content": content, "flags": SUPPRESS_EMBEDS,
                                   "allowed_mentions": {"parse": []}})
                    self._ok("send")
                    break
                except (DiscordError, OSError, ValueError, http.client.HTTPException) as exc:
                    delay = self._retry_delay("send", exc, attempt)
                    if delay is None:
                        break
                    attempt += 1
                    self.stopping.wait(delay)

    # --- Discord -> hra ---

    def _poll_loop(self):
        attempt = 0
        while not self.stopping.is_set() and self._last_id is None:
            try:
                me = self._request("GET", "/users/@me")
                # Posledni zprava kanalu - historie se do hry neposila.
                latest = self._request("GET", f"/channels/{self.channel_id}/messages?limit=1")
                self.bot_id = str(me["id"])
                self._last_id = str(latest[0]["id"]) if latest else "0"
                self._ok("poll")
                self.log(f"Discord: bot {me.get('username')}, kanal {self.channel_id}")
            except (DiscordError, OSError, ValueError, KeyError, TypeError,
                    http.client.HTTPException) as exc:
                delay = self._retry_delay("poll", exc, attempt)
                attempt += 1
                self.stopping.wait(delay or FATAL_RETRY)
        attempt = 0
        while not self.stopping.wait(POLL_INTERVAL):
            try:
                messages = self._request(
                    "GET", f"/channels/{self.channel_id}/messages?after={self._last_id}&limit=100")
                self._ok("poll")
                attempt = 0
            except (DiscordError, OSError, ValueError, http.client.HTTPException) as exc:
                delay = self._retry_delay("poll", exc, attempt)
                attempt += 1
                self.stopping.wait(delay or FATAL_RETRY)
                continue
            for message in sorted(messages or [], key=lambda m: int(m["id"])):
                self._last_id = str(message["id"])
                try:
                    self._deliver(message)
                except Exception as exc:  # jedna zprava nesmi zastavit most
                    self.log(f"zprava z Discordu nesla predat: {exc}")

    def _deliver(self, message):
        author = message.get("author") or {}
        if (author.get("bot") or message.get("webhook_id")
                or str(author.get("id")) == self.bot_id
                or message.get("type", 0) not in CHAT_TYPES):
            return
        text = plain_text(message)
        if not text:
            if not (message.get("content") or message.get("embeds")
                    or message.get("attachments") or message.get("sticker_items")):
                self._warn_once("intent", "zpravy z Discordu chodi bez textu - zapni "
                                          "Message Content Intent (Developer Portal -> "
                                          "Bot -> Privileged Gateway Intents)")
            return
        name = author.get("global_name") or author.get("username") or "?"
        self.on_message(name, text)
