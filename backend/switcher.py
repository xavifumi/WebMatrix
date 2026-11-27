"""Conmutador main/backup -> program (modo copy, sin reencodar).

Cada canal tiene dos lectores (main y backup, siempre activos: hot standby) y un único publicador `canal/program`.
El cambio de fuente se hace en un keyframe de la fuente de destino y los timestamps se reescriben para que la línea
de tiempo de `program` sea continua. `program` nunca se desconecta, así que grabación, CDNs y reproductores no ven
cortes de conexión. Limitación: si main y backup tienen parámetros de codec distintos, el reproductor puede
notar un glitch; y el cambio espera al siguiente keyframe de la fuente de destino (hasta un GOP).
"""
import logging, threading, time
from collections import deque
from fractions import Fraction

import av

log = logging.getLogger("switcher")
logging.basicConfig(level=logging.INFO)
US = Fraction(1, 1_000_000)
DEAD_AFTER = 2.0       # s sin paquetes -> fuente caída
RETURN_STABLE = 10.0   # s de main estable antes de volver
MIN_GAP = 10.0         # s mínimos entre cambios voluntarios
MAX_PER_MIN = 3        # cambios voluntarios por minuto


class Source(threading.Thread):
    def __init__(self, prog, role):
        super().__init__(daemon=True, name=f"src-{prog.channel}-{role}")
        self.prog, self.role = prog, role
        self.url = f"{prog.rtmp}/{prog.channel}/{role}?user=verneta&pass={prog.key}"
        self.last_pkt = 0.0
        self.alive_since = 0.0
        self.streams = {}
        self.stop_ev = threading.Event()

    def alive(self, now=None):
        return ((now or time.time()) - self.last_pkt) < DEAD_AFTER

    def run(self):
        while not self.stop_ev.is_set():
            c = None
            try:
                c = av.open(self.url, timeout=(5.0, 5.0))
                self.streams = {s.type: s for s in c.streams if s.type in ("video", "audio")}
                for pkt in c.demux(*self.streams.values()):
                    if self.stop_ev.is_set():
                        break
                    if pkt.dts is None:
                        continue
                    self.prog.on_packet(self, pkt)
            except Exception as e:
                log.debug("%s: %s", self.url, e)
            finally:
                if c:
                    try: c.close()
                    except Exception: pass
            self.alive_since = 0.0
            self.stop_ev.wait(2.0)


