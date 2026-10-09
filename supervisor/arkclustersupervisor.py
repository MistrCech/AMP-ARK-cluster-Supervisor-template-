#!/usr/bin/env python3
"""Supervisor ARK clusteru - vsechny mapy z jedne instalace, v jedne instanci AMP.

ARK: Survival Evolved (Linux i Windows) a ARK: Survival Ascended (Windows) - hru
vybira promenna ARK_GAME (ase / asa). Spousti se misto herniho serveru. Mapy
dostane jako argumenty (kazde zaskrtavatko v AMP se rozvine na jmeno mapy nebo
prazdno), zbytek konfigurace cte z promennych prostredi, ktere naplni sablona.

Prikazy prijima na stdin - tim je konzole AMP zaroven ovladacim panelem.
"""
import collections
import configparser
import json
import os
import queue
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from discordbridge import DiscordBridge, escape_markdown
from rcon import RconClient, RconError, RconTimeout

IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import winapi
    resource = None
else:
    import resource      # jen POSIX - vypnuti core dumpu

# Poradi map je zavazne: urcuje prirazeni portu a musi sedet se souborem
# portu sablony (arkclusterports.json / arkasaclusterports.json).
GAMES = {
    "ase": {
        "app_dir": "376030",
        "binary": "ShooterGame/Binaries/Linux/ShooterGameServer",
        "config_subdir": "LinuxServer",
        # ASE bezi i na Windows (ASA jen tam) - na nem tyhle hodnoty. RCON
        # windowsoveho buildu se chova jako ASA: na prazdny paket neodpovi
        # (v361.7, overeno 6. 10. 2026 - s terminatorem vyprsel kazdy prikaz).
        "windows": {"binary": "ShooterGame/Binaries/Win64/ShooterGameServer.exe",
                    "config_subdir": "WindowsServer",
                    "rcon_terminator": False},
        "maps": [
            "TheIsland", "TheCenter", "ScorchedEarth_P", "Ragnarok", "Aberration_P",
            "Extinction", "Valguero_P", "Genesis", "CrystalIsles", "Gen2",
            "LostIsland", "Fjordur", "Aquatica",
        ],
        # Radek logu, podle ktereho se pozna, ze mapa nabehla (30-40 s).
        "started_re": r'^(?:Server: ".*" has successfully started!'
                      r'|Server has completed startup and is now advertising for join)',
        # Konec odpovedi RCON znaci odpoved na prazdny paket - viz rcon.py.
        # Jen linuxovy build, Windows viz vyse.
        "rcon_terminator": True,
        # Odkud brat chat hracu: "rcon" = GetChat kazdych 5 s, "log" = radky
        # chatu z logu mapy (UTF-8, okamzite). Viz chat_loop.
        "chat_source": "rcon",
    },
    "asa": {
        "app_dir": "2430930",
        "binary": "ShooterGame/Binaries/Win64/ArkAscendedServer.exe",
        "config_subdir": "WindowsServer",
        "maps": [
            "TheIsland_WP", "ScorchedEarth_WP", "TheCenter_WP", "Aberration_WP",
            "Extinction_WP", "Astraeos_WP", "Ragnarok_WP", "Valguero_WP",
            "LostColony_WP", "Genesis_WP",
        ],
        # "has successfully started!" pise ASA u noveho sveta na ZACATKU
        # nacitani (0,7 GB RAM, ~5 s po startu). Svet je nacteny az s timhle
        # radkem, 1-2,5 min po startu (v94.15). RCON port posloucha od ~15 s,
        # ale odpovida az po "Full Startup", 4-10 s pred timhle radkem.
        "started_re": r"^Server has completed startup and is now advertising for join",
        # ASA na prazdny paket neodpovi a spojeni pak mlci uplne - viz rcon.py.
        "rcon_terminator": False,
        # ASA pise chat i do logu, v UTF-8 - GetChat vraci kodovou stranku
        # systemu (z c, r, e s hackem '?'). Zustava "rcon": GetChat rozhoduje,
        # co je globalni chat (tribe chat v logu byt muze a nesmi jit vsem ani
        # na Discord), pismena s hackem doplni ChatRestorer z logu.
        "chat_source": "rcon",
    },
}
GAME = (os.environ.get("ARK_GAME") or "ase").strip().lower()
PROFILE = dict(GAMES.get(GAME, GAMES["ase"]))
if IS_WINDOWS:
    PROFILE.update(PROFILE.pop("windows", {}))
CANONICAL_MAPS = PROFILE["maps"]

# Windows: herni server bez okna konzole a ve vlastni skupine procesu.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE.parent / "supervisor-state.json"



# Ceske uvozovky a pomlcky NFKD na ASCII nerozlozi - bez tohohle by z nich byl '?'.
_CHAT_PUNCT = str.maketrans({"\u201e": '"', "\u201c": '"', "\u201d": '"', "\u00ab": '"',
                             "\u00bb": '"', "\u201a": "'", "\u2018": "'", "\u2019": "'",
                             "\u2013": "-", "\u2014": "-"})


def chat_text(text):
    """Text pro ServerChat/Broadcast jen v ASCII.

    ARK prikaz z RCON neprekodovava - kazdy bajt nad 127 vezme jako znak se
    znamenkem, takze z UTF-8 'r' s hackem (C5 99) je ve hre U+FFC5 U+FF99
    (overeno v94.15). Zadne kodovani tudy neprojde; bez hacku a carek je text
    aspon citelny ('Prilis zlutoucky kun'). Emoji a neviditelne znaky
    (z Discordu) zmizi, jine pismo jde na '?'.
    """
    out = []
    for ch in unicodedata.normalize("NFKD", text.translate(_CHAT_PUNCT)):
        if ord(ch) < 128:
            out.append(ch if ch.isprintable() else " ")
            continue
        category = unicodedata.category(ch)[0]
        if category == "Z":
            out.append(" ")
        elif category not in "MSC":         # diakritika, symboly, ridici
            out.append("?")
    return " ".join("".join(out).split())


# Radek chatu hrace "Ucet (Postava): zprava" - v GetChat bez casu, v logu
# mapy s nim: "2026.10.06_20.45.56: DeNNy (DeNNy): Zdravim" (ASA v94.15).
# Jmeno ani tribe nesmi obsahovat zavorky ani dvojtecku - jinak by preposlana
# zprava ("SERVER: [Mapa] Hrac: text") strukturalne odpovidala znovu.
CHAT_RE = re.compile(r"^(?:[\d.]+_[\d.]+:\s*)?([^()\[\]:]{1,64}?)\s*\(([^()\[\]]{1,64})\):\s*(.+)$")
# V logu jen s casovym razitkem herni udalosti - bez nej by za chat mohl
# projit kterykoli technicky radek tvaru "Neco (neco): text".
LOG_CHAT_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}:\s*"
                         r"([^()\[\]:]{1,64}?)\s*\(([^()\[\]]{1,64})\):\s*(.+)$")
# Ozvena ServerChat v logu kazde mapy - i kazde preposlane zpravy, tedy
# 9 radku na jednu zpravu hrace. Do konzole nepatri.
SERVER_ECHO_RE = re.compile(r"^(?:\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}:\s*)?SERVER: ")


def parse_chat(line, pattern=CHAT_RE):
    """(hrac, zprava) z radku chatu, jinak None."""
    match = pattern.match(line)
    if not match:
        return None
    account, player, message = match.groups()
    # ASA dava ke jmenu ikonu platformy - v kodove strance z ni zbude '?'
    # (v AMP '\ufffd'). Do zpravy nepatri.
    junk = " \t\u00a0\ufffd?"
    player = player.strip(junk) or account.strip(junk)
    message = message.strip()
    if not player or not message:
        return None
    return player, message


def _degraded_forms(ch):
    """Co muze z jednoho znaku logu (UTF-8) zbyt v odpovedi GetChat.

    ASA na Windows vraci GetChat v kodove strance a pismeno, ktere v ni neni,
    nahradi '?' (overeno 9. 10. 2026: 'čus' -> '?us'). Pismeno, ktere v ni je,
    muze projit beze zmeny; U+FFFD je od dekoderu RCON. Zakladni pismeno
    ("best-fit", č -> c) se nepripousti - videno nebylo a otevrelo by shodu
    s jinou zpravou ('kde jsí?' x 'kde jsi?').
    """
    if ch.isascii():
        return (ch,)
    return ("?", "\ufffd", ch)


def rcon_degraded(log_text, rcon_text):
    """True, kdyz rcon_text muze byt log_text po pruchodu RCON (pismena mimo kodovou stranku -> '?')."""
    expected = []
    for ch in log_text:
        forms = _degraded_forms(ch)
        # Znak mimo BMP (emoji) je v UTF-16 par - z kazde poloviny vlastni '?'.
        expected.extend([forms, forms] if ord(ch) > 0xFFFF else [forms])
    return len(expected) == len(rcon_text) and all(c in f for c, f in zip(rcon_text, expected))


def _same_player(log_name, rcon_name):
    """Je rcon_name jmeno z logu po pruchodu RCON? parse_chat z nej uz odrizl
    otazniky na krajich, tedy i pismena mimo kodovou stranku na zacatku a konci."""
    if rcon_degraded(log_name, rcon_name):
        return True
    trimmed = log_name.strip()
    while trimmed and not trimmed[0].isascii():
        trimmed = trimmed[1:].lstrip()
    while trimmed and not trimmed[-1].isascii():
        trimmed = trimmed[:-1].rstrip()
    return bool(trimmed) and trimmed != log_name and rcon_degraded(trimmed, rcon_name)


