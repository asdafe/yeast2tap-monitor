#!/usr/bin/env python3
"""Gentle poller: GET /getstatus, GET /fs, GET /tschedule (hourly). Nothing else."""
import json, os, re, subprocess, time, urllib.request, glob
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

BASE = os.environ["BREW_BASE"].rstrip("/")
HOST = urlparse(BASE).hostname or BASE
PB = os.environ.get("PB_TOKEN", "")
DATA = "data"
NOW = datetime.now(timezone.utc)
TS = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
os.makedirs(DATA, exist_ok=True)

def fetch(path):
    """One GET, --max-time 15, max 1 retry. Errors are not printed (they'd contain the URL)."""
    r = subprocess.run(["curl", "-sS", "--max-time", "15", "--retry", "1", "--retry-delay", "3",
                        "-o", "-", "-w", "\n%{http_code}", BASE + path],
                       capture_output=True, text=True)
    out = r.stdout.rsplit("\n", 1)
    body, code = (out[0], out[1]) if len(out) == 2 else ("", "000")
    try: code = int(code)
    except ValueError: code = 0
    return code, body

def scrub(o):
    if isinstance(o, dict): return {k: scrub(v) for k, v in o.items()}
    if isinstance(o, list): return [scrub(v) for v in o]
    if isinstance(o, str): return o.replace(BASE, "[base]").replace(HOST, "[host]")
    return o

def parse_json(body):
    try: return json.loads(body)
    except Exception: return None

def parse_fs(body):
    j = parse_json(body)
    if isinstance(j, dict):
        return {k: v for k, v in j.items() if isinstance(v, (int, float, str, bool))}
    # fallback: "key: number" / "key=number" pairs from plain text
    return {k: int(v) for k, v in re.findall(r"([A-Za-z_]+)\s*[:=]\s*(\d+)", body or "")} or None

def load(p, d):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return d

state = load(f"{DATA}/alert_state.json", {"alerts": {}, "consec": {}, "last_tschedule": None})

codes = {}
codes["getstatus"], b = fetch("/getstatus"); status = parse_json(b) if codes["getstatus"] == 200 else None
time.sleep(2)
codes["fs"], b = fetch("/fs"); fs = parse_fs(b) if codes["fs"] == 200 else None
tsch = None
last_t = state.get("last_tschedule")
due = NOW.minute < 10 and (not last_t or NOW - datetime.fromisoformat(last_t.replace("Z", "+00:00")) > timedelta(minutes=45))
if due:
    time.sleep(2)
    codes["tschedule"], b = fetch("/tschedule")
    if codes["tschedule"] == 200:
        tsch = parse_json(b) or b[:2000]
        state["last_tschedule"] = TS

ok = isinstance(status, dict)
rec = {"ts": TS, "ok": ok, "http": codes, "getstatus": status, "fs": fs}
if due: rec["tschedule"] = tsch
rec = scrub(rec)

with open(f"{DATA}/{NOW:%Y-%m-%d}.jsonl", "a") as f: f.write(json.dumps(rec, separators=(",", ":")) + "\n")
with open(f"{DATA}/latest.json", "w") as f: json.dump(rec, f, indent=1)

# retention: 30 days
for p in glob.glob(f"{DATA}/20??-??-??.jsonl"):
    try:
        if NOW.date() - datetime.strptime(os.path.basename(p)[:10], "%Y-%m-%d").date() > timedelta(days=30): os.remove(p)
    except ValueError: pass

# ---- field extraction (BrewPiLess key aliases) ----
def pick(d, *keys):
    if not isinstance(d, dict): return (False, None)
    for k in keys:
        if k in d: return (True, d[k])
    return (False, None)

TEMP_KEYS = {"beer": ("beerTemp", "bt", "BeerTemp"), "fridge": ("fridgeTemp", "ft", "FridgeTemp"),
             "room": ("roomTemp", "rt", "RoomTemp")}
mode = pick(status, "mode", "md", "Mode")[1]
mode_off = str(mode).lower() in ("o", "off", "none") if mode is not None else False
_, bt = pick(status, "beerTemp", "bt", "BeerTemp")
_, bs = pick(status, "beerSet", "bs", "BeerSet")
heap = None
for src in (fs, status):
    h = pick(src, "heap", "freeheap", "freeHeap", "fheap", "HEAP")[1]
    if isinstance(h, (int, float)): heap = h; break

c = state.setdefault("consec", {})
c["unreach"] = 0 if ok else c.get("unreach", 0) + 1
dev = ok and isinstance(bt, (int, float)) and isinstance(bs, (int, float)) and abs(bt - bs) > 3
c["dev"] = c.get("dev", 0) + 1 if dev else 0
null_sensors = [n for n, ks in TEMP_KEYS.items() if pick(status, *ks)[0] and pick(status, *ks)[1] is None] if ok else []

conds = {
    "unreach": (c["unreach"] >= 2, "Fermantasyon kontrolcüsüne ulaşılamıyor (2+ ardışık deneme)."),
    "sensor": (ok and not mode_off and bool(null_sensors), f"Sıcaklık sensörü okunamıyor: {', '.join(null_sensors)} (mod: {mode})."),
    "deviation": (c["dev"] >= 2, f"Bira sıcaklığı hedeften >3°C sapmış: {bt}°C (hedef {bs}°C)."),
    "heap": (heap is not None and heap < 20000, f"ESP32 boş bellek düşük: {heap} bayt."),
}
NAMES = {"unreach": "Bağlantı", "sensor": "Sensör", "deviation": "Sıcaklık sapması", "heap": "Bellek"}

def push(title, body):
    if not PB: return False
    req = urllib.request.Request("https://api.pushbullet.com/v2/pushes",
        data=json.dumps({"type": "note", "title": title, "body": body}).encode(),
        headers={"Access-Token": PB, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r: return 200 <= r.status < 300
    except Exception:
        return False

A = state.setdefault("alerts", {})
for k, (active, msg) in conds.items():
    a = A.setdefault(k, {"active": False, "last_sent": None})
    if active:
        last = a.get("last_sent")
        stale = not last or NOW - datetime.fromisoformat(last.replace("Z", "+00:00")) >= timedelta(hours=2)
        if not a["active"] or stale:
            if push(f"🚨 Bira alarmı: {NAMES[k]}", msg): a["last_sent"] = TS
            print(f"alert {k}: active (push attempted)")
        if not a["active"]: a["since"] = TS
        a["active"] = True
    elif a["active"]:
        if a.get("last_sent"): push(f"✅ Düzeldi: {NAMES[k]}", f"{NAMES[k]} sorunu düzeldi ({TS}).")
        A[k] = {"active": False, "last_sent": None, "cleared": TS}
        print(f"alert {k}: cleared")

with open(f"{DATA}/alert_state.json", "w") as f: json.dump(state, f, indent=1)
print(f"ok={ok} http={codes} beer={bt} set={bs} heap={heap}")
