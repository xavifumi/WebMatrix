"""Salidas push: un ffmpeg (-c copy) por salida lee `canal/program` y lo envía al destino, con reintentos."""
import asyncio, logging, time

log = logging.getLogger("outputs")
runners = {}     # id -> {"sig", "task", "st"}


def dest_url(o):
    url = o["url"].rstrip("/") + (("/" + o["key"]) if o.get("key") else "")
    if o["protocol"] == "srt" and "pkt_size" not in url:
        url += ("&" if "?" in url else "?") + "pkt_size=1316"
    return url


def command(o, read_url):
    fmt = "mpegts" if o["protocol"] == "srt" else "flv"
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            "-i", read_url, "-c", "copy", "-f", fmt, dest_url(o)]


async def run(o, read_url, st):
    attempts = 0
    while True:
        st.update(state="connecting", bitrate_kbps=0)
        proc, err, live_since = None, "", None
        try:
            proc = await asyncio.create_subprocess_exec(*command(o, read_url), stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)

            async def read_err():
                nonlocal err
                async for raw in proc.stderr:
                    line = raw.decode(errors="ignore").strip()
                    if line:
                        err = line[-200:]

            et = asyncio.create_task(read_err())
            async for raw in proc.stdout:
                k, _, v = raw.decode(errors="ignore").strip().partition("=")
                if k == "total_size" and v.isdigit():
                    st["sent"] = int(v)
                elif k == "bitrate":
                    try: st["bitrate_kbps"] = float(v.replace("kbits/s", "")) if v[:1].isdigit() else 0
                    except ValueError: pass
                elif k == "progress" and st["state"] != "live":
                    st.update(state="live", since=time.time(), error=""); live_since = time.time()
            await proc.wait(); et.cancel()
        finally:
            if proc and proc.returncode is None:
                proc.kill()
        if live_since and time.time() - live_since > 20:
            attempts = 0
        attempts += 1
        st.update(state="retry", attempts=attempts, error=err or "conexión cerrada", bitrate_kbps=0, since=None)
        await asyncio.sleep(min(30, 2 ** min(attempts, 5)))


def reconcile(rows, read_url_fn):
    """rows: salidas push habilitadas (dicts). Arranca, reinicia o para los procesos según cambie la configuración."""
    wanted = {o["id"]: o for o in rows if o["kind"] == "push" and o["enabled"]}
    for oid in list(runners):
        sig = tuple(sorted((k, str(v)) for k, v in wanted[oid].items() if k in ("channel", "protocol", "url", "key"))) if oid in wanted else None
        if oid not in wanted or runners[oid]["sig"] != sig:
            runners.pop(oid)["task"].cancel()
    for oid, o in wanted.items():
        if oid not in runners:
            st = {"state": "connecting", "attempts": 0, "error": "", "sent": 0, "bitrate_kbps": 0, "since": None}
            sig = tuple(sorted((k, str(v)) for k, v in o.items() if k in ("channel", "protocol", "url", "key")))
            runners[oid] = {"sig": sig, "st": st, "task": asyncio.create_task(run(o, read_url_fn(o["channel"]), st))}


def status(oid):
    r = runners.get(oid)
    return dict(r["st"]) if r else {"state": "off"}


def stop_all():
    for oid in list(runners):
        runners.pop(oid)["task"].cancel()