class ChatRestorer:
    """Vraci zpravam z GetChat ceska pismena z logu mapy.

    GetChat dal rozhoduje, CO je globalni chat; log mapy (UTF-8) obsahuje i tribe
    a lokalni chat, takze z nej se bere jen text a jen kdyz je to jiste:
    - kandidat je jen radek precteny od predchozi otazky GetChat na teze mape
      (zprava z odpovedi byla odeslana az po ni) a jeste nepouzity;
    - nejdriv se pocka, az vlakno logu docte soubor do konce po odpovedi GetChat -
      pak v kandidatech urcite je i radek globalni zpravy; kdyz to nestihne
      v rozpoctu, zustanou otazniky;
    - vsichni kandidati musi mit v logu jedno jmeno ('Čech' i 'Ťech' prijdou
      z GetChat jako 'ech') a jeden text stejneho tvaru (tribe 'ťus' x globalni
      'čus'), jinak zustanou otazniky - radsi '?us' nez cizi zprava na Discordu;
    - radek se stejnym textem ma prednost (rucne napsany otaznik);
    - pozdni radek uz odeslane zpravy se zahodi, aby nepripadl dalsi zprave.
    """
    WAIT = 1.5          # rozpocet cekani na dočteni logu za jeden pruchod chat_loop
    WINDOW = 120.0      # strop stari kandidata (mapa dlouho bez GetChat)

    def __init__(self, clock=time.monotonic, sleep=time.sleep, maxlen=256):
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._recent = collections.deque(maxlen=maxlen)  # [cas, hrac, zprava, pouzity]
        self._missed = collections.deque(maxlen=32)      # [cas, hrac, text z GetChat]
        self._evicted = None             # cas posledniho radku vytlaceneho z _recent
        self.last_eof = float("-inf")    # kdy vlakno logu naposled docetlo soubor
        self.log_has_chat = False        # bez chatu v logu (napr. ASE) nema cekani smysl

    def mark_eof(self):
        """Vola vlakno logu, kdyz docte soubor do konce."""
        self.last_eof = self._clock()

    def note(self, player, message):
        """Vola vlakno logu mapy: radek chatu tak, jak je v logu (UTF-8)."""
        # Ikona platformy u jmena (soukroma oblast Unicode) do Discordu nepatri.
        player = "".join(c for c in player if unicodedata.category(c) not in ("Co", "Cn", "Cs")).strip() or player
        now = self._clock()
        with self._lock:
            self.log_has_chat = True
            entry = [now, player, message, False]
            for miss in list(self._missed):
                if now - miss[0] <= self.WINDOW and _same_player(player, miss[1]) and rcon_degraded(message, miss[2]):
                    self._missed.remove(miss)
                    entry[3] = True
                    break
            if len(self._recent) == self._recent.maxlen:
                self._evicted = self._recent[0][0]
            self._recent.append(entry)

    def _miss(self, player, message):
        with self._lock:
            self._missed.append([self._clock(), player, message])
        return player, message

    def restore(self, player, message, degraded=True, since=float("-inf"), replied=None, wait=None):
        """(hrac, zprava) s pismeny z logu, nebo puvodni dvojice.

        degraded: radek z GetChat obsahoval '?' nebo U+FFFD (i ten, ktery parse_chat
        odrizl z kraje jmena). since: kdy se tahle mapa naposledy ptala GetChat.
        replied: kdy prisla tahle odpoved (None = bez cekani, jen testy).
        wait: kolik smi cekat na docteni logu.
        """
        if not degraded:
            return player, message
        if not self.log_has_chat:
            return self._miss(player, message)
        if replied is not None:
            deadline = self._clock() + max(0.0, self.WAIT if wait is None else wait)
            while self.last_eof < replied:
                if self._clock() >= deadline:
                    return self._miss(player, message)
                self._sleep(0.05)
        now = self._clock()
        with self._lock:
            if self._evicted is not None and self._evicted >= since:   # vytlaceny radek mohl byt ten globalni
                return player, message
            fresh = [e for e in self._recent if not e[3] and e[0] >= since and now - e[0] <= self.WINDOW
                     and _same_player(e[1], player)]
            exact = [e for e in fresh if e[2] == message]
            hits = exact or [e for e in fresh if rcon_degraded(e[2], message)]
            if not hits:
                self._missed.append([now, player, message])
                return player, message
            names = {e[1] for e in fresh if e[2] == message or rcon_degraded(e[2], message)}
            if len(names) > 1 or (not exact and len({e[2] for e in hits}) > 1):
                return player, message
            hits[0][3] = True
            return hits[0][1], hits[0][2]


_print_lock = threading.Lock()


def emit(source, line):
    """Jediny zpusob, jak z supervisoru neco vypsat.

    Prefix je nutny - konzole AMP sliva 13 zdroju do jednoho okna a bez nej
    by se v tom nedalo vyznat. Zaroven na tenhle format ciluji Console.*Regex
    v sablone, takze se nesmi menit bez upravy arkcluster.kvp.
    """
    with _print_lock:
        # Kazdy radek s prefixem - viceradkova odpoved RCON by jinak mela
        # prefix jen na prvnim.
        for part in str(line).splitlines() or [""]:
            sys.stdout.write(f"[{source}] {part}\n")
        sys.stdout.flush()


def log(msg):
    emit("supervisor", msg)


# --------------------------------------------------------------------------
# Konfigurace
# --------------------------------------------------------------------------

def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_int(name, default):
    try:
        return int(float(env(name) or default))
    except ValueError:
        return int(default)


def env_bool(name):
    return env(name) not in ("", "0", "False", "false")


class Config:
    def __init__(self):
        base = env("ARK_BASE_DIR")
        # Fallback pro rucni spousteni mimo AMP: supervisor/ je pod base dir.
        self.base = Path(base) if base else HERE.parent
        self.set_game_dir(self.base / PROFILE["app_dir"])
        self.cluster_dir = self.base / "clusterdata"
        # Windows: kratka cesta (junction) ke hre - viz use_short_path.
        self.short_path = env("ARK_SHORT_PATH")
        self.log_dir = self.base / "logs"

        # '?' by v URL mapy zacal dalsi volbu, '"' by na Windows rozbil
        # uvozovky kolem URL.
        self.session_name = (re.sub(r'[?"]', "", env("ARK_SESSION_NAME")).strip()
                             or "ARK Cluster")
        self.cluster_id = env("ARK_CLUSTER_ID") or "arkcluster"
        self.rcon_password = env("ARK_RCON_PASSWORD")
        self.server_password = env("ARK_SERVER_PASSWORD")
        self.max_players = env_int("ARK_MAX_PLAYERS", 70)
        self.start_delay = env_int("ARK_START_DELAY", 90)
        self.ready_timeout = env_int("ARK_READY_TIMEOUT", 600)
        self.save_timeout = env_int("ARK_SAVE_TIMEOUT", 180)
        self.cpu_pinning = env_bool("ARK_CPU_PINNING")
        self.auto_restart = env_bool("ARK_AUTO_RESTART")
        self.cross_chat = env_bool("ARK_CROSS_CHAT")
        # Most do Discordu - token bota, ID kanalu a smer z Discordu do hry,
        # viz discordbridge.py. Token se nikam nevypisuje.
        self.discord_token = env("ARK_DISCORD_TOKEN")
        self.discord_channel = env("ARK_DISCORD_CHANNEL")
        self.discord_to_game = env_bool("ARK_DISCORD_TO_GAME")
        self.rate_preset = env("ARK_RATE_PRESET") or "normal"
        self.custom_options = env("ARK_CUSTOM_OPTIONS")
        # Mody - cisla projektu z CurseForge (ASA) / Steam Workshopu (ASE).
        self.mods = [m for m in re.split(r"[\s,;]+", env("ARK_MODS")) if m]
        # Prepinace za URL mapy (-Neco), napr. -AllowFlyerSpeedLeveling.
        self.custom_args = env("ARK_CUSTOM_ARGS")
        # Radky navic na konec Game.ini - bez strip, jde o radky.
        self.gameini_extra = os.environ.get("ARK_GAMEINI_EXTRA", "")
        # Klice do GameUserSettings.ini (se sekcemi) - viz patch_gus.
        self.gus_extra = os.environ.get("ARK_GUS_EXTRA", "")
        self.bind_ip = env("ARK_BIND_IP") or "0.0.0.0"
        # MultiHome ma smysl jen kdyz AMP prideli konkretni adresu. Samotna
        # adresa nestaci - bez prepinace -MULTIHOME se funkce nezapne.
        self.multihome = self.bind_ip not in ("", "0.0.0.0", "::")
        self.hive_cleanup = env_bool("ARK_HIVE_CLEANUP")
        # Musi odpovidat App.ExitTimeout v sablone. SIGKILL od AMP se chytit
        # neda, takze jedina obrana je stihnout to driv.
        self.exit_timeout = env_int("ARK_EXIT_TIMEOUT", 900)
        # Plni write_shared_config z config/ServerSettings.ini.
        self.server_settings = []

        self.game_port = env_int("ARK_GAME_PORT", 7777)
        self.query_port = env_int("ARK_QUERY_PORT", 27015)
        self.rcon_port = env_int("ARK_RCON_PORT", 27100)

    def set_game_dir(self, game):
        self.game = Path(game)
        self.binary = self.game / PROFILE["binary"]
        # Win64 na Linuxu je symlink na Linux (update krok sablony), na Windows
        # je to skutecny adresar binarek.
        self.workdir = self.game / "ShooterGame/Binaries/Win64"
        self.config_dir = self.game / "ShooterGame/Saved/Config" / PROFILE["config_subdir"]

    def use_short_path(self):
        """Windows: spoustet hru pres junction s kratkou cestou (ARK_SHORT_PATH).

        ARK (UE4) neumi cesty nad 260 znaku a mody s -AutoManagedMods rozbaluje
        z <hra>/Engine/Binaries/ThirdParty/SteamCMD/Win64/steamapps/workshop/
        content/346110/<id>/WindowsNoEditor/... Pod instanci AMP ma zaklad hry
        63 znaku a zdroj nejhlubsiho souboru (CKF Remastered) 312 - rozbalovani
        se na prvnim dlouhem souboru tise zastavi a mod bez <id>.mod se
        nenacte (overeno 7. 10. 2026: 4 ze 7 modu). Cestu ke hre si UE bere
        z cesty k .exe, takze spusteni pres junction zkrati i tyhle cesty.
        """
        if not self.short_path or not IS_WINDOWS:
            return
        link, target = Path(self.short_path), self.game
        try:
            if os.path.lexists(link):
                current = os.readlink(link)
                if current.startswith("\\\\?\\"):     # os.readlink vraci \\?\D:\...
                    current = current[4:]
                if os.path.normcase(os.path.normpath(current)) != os.path.normcase(str(target)):
                    log(f"VAROVANI: {link} ukazuje na {current}, ne na {target} - "
                        f"kratka cesta se nepouzije")
                    return
            else:
                import _winapi
                _winapi.CreateJunction(str(target), str(link))
                log(f"zalozen junction {link} -> {target}")
        except OSError as exc:
            log(f"VAROVANI: kratka cesta {link} nejde pouzit ({exc}) - mody s dlouhymi "
                f"cestami se nenainstaluji")
            return
        self.set_game_dir(link)
        log(f"hra pres kratkou cestu {link}")

    def ports_for(self, index):
        """Porty se odvozuji od kanonickeho indexu mapy, ne od poradi spusteni.

        Diky tomu ma mapa porad stejny port, i kdyz se jina odskrtne.
        """
        return {
            "game": self.game_port + 2 * index,
            "query": self.query_port + index,
            "rcon": self.rcon_port + index,
        }


# --------------------------------------------------------------------------
# Topologie CPU
# --------------------------------------------------------------------------

def physical_cores():
    """Vrati seznam fyzickych jader jako mnoziny logickych CPU (vcetne SMT).

    Linux: sysfs, ne lscpu - spolehlivejsi a bez zavislosti na formatu
    vystupu. Windows: GetLogicalProcessorInformation.
    """
    if IS_WINDOWS:
        try:
            return winapi.physical_cores()
        except OSError:
            return []
    cores = []
    seen = set()
    base = Path("/sys/devices/system/cpu")
    try:
        cpu_dirs = sorted(base.glob("cpu[0-9]*"),
                          key=lambda p: int(p.name[3:]))
    except OSError:
        return cores
    for cpu_dir in cpu_dirs:
        siblings_file = cpu_dir / "topology/thread_siblings_list"
        try:
            raw = siblings_file.read_text().strip()
        except OSError:
            continue
        cpus = set()
        for part in raw.split(","):
            if "-" in part:
                lo, hi = part.split("-")
                cpus.update(range(int(lo), int(hi) + 1))
            else:
                cpus.add(int(part))
        key = tuple(sorted(cpus))
        if key not in seen:
            seen.add(key)
            cores.append(cpus)
    return cores


def assign_cores(map_names):
    """Kazda mapa dostane vlastni fyzicka jadra - ferovy dil volnych jader.

    Souvisly blok, protoze sousedni jadra byvaji ve stejnem CCX (spolecna L3).
    Dokud je jader aspon tolik co map, zadne dve mapy nesdileji jadro.
    Puvodne mela kazda mapa jedno jadro; ASA (UE5) je ale vic vlaknova a na
    Windows si pocet vlaken bere ze VSECH jader stroje ("Number of cores 32"
    i pri pinningu) - na dvou logickych CPU by se tlacila."""
    # Jen CPU, ktere proces smi pouzit - v kontejneru AMP s omezenym cpusetem
    # by jinak pinning mohl mirit na zakazana jadra a selhat.
    try:
        allowed = winapi.allowed_cpus() if IS_WINDOWS else os.sched_getaffinity(0)
    except OSError:
        allowed = None
    cores = physical_cores()
    if allowed is not None:
        cores = [core & allowed for core in cores if core & allowed]
    if not cores:
        log("VAROVANI: topologii CPU se nepodarilo precist, pinning vypnut")
        return {}
    # Jadro 0 nechavame systemu, pokud je jader dost.
    pool = cores[1:] if len(cores) > len(map_names) else cores
    if len(pool) < len(map_names):
        log(f"VAROVANI: {len(map_names)} map na {len(pool)} fyzickych jader - "
            f"nektere se o jadro podeli")
        return {name: pool[i % len(pool)] for i, name in enumerate(map_names)}
    # Zbytek po deleni dostanou prvni mapy - zadne jadro nezustane ladem.
    share, extra = divmod(len(pool), len(map_names))
    out, first = {}, 0
    for i, name in enumerate(map_names):
        size = share + (1 if i < extra else 0)
        out[name] = set().union(*pool[first:first + size])
        first += size
    return out


