"""Backend de la Fase 0: muestrea la API de MediaMTX, calcula métricas y sirve el panel."""
import asyncio, hmac, json, logging, os, re, secrets, sqlite3, statistics, threading, time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import outputs
from switcher import Program

API = os.getenv("MEDIAMTX_API", "http://mediamtx:9997")
DB_PATH = os.getenv("DB_PATH", "/data/verneta.db")
STATIC = os.getenv("STATIC_DIR", os.path.join(os.path.dirname(__file__), "static"))
RTSP = os.getenv("MEDIAMTX_RTSP", "rtsp://mediamtx:8554")
INTERNAL_KEY = os.getenv("INTERNAL_KEY") or secrets.token_urlsafe(24)   # credencial interna del conmutador y las salidas
RTMP = os.getenv("MEDIAMTX_RTMP", "rtmp://mediamtx:1935")
logging.getLogger("httpx").setLevel(logging.WARNING)
programs = {}
PORTS = {"host": os.getenv("PUBLIC_HOST", ""), "webrtc": int(os.getenv("PUBLIC_WEBRTC_PORT", 8889)),
         "srt": int(os.getenv("PUBLIC_SRT_PORT", 8890)),
         "rtmp": int(os.getenv("PUBLIC_RTMP_PORT", 1935))}
HIST, INTERVAL = 150, 2.0   # 150 muestras x 2 s = 5 min de gráfica
PROTO = {"srtConn": "SRT", "rtmpConn": "RTMP", "rtmpsConn": "RTMPS", "rtspSession": "RTSP",
         "rtspsSession": "RTSPS", "webRTCSession": "WebRTC", "srtSource": "SRT (pull)",
         "rtmpSource": "RTMP (pull)", "rtspSource": "RTSP (pull)"}

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
create table if not exists usage(day text, channel text, direction text, bytes integer,
  primary key(day, channel, direction));
create table if not exists events(ts text, channel text, type text, message text);
create table if not exists channels(name text primary key, created text);
create table if not exists inputs(channel text, role text, key text, primary key(channel, role));
create table if not exists outputs(id integer primary key autoincrement, channel text, name text, kind text,
  protocol text, url text, key text, enabled integer default 1);
