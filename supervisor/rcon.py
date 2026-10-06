"""Minimalni klient Source RCON pro ARK.

Zamerne bez externi zavislosti - viz requirements.txt.
"""
import select
import socket
import struct
import threading
import time

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

# ARK vraci tohle misto prazdne odpovedi, napr. kdyz v GetChat nic neni.
ARK_EMPTY = "Server received, But no response!!"


class RconError(Exception):
    pass


class RconTimeout(RconError):
    """Odpoved neprisla v limitu. Prikaz mohl na serveru probehnout."""


class RconClient:
    """Synchronni RCON klient. Jedna instance na mapu, chraneny zamkem.

    Vsechno bezi proti jednomu deadline na cely prikaz - pripojeni, prihlaseni,
    odeslani i kazdy recv. ARK posila na kazde spojeni kazdych 10 s
    nevyzadany paket "Keep Alive" (id 0); kdyby timeout platil na jednotlivy
    recv, kazdy Keep Alive by ho obnovil a cekani na odpoved, ktera neprijde,
    by nikdy neskoncilo (overeno na serveru v361.7).
    """

    def __init__(self, host, port, password, timeout=10.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock = None
        self._next_id = 1
        self._lock = threading.Lock()

    # --- spojeni ---

    def connect(self):
        with self._lock:
            self._connect_locked(time.monotonic() + self.timeout)

    def _connect_locked(self, deadline):
        self._close_locked()
        sock = socket.create_connection((self.host, self.port),
                                        self._remaining(deadline))
        self._sock = sock
        try:
            req_id = self._send_locked(SERVERDATA_AUTH, self.password, deadline)
            # Po auth chodi nekdy prazdny RESPONSE_VALUE pred AUTH_RESPONSE.
            while True:
                pkt_id, pkt_type, _ = self._recv_locked(deadline)
                if pkt_type != SERVERDATA_AUTH_RESPONSE:
                    continue
                if pkt_id == -1:
                    raise RconError("RCON: spatne heslo")
                if pkt_id != req_id:
                    raise RconError("RCON: neocekavane id v odpovedi auth")
                return
        except BaseException:
            # Neprihlaseny soket nesmi zustat - dalsi prikaz by sel na nej.
            self._close_locked()
            raise

    def close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def connected(self):
        return self._sock is not None

    # --- prikazy ---

    def command(self, cmd, retry=True, timeout=None):
        """Posle prikaz a vrati odpoved.

        `timeout` je CELKOVY limit na prikaz vcetne pripadneho pripojeni.

        Odeslany prikaz se NIKDY neopakuje. Na urovni TCP nejde rozlisit "stare
        spojeni bylo mrtve" od "server prikaz prijal a spojeni zavrel" (presne
        to dela DoExit) - opakovani by ServerChat zdvojilo a SaveWorld by psal
        do sveta dvakrat naraz. Mrtve spojeni se proto pozna PRED odeslanim.
        `retry` zustava kvuli volajicim, uz nic nedela.
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self._lock:
            try:
                self._ready_locked(deadline)
                return self._command_locked(cmd, deadline)
            except (OSError, RconError):
                self._close_locked()
                raise

    def _ready_locked(self, deadline):
        """Pred odeslanim: zahodit cekajici Keep Alive, mrtve spojeni nahradit."""
        if self._sock is None:
            self._connect_locked(deadline)
            return
        try:
            while select.select([self._sock], [], [], 0)[0]:
                if not self._sock.recv(1, socket.MSG_PEEK):
                    raise ConnectionResetError("protejsek spojeni zavrel")
                self._recv_locked(deadline)          # Keep Alive - zahodit
        except (OSError, RconError, ValueError):
            self._connect_locked(deadline)

    def _command_locked(self, cmd, deadline):
        req_id = self._send_locked(SERVERDATA_EXECCOMMAND, cmd, deadline)
        # Prazdny RESPONSE_VALUE hned za prikazem. ARK zpracovava pakety jednoho
        # spojeni po poradi a odpovi i na nej, takze jeho odpoved spolehlive
        # znaci konec odpovedi na prikaz - bez hadani podle delky paketu a bez
        # cekani na dalsi paket, ktery neprijde.
        end_id = self._send_locked(SERVERDATA_RESPONSE_VALUE, "", deadline)
        parts = []
        while True:
            try:
                pkt_id, _, body = self._recv_locked(deadline)
            except (OSError, RconError) as exc:
                # Server odpovedel a pak spojeni zavrel (DoExit: "Exiting...").
                # Odpoved je, jen terminator uz neprisel - neni to chyba.
                if parts and not isinstance(exc, RconTimeout):
                    self._close_locked()
                    break
                raise
            if pkt_id == req_id:
                parts.append(body)
            elif pkt_id == end_id:
                break
            # Jinak Keep Alive (id 0) - zahodit, deadline bezi dal.
        out = b"".join(parts).decode("utf-8", errors="replace").strip()
        return "" if out == ARK_EMPTY else out

    # --- protokol ---

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RconTimeout("RCON: odpoved neprisla v limitu")
        return remaining

    def _send_locked(self, pkt_type, body, deadline):
        req_id = self._next_id
        self._next_id = self._next_id % 0x7FFFFFFF + 1
        payload = struct.pack("<ii", req_id, pkt_type) + body.encode("utf-8") + b"\x00\x00"
        self._sock.settimeout(self._remaining(deadline))
        try:
            self._sock.sendall(struct.pack("<i", len(payload)) + payload)
        except socket.timeout:
            raise RconTimeout("RCON: odeslani nestihlo limit") from None
        return req_id

    def _recv_locked(self, deadline):
        raw_len = self._recv_exactly(4, deadline)
        (length,) = struct.unpack("<i", raw_len)
        if not 10 <= length <= 4 * 1024 * 1024:
            raise RconError(f"RCON: nesmyslna delka paketu {length}")
        payload = self._recv_exactly(length, deadline)
        pkt_id, pkt_type = struct.unpack("<ii", payload[:8])
        # Bajty, ne str - vicebajtovy znak muze byt rozdeleny mezi pakety.
        return pkt_id, pkt_type, payload[8:-2]

    def _recv_exactly(self, count, deadline):
        buf = b""
        while len(buf) < count:
            # Timeout se pocita znovu pro kazdy recv - pomalu kapajici paket
            # nesmi limit prekrocit.
            self._sock.settimeout(self._remaining(deadline))
            try:
                chunk = self._sock.recv(count - len(buf))
            except socket.timeout:
                raise RconTimeout("RCON: odpoved neprisla v limitu") from None
            if not chunk:
                raise RconError("RCON: spojeni zavreno protejskem")
            buf += chunk
        return buf
