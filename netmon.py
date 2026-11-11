#!/usr/bin/env python3
"""netmon: home internet monitor for a Meraki MX.

  netmon.py run                       monitoring daemon (run under systemd)
  netmon.py report [--days N]         outages, uptime, and speed summary
  netmon.py export TABLE [--days N]   CSV to stdout (outages, speedtests, loss_latency, usage, events)
  netmon.py test-alert                send a test ntfy notification (and email, if configured)

Collects:
  - local ping checks every few seconds -> outage start/end, alerts
  - Meraki API: WAN loss/latency/jitter, bytes used, uplink status / public IP
  - periodic speed test (Ookla `speedtest`, `speedtest-cli`, or macOS `networkQuality`)
Stores everything in SQLite. Needs only the Python 3.8+ standard library.
"""
import argparse
import configparser
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.meraki.com/api/v1"

# Supported: macOS (launchd, runs as your user) and Linux / Raspberry Pi OS (systemd, runs as `netmon`).
IS_MAC = sys.platform == "darwin"
if IS_MAC:
    DEFAULT_CONFIG = os.path.expanduser("~/.config/netmon/netmon.ini")
    DEFAULT_DB = os.path.expanduser("~/Library/Application Support/netmon/netmon.db")
elif sys.platform.startswith("linux"):
    DEFAULT_CONFIG = "/etc/netmon/netmon.ini"
    DEFAULT_DB = "/var/lib/netmon/netmon.db"
else:
    sys.exit(f"netmon supports macOS and Linux, not {sys.platform}")