class Program:
    def __init__(self, channel, rtmp, key, on_event=None):
        self.channel, self.rtmp, self.key = channel, rtmp, key
        self.pub_url = f"{rtmp}/{channel}/program?user=verneta&pass={key}"
        self.on_event = on_event or (lambda *a: None)
        self.lock = threading.RLock()
        self.sources = {r: Source(self, r) for r in ("main", "backup")}
        self.mode, self.locked = "auto", False
        self.active = self.pending = None
        self.out = None
        self.out_streams = {}
        self.off, self.last_dts, self.dur, self.need_base = {}, {}, {}, {}
        self.switches = deque(maxlen=20)        # (t, voluntario)
        self.last_switch = 0.0
        self.out_ok = False
        self.stop_ev = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name=f"prog-{channel}")

    def start(self):
        for s in self.sources.values(): s.start()
        self.thread.start()

    def stop(self):
        self.stop_ev.set()
        for s in self.sources.values(): s.stop_ev.set()
        with self.lock: self._close_out()

    def set_mode(self, mode, locked):
        with self.lock:
            self.mode, self.locked = mode, bool(locked)

    # ---------- salida ----------
    def _close_out(self):
        if self.out:
            try: self.out.close()
            except Exception: pass
        self.out, self.out_streams, self.out_ok = None, {}, False
        self.last_dts, self.dur = {}, {}

    def _open_out(self, src):
        if not src.streams.get("video"):
            return
        out = av.open(self.pub_url, "w", format="flv")
        self.out_streams = {k: out.add_stream_from_template(s) for k, s in src.streams.items()}
        self.out, self.out_ok = out, True

    # ---------- paquetes ----------
    def on_packet(self, src, pkt):
        now = time.time()
        with self.lock:
            if not src.alive(now) or src.alive_since == 0.0:
                src.alive_since = now
            src.last_pkt = now
            kind = pkt.stream.type
            if self.pending == src.role and kind == "video" and pkt.is_keyframe:
                if self.out is None:
                    try: self._open_out(src)
                    except Exception as e:
                        log.warning("%s: no puedo publicar program: %s", self.channel, e); return
                if self.out is not None:
                    self._commit(src.role)
            if self.active == src.role and self.out is not None and kind in self.out_streams:
                self._forward(pkt, kind)

    def _commit(self, role):
        prev = self.active
        self.active, self.pending = role, None
        self.need_base = {"video": True, "audio": True}
        self.on_event(self.channel, "switch" if prev else "program_up",
                      f"Programa: {role}" if not prev else f"Programa: cambio de {prev} a {role}")

    def _forward(self, pkt, kind):
        tb = pkt.time_base
        dts = int(pkt.dts * tb / US)
        pts = int(pkt.pts * tb / US) if pkt.pts is not None else dts
        if self.need_base.get(kind):
            self.off[kind] = (self.last_dts[kind] + self.dur.get(kind, 20_000) - dts) if kind in self.last_dts else -dts
            self.need_base[kind] = False
        o_dts, o_pts = dts + self.off[kind], pts + self.off[kind]
        last = self.last_dts.get(kind)
        if last is not None:
            if o_dts <= last:
                o_pts += last + 1 - o_dts; o_dts = last + 1
            self.dur[kind] = min(max(o_dts - last, 5_000), 200_000) if o_dts - last < 500_000 else self.dur.get(kind, 20_000)
        self.last_dts[kind] = o_dts
        pkt.stream = self.out_streams[kind]
        pkt.time_base = US
        pkt.dts, pkt.pts = o_dts, max(o_pts, o_dts)
        try:
            self.out.mux(pkt)
        except Exception as e:
            log.warning("%s: fallo escribiendo program: %s", self.channel, e)
            self._close_out(); self.active = None; self.need_base = {}

    # ---------- decisión ----------
    def _loop(self):
        while not self.stop_ev.is_set():
            time.sleep(0.1)
            try:
                with self.lock: self._decide(time.time())
            except Exception as e:
                log.exception("decisión: %s", e)

    def _decide(self, now):
        s = self.sources
        alive = {r: s[r].alive(now) for r in s}
        cur = self.active
        if self.mode in ("main", "backup"):
            want = self.mode
        elif self.locked and cur:
            want = cur
        elif cur is None:
            want = "main" if alive["main"] else "backup" if alive["backup"] else None
        else:
            other = "backup" if cur == "main" else "main"
            want = cur
            recent = [t for t, vol in self.switches if vol and now - t < 60]
            if not alive[cur] and alive[other]:
                want = other                                   # fallo: se cambia siempre
            elif (cur == "backup" and alive["main"] and now - s["main"].alive_since >= RETURN_STABLE
                  and now - self.last_switch >= MIN_GAP and len(recent) < MAX_PER_MIN):
                want = "main"                                  # retorno automático
        if want and want != cur and want != self.pending and alive.get(want):
            self.pending = want
            vol = cur is not None and alive.get(cur, False)
            self.switches.append((now, vol)); self.last_switch = now
        if self.pending and not alive.get(self.pending):
            self.pending = None
        if self.active and not alive[self.active] and not any(alive.values()) and self.out_ok:
            pass  # sin fuentes: program se mantiene abierto, sin datos, hasta que vuelva alguna

    def status(self):
        now = time.time()
        with self.lock:
            return {"active": self.active, "pending": self.pending, "mode": self.mode, "locked": self.locked,
                    "alive": {r: self.sources[r].alive(now) for r in self.sources}, "publishing": self.out_ok}