""")
for col, ddl in (("mode", "text default 'auto'"), ("locked", "integer default 0")):
    try:
        db.execute(f"alter table channels add column {col} {ddl}")
    except sqlite3.OperationalError:
        pass

rt, known, sess = {}, {}, {}   # métricas por path, canales vistos, sesión por canal
view = {"ok": False, "error": "arrancando", "channels": [], "usage": {}}
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def new_key(): return secrets.token_urlsafe(16)


# ---- Vúmetros: ffmpeg lee SOLO la pista de audio por RTSP y mide pico y RMS cada ~50 ms ----
SIL_DB = -60.0
meters = {}
AF = ("asetnsamples=n=2400:p=0,astats=metadata=1:reset=1:measure_perchannel=Peak_level+RMS_level:"
      "measure_overall=none,ametadata=mode=print:direct=1:file=-")


def db_val(x):
    try:
        return max(float(x), -90.0)       # -inf (silencio) -> -90
    except ValueError:
        return -90.0


def silent_for(m):
    return round(time.time() - m["quiet_since"], 1) if m and m.get("quiet_since") else 0


def publish_levels(m, peak, rms):
    now, ks = time.time(), sorted(peak)
    m["peak"], m["rms"], m["t"] = [peak[k] for k in ks], [rms.get(k, -90.0) for k in ks], now
    if max(m["peak"], default=-90.0) > SIL_DB:
        m["quiet_since"] = None
    elif m.get("quiet_since") is None:
        m["quiet_since"] = now


async def run_meter(name):
    m = meters[name]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-allowed_media_types", "audio",
           "-i", f"{RTSP}/{name}", "-vn", "-af", AF, "-f", "null", "-"]
    while True:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            peak, rms = {}, {}
            async for raw in proc.stdout:
                line = raw.decode(errors="ignore").strip()
                if line.startswith("frame:"):
                    if peak:
                        publish_levels(m, peak, rms)
                    peak, rms = {}, {}
                elif line.startswith("lavfi.astats."):
                    k, _, v = line.partition("=")
                    _, _, ch, metric = k.split(".")
                    (peak if metric == "Peak_level" else rms)[int(ch)] = db_val(v)
            await proc.wait()
        finally:
            if proc and proc.returncode is None:
                proc.kill()
        m.update(peak=[], rms=[], quiet_since=None)
        await asyncio.sleep(2)


def ensure_meter(name):
    if name not in meters:
        meters[name] = {"peak": [], "rms": [], "t": 0, "quiet_since": None}
        meters[name]["task"] = asyncio.create_task(run_meter(name))


def stop_meter(name):
    m = meters.pop(name, None)
    if m:
        m["task"].cancel()



def utc(): return datetime.now(timezone.utc)
_ev_lock = threading.Lock()


def event(ch, typ, msg):
    with _ev_lock:
        _event(ch, typ, msg)


def _event(ch, typ, msg):
    db.execute("insert into events values(?,?,?,?)", (utc().isoformat(timespec="seconds"), ch, typ, msg))
    db.commit()


def split(name):
    ch, _, role = name.rpartition("/")
    return (ch, role) if ch and role in ("main", "backup", "program") else (name, "main")


def tracks(item):
    video = audio = None
    for t in item.get("tracks2") or []:
        p = t.get("codecProps") or {}
        if "width" in p and not video:
            video = {"codec": t["codec"], "width": p["width"], "height": p["height"],
                     "profile": p.get("profile"), "level": p.get("level")}
        elif "sampleRate" in p and not audio:
            audio = {"codec": {"MPEG-4 Audio": "AAC"}.get(t["codec"], t["codec"]), "sampleRate": p["sampleRate"], "channels": p.get("channelCount")}
    return video, audio


def health(r, silent=0):
    hist = list(r["hist"])
    if len(hist) < 3:
        return "ok", "señal correcta"
    prev = hist[-31:-1]
    med = statistics.median(prev) if len(prev) >= 5 else None
    if hist[-1] <= 0.001 or (med and hist[-1] < 0.15 * med):
        return "bad", "bitrate bajo"
    loss = (r.get("srt") or {}).get("loss_pct", 0)
    if loss > 2:
        return "warn", f"pérdida {loss:.1f} %".replace(".", ",")
    if silent >= 5:
        return "warn", "audio en silencio"
    return "ok", "señal correcta"


def prog_view(ch):
    r = rt.get(f"{ch}/program")
    p = programs.get(ch)
    online = bool(r and r["online"])
    return {"online": online, "path": f"{ch}/program", "mbps": (r["hist"][-1] if online and r["hist"] else 0),
            "hist": list(r["hist"]) if r else [], "video": r.get("video") if r else None,
            "audio": r.get("audio") if r else None, "readers": r.get("readers", 0) if r else 0,
            **(p.status() if p else {})}


def output_rows(ch=None):
    q = "select id, channel, name, kind, protocol, url, key, enabled from outputs" + (" where channel=?" if ch else "") + " order by id"
    cols = ("id", "channel", "name", "kind", "protocol", "url", "key", "enabled")
    return [{**dict(zip(cols, x)), "enabled": bool(x[7])} for x in db.execute(q, (ch,) if ch else ())]


def sync_programs(names):
    modes = {n: (m, l) for n, m, l in db.execute("select name, mode, locked from channels")}
    for ch in names:
        if ch not in programs:
            programs[ch] = Program(ch, RTMP, INTERNAL_KEY, on_event=event)
            programs[ch].start()
        programs[ch].set_mode(*(modes.get(ch) or ("auto", 0)))
    for ch in list(programs):
        if ch not in names:
            programs.pop(ch).stop()


async def sample(c):
    now = time.time()
    items = (await c.get(f"{API}/v3/paths/list")).json().get("items", [])
    srt = (await c.get(f"{API}/v3/srtconns/list")).json().get("items", [])
    srt_pub = {x["path"]: x for x in srt if x.get("state") == "publish"}
    seen, add = set(), {}
    names = {x[0] for x in db.execute("select name from channels")}
    sync_programs(names)

    for it in items:
        name = it["name"]
        ch, role = split(name)
        if ch not in names or not it.get("ready"):
            continue
        seen.add(name)
        known.setdefault(ch, {})[role] = name
        r = rt.get(name)
        if r is None:
            r = rt[name] = {"t": None, "hist": deque(maxlen=HIST), "loss": deque(maxlen=15), "online": False}
        if not r["online"]:
            r.update(online=True, t=None, since=it.get("readyTime"))
            r["hist"].clear()
            if role != "program":
                event(ch, "input_up", f"Entrada {role} conectada")
        b_in, b_out = it.get("inboundBytes", 0), it.get("outboundBytes", 0)
        if r["t"] is not None:
            dt = max(now - r["t"], 0.001)
            d_in = b_in - r["in"] if b_in >= r["in"] else b_in      # contador reiniciado
            d_out = b_out - r["out"] if b_out >= r["out"] else b_out
            r["hist"].append(round(d_in * 8 / dt / 1e6, 3))
            if role != "program":
                add[(ch, "in")] = add.get((ch, "in"), 0) + d_in
                add[(ch, "out")] = add.get((ch, "out"), 0) + d_out
        r["t"], r["in"], r["out"] = now, b_in, b_out
        r["protocol"] = PROTO.get((it.get("source") or {}).get("type"), (it.get("source") or {}).get("type") or "—")
        r["video"], r["audio"] = tracks(it)
        if r["audio"] and role != "program":
            ensure_meter(name)
        else:
            stop_meter(name)
        r["readers"] = len([x for x in it.get("readers") or [] if x.get("type") != "rtspSession"])   # sin contar los vúmetros
        r["errors"] = it.get("inboundFramesInError", 0)
        s = srt_pub.get(name)
        if s:
            r["loss"].append((s["packetsReceived"], s["packetsReceivedLoss"]))
            (p0, l0), (p1, l1) = r["loss"][0], r["loss"][-1]
            dp, dl = p1 - p0, l1 - l0
            r["srt"] = {"rtt": round(s["msRTT"], 1), "loss_pct": round(100 * dl / (dp + dl), 2) if dp + dl > 0 else 0.0,
                        "loss_total": s["packetsReceivedLoss"], "retrans": s["packetsReceivedRetrans"],
                        "link_mbps": round(s["mbpsLinkCapacity"], 1), "remote": s["remoteAddr"]}
        else:
            r["srt"] = None
            r["loss"].clear()

    for name, r in rt.items():
        if r["online"] and name not in seen:
            r["online"] = False
            r["hist"].append(0.0)
            stop_meter(name)
            if split(name)[1] != "program":
                event(split(name)[0], "input_down", f"Entrada {split(name)[1]} perdida")

    for (ch, d), v in add.items():
        db.execute("insert into usage values(?,?,?,?) on conflict(day,channel,direction) "
                   "do update set bytes = bytes + excluded.bytes", (utc().strftime("%Y-%m-%d"), ch, d, v))
    db.commit()

    chans, prev_rec = [], {c["name"]: c.get("recommended") for c in view.get("channels", [])}
    keys = {}
    for c_, r_, k_ in db.execute("select channel, role, key from inputs"):
        keys.setdefault(c_, {})[r_] = k_
    for ch in sorted(names):
        inputs = {}
        for role in ("main", "backup"):
            name = f"{ch}/{role}"
            r = rt.get(name)
            if r and r["online"]:
                silent = silent_for(meters.get(name)) if r["audio"] else 0
                if (silent >= 5) != r.get("sil", False):
                    r["sil"] = silent >= 5
                    event(ch, "audio", f"Entrada {role}: audio en silencio" if r["sil"] else f"Entrada {role}: audio recuperado")
                h, why = health(r, silent)
                inputs[role] = {"role": role, "path": name, "configured": True, "online": True, "protocol": r["protocol"],
                                "since": r["since"], "mbps": r["hist"][-1] if r["hist"] else 0, "hist": list(r["hist"]),
                                "video": r["video"], "audio": r["audio"], "readers": r["readers"], "errors": r["errors"],
                                "srt": r["srt"], "health": h, "reason": why}
            else:
                inputs[role] = {"role": role, "path": name, "configured": True, "online": False,
                                "hist": list(r["hist"]) if r else [], "health": "down", "reason": "sin señal"}
        # Decisión simulada (la Fase 0 aún no tiene conmutador): main si está sana, si no backup.
        rec = next((i["role"] for i in inputs.values() if i["online"] and i["health"] == "ok"), None) \
            or next((i["role"] for i in inputs.values() if i["online"] and i["health"] == "warn"), None)
        any_on = any(i["online"] for i in inputs.values())
        if any_on and ch not in sess:
            sess[ch] = {"start": now, "in": 0, "out": 0}
        if not any_on:
            sess.pop(ch, None)
        if ch in sess:
            sess[ch]["in"] += add.get((ch, "in"), 0)
            sess[ch]["out"] += add.get((ch, "out"), 0)
        chans.append({"name": ch, "inputs": inputs, "recommended": rec, "session": sess.get(ch), "keys": keys.get(ch, {}), "program": prog_view(ch)})
    view.update(ok=True, error=None, channels=chans, now=now)


def usage():
    month = utc().strftime("%Y-%m")
    rows = db.execute("select channel, direction, sum(bytes) from usage where day like ? group by 1,2", (month + "%",)).fetchall()
    out = {"month_in": 0, "month_out": 0, "by_channel": {}}
    for ch, d, b in rows:
        out["month_" + d] += b
        out["by_channel"].setdefault(ch, {"in": 0, "out": 0})[d] = b
    return out


async def loop():
    async with httpx.AsyncClient(timeout=3) as c:
        while True:
            try:
                await sample(c)
                outputs.reconcile(output_rows(), lambda ch: f"{RTMP}/{ch}/program?user=verneta&pass={INTERNAL_KEY}")
            except Exception as e:
                view.update(ok=False, error=type(e).__name__)
            await asyncio.sleep(INTERVAL)


@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(loop())
    yield
    task.cancel()
    outputs.stop_all()
    for p in programs.values():
        p.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/api/state")
def state():
    ev = db.execute("select ts, channel, type, message from events order by rowid desc limit 40").fetchall()
    outs = {}
    for o in output_rows():
        st = outputs.status(o["id"]) if o["kind"] == "push" else {"state": "available" if o["enabled"] else "off"}
        outs.setdefault(o["channel"], []).append({**o, "status": st})
    chans = [{**c, "outputs": outs.get(c["name"], [])} for c in view["channels"]]
    return {**view, "channels": chans, "usage": usage(), "public": PORTS,
            "events": [{"ts": a, "channel": b, "type": c, "message": d} for a, b, c, d in ev]}


@app.get("/api/meters")
async def meters_stream():
    """Niveles de audio en tiempo real (~10 Hz) por Server-Sent Events."""
    async def gen():
        while True:
            now = time.time()
            data = {n: {"peak": m["peak"], "rms": m["rms"], "age": round(now - m["t"], 2), "silent_s": silent_for(m)}
                    for n, m in meters.items() if m["peak"]}
            yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(0.1)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/auth")
async def auth(req: Request):
    """MediaMTX consulta aquí cada conexión (authMethod: http). Publicar exige la clave del canal/entrada."""
    b = await req.json()
    action, path, proto = b.get("action"), b.get("path") or "", b.get("protocol") or ""
    pw = str(b.get("password") or "")
    ch, _, role = path.rpartition("/")
    row = db.execute("select key from inputs where channel=? and role=?", (ch, role)).fetchone()
    exists = bool(db.execute("select 1 from channels where name=?", (ch,)).fetchone())
    if action == "publish":
        ok = (exists and hmac.compare_digest(pw, INTERNAL_KEY)) if role == "program" else (bool(row) and hmac.compare_digest(pw, row[0]))
    elif action in ("read", "playback"):
        if exists and hmac.compare_digest(pw, INTERNAL_KEY):   # servicios internos (conmutador, salidas)
            ok = True
        elif role == "program" and exists:   # program: panel y servicios internos siempre; SRT/RTMP externos solo con una salida pull activa
            ok = proto in ("webrtc", "rtsp") or bool(db.execute(
                "select 1 from outputs where channel=? and kind='pull' and protocol=? and enabled=1", (ch, proto)).fetchone())
        else:
            ok = bool(row) and proto in ("webrtc", "rtsp")
    else:
        ok = False
    return Response(status_code=200 if ok else 401)


@app.post("/api/channels")
async def create_channel(req: Request):
    name = str((await req.json()).get("name", "")).strip().lower()
    if not NAME.match(name):
        raise HTTPException(400, "Nombre no válido: minúsculas, números, - y _ (máx. 40).")
    if db.execute("select 1 from channels where name=?", (name,)).fetchone():
        raise HTTPException(409, "Ya existe un canal con ese nombre.")
    db.execute("insert into channels(name, created) values(?,?)", (name, utc().isoformat(timespec="seconds")))
    for role in ("main", "backup"):
        db.execute("insert into inputs values(?,?,?)", (name, role, new_key()))
    db.commit()
    event(name, "channel", "Canal creado")
    return {"name": name}


@app.post("/api/channels/{name}/inputs/{role}/rotate-key")
def rotate_key(name: str, role: str):
    if not db.execute("select 1 from inputs where channel=? and role=?", (name, role)).fetchone():
        raise HTTPException(404, "Entrada no encontrada")
    db.execute("update inputs set key=? where channel=? and role=?", (new_key(), name, role))
    db.commit()
    event(name, "key", f"Clave de {role} regenerada")
    return {"ok": True}


@app.delete("/api/channels/{name}")
def delete_channel(name: str):
    db.execute("delete from inputs where channel=?", (name,))
    db.execute("delete from outputs where channel=?", (name,))
    p = programs.pop(name, None)
    if p:
        p.stop()
    db.execute("delete from channels where name=?", (name,))
    db.commit()
    for role in ("main", "backup"):
        rt.pop(f"{name}/{role}", None)
        stop_meter(f"{name}/{role}")
    sess.pop(name, None)
    return {"ok": True}


PUSH = {"rtmp": "rtmp://", "rtmps": "rtmps://", "srt": "srt://"}


def exists_channel(name):
    return bool(db.execute("select 1 from channels where name=?", (name,)).fetchone())


def check_output(o):
    if o["kind"] == "push":
        if o["protocol"] not in PUSH or not o["url"].startswith(PUSH[o["protocol"]]):
            raise HTTPException(400, "La URL debe empezar por " + PUSH.get(o["protocol"], "rtmp:// / rtmps:// / srt://"))
    elif o["kind"] == "pull":
        if o["protocol"] not in ("srt", "rtmp"):
            raise HTTPException(400, "Pull: protocolo SRT o RTMP")
        o["url"] = o["key"] = ""
    else:
        raise HTTPException(400, "Tipo de salida no válido")
    if not o["name"]:
        raise HTTPException(400, "Falta el nombre de la salida")


@app.post("/api/channels/{name}/mode")
async def set_mode(name: str, req: Request):
    b = await req.json()
    mode, locked = b.get("mode", "auto"), 1 if b.get("locked") else 0
    if mode not in ("auto", "main", "backup"):
        raise HTTPException(400, "Modo no válido")
    if not exists_channel(name):
        raise HTTPException(404, "Canal no encontrado")
    db.execute("update channels set mode=?, locked=? where name=?", (mode, locked, name))
    db.commit()
    if name in programs:
        programs[name].set_mode(mode, locked)
    event(name, "mode", f"Conmutación: {mode}" + (" + lock" if locked else ""))
    return {"ok": True}


@app.post("/api/channels/{name}/outputs")
async def add_output(name: str, req: Request):
    if not exists_channel(name):
        raise HTTPException(404, "Canal no encontrado")
    b = await req.json()
    o = {"kind": str(b.get("kind", "")), "protocol": str(b.get("protocol", "")), "name": str(b.get("name", "")).strip()[:60],
         "url": str(b.get("url", "")).strip(), "key": str(b.get("key", "")).strip()}
    check_output(o)
    cur = db.execute("insert into outputs(channel, name, kind, protocol, url, key, enabled) values(?,?,?,?,?,?,1)",
                     (name, o["name"], o["kind"], o["protocol"], o["url"], o["key"]))
    db.commit()
    event(name, "output", f"Salida «{o['name']}» creada")
    return {"id": cur.lastrowid}


@app.patch("/api/outputs/{oid}")
async def edit_output(oid: int, req: Request):
    cur = next((o for o in output_rows() if o["id"] == oid), None)
    if not cur:
        raise HTTPException(404, "Salida no encontrada")
    b = await req.json()
    new = {**cur, **{k: (str(v).strip() if k != "enabled" else bool(v)) for k, v in b.items() if k in ("name", "protocol", "url", "key", "enabled")}}
    check_output(new)
    db.execute("update outputs set name=?, protocol=?, url=?, key=?, enabled=? where id=?",
               (new["name"], new["protocol"], new["url"], new["key"], int(new["enabled"]), oid))
    db.commit()
    if new["enabled"] != cur["enabled"]:
        event(cur["channel"], "output", f"Salida «{new['name']}» {'activada' if new['enabled'] else 'apagada'}")
    return {"ok": True}


@app.delete("/api/outputs/{oid}")
def delete_output(oid: int):
    cur = next((o for o in output_rows() if o["id"] == oid), None)
    db.execute("delete from outputs where id=?", (oid,))
    db.commit()
    if cur:
        event(cur["channel"], "output", f"Salida «{cur['name']}» eliminada")
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