DEFAULTS = {
    "meraki": {"serial": "", "network_id": "", "org_id": "", "probe_ip": "8.8.8.8", "interval_minutes": "10"},
    "ping": {"targets": "1.1.1.1, 8.8.8.8, 9.9.9.9", "interval_seconds": "10", "fail_threshold": "3"},
    "speedtest": {"interval_minutes": "60", "expected_down_mbps": "", "expected_up_mbps": "", "slow_alert_percent": "50"},
    "alerts": {"ntfy_url": "", "ntfy_token": "", "email": "", "daily_summary_hour": "8"},
    "storage": {"db": ""},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS outages(id INTEGER PRIMARY KEY, start TEXT NOT NULL, end TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS loss_latency(ts TEXT PRIMARY KEY, loss_pct REAL, latency_ms REAL, jitter_ms REAL);
CREATE TABLE IF NOT EXISTS usage(ts TEXT PRIMARY KEY, sent_bytes INTEGER, received_bytes INTEGER, seconds INTEGER);
CREATE TABLE IF NOT EXISTS speedtests(ts TEXT PRIMARY KEY, down_mbps REAL, up_mbps REAL, ping_ms REAL, server TEXT);
CREATE TABLE IF NOT EXISTS events(ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS pending_alerts(id INTEGER PRIMARY KEY, title TEXT, body TEXT, priority TEXT, tags TEXT, email INTEGER);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


# ---------- helpers ----------

def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def local(s, fmt="%Y-%m-%d %H:%M"):
    return parse(s).astimezone().strftime(fmt)


def fmt_dur(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


def log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def load_config(path):
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read_dict(DEFAULTS)
    if path:
        if not cfg.read(path):
            sys.exit(f"config file not found: {path}")
    return cfg


def open_db(cfg):
    path = os.path.expanduser(cfg["storage"]["db"] or DEFAULT_DB)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def kv_get(db, k, default=None):
    row = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def kv_set(db, k, v):
    db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, v))
    db.commit()


def event(db, kind, detail):
    db.execute("INSERT INTO events(ts, kind, detail) VALUES(?, ?, ?)", (iso(utcnow()), kind, detail))
    db.commit()
    log(f"{kind}: {detail}")


# ---------- connectivity ----------

def ping(host):
    timeout = ["-t", "2"] if IS_MAC else ["-W", "2"]
    try:
        return subprocess.run(["ping", "-c", "1", *timeout, host], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=5).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def default_gateway():
    """The LAN router's IP. Skips VPN tunnel routes, which have no gateway address."""
    try:
        if IS_MAC:
            out = subprocess.run(["netstat", "-rn", "-f", "inet"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                f = line.split()
                if len(f) >= 2 and f[0] == "default" and f[1].count(".") == 3:
                    return f[1]
        else:
            out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout.split()
            if "via" in out:
                return out[out.index("via") + 1]
    except FileNotFoundError:
        pass
    return None


# ---------- alerts ----------

def send_ntfy(cfg, title, body, priority="default", tags="", email=False):
    a = cfg["alerts"]
    if not a["ntfy_url"] or "CHANGE-ME" in a["ntfy_url"]:
        log(f"(ntfy_url not set) {title}: {body}")
        return True
    if email and a["email"] and not a["ntfy_token"]:
        log("email is set but ntfy_token is not; ntfy.sh only sends email for accounts, so no email will go out")
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if email and a["email"]:
        headers["Email"] = a["email"]
    if a["ntfy_token"]:
        headers["Authorization"] = f"Bearer {a['ntfy_token']}"
    req = urllib.request.Request(a["ntfy_url"], data=body.encode(), headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except (urllib.error.URLError, OSError) as e:
        log(f"ntfy send failed: {e}")
        return False


def queue_alert(db, title, body, priority="default", tags="", email=False):
    db.execute("INSERT INTO pending_alerts(title, body, priority, tags, email) VALUES(?, ?, ?, ?, ?)",
               (title, body, priority, tags, int(email)))
    db.commit()


def flush_alerts(db, cfg):
    for row in db.execute("SELECT * FROM pending_alerts ORDER BY id").fetchall():
        if not send_ntfy(cfg, row["title"], row["body"], row["priority"], row["tags"], bool(row["email"])):
            return
        db.execute("DELETE FROM pending_alerts WHERE id=?", (row["id"],))
        db.commit()


# ---------- Meraki ----------

def meraki_get(path, params):
    key = os.environ.get("MERAKI_DASHBOARD_API_KEY")
    if not key:
        keyfile = os.path.expanduser("~/.meraki_key")
        if os.path.exists(keyfile):
            key = open(keyfile).read().strip()
    if not key:
        raise RuntimeError("MERAKI_DASHBOARD_API_KEY not set")
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def resolve_meraki_ids(cfg):
    """Fill in network_id and org_id from the serial when the config leaves them blank."""
    m = cfg["meraki"]
    if not m["network_id"]:
        m["network_id"] = meraki_get(f"/devices/{m['serial']}", {})["networkId"]
    if not m["org_id"]:
        m["org_id"] = meraki_get(f"/networks/{m['network_id']}", {})["organizationId"]


def meraki_sync(db, cfg):
    resolve_meraki_ids(cfg)
    m = cfg["meraki"]
    now = utcnow()

    # Loss/latency: resume an hour before the last stored bucket (to refresh partial buckets);
    # the first run backfills the 30 days the API keeps.
    last = db.execute("SELECT max(ts) AS ts FROM loss_latency").fetchone()["ts"]
    t0 = max(parse(last) - timedelta(hours=1), now - timedelta(days=30)) if last else now - timedelta(days=30)
    rows = meraki_get(f"/devices/{m['serial']}/lossAndLatencyHistory",
                      {"t0": iso(t0), "t1": iso(now), "uplink": "wan1", "ip": m["probe_ip"], "resolution": 600})
    db.executemany("INSERT OR REPLACE INTO loss_latency VALUES(?, ?, ?, ?)",
                   [(r["startTs"], r["lossPercent"], r["latencyMs"], r.get("jitter")) for r in rows])

    last = db.execute("SELECT max(ts) AS ts FROM usage").fetchone()["ts"]
    t0 = max(parse(last) - timedelta(hours=1), now - timedelta(days=7)) if last else now - timedelta(days=7)
    rows = meraki_get(f"/networks/{m['network_id']}/appliance/uplinks/usageHistory",
                      {"t0": iso(t0), "t1": iso(now), "resolution": 600})
    for r in rows:
        wan1 = next((i for i in r["byInterface"] if i["interface"] == "wan1"), None)
        if wan1:
            secs = int((parse(r["endTime"]) - parse(r["startTime"])).total_seconds())
            db.execute("INSERT OR REPLACE INTO usage VALUES(?, ?, ?, ?)",
                       (r["startTime"], wan1["sent"], wan1["received"], secs))
    db.commit()

    statuses = meraki_get(f"/organizations/{m['org_id']}/appliance/uplink/statuses",
                          {"serials[]": m["serial"]})
    for dev in statuses:
        for up in dev["uplinks"]:
            if up["interface"] != "wan1":
                continue
            prev_status = kv_get(db, "wan1_status")
            if up["status"] != prev_status:
                if prev_status is not None:
                    event(db, "wan1_status", f"{prev_status} -> {up['status']}")
                kv_set(db, "wan1_status", up["status"])
            ip, prev_ip = up.get("publicIp"), kv_get(db, "public_ip")
            if ip and ip != prev_ip:
                if prev_ip:
                    event(db, "public_ip_change", f"{prev_ip} -> {ip}")
                    queue_alert(db, "Public IP changed", f"{prev_ip} -> {ip} (often means the ISP reset the line)",
                                tags="globe_with_meridians")
                kv_set(db, "public_ip", ip)


# ---------- speed test ----------

def run_json(cmd, timeout=180):
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def speedtest():
    """Returns (down_mbps, up_mbps, ping_ms, server) or None if no tool is installed."""
    st = shutil.which("speedtest")
    if st and "Ookla" in subprocess.run([st, "--version"], capture_output=True, text=True).stdout:
        d = run_json([st, "--format=json", "--accept-license", "--accept-gdpr"])
        return (d["download"]["bandwidth"] * 8 / 1e6, d["upload"]["bandwidth"] * 8 / 1e6,
                d["ping"]["latency"], d["server"]["name"])
    st = shutil.which("speedtest-cli") or st
    if st:
        d = run_json([st, "--json", "--secure"])
        return d["download"] / 1e6, d["upload"] / 1e6, d["ping"], d["server"]["sponsor"]
    if shutil.which("networkQuality"):
        d = run_json(["networkQuality", "-c"])
        return d["dl_throughput"] / 1e6, d["ul_throughput"] / 1e6, d.get("base_rtt"), "Apple networkQuality"
    return None


def do_speedtest(db, cfg):
    result = speedtest()
    if result is None:
        log("no speed test tool installed (install Ookla `speedtest` or `speedtest-cli`)")
        return
    down, up, ping_ms, server = result
    db.execute("INSERT OR REPLACE INTO speedtests VALUES(?, ?, ?, ?, ?)", (iso(utcnow()), down, up, ping_ms, server))
    db.commit()
    log(f"speedtest: {down:.1f} down / {up:.1f} up Mbps, {ping_ms} ms ({server})")

    s = cfg["speedtest"]
    pct = float(s["slow_alert_percent"]) / 100
    slow = []
    if s["expected_down_mbps"] and down < float(s["expected_down_mbps"]) * pct:
        slow.append(f"download {down:.0f} of {s['expected_down_mbps']} Mbps")
    if s["expected_up_mbps"] and up < float(s["expected_up_mbps"]) * pct:
        slow.append(f"upload {up:.0f} of {s['expected_up_mbps']} Mbps")
    if slow:
        event(db, "slow_speed", ", ".join(slow))
        queue_alert(db, "Internet is slow", "Speed test: " + ", ".join(slow), priority="high", tags="turtle")


# ---------- reporting ----------

def outage_runs_from_meraki(db, since):
    """Consecutive 10-min buckets where the MX had no data or >=50% loss."""
    runs, cur = [], None
    for r in db.execute("SELECT ts, loss_pct FROM loss_latency WHERE ts >= ? ORDER BY ts", (iso(since),)):
        bad = r["loss_pct"] is None or r["loss_pct"] >= 50
        end = iso(parse(r["ts"]) + timedelta(minutes=10))
        if bad and cur and cur[1] == r["ts"]:
            cur[1] = end
        elif bad:
            cur = [r["ts"], end]
            runs.append(cur)
    return runs


def summary_text(db, days):
    since = utcnow() - timedelta(days=days)
    lines = []

    outs = db.execute("SELECT * FROM outages WHERE start >= ? OR end IS NULL ORDER BY start", (iso(since),)).fetchall()
    total = sum(((parse(o["end"]) if o["end"] else utcnow()) - parse(o["start"])).total_seconds() for o in outs)
    lines.append(f"Outages seen by this monitor: {len(outs)}, total {fmt_dur(total)}")
    for o in outs:
        end = local(o["end"], "%m-%d %H:%M") if o["end"] else "ongoing"
        dur = ((parse(o["end"]) if o["end"] else utcnow()) - parse(o["start"])).total_seconds()
        lines.append(f"  {local(o['start'], '%m-%d %H:%M')} -> {end}  {fmt_dur(dur):>8}  {o['kind']}")

    n = db.execute("SELECT count(*) AS n FROM loss_latency WHERE ts >= ?", (iso(since),)).fetchone()["n"]
    if n:
        runs = outage_runs_from_meraki(db, since)
        bad = sum((parse(e) - parse(s)).total_seconds() for s, e in runs) / 600
        lines.append(f"Meraki MX view: {100 * (n - bad) / n:.2f}% uptime ({len(runs)} outage(s), 10-min buckets)")
        for s, e in runs:
            lines.append(f"  {local(s, '%m-%d %H:%M')} -> {local(e, '%m-%d %H:%M')}  "
                         f"{fmt_dur((parse(e) - parse(s)).total_seconds()):>8}")
        q = db.execute("SELECT avg(loss_pct) AS loss, avg(latency_ms) AS lat, max(latency_ms) AS maxlat "
                       "FROM loss_latency WHERE ts >= ? AND loss_pct < 50", (iso(since),)).fetchone()
        if q["lat"] is not None:
            lines.append(f"While up: avg loss {q['loss']:.2f}%, latency avg {q['lat']:.1f} ms / max {q['maxlat']:.1f} ms")

    u = db.execute("SELECT sum(sent_bytes) AS s, sum(received_bytes) AS r FROM usage WHERE ts >= ?",
                   (iso(since),)).fetchone()
    if u["r"]:
        lines.append(f"Data used: {u['r'] / 1e9:.1f} GB down, {u['s'] / 1e9:.1f} GB up")

    sp = db.execute("SELECT count(*) AS n, avg(down_mbps) AS ad, min(down_mbps) AS mind, max(down_mbps) AS maxd, "
                    "avg(up_mbps) AS au, min(up_mbps) AS minu FROM speedtests WHERE ts >= ?", (iso(since),)).fetchone()
    if sp["n"]:
        lines.append(f"Speed tests ({sp['n']}): down avg {sp['ad']:.0f} Mbps (min {sp['mind']:.0f}, max {sp['maxd']:.0f}), "
                     f"up avg {sp['au']:.0f} Mbps (min {sp['minu']:.0f})")

    for e in db.execute("SELECT * FROM events WHERE ts >= ? AND kind != 'outage' ORDER BY ts", (iso(since),)):
        lines.append(f"Event {local(e['ts'], '%m-%d %H:%M')}: {e['kind']} {e['detail']}")
    return "\n".join(lines)


def maybe_daily_summary(db, cfg):
    hour = cfg["alerts"]["daily_summary_hour"]
    if not hour:
        return
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.hour >= int(hour) and kv_get(db, "last_summary") != today:
        queue_alert(db, f"Internet report {today}", summary_text(db, 1), priority="low",
                    tags="bar_chart", email=True)
        kv_set(db, "last_summary", today)


# ---------- daemon ----------

def run(cfg):
    db = open_db(cfg)
    targets = [t.strip() for t in cfg["ping"]["targets"].split(",") if t.strip()]
    interval = int(cfg["ping"]["interval_seconds"])
    threshold = int(cfg["ping"]["fail_threshold"])
    meraki_every = int(cfg["meraki"]["interval_minutes"]) * 60
    speed_every = int(cfg["speedtest"]["interval_minutes"]) * 60
    use_meraki = bool(cfg["meraki"]["serial"])

    open_outage = db.execute("SELECT id, start FROM outages WHERE end IS NULL").fetchone()
    fails, first_fail = 0, None
    next_meraki = time.monotonic()
    next_speed = time.monotonic() + 120
    log(f"netmon started; pinging {', '.join(targets)} every {interval}s"
        + (f"; resuming open outage since {local(open_outage['start'])}" if open_outage else ""))

    while True:
        started = time.monotonic()
        online = any(ping(t) for t in targets)

        if online:
            if open_outage:
                end = utcnow()
                db.execute("UPDATE outages SET end=? WHERE id=?", (iso(end), open_outage["id"]))
                db.commit()
                dur = fmt_dur((end - parse(open_outage["start"])).total_seconds())
                event(db, "outage", f"recovered after {dur}")
                queue_alert(db, "Internet is back",
                            f"Down from {local(open_outage['start'])} to {local(iso(end), '%H:%M')} ({dur})",
                            priority="high", tags="white_check_mark", email=True)
                open_outage = None
            fails = 0
        else:
            if fails == 0:
                first_fail = utcnow()
            fails += 1
            if fails == threshold and not open_outage:
                gw = default_gateway()
                kind = "UNKNOWN" if not gw else ("WAN" if ping(gw) else "LAN")
                cur = db.execute("INSERT INTO outages(start, kind) VALUES(?, ?)", (iso(first_fail), kind))
                db.commit()
                open_outage = {"id": cur.lastrowid, "start": iso(first_fail)}
                event(db, "outage", f"{kind} down since {local(iso(first_fail), '%H:%M:%S')} (gateway {gw})")
                # Usually can't be delivered right now; sent only if the internet is partially up.
                send_ntfy(cfg, "Internet is DOWN", f"{kind} outage since {local(iso(first_fail), '%H:%M')}",
                          priority="urgent", tags="rotating_light")

        if online:
            flush_alerts(db, cfg)
            if use_meraki and time.monotonic() >= next_meraki:
                try:
                    meraki_sync(db, cfg)
                except Exception as e:  # keep monitoring if the API hiccups
                    log(f"meraki sync failed: {e}")
                next_meraki = time.monotonic() + meraki_every
            if speed_every and time.monotonic() >= next_speed:
                try:
                    do_speedtest(db, cfg)
                except Exception as e:
                    log(f"speedtest failed: {e}")
                next_speed = time.monotonic() + speed_every
            maybe_daily_summary(db, cfg)

        time.sleep(max(0, interval - (time.monotonic() - started)))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.environ.get("NETMON_CONFIG", DEFAULT_CONFIG))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    r = sub.add_parser("report")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--sync", action="store_true", help="pull fresh Meraki data first")
    e = sub.add_parser("export")
    e.add_argument("table", choices=["outages", "speedtests", "loss_latency", "usage", "events"])
    e.add_argument("--days", type=int, default=30)
    sub.add_parser("test-alert")
    args = p.parse_args()

    cfg = load_config(args.config)
    if args.cmd == "run":
        run(cfg)
    elif args.cmd == "report":
        db = open_db(cfg)
        if args.sync:
            meraki_sync(db, cfg)
        print(f"Last {args.days} day(s), times in local time\n" + summary_text(db, args.days))
    elif args.cmd == "export":
        db = open_db(cfg)
        col = "start" if args.table == "outages" else "ts"
        rows = db.execute(f"SELECT * FROM {args.table} WHERE {col} >= ? ORDER BY {col}",
                          (iso(utcnow() - timedelta(days=args.days)),)).fetchall()
        w = csv.writer(sys.stdout)
        if rows:
            w.writerow(rows[0].keys())
            w.writerows([tuple(r) for r in rows])
    elif args.cmd == "test-alert":
        ok = send_ntfy(cfg, "netmon test", "If you can read this, alerts work.", tags="tada", email=True)
        print("sent" if ok else "failed")


if __name__ == "__main__":
    main()