def cpu_list(cpus):
    """{2, 3, 4, 5, 9} -> '2-5,9' - do logu a statusu, i pro 20 CPU na mapu."""
    runs, out = [], []
    for cpu in sorted(cpus):
        if runs and cpu == runs[-1][1] + 1:
            runs[-1][1] = cpu
        else:
            runs.append([cpu, cpu])
    for first, last in runs:
        out.append(str(first) if first == last else f"{first}-{last}")
    return ",".join(out)


def display_name(map_name):
    """'TheIsland_WP' -> 'The Island', 'ScorchedEarth_P' -> 'Scorched Earth'.

    Do jmena serveru v prohlizeci - hracum interni jmeno mapy nic nerekne.
    """
    base = re.sub(r"_(WP|P)$", "", map_name)
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", base)


def udp_port_busy(port, host="0.0.0.0"):
    """Drzi UDP port uz nekdo? U UDP neni TIME_WAIT, takze test bindem
    nesplete dobehle spojeni s bezicim serverem."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def win_command_line(args):
    """Prikazova radka pro CreateProcess.

    subprocess.list2cmdline by do uvozovek uzavrel cely prepinac
    '-Klic=hodnota s mezerou'. UE ale hodnotu cte od '=' do prvni mezery,
    pokud sama nezacina uvozovkou - z -ClusterDirOverride by zbyl kus cesty.
    Proto -Klic="hodnota". Ostatni (exe, URL mapy) list2cmdline - URL s
    mezerou v SessionName tak projde (overeno v94.15).
    """
    out = []
    for arg in args:
        key, sep, value = arg.partition("=")
        if (arg.startswith("-") and sep and '"' not in value
                and any(ch.isspace() for ch in value)):
            out.append(f'{key}="{value}"')
        else:
            out.append(subprocess.list2cmdline([arg]))
    return " ".join(out)


_CLK_TCK = None if IS_WINDOWS else os.sysconf("SC_CLK_TCK")


def proc_stats(proc):
    """(RAM v kB, spotrebovane CPU v sekundach) procesu, nebo (0, None)."""
    if proc is None:
        return 0, None
    if IS_WINDOWS:
        try:
            return winapi.process_stats(proc._handle)
        except (OSError, AttributeError, ValueError):
            return 0, None
    rss_kb, cpu = 0, None
    try:
        with open(f"/proc/{proc.pid}/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
        with open(f"/proc/{proc.pid}/stat") as handle:
            # comm muze obsahovat mezery i zavorky - delit az za posledni ')'.
            fields = handle.read().rsplit(")", 1)[1].split()
        cpu = (int(fields[11]) + int(fields[12])) / _CLK_TCK
    except (OSError, ValueError, IndexError):
        pass
    return rss_kb, cpu


# ListPlayers: "0. Jmeno, 76561198012345678" - u crossplay muze byt misto
# SteamID id EOS (hex). Jmeno muze obsahovat carku, id je az za posledni.
PLAYER_LINE = re.compile(r"^\s*\d+\.\s*(?P<name>.+?),\s*(?P<id>[0-9A-Za-z]+)\s*$")


def parse_players(reply):
    """id -> jmeno. "No Players Connected" i cokoli necekaneho = zadny hrac."""
    out = {}
    for line in reply.splitlines():
        match = PLAYER_LINE.match(line)
        if match:
            out[match.group("id")] = match.group("name").strip()
    return out


# --------------------------------------------------------------------------
# Presety a stav
# --------------------------------------------------------------------------

def load_json(path, fallback):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        log(f"VAROVANI: {path.name} nelze precist ({exc}), pouzivam vychozi")
        return fallback


class State:
    """Prezije restart instance.

    Bez toho by se cluster po necekanem restartu tise vratil na normal
    uprostred eventu a nikdo by si toho hned nevsiml.
    """

    def __init__(self, default_preset):
        self.data = {"preset": default_preset, "pending": None,
                     "amp_preset": default_preset}
        if STATE_FILE.exists():
            loaded = load_json(STATE_FILE, None)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        # Rate Preset v nastaveni AMP se od posledniho behu zmenil -> ma
        # prednost pred presetem, ktery zbyl ve stavu (napr. po 'event set').
        # Bez toho by prepnuti v nastaveni instance nemelo zadny efekt.
        if self.data.get("amp_preset") != default_preset:
            self.data.update(preset=default_preset, pending=None,
                             amp_preset=default_preset)
            self.save()

    @property
    def preset(self):
        return self.data.get("preset") or "normal"

    @property
    def pending(self):
        return self.data.get("pending")

    def set_pending(self, name):
        self.data["pending"] = name
        self.save()

    def apply_pending(self):
        """Zavola se pri startu mapy - tim se preset 'veze' na restartu."""
        if self.data.get("pending"):
            self.data["preset"] = self.data.pop("pending")
            self.data["pending"] = None
            self.save()
        return self.preset

    def rcon_password(self):
        """Formular slibuje 'prazdne = vygeneruje se nahodne'.

        Heslo se uklada do stavu, aby prezilo restart - jinak by se menilo
        s kazdym startem a externi RCON nastroje by prestaly fungovat.
        """
        if not self.data.get("rcon_password"):
            self.data["rcon_password"] = secrets.token_urlsafe(18)
            self.save()
            log(f"RCON heslo neni nastavene - vygenerovano, je v {STATE_FILE.name}")
        return self.data["rcon_password"]

    def save(self):
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            # Muze obsahovat RCON heslo.
            tmp.chmod(0o600)
            tmp.replace(STATE_FILE)
        except OSError as exc:
            log(f"VAROVANI: stav se nepodarilo ulozit: {exc}")


# --------------------------------------------------------------------------
# Generovani sdilene konfigurace
# --------------------------------------------------------------------------

def load_server_settings():
    """[ServerSettings] z config/ServerSettings.ini -> ["Klic=hodnota", ...].

    Jde to na prikazovou radku, ne do GameUserSettings.ini: rucne psany
    GameUserSettings.ini ARK cely zahodi (overeno na v361.7), hodnoty z
    prikazove radky prevezme.
    """
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str           # ARK rozlisuje velikost pismen v klicich
    path = HERE / "config" / "ServerSettings.ini"
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        log(f"VAROVANI: {path.name} nelze precist ({exc}) - ServerSettings vynechany")
        return []
    out = []
    if parser.has_section("ServerSettings"):
        for key, value in parser.items("ServerSettings"):
            value = value.strip()
            # Mezera prikazovou radku utne, '?' ji rozdeli - oboji by tise
            # zahodilo vsechno za timhle klicem.
            if (not value or "?" in value or '"' in value
                    or any(ch.isspace() for ch in value)):
                log(f"VAROVANI: {key}={value!r} vynechano - prazdne, s mezerou "
                    f"nebo '?' by rozbilo prikazovou radku")
                continue
            out.append(f"{key}={value}")
    return out


def write_shared_config(cfg, presets, preset_name):
    """Pred startem mapy: Game.ini ze sablony a ServerSettings pro prikazovou radku.

    Game.ini se generuje a zamyka na 444 - ve vsech testech ho ARK nechal
    netknuty. GameUserSettings.ini se ZAMERNE negeneruje: ARK ho rucne napsany
    zahodi a pri kazdem startu i vypnuti si ho stejne prepise sam (zamek 444
    ho nezastavi - soubor smaze a zalozi znovu).
    """
    cfg.server_settings = load_server_settings()
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    preset = presets.get(preset_name) or {}
    extra = [line.strip() for line in cfg.gameini_extra.splitlines() if line.strip()]
    # Sazbu presetu, kterou instance nastavuje sama, z Game.ini vynechat -
    # jinak by tam klic byl dvakrat. Opakovane klice sablony (stacky,
    # engramy) se netykaji, preset je nema.
    own = {line.split("=", 1)[0].strip().lower() for line in extra if "=" in line}
    gameini_rates = {key: value for key, value in preset.get("gameini", {}).items()
                     if key.lower() not in own}

    rates = "\n".join(f"{key}={value}" for key, value in sorted(gameini_rates.items()))
    if not rates:
        rates = "; (preset 'normal' - zadne prepisy)"

    template = HERE / "config" / "Game.ini.template"
    out = cfg.config_dir / "Game.ini"
    if not template.exists():
        log("VAROVANI: chybi sablona Game.ini.template, Game.ini se negeneruje")
    else:
        try:
            if out.exists():
                out.chmod(0o644)
            text = template.read_text().replace("{{RATES}}", rates)
            if extra:
                # Konec sablony je porad sekce ShooterGameMode.
                text += ("\n; --- Z nastaveni instance (Game.ini - vlastni radky) ---\n"
                         + "\n".join(extra) + "\n")
            out.write_text(text)
            out.chmod(0o444)
        except OSError as exc:
            log(f"CHYBA: Game.ini nelze zapsat: {exc}")
    log(f"Konfig pripraven: preset '{preset_name}', Game.ini zamceno na 444, "
        f"ServerSettings {len(cfg.server_settings)} klicu na prikazovou radku")
    patch_gus(cfg)


def gus_entries(text):
    """[(sekce, klic, hodnota)] z radku pole instance; bez sekce = ServerSettings."""
    section, out = "ServerSettings", []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
        elif "=" in line:
            key, value = line.split("=", 1)
            if key.strip():
                out.append((section, key.strip(), value.strip()))
    return out


def patch_gus(cfg):
    """Doplni klice z nastaveni instance do GameUserSettings.ini, ktery napsal ARK.

    Nektere volby ARK z prikazove radky nebere (wiki: cli No) - napr.
    AllowCaveBuildingPvE nebo AllowMultipleTamedUnicorns v sekci [Ragnarok].
    Cely rucne psany soubor ARK zahodi (viz write_shared_config), ale klice
    doplnene do jeho vlastniho souboru nacte a pri prepisu ponecha (overeno
    na ASE v361.7 7. 10. 2026). Pred kazdym startem znovu - soubor je pro
    vsechny mapy spolecny a ARK ho prepisuje.
    """
    entries = gus_entries(cfg.gus_extra)
    if not entries:
        return
    path = cfg.config_dir / "GameUserSettings.ini"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        log("GameUserSettings.ini jeste neexistuje (prvni start) - vlastni klice "
            "se doplni pred dalsi mapou")
        return
    except OSError as exc:
        log(f"VAROVANI: GameUserSettings.ini nejde precist: {exc}")
        return
    encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
    text = raw.decode(encoding, errors="surrogateescape")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    for section, key, value in entries:
        start = next((i for i, line in enumerate(lines)
                      if line.strip().lower() == f"[{section.lower()}]"), None)
        if start is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [f"[{section}]", f"{key}={value}"]
            continue
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip().startswith("[")), len(lines))
        hits = [i for i in range(start + 1, end)
                if lines[i].split("=", 1)[0].strip().lower() == key.lower()]
        for i in hits:
            lines[i] = f"{key}={value}"
        if not hits:
            lines.insert(start + 1, f"{key}={value}")
    out = newline.join(lines) + newline
    if out == text:
        return
    try:
        path.write_bytes(out.encode(encoding, errors="surrogateescape"))
        log(f"GameUserSettings.ini: doplneno z nastaveni instance ({len(entries)} klicu)")
    except OSError as exc:
        log(f"VAROVANI: GameUserSettings.ini nejde zapsat: {exc}")


# --------------------------------------------------------------------------
# Jedna mapa
# --------------------------------------------------------------------------

class MapServer:
    # Radky logu mapy, ktere do konzole AMP nepatri. "Commandline:" ARK vypisuje
    # cely i s hesly - ten ven nesmi NIKDY.
    LOG_DROP = re.compile(
        r"^(?:Commandline:|Log file open|Number of cores|ADayCycleManager"
        r"|Server attempting to run new years|Sever Is not set to official"
        r"|Set New Years event location|SteamSocketsOpenSource: gethostname failed"
        r"|gethostbyname failed"
        # ASA: statistiky pameti a nastaveni CurseForge pri startu (i jeho
        # JSON), poznamky. Varovani a chyby LogMemory/LogCFCore projdou.
        r"|LogMemory: (?:Platform Memory Stats|Process Physical Memory:"
        r"|Process Virtual Memory:|Physical Memory:|Virtual Memory:)"
        r"|LogCFCore: (?:InternalSettings called|Detected OS)"
        r'|Added Explorer Note Entry|\d+$|[{}]$|")')
    # ASA sype na stdout analytiku (GameAnalytics) a kazdou minutu JSON
    # s vykonem, rozsekany a promichany mezi vlakny - ~65 radku za minutu na
    # mapu. Do konzole ani do logs/ nepatri.
    STDOUT_DROP = re.compile(r'GameAnalytics|^\s*["{}]|^\s*[\[\]]+,?\s*$')
    # Na tenhle radek logu se pozna, ze mapa nabehla - nezavisle na RCON,
    # ktery pri zatezi umi byt pomaly. Lisi se podle hry, viz GAMES.
    STARTED_RE = re.compile(PROFILE["started_re"])
    # Volby URL, ktere sklada supervisor sam (porty, hesla, save, jmeno).
    # Z nastaveni instance by rozbily cluster - napr. vlastni Port by dal
    # dvema mapam stejny port.
    MANAGED_OPTIONS = {"listen", "port", "queryport", "rconenabled", "rconport",
                       "serveradminpassword", "maxplayers", "altsavedirectoryname",
                       "rconservergamelogbuffer", "multihome", "serverpassword",
                       "gamemodids", "sessionname"}
    LOG_PREFIX = re.compile(r"^\[\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}:\d{3}\]\[\s*\d+\]")

    def __init__(self, name, index, cfg, cores):
        self.name = name
        self.index = index
        self.cfg = cfg
        self.cores = cores
        self.ports = cfg.ports_for(index)
        self.proc = None
        # -log=<Mapa>.log - viz command_line.
        self.log_file = cfg.game / "ShooterGame/Saved/Logs" / f"{name}.log"
        rcon_host = cfg.bind_ip if cfg.multihome else "127.0.0.1"
        self.rcon = RconClient(rcon_host, self.ports["rcon"], cfg.rcon_password,
                               terminator=PROFILE["rcon_terminator"],
                               encoding="mbcs" if IS_WINDOWS else "utf-8")
        self.ready = False
        self._ready_at = 0.0
        # Nastavi cteni logu na radek STARTED_RE.
        self.started = threading.Event()
        self.restarts = 0
        self.started_at = 0.0
        self.gave_up = False
        # id -> jmeno. Vzdy se NAHRAZUJE celym slovnikem, nikdy nemeni na
        # miste - ctenari (status, whereis, metriky) pak iteruji bez zamku.
        self.players = {}
        # Pri chat_source "log" sem _tail_log predava (mapa, hrac, zprava).
        self.chat_sink = None
        # Radky chatu z logu (UTF-8) - pri chat_source "rcon" z nich chat_loop
        # vraci ceska pismena, ktera GetChat nahradil '?'. Viz ChatRestorer.
        self.chat_restorer = ChatRestorer()
        # Kdy se chat_loop naposledy ptal GetChat (time.monotonic) - viz ChatRestorer.
        self.chat_asked = float("-inf")
        self._reader = None
        # Windows: job object mapy (KILL_ON_JOB_CLOSE + pinning), viz winapi.
        self._job = None
        # Zamysleny stav. Bez nej by hlidac po 30 s vratil mapu, kterou
        # obsluha zamerne zastavila.
        self.desired_up = False
        # RLock, protoze restart_map drzi zamek pres stop() i start().
        self._lock = threading.RLock()

    # --- spousteni ---

    def command_line(self, preset_rates):
        # Hesla MUSI byt na prikazove radce, i kdyz je odtud vidi kazdy lokalni
        # uzivatel v /proc/*/cmdline (a ARK radek vypise do logu). Overeno na
        # v361.7: bez ServerAdminPassword na radce se RCON port vubec neotevre
        # a ServerPassword jen z GameUserSettings.ini nechal server verejny.
        # Obrana je na urovni systemu (mount /proc s hidepid=2).
        opts = [self.name, "listen", f"Port={self.ports['game']}"]
        if GAME == "ase":
            # ASA bere query port jen z -QueryPort= (viz nize).
            opts.append(f"QueryPort={self.ports['query']}")
        opts += [
            "RCONEnabled=True",
            f"RCONPort={self.ports['rcon']}",
            f"ServerAdminPassword={self.cfg.rcon_password}",
        ]
        if GAME == "ase":
            # ASA bere pocet hracu z -WinLiveMaxPlayers (viz nize).
            opts.append(f"MaxPlayers={self.cfg.max_players}")
        opts += [
            # Kazda mapa MUSI mit vlastni save adresar, jinak si prepisou svet.
            f"AltSaveDirectoryName={self.name}",
            "RCONServerGameLogBuffer=600",
        ]
        if self.cfg.multihome:
            opts.append(f"MultiHome={self.cfg.bind_ip}")
        if self.cfg.server_password:
            opts.append(f"ServerPassword={self.cfg.server_password}")
        # Nastaveni hry: kazdy klic jen jednou a pozdejsi zdroj vyhrava -
        # ServerSettings.ini z repa < preset < nastaveni instance. Se dvema
        # vyskyty stejneho klice v URL neni jiste, ktery ARK vezme, takze by
        # hodnota z instance mohla tise prohrat s vychozi z repa.
        settings = {}

        def put(option, source):
            key = option.split("=", 1)[0].strip()
            if key.lower() in self.MANAGED_OPTIONS:
                emit(self.name, f"VAROVANI: {key} z {source} vynechano - "
                                f"spravuje ho supervisor")
                return
            settings.pop(key.lower(), None)
            settings[key.lower()] = option

        for option in self.cfg.server_settings:
            put(option, "ServerSettings.ini")
        for key, value in sorted(preset_rates.items()):
            put(f"{key}={value}", "presetu")
        for extra in self.cfg.custom_options.replace("\n", "?").split("?"):
            extra = extra.strip()
            if not extra:
                continue
            if '"' in extra or any(ch.isspace() for ch in extra):
                emit(self.name, f"VAROVANI: vlastni volba {extra!r} vynechana - "
                                f"mezera by prikazovou radku utnula")
                continue
            put(extra, "Extra Launch Options")
        opts.extend(settings.values())
        # SessionName POSLEDNI a BEZ uvozovek. UE si prikazovou radku sklada z
        # argv a URL mapy utne u prvni mezery - vsechno za ni tise zmizi.
        # Overeno na v361.7: s 'SessionName="X - Mapa"' uprostred se ztratil
        # ServerPassword (server byl verejny) a jmeno spadlo na 'ARK #205902'.
        if GAME == "ase" and self.cfg.mods:
            # ASE stahuje mody sam diky -AutoManagedMods (viz nize).
            opts.append("GameModIds=" + ",".join(self.cfg.mods))
        opts.append(f"SessionName={self.cfg.session_name} - {display_name(self.name)}")

        args = [str(self.cfg.binary), "?".join(opts)]
        args += [
            f"-ClusterDirOverride={self.cfg.cluster_dir}",
            f"-clusterid={self.cfg.cluster_id}",
        ]
        if GAME == "asa":
            # ?Port= v URL ASA ignoruje: mapa zkusi vychozi 7777, a kdyz je
            # obsazeny, vezme tise dalsi volny (overeno v94.15 - Ragnarok s
            # Port=7789 skoncil na 7779). Herni port jen pres -port=.
            # ?QueryPort= taky ne: Steam subsystem (overeni hracu ze Steamu,
            # servery se jinak hledaji pres EOS) pak u vsech map chce 27015
            # a uspeje jen prvni ("Steam Subsystem initialized: FAILED").
            # Dal jako upstream sablona CubeCoders ark-sa: pocet hracu,
            # crossplay vsech platforem, bez BattlEye.
            args += [f"-port={self.ports['game']}",
                     f"-QueryPort={self.ports['query']}",
                     f"-WinLiveMaxPlayers={self.cfg.max_players}",
                     "-ServerPlatform=ALL", "-NoBattlEye"]
            if self.cfg.mods:
                # Mody z CurseForge si server stahne sam pri startu.
                args.append("-mods=" + ",".join(self.cfg.mods))
        else:
            args += ["-AutoManagedMods", "-Crossplay", "-server"]
        for extra in self.cfg.custom_args.split():
            if extra.startswith("-") and '"' not in extra:
                args.append(extra)
            else:
                emit(self.name, f"VAROVANI: prepinac {extra!r} vynechan - musi "
                                f"zacinat '-' a nesmi obsahovat uvozovky")
        args += [
            # Vlastni log pro kazdou mapu - jinak 13 map pise do jednoho
            # ShooterGame.log a kazdy start ho prejmenuje na zalohu.
            f"-log={self.name}.log",
            # ARK jinak log drzi v bufferu a za behu je soubor prazdny.
            "-forcelogflush",
            "-servergamelog",
        ]
        if self.cfg.multihome:
            # Adresa sama o sobe nic nezapne - tohle je ten prepinac.
            args.append("-MULTIHOME")
        return args

    def start(self, preset_rates):
        with self._lock:
            self._start_locked(preset_rates)

    def _start_locked(self, preset_rates):
        if self.running:
            emit(self.name, "uz bezi, start preskocen")
            return
        self.desired_up = True
        self.started_at = time.time()
        self.gave_up = False
        self.ready = False
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.cluster_dir.mkdir(parents=True, exist_ok=True)
        self.ready = False
        self.started.clear()
        self.players = {}

        args = self.command_line(preset_rates)
        bind = self.cfg.bind_ip if self.cfg.multihome else "0.0.0.0"
        busy = [port for port in (self.ports["game"], self.ports["query"])
                if udp_port_busy(port, bind)]
        if busy:
            # Druha kopie mapy by bezela nad stejnym savem; ASA by si navic
            # tise vzala jiny port (overeno), takze by o ni nikdo nevedel.
            emit(self.name, f"CHYBA: UDP port {busy[0]} uz nekdo drzi - nebezi tu "
                            f"mapa z minuleho behu? Start vynechan.")
            return
        old_log = self._log_identity()
        emit(self.name, f"start na portech game={self.ports['game']} "
                        f"query={self.ports['query']} rcon={self.ports['rcon']}")
        # Afinita je na Linuxu vlastnost VLAKNA a fork ji dedi od vlakna, ktere
        # ho vola. Nastavit ji procesu az po startu chyti jen hlavni vlakno -
        # co server stihne vytvorit driv, zustane na vsech jadrech. Proto se na
        # okamzik prisprendli volajici vlakno a server se narodi uz pripnuty,
        # vcetne vsech svych budoucich vlaken.
        restore = None
        if self.cfg.cpu_pinning and self.cores and not IS_WINDOWS:
            try:
                restore = os.sched_getaffinity(0)
                os.sched_setaffinity(0, self.cores)
            except OSError as exc:
                restore = None
                emit(self.name, f"VAROVANI: pinning selhal: {exc}")
        if self._job:
            # Proces v nem uz nebezi (viz self.running vyse).
            winapi.close_handle(self._job)
            self._job = None
        try:
            self.proc = subprocess.Popen(
                win_command_line(args) if IS_WINDOWS else args,
                cwd=str(self.cfg.workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                               if IS_WINDOWS else 0),
            )
        finally:
            if restore is not None:
                try:
                    os.sched_setaffinity(0, restore)
                except OSError:
                    pass
        if restore is not None:
            emit(self.name, f"pin na CPU {cpu_list(self.cores)}")
        elif IS_WINDOWS:
            # Na Windows plati limit jobu pro cely proces vcetne vlaken, co uz
            # bezi - staci hned po startu. Viz winapi.bind_to_job.
            cpus = self.cores if self.cfg.cpu_pinning and self.cores else None
            try:
                self._job = winapi.bind_to_job(self.proc._handle, cpus)
                if cpus:
                    emit(self.name, f"pin na CPU {cpu_list(cpus)}")
            except (OSError, AttributeError) as exc:
                emit(self.name, f"VAROVANI: job object selhal - bez pinningu a bez "
                                f"ukonceni mapy pri padu supervisoru: {exc}")

        self._reader = threading.Thread(target=self._pump_output, daemon=True)
        self._reader.start()
        threading.Thread(target=self._tail_log, args=(self.proc, old_log),
                         daemon=True).start()

    def _log_identity(self):
        """(inode, zacatek souboru) logu mapy, nebo None.

        ARK pri startu stary log ZKOPIRUJE do zalohy a puvodni soubor vyprazdni
        a pise do nej znovu - inode zustava stejny (overeno). Novy log se proto
        pozna podle hlavicky "Log file open, <datum a cas>" na zacatku.
        """
        try:
            with open(self.log_file, "rb") as handle:
                return os.fstat(handle.fileno()).st_ino, handle.read(100)
        except OSError:
            return None

    def _tail_log(self, proc, old_identity):
        """Posila radky logu mapy do konzole.

        Na stdout ARK skoro nic nepise (dva radky ze Steam API), takze bez
        tohohle by v konzoli AMP nebylo ze serveru videt nic - ani start, ani
        savy, ani chyby.
        """
        handle, partial = None, ""
        try:
            while True:
                alive = proc.poll() is None
                if handle is None:
                    current = self._log_identity()
                    # Novy log: jina hlavicka (nebo inode) a uz neco obsahuje.
                    if current and current != old_identity and current[1]:
                        try:
                            handle = open(self.log_file, encoding="utf-8-sig",
                                          errors="replace", newline="")
                        except OSError:
                            handle = None
                    if handle is None:
                        if not alive:
                            return
                        time.sleep(0.2)
                        continue
                chunk = handle.readline()
                if not chunk:
                    self.chat_restorer.mark_eof()
                    if not alive:
                        return          # proces skoncil a zbytek je doctene
                    try:
                        # Zkraceny soubor (dalsi start mapy) - cist od zacatku.
                        if os.fstat(handle.fileno()).st_size < handle.tell():
                            handle.seek(0)
                            partial = ""
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.5)
                    continue
                partial += chunk
                if not partial.endswith("\n"):
                    continue            # radek jeste neni cely
                line, partial = partial, ""
                text = self.LOG_PREFIX.sub("", line.strip("\r\n")).strip()
                if self.STARTED_RE.match(text):
                    self.started.set()
                if not text or self.LOG_DROP.match(text) or SERVER_ECHO_RE.match(text):
                    continue
                chat = parse_chat(text, LOG_CHAT_RE)
                if chat:
                    try:
                        self.chat_restorer.note(*chat)
                    except Exception:           # nesmi zastavit cteni logu
                        pass
                    # Do konzole ho vypise relay jako "<hrac> zprava" - surovy
                    # radek by tam byl podruhe.
                    if self.chat_sink:
                        self.chat_sink(self, *chat)
                    continue
                emit(self.name, text)
        except (OSError, ValueError):
            pass
        finally:
            if handle:
                handle.close()

    def _pump_output(self):
        """Cte vystup serveru a normalizuje ho na format, ktery ceka AMP."""
        log_path = self.cfg.log_dir / f"{self.name}.log"
        try:
            handle = open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            handle = None
        try:
            for raw in self.proc.stdout:
                line = raw.rstrip("\n")
                # Herni log na stdout NENI (ASE v361.7: dva radky ze Steam API,
                # ASA: analytika, JSON a par radku zdvojenych s logem) - hraci
                # jdou z ListPlayers, viz players_loop. LOG_DROP i tady: hesla
                # z "Commandline:" nesmi ven, ani kdyby ho server vypsal sem.
                if (not line.strip() or self.STDOUT_DROP.search(line)
                        or self.LOG_DROP.match(line.strip())):
                    continue
                if handle:
                    handle.write(line + "\n")
                    handle.flush()
                emit(self.name, line)
        except (OSError, ValueError):
            pass
        finally:
            if handle:
                handle.close()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def probe_ready(self, timeout=5.0, cache=10.0):
        """Zjisti, jestli mapa PRAVE TED odpovida na RCON.

        Puvodne to byla jednorazova zapadka nastavena pri startu - mapa, ktera
        nabehla pozdeji nez ReadyTimeout, uz se nikdy nepovazovala za zivou
        a pri vypnuti nedostala SaveWorld vubec. Proto se to overuje az v
        okamziku pouziti, s kratkou cache.
        """
        if not self.running:
            self.ready = False
            return False
        now = time.time()
        if self.ready and now - self._ready_at < cache:
            return True
        try:
            self.rcon.command("ListPlayers", retry=False, timeout=timeout)
            self.ready, self._ready_at = True, now
            return True
        except (RconError, OSError):
            self.ready = False
            return False

    def wait_ready(self, timeout, abort=None):
        """Ceka, az mapa nabehne: radek v logu, nebo odpoved RCON.

        Log je spolehlivy a nezavisly na RCON. Funkce zavisle na RCON (chat,
        hraci) si ho overuji samy - players_loop drzi "ready".
        """
        deadline = time.time() + timeout
        next_probe = 0.0
        while time.time() < deadline:
            # Vypinani mapy (stop nastavi desired_up=False) nebo celeho clusteru
            # - necekat dal na start, stop_all by jinak cekal na nas.
            if not self.desired_up or (abort is not None and abort.is_set()):
                return False
            if not self.running:
                emit(self.name, "CHYBA: proces skoncil driv, nez nabehl")
                return False
            if self.started.is_set():
                emit(self.name, "ready")
                return True
            if time.time() >= next_probe:
                if self.probe_ready(timeout=5.0, cache=0.0):
                    emit(self.name, "ready (RCON)")
                    return True
                next_probe = time.time() + 15
            self.started.wait(1.0)
        emit(self.name, f"VAROVANI: nenabehla do {timeout} s")
        return False

    # --- ukonceni ---

    def stop(self, fixes, save_timeout, deadline=None):
        """deadline: cas (time.time()), do kdy musi byt mapa dole - rozpocet
        vypinani clusteru. Uklid se podle nej zkrati, SIGINT ma prednost."""
        with self._lock:
            self._stop_locked(fixes, save_timeout, deadline)

    def _stop_locked(self, fixes, save_timeout, deadline=None):
        """Zebrik: uklid -> korektni konec -> tvrde ukonceni.

        Linux (ASE): SIGINT - ARK pri nem svet ulozi a do par sekund skonci
        (overeno na v361.7, 7/7) a funguje, i kdyz RCON neodpovida.
        Windows (ASA): signal se tam poslat neda, korektni konec je RCON
        DoExit - ARK pri nem svet ulozi (stejne vypina upstream sablona ark-sa).
        """
        # Nastavit i kdyz uz mapa nebezi - jinak by ji hlidac zvedl zpatky.
        self.desired_up = False
        if not self.running:
            return
        rcon_ok = self.probe_ready()
        cleaned = False
        if fixes:
            if rcon_ok:
                cleaned, busy = self._run_fixes(fixes, save_timeout, deadline)
                if busy:
                    # Prikaz bez odpovedi na serveru dal bezi (DestroyWildDinos
                    # na Astraeos > 15 s, overeno) - RCON je jen zaneprazdneny.
                    rcon_ok = False
            else:
                emit(self.name, "RCON neodpovida - uklid vynechan")
        if IS_WINDOWS:
            self._exit_windows(rcon_ok, cleaned, save_timeout, deadline)
            return
        # ARK posloucha na SIGINT, ne SIGTERM; pri nem svet ulozi.
        emit(self.name, "posilam SIGINT (ARK pri nem svet ulozi)")
        self._signal(signal.SIGINT)
        if self._wait_exit(self._exit_wait(save_timeout, deadline)):
            emit(self.name, "ukonceno korektne")
            return
        emit(self.name, "CHYBA: nereaguje ani na SIGINT, tvrde ukonceni "
                        "(svet muze byt starsi)")
        self._kill()
        self._wait_exit(15)

    def _loaded(self):
        """Nabehla mapa od posledniho startu aspon jednou (log nebo RCON)?"""
        return self.started.is_set() or self._ready_at >= self.started_at

    def _exit_windows(self, rcon_ok, cleaned, save_timeout, deadline):
        if not rcon_ok and not self._loaded():
            # Svet se jeste nacita (RCON odpovida az po "Full Startup") - neni
            # co ukladat a cekani by stop nebo restart protahlo o minuty.
            emit(self.name, "jeste nenabehla - neni co ukladat, ukoncuji")
            self._kill()
            self._wait_exit(15)
            return
        if not rcon_ok:
            # Pomaly nebo zaneprazdneny (dobiha uklid) - bez RCON se mapa
            # korektne vypnout neda. Nejvys save_timeout a tak, aby pred
            # koncem rozpoctu zbyl cas.
            limit = time.time() + max(60, save_timeout)
            if deadline is not None:
                limit = min(limit, deadline - 30)
            while self.running and time.time() < limit:
                time.sleep(max(0.0, min(10.0, limit - time.time())))
                if self.probe_ready(cache=0.0):
                    break
        if not self.running:
            emit(self.name, "ukoncena")
            return
        if self.probe_ready():
            if cleaned:
                # Tezky prikaz par sekund po jinem tezkem ARK nevezme.
                emit(self.name, f"cekam {self.FIX_SPACING} s po uklidu")
                time.sleep(self.FIX_SPACING)
            try:
                reply = self.rcon.command("DoExit", timeout=30)
                emit(self.name, f"DoExit: {reply or '(prazdna odpoved)'}")
            except (RconError, OSError) as exc:
                emit(self.name, f"VAROVANI: DoExit selhal: {exc}")
            if self._wait_exit(self._exit_wait(save_timeout, deadline)):
                emit(self.name, "ukonceno korektne")
                return
            emit(self.name, "CHYBA: neskoncila, tvrde ukonceni (svet muze byt starsi)")
        else:
            # Bez DoExit sama neskonci - cekat na ni by jen protahlo vypinani.
            emit(self.name, "CHYBA: RCON neodpovida, korektni konec neni mozny - "
                            "tvrde ukonceni (svet muze byt starsi)")
        self._kill()
        self._wait_exit(15)

    @staticmethod
    def _exit_wait(save_timeout, deadline):
        """Jak dlouho cekat na konec - tvrde ukonceni ze stop_all prijde po deadline."""
        if deadline is None:
            return save_timeout
        return max(5, min(save_timeout, deadline - time.time() - 5))

    # Rozestup mezi tezkymi prikazy uklidu. Overeno na v361.7: tezky prikaz
    # (DestroyWildDinos, DestroyAll, SaveWorld) poslany par sekund po jinem
    # tezkem prikazu nedostal odpoved a RCON mapy pak minuty mlcel; s
    # rozestupem 40 s prosly tri po sobe.
    FIX_SPACING = 45

    # Jak dlouho cekat na odpoved prikazu uklidu. DestroyWildDinos na velke
    # mape (Astraeos, 16 GB) nestihl 15 s - server ho dodelal, ale supervisor
    # ho mezitim prohlasil za mrtvy a tvrde ukoncil.
    FIX_TIMEOUT = 120

    def _run_fixes(self, commands, save_timeout, deadline=None):
        """Uklid patri PRED vypnuti - repopulace pak probehne pri bootu.

        Vraci (odeslano, zaneprazdneny). Mezi prikazy FIX_SPACING s. Po prvnim
        prikazu bez odpovedi se konci - kazdy dalsi by jen cekal ve fronte za
        nim. Uklid se zkrati i tehdy, kdyby jinak nezbyl cas na korektni konec.
        """
        sent = False
        for position, cmd in enumerate(commands):
            if deadline is not None and (time.time() + self.FIX_SPACING + self.FIX_TIMEOUT
                                         + save_timeout > deadline):
                emit(self.name, "zbytek uklidu vynechan - nezbyl by cas "
                                "na korektni vypnuti")
                return sent, False
            if position:
                emit(self.name, f"cekam {self.FIX_SPACING} s pred dalsim uklidem")
                time.sleep(self.FIX_SPACING)
            try:
                reply = self.rcon.command(cmd, timeout=self.FIX_TIMEOUT)
            except RconTimeout as exc:
                emit(self.name, f"FIX {cmd!r} bez odpovedi ({exc}) - server ho "
                                f"nejspis jeste dodelava, zbytek uklidu vynechan")
                return True, True
            except (RconError, OSError) as exc:
                emit(self.name, f"FIX SELHAL {cmd!r}: {exc} - zbytek uklidu "
                                f"vynechan, RCON neodpovida")
                return sent, True
            sent = True
            # Preklep v nazvu tridy tise nedela nic - proto se loguje odpoved.
            emit(self.name, f"FIX {cmd} -> {reply or '(prazdna odpoved)'}")
        return sent, False

    def _signal(self, sig):
        """Jen POSIX - na Windows send_signal SIGINT nepodporuje."""
        try:
            self.proc.send_signal(sig)
        except (OSError, AttributeError, ValueError):
            pass

    def _kill(self):
        """Tvrde ukonceni: SIGKILL na Linuxu, TerminateProcess na Windows."""
        try:
            self.proc.kill()
        except (OSError, AttributeError):
            pass

    def _wait_exit(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running:
                return True
            time.sleep(1)
        return not self.running


# --------------------------------------------------------------------------
# Supervisor
# --------------------------------------------------------------------------

class Supervisor:
    MAX_RESTARTS = 5

    def __init__(self, cfg, map_names):
        self.cfg = cfg
        self.presets = load_json(HERE / "presets.json", {"normal": {}})
        self.fixes = load_json(HERE / "mapfixes.json", {})
        self.state = State(cfg.rate_preset)
        # Musi byt pred vytvorenim map - RCON klient dostava heslo v konstruktoru.
        if not cfg.rcon_password:
            cfg.rcon_password = self.state.rcon_password()
        self.stopping = threading.Event()
        # Nastavi se, az stop_all DOBEHNE - druhy volajici (signal behem DoExit)
        # na nej pocka, misto aby proces ukoncil uprostred vypinani.
        self.stopped = threading.Event()
        self._stop_claim = threading.Lock()
        self.quiesced = False
        # Zabranuje tomu, aby stop_all prosel kolem mapy, kterou prave
        # startuje start_all nebo hlidac - takova mapa by prezila supervisor.
        self.lifecycle = threading.Lock()
        self._rolling_lock = threading.Lock()
        self.startup = None
        # DiscordBridge, kdyz je nastaveny token i kanal - viz main().
        self.discord = None
        self._chat_queue = queue.Queue()

        cores = assign_cores(map_names) if cfg.cpu_pinning else {}
        self.maps = {}
        for name in map_names:
            index = CANONICAL_MAPS.index(name)
            self.maps[name] = MapServer(name, index, cfg, cores.get(name))
            if PROFILE["chat_source"] == "log":
                self.maps[name].chat_sink = self._queue_chat

    # --- presety ---

    def rates(self):
        preset = self.presets.get(self.state.preset) or {}
        return preset.get("cmdline", {})

    def fixes_for(self, name):
        """Bezpecny uklid vzdy, destruktivni jen kdyz je vyslovne zapnuty."""
        entry = self.fixes.get(name) or self.fixes.get("_default") or {}
        if isinstance(entry, list):        # starsi format souboru
            return list(entry)
        out = list(entry.get("safe", []))
        if self.cfg.hive_cleanup:
            out += list(entry.get("destructive", []))
        return out

    # --- zivotni cyklus ---

    def _safe_start(self, server):
        """server.start, ktery volajici vlakno neshodi.

        Popen umi vyhodit OSError (chybi binarka po nepovedenem updatu,
        EAGAIN...). Bez tohohle by umrel start_all nebo hlidac a zbyle mapy
        by se uz nikdy nespustily - bez jedineho radku v konzoli.
        """
        try:
            server.start(self.rates())
            return True
        except Exception as exc:  # noqa: BLE001 - vlakno nesmi umrit
            emit(server.name, f"CHYBA: start selhal: {exc}")
            return False

    def start_all(self):
        preset = self.state.apply_pending()
        try:
            write_shared_config(self.cfg, self.presets, preset)
        except OSError as exc:
            log(f"CHYBA: konfiguraci nejde pripravit: {exc}")
        log(f"Startuji {len(self.maps)} map, preset '{preset}', "
            f"prodleva {self.cfg.start_delay} s mezi mapami")
        for position, server in enumerate(self.maps.values()):
            if position:
                # Na cisty instalaci soubor vytvori az prvni mapa.
                patch_gus(self.cfg)
            pending = self._mods_pending()
            if pending:
                log(f"mody {', '.join(pending)} nejsou nainstalovane - {server.name} je "
                    f"nainstaluje a dalsi mapa pocka (limit {self.MOD_INSTALL_TIMEOUT} s "
                    f"misto {self.cfg.ready_timeout} s)")
            # Poradi zamku vsude stejne: lifecycle -> zamek mapy.
            with self.lifecycle:
                if self.stopping.is_set():
                    return
                started = self._safe_start(server)
            # wait_ready zamerne MIMO zamek - blokuje az ready_timeout a
            # nesmi tim drzet pripadne vypinani.
            if started:
                server.wait_ready(self.MOD_INSTALL_TIMEOUT if pending else self.cfg.ready_timeout,
                                  self.stopping)
            # Prodleva az mezi mapami, ne po posledni.
            if position < len(self.maps) - 1 and self.cfg.start_delay:
                log(f"cekam {self.cfg.start_delay} s pred dalsi mapou")
                self.stopping.wait(self.cfg.start_delay)
        log("vsechny mapy nastartovany")

    # Prvni start s ~1 GB modu trval 13 min (ASE, 7. 10. 2026).
    MOD_INSTALL_TIMEOUT = 7200

    def _mods_pending(self):
        """ASE: mody, ktere jeste nejsou rozbalene v Content/Mods (chybi <id>.mod).

        Mapa je s -AutoManagedMods stahuje a rozbaluje sama pred nactenim
        sveta. Kdyz pritom vyprsi ready timeout a nastartuje dalsi mapa,
        rozbaluji obe tytez soubory naraz - 4 ze 7 modu tak zustaly bez .mod
        a mapa bezela bez nich (overeno 7. 10. 2026).
        """
        if GAME != "ase" or not self.cfg.mods:
            return []
        mods_dir = self.cfg.game / "ShooterGame/Content/Mods"
        return [mod for mod in self.cfg.mods if not (mods_dir / f"{mod}.mod").exists()]

    def stop_all(self):
        """Ukonci cluster. Idempotentni: dalsi volajici pocka na dokonceni."""
        with self._stop_claim:
            first = not self.stopping.is_set()
            self.stopping.set()
        if not first:
            self.stopped.wait()
            return
        try:
            self._stop_all()
        finally:
            self.stopped.set()

    def _stop_all(self):
        # Sekvencne by 13 map trvalo nekolikanasobek App.ExitTimeout, AMP by
        # poslalo SIGKILL uprostred a zbytek map by zustal neulozeny a bezici.
        # Paralelne je cena maximum z map, ne soucet.
        # Musi se vejit POD App.ExitTimeout - SIGKILL od AMP chytit nejde,
        # takze jedina obrana je stihnout to driv.
        budget = max(30, min(int(self.cfg.exit_timeout * 0.8),
                             self.cfg.exit_timeout - 30))
        log(f"ukoncuji cluster paralelne, rozpocet {budget} s "
            f"(App.ExitTimeout={self.cfg.exit_timeout})")

        # Pockat, az postupny start uvidi 'stopping' a stahne se. Jinak by
        # stop_all prosel kolem mapy, kterou start_all za chvili spusti, a ta
        # by prezila supervisor a drzela porty.
        if self.startup is not None and self.startup.is_alive():
            log("cekam, az se dokonci rozbehnuty start")
            self.startup.join(timeout=30)

        # Rozpocet bezi od chvile, kdy vypinani opravdu zacina.
        deadline = time.time() + budget
        # Zamek zabrani tomu, aby start_all/hlidac spustil mapu za nami.
        with self.lifecycle:
            threads = []
            for server in self.maps.values():
                t = threading.Thread(
                    target=server.stop,
                    args=(self.fixes_for(server.name), self.cfg.save_timeout,
                          deadline),
                    daemon=True)
                t.start()
                threads.append(t)

        for t in threads:
            t.join(timeout=max(0, deadline - time.time()))

        # Sirotci: zabity supervisor nechava ShooterGameServer bezet a drzet
        # porty, takze pristi start instance neprojde. Tohle musi probehnout
        # na kazde ceste ven.
        for server in self.maps.values():
            if server.running:
                emit(server.name, "CHYBA: nedobehla v rozpoctu, tvrde ukonceni")
                server._kill()
                server._wait_exit(10)
            try:
                server.rcon.close()
            except OSError:
                pass
        left = [s.name for s in self.maps.values() if s.running]
        log("cluster ukoncen" + (f" - ZBYLY PROCESY: {left}" if left else ""))

    def restart_map(self, server):
        # Stop BEZ lifecycle: trva i minuty (uklid s rozestupy, SIGINT) a
        # nesmi tim blokovat vypinani clusteru ani hlidace.
        if self.stopping.is_set():
            return
        server.stop(self.fixes_for(server.name), self.cfg.save_timeout)
        preset = self.state.apply_pending()
        try:
            write_shared_config(self.cfg, self.presets, preset)
        except OSError as exc:
            log(f"CHYBA: konfiguraci nejde pripravit: {exc}")
        with self.lifecycle:
            if self.stopping.is_set():
                return
            server.restarts = 0
            started = self._safe_start(server)
        if started:
            server.wait_ready(self.cfg.ready_timeout, self.stopping)

    # --- smycky na pozadi ---

    # Mapa, ktera pred padem bezela aspon tak dlouho, dostane hlidace zpet
    # s plnym poctem pokusu - pet padu za tydny neni smycka padu.
    STABLE_UPTIME = 3600

    def watchdog_loop(self):
        while not self.stopping.wait(30):
            if not self.cfg.auto_restart:
                continue
            for server in self.maps.values():
                if self.stopping.is_set():
                    return
                # desired_up: mapu zastavenou obsluhou hlidac NESMI zvedat.
                if not server.desired_up or server.proc is None or server.running:
                    continue
                if time.time() - server.started_at > self.STABLE_UPTIME:
                    server.restarts = 0
                if server.restarts >= self.MAX_RESTARTS:
                    if not server.gave_up:
                        server.gave_up = True
                        emit(server.name, f"CHYBA: {self.MAX_RESTARTS} padu po sobe, "
                                          f"hlidac to vzdava - 'start {server.name}' rucne")
                    continue
                started = False
                # Poradi zamku vsude stejne: lifecycle -> zamek mapy.
                with self.lifecycle:
                    if self.stopping.is_set():
                        return
                    # Neblokujici - kdyz uz s mapou nekdo manipuluje, pristi kolo.
                    if not server._lock.acquire(blocking=False):
                        continue
                    try:
                        if server.running or not server.desired_up:
                            continue
                        server.restarts += 1
                        emit(server.name, f"spadla, restart {server.restarts}/"
                                          f"{self.MAX_RESTARTS}")
                        started = self._safe_start(server)
                    finally:
                        server._lock.release()
                if started:
                    server.wait_ready(self.cfg.ready_timeout, self.stopping)

    def _queue_chat(self, origin, player, message):
        # Vola vlakno logu mapy - preposilani (RCON na 9 map, Discord) ho
        # nesmi brzdit.
        self._chat_queue.put((origin, player, message))

    def chat_loop(self):
        """Chat z map do konzole AMP; s CrossChat i mezi mapami (misto Cross-Ark-Chat)."""
        if PROFILE["chat_source"] == "log":
            while not self.stopping.is_set():
                try:
                    origin, player, message = self._chat_queue.get(timeout=1)
                except queue.Empty:
                    continue
                self._relay(origin, player, message)
            return
        while not self.stopping.wait(5):
            # Rozpocet cekani na radky logu (ChatRestorer) na cely pruchod pres mapy.
            budget_end = time.monotonic() + ChatRestorer.WAIT
            for server in self.maps.values():
                if not server.running or not server.probe_ready():
                    continue
                asked = time.monotonic()
                try:
                    chat = server.rcon.command("GetChat")
                except (RconError, OSError):
                    continue
                replied, since = time.monotonic(), server.chat_asked
                server.chat_asked = asked
                for line in chat.splitlines():
                    line = line.strip()
                    # Vlastni preposlana zprava se vraci zpet v GetChat jako
                    # "SERVER: ..." (overeno v94.15) a regex ji nechyti ani
                    # jinak - chybi "(Postava)". Bez toho by se chat mezi
                    # mapami lavinovite rozmnozil.
                    if not line or line.startswith("SERVER:"):
                        continue
                    chat_line = parse_chat(line)
                    if chat_line:
                        # GetChat urcuje, co je globalni chat; pismena s hackem
                        # (v kodove strance '?') se vezmou z logu mapy.
                        try:
                            chat_line = server.chat_restorer.restore(
                                *chat_line, degraded="?" in line or "\ufffd" in line,
                                since=since, replied=replied, wait=budget_end - time.monotonic())
                        except Exception as exc:    # oprava pismen nesmi zastavit chat
                            log(f"chat: pismena z logu nejdou doplnit ({exc!r})")
                        self._relay(server, *chat_line)

    def _relay(self, origin, player, message):
        where = display_name(origin.name)
        # Do konzole AMP vzdy - na tenhle radek cili Console.UserChatRegex.
        emit(origin.name, f"<{player}> {message}")
        if self.discord:
            self.discord.send(f"**{escape_markdown(player)}** [{escape_markdown(where)}]: "
                              f"{escape_markdown(message)}")
        if self.cfg.cross_chat:
            self._server_chat(where, player, message, skip=origin)

    def relay_from_discord(self, name, text):
        """Zprava z kanalu na Discordu do hry - na vsechny mapy."""
        emit("discord", f"<{name}> {text}")
        self._server_chat("Discord", name, text)

    # Delsi zpravu hra stejne neukaze celou (zprava z Discordu muze mit
    # 2000 znaku).
    CHAT_MAX = 300

    def _server_chat(self, where, name, message, skip=None):
        # Jmeno zvlast - z "Mistr <emoji>" by jinak zbylo "Mistr : zprava".
        payload = f"[{where}] {chat_text(name) or '?'}: {chat_text(message)}"
        if len(payload) > self.CHAT_MAX:
            payload = payload[:self.CHAT_MAX - 3] + "..."
        for server in self.maps.values():
            if server is skip or not server.ready or not server.running:
                continue
            try:
                server.rcon.command(f"ServerChat {payload}")
            except (RconError, OSError):
                pass

    def metrics_loop(self):
        # AMP meri jen proces supervisoru (App.MonitorChildProcess umi jedno
        # dite, ne 13), takze jeho grafy CPU a RAM by ukazovaly skoro nulu.
        # Soucet za mapy si proto supervisor meri sam a posila ho v METRICS.
        last = {}                            # pid -> (CPU sekundy, cas)
        while not self.stopping.wait(60):
            up, rss_kb, cpu = 0, 0, 0.0
            now = time.time()
            seen = {}
            for server in self.maps.values():
                if not server.running:
                    continue
                up += 1
                pid = server.proc.pid
                map_rss, map_cpu = proc_stats(server.proc)
                rss_kb += map_rss
                if map_cpu is None:
                    continue
                seen[pid] = (map_cpu, now)
                if pid in last and now > last[pid][1]:
                    cpu += (map_cpu - last[pid][0]) / (now - last[pid][1])
            last = seen
            players = sum(len(s.players) for s in self.maps.values())
            # Na tenhle radek cili Console.MetricsRegex - z toho jsou grafy v AMP.
            emit("supervisor", f"METRICS maps_up={up} players={players} "
                               f"ram_gb={rss_kb / 1048576:.1f} cpu_cores={cpu:.1f}")

    def players_loop(self):
        """Seznam hracu z RCON ListPlayers.

        Server herni log na stdout nepise, takze join/leave odtamtud nejdou.
        ListPlayers navic dava ID, ktere KickPlayer/BanPlayer potrebuji -
        jmeno nestaci. Na tenhle vystup ciluji Console.UserJoinRegex a
        UserLeaveRegex v sablone.
        """
        while not self.stopping.wait(15):
            for server in self.maps.values():
                if self.stopping.is_set():
                    return
                if not server.running:
                    # Spadla nebo zastavena mapa - hraci na ni nejsou.
                    if server.players:
                        self._diff_players(server, {})
                    continue
                try:
                    reply = server.rcon.command("ListPlayers", timeout=5)
                except (RconError, OSError):
                    server.ready = False
                    continue          # stav nevime - seznam nemenit
                # Mapa nabehla podle logu, ale RCON se overuje az tady - bez
                # toho by ready zustalo False a chat ani hraci by se nerozjeli.
                server.ready, server._ready_at = True, time.time()
                if not reply:
                    # Prazdna odpoved neni "nikdo tu neni" - prazdny server pise
                    # "No Players Connected". Jinak by vsichni odesli a vratili se.
                    continue
                self._diff_players(server, parse_players(reply))

    @staticmethod
    def _diff_players(server, now):
        before = server.players
        for pid in now.keys() - before.keys():
            emit(server.name, f">>> {now[pid]} ({pid}) joined this ARK!")
        for pid in before.keys() - now.keys():
            emit(server.name, f"<<< {before[pid]} ({pid}) left this ARK!")
        server.players = now

    # --- prikazy z konzole AMP ---

    def find(self, name):
        if name in self.maps:
            return self.maps[name]
        matches = [s for key, s in self.maps.items()
                   if key.lower().startswith(name.lower())]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            log(f"mapa '{name}' nebezi nebo neexistuje")
        else:
            log(f"'{name}' je nejednoznacne: "
                f"{', '.join(s.name for s in matches)}")
        return None

    @staticmethod
    def _drop_token(text):
        """Zahodi prvni token ze SUROVEHO textu, s respektem k uvozovkam.

        shlex.split() uvozovky odstrani, ale zpetne slozit tokeny mezerou
        rozbije vsechno, co jich obsahuje vic. Proto se pracuje se surovym
        textem a jen se z nej ukroji prvni token.
        """
        text = text.lstrip()
        if text[:1] in ('"', "'"):
            end = text.find(text[0], 1)
            if end != -1:
                return text[end + 1:].lstrip()
        _, _, tail = text.partition(" ")
        return tail.lstrip()

    def handle(self, raw):
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]
        rest = raw.split(None, 1)[1] if len(parts) > 1 else ""

        if cmd in ("doexit", "exit", "stop") and not args:
            self.stop_all()
        elif cmd == "help":
            self.cmd_help()
        elif cmd == "status":
            self.cmd_status()
        elif cmd == "players":
            self.cmd_players()
        elif cmd == "start" and args:
            self._start_one(args[0])
        elif cmd == "stop" and args:
            self._stop_one(args[0])
        elif cmd == "restart" and args:
            self._restart_one(args[0])
        elif cmd == "broadcast" and rest:
            msg = args[0] if len(args) == 1 else rest
            self.cmd_all_rcon(f"Broadcast {chat_text(msg)}", quiet=True)
            log(f"broadcast: {msg}")
        elif cmd == "say" and len(args) >= 2:
            server = self.find(args[0])
            if server:
                msg = args[1] if len(args) == 2 else self._drop_token(rest)
                server.rcon.command(f"ServerChat {chat_text(msg)}")
        elif cmd == "rcon" and len(args) >= 2:
            self.cmd_rcon(args[0], args[1] if len(args) == 2
                          else self._drop_token(rest))
        elif cmd == "rconall" and rest:
            # DestroyWildDinos na velke mape trva i desitky sekund.
            self.cmd_all_rcon(rest, timeout=60)
        elif cmd == "saveall":
            # Velky svet se uklada i desitky sekund - 10 s by ho ohlasilo
            # jako chybu a zavrelo spojeni uprostred.
            self.cmd_all_rcon("SaveWorld", timeout=120)
        elif cmd in ("kick", "ban") and args:
            self.cmd_moderate(cmd, args[0])
        elif cmd == "whereis" and args:
            self.cmd_whereis(args[0])
        elif cmd == "event":
            self.cmd_event(args)
        elif cmd == "quiesce":
            self.cmd_quiesce(True)
        elif cmd == "dequiesce":
            self.cmd_quiesce(False)
        else:
            log(f"neznamy prikaz '{raw}' - napis 'help'")

    def cmd_help(self):
        for line in [
            "status                prehled map",
            "players               hraci po mapach",
            "start|stop|restart <mapa>",
            "broadcast <zprava>    na vsechny mapy",
            "say <mapa> <zprava>",
            "rcon <mapa> <prikaz>  |  rconall <prikaz>",
            "saveall               SaveWorld vsude (muze RCON map na minuty umlcet)",
            "kick|ban <hrac>       supervisor mapu dohleda sam",
            "whereis <hrac>",
            "event list|status|set <preset>|apply",
            "quiesce | dequiesce   zaloha za behu z AMP (save je atomicky)",
            "DoExit                korektni ukonceni celeho clusteru",
        ]:
            log("  " + line)

    def cmd_status(self):
        log(f"preset '{self.state.preset}'"
            + (f", ceka '{self.state.pending}' na pristi restart"
               if self.state.pending else ""))
        for server in self.maps.values():
            if server.running:
                state = "ready" if server.ready else "startuje"
                pid = server.proc.pid
            elif server.desired_up:
                state, pid = "SPADLA", "-"     # hlidac ji zvedne
            else:
                state, pid = "STOJI", "-"      # zastavena zamerne
            cores = cpu_list(server.cores) if server.cores else "-"
            ram = (f"{proc_stats(server.proc)[0] / 1048576:.1f}G"
                   if server.running else "-")
            log(f"  {server.name:<16} {state:<9} pid={pid:<8} "
                f"game={server.ports['game']} hracu={len(server.players)} "
                f"ram={ram} cpu={cores}")

    def cmd_players(self):
        total = 0
        for server in self.maps.values():
            if not server.running:
                continue
            names = sorted(server.players.values())
            total += len(names)
            log(f"  {server.name:<16} {len(names):>3}  "
                f"{', '.join(names) if names else '-'}")
        log(f"  celkem {total} hracu")

    def _start_one(self, name):
        server = self.find(name)
        if not server:
            return
        with self.lifecycle:
            # Bez teto kontroly by se mapa spustila za zady stop_all a
            # prezila supervisor (os._exit deti nezabiji).
            if self.stopping.is_set():
                log("cluster se vypina, start neprovedeny")
                return
            # Rucni start znovu otevre rozpocet restartu, jinak by pet
            # rucnich cyklu nechalo mapu bez ochrany proti padu.
            server.restarts = 0
            started = self._safe_start(server)
        if started:
            server.wait_ready(self.cfg.ready_timeout, self.stopping)

    def _stop_one(self, name):
        server = self.find(name)
        if server:
            server.stop(self.fixes_for(server.name), self.cfg.save_timeout)

    def _restart_one(self, name):
        server = self.find(name)
        if server:
            self.restart_map(server)

    def cmd_rcon(self, name, command):
        server = self.find(name)
        if not server:
            return
        try:
            emit(server.name, server.rcon.command(command) or "(prazdna odpoved)")
        except (RconError, OSError) as exc:
            emit(server.name, f"RCON selhalo: {exc}")

    def cmd_all_rcon(self, command, quiet=False, timeout=None):
        for server in self.maps.values():
            if not server.running:
                continue
            if not server.probe_ready():
                emit(server.name, f"RCON neodpovida, '{command}' neodeslano")
                continue
            try:
                reply = server.rcon.command(command, timeout=timeout)
                if not quiet:
                    emit(server.name, reply or "(prazdna odpoved)")
            except (RconError, OSError) as exc:
                emit(server.name, f"RCON selhalo: {exc}")

    def _locate(self, who):
        """Hrac podle ID (presne) nebo jmena (i cast). -> (mapa, id, jmeno)."""
        for server in self.maps.values():
            players = server.players
            if who in players:
                return server, who, players[who]
        needle = who.lower()
        for server in self.maps.values():
            for pid, name in server.players.items():
                low = name.lower()
                if low == needle or needle in low:
                    return server, pid, name
        return None, None, None

    def cmd_whereis(self, who):
        server, pid, name = self._locate(who)
        if server:
            log(f"{name} ({pid}) je na mape {server.name}")
        else:
            log(f"hrac '{who}' nenalezen na zadne mape")

    def cmd_moderate(self, action, who):
        """kick/ban z tlacitka v AMP - supervisor mapu dohleda sam.

        KickPlayer/BanPlayer chteji ID hrace, ne jmeno - proto ListPlayers.
        """
        server, pid, name = self._locate(who)
        if not server:
            log(f"hrac '{who}' nenalezen, {action} neprovedeno")
            return
        command = f"KickPlayer {pid}" if action == "kick" else f"BanPlayer {pid}"
        try:
            emit(server.name, f"{action} {name} ({pid}): "
                              f"{server.rcon.command(command) or 'odeslano'}")
        except (RconError, OSError) as exc:
            emit(server.name, f"{action} selhalo: {exc}")

    def cmd_event(self, args):
        available = [k for k in self.presets if not k.startswith("_")]
        if not args or args[0] == "status":
            log(f"aktivni preset: {self.state.preset}")
            log(f"ceka na restart: {self.state.pending or '(nic)'}")
            return
        if args[0] == "list":
            for name in available:
                marker = " <- aktivni" if name == self.state.preset else ""
                log(f"  {name}{marker}")
            return
        if args[0] == "apply":
            if not self._rolling_lock.acquire(blocking=False):
                log("rolling restart uz bezi, ignoruji")
                return
            log("aplikuji preset rolling restartem, mapa po mape")
            threading.Thread(target=self._rolling_restart, daemon=True).start()
            return
        if args[0] == "set" and len(args) > 1:
            name = args[1]
            if name not in available:
                log(f"preset '{name}' neexistuje, dostupne: {', '.join(available)}")
                return
            self.state.set_pending(name)
            log(f"preset '{name}' nastaven - projevi se pri PRISTIM startu mapy. "
                f"Rani restart ho vezme s sebou, nebo pouzij 'event apply'.")
            return
        log("pouziti: event list | status | set <preset> | apply")

    def _rolling_restart(self):
        try:
            for server in self.maps.values():
                if self.stopping.is_set():
                    return
                if not server.running:
                    continue
                self.restart_map(server)
            log("rolling restart hotov")
        finally:
            self._rolling_lock.release()

    def cmd_quiesce(self, on):
        """Zaloha za behu - AMP vola pres App.QuiesceCommand misto vypnuti.

        Zamerne BEZ SaveWorld. ARK save zapisuje atomicky (.ark dostane novy
        inode naraz, rozepsany soubor nikdy neexistuje - overeno na v361.7),
        takze kopie za behu je vzdy cely soubor, nanejvys o autosave starsi.
        RCON SaveWorld by naopak mohl RCON map na minuty umlcet (viz README).
        Prikaz tu je kvuli tomu, ze s nim AMP zalohuje bez vypnuti clusteru.
        """
        self.quiesced = on
        log("QUIESCED - save se zapisuje atomicky, zaloha muze bezet"
            if on else "DEQUIESCED - normalni provoz")


# --------------------------------------------------------------------------
# Vstupni bod
# --------------------------------------------------------------------------

def main():
    # Vystup v UTF-8 nezavisle na systemu - AMP ho tak cte (overeno 2.8, Windows);
    # znak mimo kodovou stranku by jinak shodil vypis i vlakno, ktere ho psalo.
    # Vstup ale AMP na Windows posila v OEM strance s priblizenim ("Rehor
    # zkous\xa1" z "Rehor zkousi" s hacky a carkami, cp437) - cesky znak se ztrati
    # uz v AMP, "oem" zachrani aspon ty, co v ni jsou. Linux: UTF-8, -sig pro
    # pripadny BOM pred prvnim prikazem.
    for stream, encoding in ((sys.stdout, "utf-8"), (sys.stderr, "utf-8"),
                             (sys.stdin, "oem" if IS_WINDOWS else "utf-8-sig")):
        try:
            stream.reconfigure(encoding=encoding, errors="replace")
        except (AttributeError, ValueError):
            pass

    if GAME not in GAMES:
        log(f"CHYBA: neznama hra ARK_GAME={GAME!r}, znam: {', '.join(GAMES)}")
        return 1
    cfg = Config()
    cfg.use_short_path()

    # ARK po kazdem korektnim vypnuti (SIGINT) uz po ulozeni spadne na
    # SIGABRT (overeno 3/3 na v361.7). Bez tohohle by kazdy restart mapy
    # nechal core dump o velikosti jeji RAM - pres systemd-coredump ~1,3 GB,
    # jako soubor 'core' v adresari serveru ~6 GB. Mapy limit zdedi. (POSIX)
    if resource is not None:
        try:
            _, hard = resource.getrlimit(resource.RLIMIT_CORE)
            resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
        except (ValueError, OSError):
            pass

    selected, unknown = [], []
    for arg in sys.argv[1:]:
        name = arg.strip()
        if not name:
            continue
        if name in CANONICAL_MAPS:
            if name not in selected:
                selected.append(name)
        else:
            unknown.append(name)
    for name in unknown:
        log(f"VAROVANI: neznama mapa '{name}', ignoruji")

    if not selected:
        log("CHYBA: nevybrana zadna mapa. Zaskrtni aspon jednu v nastaveni "
            "instance (sekce Maps) a spust znovu.")
        return 1
    if not cfg.binary.exists():
        log(f"CHYBA: server nenalezen na {cfg.binary}. Spust nejdriv Update.")
        return 1
    bad_mods = [m for m in cfg.mods if not m.isdigit()]
    if bad_mods:
        log(f"VAROVANI: mody {', '.join(bad_mods)} nejsou cisla projektu - vynechany")
        cfg.mods = [m for m in cfg.mods if m.isdigit()]
    for label, value in (("RCON Password", cfg.rcon_password),
                         ("Server Password", cfg.server_password)):
        if value and ("?" in value or '"' in value
                      or any(ch.isspace() for ch in value)):
            # ARK prikazovou radku u mezery utne a zbytek tise zahodi - server
            # by mohl bezet bez hesla a bez jmena. Uvozovky by na Windows
            # rozbily uvozovky kolem URL mapy. Radsi nespustit vubec.
            log(f"CHYBA: {label} obsahuje mezeru, '?' nebo uvozovky - zmen ho "
                f"v nastaveni instance. ARK by prikazovou radku u nej utnul.")
            return 1
    # Kanonicke poradi, ne poradi argumentu - porty musi sedet s ports.json.
    selected.sort(key=CANONICAL_MAPS.index)
    supervisor = Supervisor(cfg, selected)

    # Obsluha signalu smi udelat JEN set(). Puvodne tady bezel cely vypinaci
    # zebrik - kdyz signal prisel ve chvili, kdy preruseny thread drzel
    # _print_lock nebo RCON zamek, obsluha se na tomtez zamku zablokovala,
    # nic se neulozilo a AMP nakonec poslalo SIGKILL.
    shutdown_requested = threading.Event()

    def on_signal(_signum, _frame):
        shutdown_requested.set()

    def shutdown_worker():
        shutdown_requested.wait()
        log("signal, ukoncuji")
        supervisor.stop_all()
        sys.stdout.flush()
        # main() visi na sys.stdin a ostatni vlakna jsou daemony - z vlakna
        # se ven jinak nedostaneme.
        os._exit(0)

    threading.Thread(target=shutdown_worker, daemon=False).start()

    # SIGINT je to, co posila LinuxGSM; SIGTERM to, co posila AMP a systemd.
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    log(f"ARK cluster supervisor: {', '.join(selected)}")
    supervisor.startup = threading.Thread(target=supervisor.start_all, daemon=True)
    supervisor.startup.start()
    for loop in (supervisor.watchdog_loop, supervisor.chat_loop,
                 supervisor.metrics_loop, supervisor.players_loop):
        threading.Thread(target=loop, daemon=True).start()
    if cfg.discord_token and cfg.discord_channel.isdigit():
        supervisor.discord = DiscordBridge(
            cfg.discord_token, cfg.discord_channel, supervisor.relay_from_discord,
            lambda msg: emit("discord", msg), supervisor.stopping,
            inbound=cfg.discord_to_game)
        supervisor.discord.start()
    elif cfg.discord_token or cfg.discord_channel:
        log("VAROVANI: most k Discordu vypnuty - chce token bota i ID kanalu "
            "(cislo, ne nazev kanalu)")

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                supervisor.handle(line)
            except Exception as exc:  # konzole nesmi shodit supervisor
                log(f"prikaz selhal: {exc}")
            if supervisor.stopping.is_set():
                break
    except KeyboardInterrupt:
        pass

    # Sem se dostaneme i pri EOF na stdin. Pod AMP zustava konzole otevrena,
    # takze EOF znamena bud konec instance, nebo rucni spusteni s </dev/null.
    # V obou pripadech se ma vypnout korektne - ale az potom, co dobehne
    # rozjety start.
    if not supervisor.stopping.is_set():
        log("stdin uzavren, ukoncuji cluster")
    # Pri soubeznem signalu pocka, az vypinani dobehne (stop_all je
    # idempotentni), a pak konec natvrdo: shutdown_worker neni daemon a
    # ceka na signal, ktery na teto ceste (DoExit z AMP, EOF) neprijde.
    supervisor.stop_all()
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
