#!/usr/bin/env python3
"""Supervisor ARK SE clusteru - vsechny mapy z jedne instalace, v jedne instanci AMP.

Spousti se misto ShooterGameServer. Mapy dostane jako argumenty (kazde
zaskrtavatko v AMP se rozvine na jmeno mapy nebo prazdno), zbytek konfigurace
cte z promennych prostredi, ktere naplni sablona.

Prikazy prijima na stdin - tim je konzole AMP zaroven ovladacim panelem.
"""
import configparser
import json
import os
import re
import resource
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from rcon import RconClient, RconError

# Poradi je zavazne: urcuje prirazeni portu a musi sedet s arkclusterports.json.
CANONICAL_MAPS = [
    "TheIsland", "TheCenter", "ScorchedEarth_P", "Ragnarok", "Aberration_P",
    "Extinction", "Valguero_P", "Genesis", "CrystalIsles", "Gen2",
    "LostIsland", "Fjordur", "Aquatica",
]

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE.parent / "supervisor-state.json"

# Znak, ktery hrac do ARK chatu nenapise. Znaci preposlanou zpravu, aby se
# relay mezi mapami nezacyklil.
RELAY_MARK = "\u2508"

_print_lock = threading.Lock()


def emit(source, line):
    """Jediny zpusob, jak z supervisoru neco vypsat.

    Prefix je nutny - konzole AMP sliva 13 zdroju do jednoho okna a bez nej
    by se v tom nedalo vyznat. Zaroven na tenhle format ciluji Console.*Regex
    v sablone, takze se nesmi menit bez upravy arkcluster.kvp.
    """
    with _print_lock:
        sys.stdout.write(f"[{source}] {line}\n")
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
        self.game = self.base / "376030"
        self.binary = self.game / "ShooterGame/Binaries/Linux/ShooterGameServer"
        self.workdir = self.game / "ShooterGame/Binaries/Win64"
        self.config_dir = self.game / "ShooterGame/Saved/Config/LinuxServer"
        self.cluster_dir = self.base / "clusterdata"
        self.log_dir = self.base / "logs"

        self.session_name = env("ARK_SESSION_NAME") or "ARK Cluster"
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
        self.rate_preset = env("ARK_RATE_PRESET") or "normal"
        self.custom_options = env("ARK_CUSTOM_OPTIONS")
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

    Cte se ze sysfs, ne z lscpu - je to spolehlivejsi a bez zavislosti na
    formatu vystupu.
    """
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
    """Kazda mapa dostane vlastni fyzicke jadro. Zadne dve nesdileji vlakno."""
    # Jen CPU, ktere proces smi pouzit - v kontejneru AMP s omezenym cpusetem
    # by jinak pinning mohl mirit na zakazana jadra a selhat.
    try:
        allowed = os.sched_getaffinity(0)
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


def proc_rss_kb(pid):
    """VmRSS procesu v kB, 0 kdyz proces neni."""
    try:
        with open(f"/proc/{pid}/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def proc_cpu_ticks(pid):
    """utime + stime procesu (vsechna vlakna) v tikach hodin, None kdyz neni."""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            # comm muze obsahovat mezery i zavorky - delit az za posledni ')'.
            fields = handle.read().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])
    except (OSError, ValueError, IndexError):
        return None


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
            if not value or "?" in value or any(ch.isspace() for ch in value):
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
    gameini_rates = preset.get("gameini", {})

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
            out.write_text(template.read_text().replace("{{RATES}}", rates))
            out.chmod(0o444)
        except OSError as exc:
            log(f"CHYBA: Game.ini nelze zapsat: {exc}")
    log(f"Konfig pripraven: preset '{preset_name}', Game.ini zamceno na 444, "
        f"ServerSettings {len(cfg.server_settings)} klicu na prikazovou radku")


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
        r"|gethostbyname failed)")
    # Na tenhle radek logu se pozna, ze mapa nabehla - za 30-40 s a nezavisle
    # na RCON, ktery nabiha o chvili pozdeji a pri zatezi umi byt pomaly.
    STARTED_RE = re.compile(r'^Server: ".*" has successfully started!')
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
        self.rcon = RconClient(rcon_host, self.ports["rcon"], cfg.rcon_password)
        self.ready = False
        self._ready_at = 0.0
        # Nastavi cteni logu na "Server: ... has successfully started!".
        self.started = threading.Event()
        self.restarts = 0
        self.started_at = 0.0
        self.gave_up = False
        # id -> jmeno. Vzdy se NAHRAZUJE celym slovnikem, nikdy nemeni na
        # miste - ctenari (status, whereis, metriky) pak iteruji bez zamku.
        self.players = {}
        self._reader = None
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
        opts = [
            self.name,
            "listen",
            f"Port={self.ports['game']}",
            f"QueryPort={self.ports['query']}",
            "RCONEnabled=True",
            f"RCONPort={self.ports['rcon']}",
            f"ServerAdminPassword={self.cfg.rcon_password}",
            f"MaxPlayers={self.cfg.max_players}",
            # Kazda mapa MUSI mit vlastni save adresar, jinak si prepisou svet.
            f"AltSaveDirectoryName={self.name}",
            "RCONServerGameLogBuffer=600",
        ]
        if self.cfg.multihome:
            opts.append(f"MultiHome={self.cfg.bind_ip}")
        if self.cfg.server_password:
            opts.append(f"ServerPassword={self.cfg.server_password}")
        opts.extend(self.cfg.server_settings)
        for key, value in sorted(preset_rates.items()):
            opts.append(f"{key}={value}")
        for extra in self.cfg.custom_options.replace("\n", "?").split("?"):
            extra = extra.strip()
            if not extra:
                continue
            if any(ch.isspace() for ch in extra):
                emit(self.name, f"VAROVANI: vlastni volba {extra!r} vynechana - "
                                f"mezera by prikazovou radku utnula")
                continue
            opts.append(extra)
        # SessionName POSLEDNI a BEZ uvozovek. UE si prikazovou radku sklada z
        # argv a URL mapy utne u prvni mezery - vsechno za ni tise zmizi.
        # Overeno na v361.7: s 'SessionName="X - Mapa"' uprostred se ztratil
        # ServerPassword (server byl verejny) a jmeno spadlo na 'ARK #205902'.
        opts.append(f"SessionName={self.cfg.session_name} - {self.name}")

        args = [str(self.cfg.binary), "?".join(opts)]
        args += [
            f"-ClusterDirOverride={self.cfg.cluster_dir}",
            f"-clusterid={self.cfg.cluster_id}",
            "-AutoManagedMods",
            "-Crossplay",
            "-server",
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
        old_log = self._log_identity()
        emit(self.name, f"start na portech game={self.ports['game']} "
                        f"query={self.ports['query']} rcon={self.ports['rcon']}")
        # Afinita je na Linuxu vlastnost VLAKNA a fork ji dedi od vlakna, ktere
        # ho vola. Nastavit ji procesu az po startu chyti jen hlavni vlakno -
        # co server stihne vytvorit driv, zustane na vsech jadrech. Proto se na
        # okamzik prisprendli volajici vlakno a server se narodi uz pripnuty,
        # vcetne vsech svych budoucich vlaken.
        restore = None
        if self.cfg.cpu_pinning and self.cores:
            try:
                restore = os.sched_getaffinity(0)
                os.sched_setaffinity(0, self.cores)
            except OSError as exc:
                restore = None
                emit(self.name, f"VAROVANI: pinning selhal: {exc}")
        try:
            self.proc = subprocess.Popen(
                args,
                cwd=str(self.cfg.workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=1,
                text=True,
                errors="replace",
            )
        finally:
            if restore is not None:
                try:
                    os.sched_setaffinity(0, restore)
                except OSError:
                    pass
        if restore is not None:
            emit(self.name, f"pin na CPU {sorted(self.cores)}")

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
                if text and not self.LOG_DROP.match(text):
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
            handle = open(log_path, "a", errors="replace")
        except OSError:
            handle = None
        try:
            for raw in self.proc.stdout:
                line = raw.rstrip("\n")
                if handle:
                    handle.write(line + "\n")
                    handle.flush()
                # Herni log na stdout NENI (v361.7 tu jsou jen dva radky ze
                # Steam API) - hraci jdou z ListPlayers, viz players_loop.
                if line.strip():
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

    def wait_ready(self, timeout):
        """Ceka, az mapa nabehne: radek v logu, nebo odpoved RCON.

        Log je spolehlivy (30-40 s) a nezavisly na RCON. Funkce zavisle na
        RCON (chat, hraci) si ho overuji samy - players_loop drzi "ready".
        """
        deadline = time.time() + timeout
        next_probe = 0.0
        while time.time() < deadline:
            # Vypinani (stop nastavi desired_up=False) - necekat dal na start.
            if not self.desired_up:
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
        """Zebrik: uklid -> SIGINT -> SIGKILL.

        SIGINT ARK svet ulozi a do par sekund skonci (overeno na v361.7, 7/7)
        a funguje, i kdyz RCON neodpovida. RCON SaveWorld se sem zamerne
        nedava: ARK po nem RCON obcas na minuty umlci (viz README) a SIGINT
        uklada tak jako tak.
        """
        # Nastavit i kdyz uz mapa nebezi - jinak by ji hlidac zvedl zpatky.
        self.desired_up = False
        if not self.running:
            return
        if fixes:
            if self.probe_ready():
                self._run_fixes(fixes, save_timeout, deadline)
            else:
                emit(self.name, "RCON neodpovida - uklid vynechan")
        # ARK posloucha na SIGINT, ne SIGTERM; pri nem svet ulozi.
        emit(self.name, "posilam SIGINT (ARK pri nem svet ulozi)")
        self._signal(signal.SIGINT)
        wait = save_timeout
        if deadline is not None:
            # SIGKILL ze stop_all prijde po deadline - do te doby to musi stihnout.
            wait = max(5, min(save_timeout, deadline - time.time() - 5))
        if self._wait_exit(wait):
            emit(self.name, "ukonceno korektne")
            return
        emit(self.name, "CHYBA: nereaguje ani na SIGINT, SIGKILL "
                        "(svet muze byt starsi)")
        self._signal(signal.SIGKILL)
        self._wait_exit(15)

    # Rozestup mezi tezkymi prikazy uklidu. Overeno na v361.7: tezky prikaz
    # (DestroyWildDinos, DestroyAll, SaveWorld) poslany par sekund po jinem
    # tezkem prikazu nedostal odpoved a RCON mapy pak minuty mlcel; s
    # rozestupem 40 s prosly tri po sobe.
    FIX_SPACING = 45

    def _run_fixes(self, commands, save_timeout, deadline=None):
        """Uklid patri PRED vypnuti - repopulace pak probehne pri bootu.

        Mezi prikazy FIX_SPACING s. Po prvnim prikazu bez odpovedi se konci:
        RCON neodpovida a kazdy dalsi by jen protahl vypinani o svuj timeout.
        Uklid se zkrati i tehdy, kdyby jinak nezbyl cas na korektni SIGINT.
        """
        for position, cmd in enumerate(commands):
            if deadline is not None and (time.time() + self.FIX_SPACING + 15
                                         + save_timeout > deadline):
                emit(self.name, "zbytek uklidu vynechan - nezbyl by cas "
                                "na korektni vypnuti")
                return
            if position:
                emit(self.name, f"cekam {self.FIX_SPACING} s pred dalsim uklidem")
                time.sleep(self.FIX_SPACING)
            try:
                reply = self.rcon.command(cmd, timeout=15)
            except (RconError, OSError) as exc:
                emit(self.name, f"FIX SELHAL {cmd!r}: {exc} - zbytek uklidu "
                                f"vynechan, RCON neodpovida")
                return
            # Preklep v nazvu tridy tise nedela nic - proto se loguje odpoved.
            emit(self.name, f"FIX {cmd} -> {reply or '(prazdna odpoved)'}")

    def _signal(self, sig):
        try:
            self.proc.send_signal(sig)
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

        cores = assign_cores(map_names) if cfg.cpu_pinning else {}
        self.maps = {}
        for name in map_names:
            index = CANONICAL_MAPS.index(name)
            self.maps[name] = MapServer(name, index, cfg, cores.get(name))

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
            # Poradi zamku vsude stejne: lifecycle -> zamek mapy.
            with self.lifecycle:
                if self.stopping.is_set():
                    return
                started = self._safe_start(server)
            # wait_ready zamerne MIMO zamek - blokuje az ready_timeout a
            # nesmi tim drzet pripadne vypinani.
            if started:
                server.wait_ready(self.cfg.ready_timeout)
            # Prodleva az mezi mapami, ne po posledni.
            if position < len(self.maps) - 1 and self.cfg.start_delay:
                log(f"cekam {self.cfg.start_delay} s pred dalsi mapou")
                self.stopping.wait(self.cfg.start_delay)
        log("vsechny mapy nastartovany")

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
                emit(server.name, "CHYBA: nedobehla v rozpoctu, SIGKILL")
                server._signal(signal.SIGKILL)
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
            server.wait_ready(self.cfg.ready_timeout)

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
                    server.wait_ready(self.cfg.ready_timeout)

    def chat_loop(self):
        """Chat z map do konzole AMP; s CrossChat i mezi mapami (misto Cross-Ark-Chat)."""
        while not self.stopping.wait(5):
            for server in self.maps.values():
                if not server.running or not server.probe_ready():
                    continue
                try:
                    chat = server.rcon.command("GetChat")
                except (RconError, OSError):
                    continue
                for line in chat.splitlines():
                    line = line.strip()
                    # Vlastni preposlana zprava se vraci zpet v GetChat.
                    # Bez tehle zabrany by se chat mezi mapami lavinovite
                    # rozmnozil. RELAY_MARK je znak, ktery hrac do chatu
                    # nenapise.
                    if not line or RELAY_MARK in line or line.startswith("SERVER:"):
                        continue
                    self._relay(server, line)

    def _relay(self, origin, line):
        # Jmeno ani tribe nesmi obsahovat zavorky ani dvojtecku - jinak by
        # preposlana zprava strukturalne odpovidala znovu.
        match = re.match(
            r"^(?:[\d.]+_[\d.]+:\s*)?([^()\[\]:]{1,64}?)\s*\(([^()\[\]]{1,64})\):\s*(.+)$",
            line)
        if not match:
            return
        _steam, player, message = match.groups()
        # Do konzole AMP vzdy - na tenhle radek cili Console.UserChatRegex.
        emit(origin.name, f"<{player}> {message}")
        if not self.cfg.cross_chat:
            return
        payload = f"{RELAY_MARK}[{origin.name}] {player}: {message}"
        for server in self.maps.values():
            if server is origin or not server.ready or not server.running:
                continue
            try:
                server.rcon.command(f"ServerChat {payload}")
            except (RconError, OSError):
                pass

    def metrics_loop(self):
        # AMP meri jen proces supervisoru (App.MonitorChildProcess umi jedno
        # dite, ne 13), takze jeho grafy CPU a RAM by ukazovaly skoro nulu.
        # Soucet za mapy si proto supervisor meri sam a posila ho v METRICS.
        tick = os.sysconf("SC_CLK_TCK")
        last = {}                            # pid -> (tiky, cas)
        while not self.stopping.wait(60):
            up, rss_kb, cpu = 0, 0, 0.0
            now = time.time()
            seen = {}
            for server in self.maps.values():
                if not server.running:
                    continue
                up += 1
                pid = server.proc.pid
                rss_kb += proc_rss_kb(pid)
                ticks = proc_cpu_ticks(pid)
                if ticks is None:
                    continue
                seen[pid] = (ticks, now)
                if pid in last and now > last[pid][1]:
                    cpu += (ticks - last[pid][0]) / tick / (now - last[pid][1])
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
                    now = parse_players(server.rcon.command("ListPlayers", timeout=5))
                except (RconError, OSError):
                    server.ready = False
                    continue          # stav nevime - seznam nemenit
                # Mapa nabehla podle logu, ale RCON se overuje az tady - bez
                # toho by ready zustalo False a chat ani hraci by se nerozjeli.
                server.ready, server._ready_at = True, time.time()
                self._diff_players(server, now)

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
            self.cmd_all_rcon(f"Broadcast {msg}", quiet=True)
            log(f"broadcast: {msg}")
        elif cmd == "say" and len(args) >= 2:
            server = self.find(args[0])
            if server:
                msg = args[1] if len(args) == 2 else self._drop_token(rest)
                server.rcon.command(f"ServerChat {msg}")
        elif cmd == "rcon" and len(args) >= 2:
            self.cmd_rcon(args[0], args[1] if len(args) == 2
                          else self._drop_token(rest))
        elif cmd == "rconall" and rest:
            self.cmd_all_rcon(rest)
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
            cores = sorted(server.cores) if server.cores else "-"
            ram = (f"{proc_rss_kb(server.proc.pid) / 1048576:.1f}G"
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
            server.wait_ready(self.cfg.ready_timeout)

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
    cfg = Config()

    # ARK po kazdem korektnim vypnuti (SIGINT) uz po ulozeni spadne na
    # SIGABRT (overeno 3/3 na v361.7). Bez tohohle by kazdy restart mapy
    # nechal core dump o velikosti jeji RAM - pres systemd-coredump ~1,3 GB,
    # jako soubor 'core' v adresari serveru ~6 GB. Mapy limit zdedi.
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
    for label, value in (("RCON Password", cfg.rcon_password),
                         ("Server Password", cfg.server_password)):
        if value and ("?" in value or any(ch.isspace() for ch in value)):
            # ARK prikazovou radku u mezery utne a zbytek tise zahodi - server
            # by mohl bezet bez hesla a bez jmena. Radsi nespustit vubec.
            log(f"CHYBA: {label} obsahuje mezeru nebo '?' - zmen ho v nastaveni "
                f"instance. ARK by prikazovou radku u nej utnul.")
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
