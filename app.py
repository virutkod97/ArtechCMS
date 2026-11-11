#!/usr/bin/env python3
"""
ArtechCMS - web management for Artech voice loggers.
Flask app: multi-device management, scheduled auto-download, browser playback.
"""
import os, io, json, time, threading, subprocess, sys
from flask import (Flask, request, jsonify, send_file, Response,
                   render_template, abort)
from artech.db import DB
from artech import protocol, downloader

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
REC_ROOT = os.path.join(BASE, "recordings")
DB_PATH = os.path.join(DATA_DIR, "artechcms.db")
GITHUB_REPO = "https://github.com/virutkod97/ArtechCMS"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REC_ROOT, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
db = DB(DB_PATH)

_log_lines = []
def log(*a):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), " ".join(str(x) for x in a))
    _log_lines.append(line)
    del _log_lines[:-400]
    print(line, flush=True)


# ------------------------------------------------------------------ scheduler
_sched_stop = threading.Event()

def scheduler_loop():
    log("scheduler started")
    while not _sched_stop.is_set():
        try:
            now = int(time.time())
            for d in db.list_devices():
                if not d["enabled"]:
                    continue
                interval = max(1, int(d["interval_min"] or 15)) * 60
                if now - int(d["last_run"] or 0) >= interval:
                    log("auto-run device #%d %s" % (d["id"], d["name"]))
                    downloader.run_device(db, d, REC_ROOT, log=log)
        except Exception as e:
            log("scheduler error: %s" % e)
        _sched_stop.wait(20)


# ------------------------------------------------------------------ pages
@app.route("/")
def index():
    return render_template("index.html", repo=GITHUB_REPO)


# ------------------------------------------------------------------ device API
@app.route("/api/devices", methods=["GET"])
def api_devices():
    devs = db.list_devices()
    for d in devs:
        d.pop("password", None)
        d["recordings"] = db.count_recordings(d["id"])
    return jsonify(devs)

@app.route("/api/devices", methods=["POST"])
def api_add_device():
    d = request.get_json(force=True)
    if not d.get("name") or not d.get("host"):
        return jsonify({"error": "name and host required"}), 400
    did = db.add_device(d)
    return jsonify({"id": did})

@app.route("/api/devices/<int:did>", methods=["PUT"])
def api_update_device(did):
    db.update_device(did, request.get_json(force=True))
    return jsonify({"ok": True})

@app.route("/api/devices/<int:did>", methods=["DELETE"])
def api_delete_device(did):
    db.delete_device(did)
    return jsonify({"ok": True})

@app.route("/api/devices/<int:did>/download", methods=["POST"])
def api_download_now(did):
    import datetime
    d = db.get_device(did)
    if not d:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    dates = None
    df, dt = body.get("from"), body.get("to")
    if df and dt:
        try:
            a = datetime.datetime.strptime(df, "%Y-%m-%d").date()
            b = datetime.datetime.strptime(dt, "%Y-%m-%d").date()
            if b < a:
                a, b = b, a
            dates = [a + datetime.timedelta(days=i) for i in range((b - a).days + 1)]
        except Exception:
            return jsonify({"error": "from/to phai dang YYYY-MM-DD"}), 400
    def job():
        downloader.run_device(db, d, REC_ROOT, log=log, dates=dates)
    threading.Thread(target=job, daemon=True).start()
    rng = ("%s..%s" % (df, dt)) if dates else ("mặc định (%d ngày)" % int(d.get("lookback_days", 1) or 1))
    return jsonify({"ok": True, "msg": "Đã bắt đầu tải: %s" % rng})

@app.route("/api/devices/<int:did>/test", methods=["POST"])
def api_test_device(did):
    d = db.get_device(did)
    if not d:
        return jsonify({"error": "not found"}), 404
    method = (d.get("method") or "db6066").lower()
    if method == "ftp":
        from artech import ftp_downloader
        user = d.get("ftp_user") or d.get("user", "admin")
        pw = d.get("ftp_pass") or d.get("password", "1111")
        log("[test ftp %s:%s] user=%s" % (d["host"], d.get("ftp_port", 21), user))
        ok, msg = ftp_downloader.probe(d["host"], d.get("ftp_port", 21), user, pw)
        log("[test ftp %s] %s" % (d["host"], msg))
        return jsonify({"ok": ok, "msg": msg})
    # standalone db6066: a successful 6066 login is all that's needed
    res = protocol.login_probe(d, log=log)
    return jsonify(res)


# ------------------------------------------------------------------ recordings API
CDR_FIELDS = ["AutoID", "RecFilePath", "RecType", "RecChannel", "RingTime",
              "RecTime", "TalkTime", "FileSize", "PhoneNumber", "SystemID", "SystemName"]

@app.route("/api/devices/<int:did>/calllog", methods=["POST"])
def api_calllog(did):
    """Pull the device's call log (CDR) for a date range, live from the device."""
    import datetime
    from artech import db_downloader
    d = db.get_device(did)
    if not d:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(force=True)
    try:
        date_from = datetime.datetime.strptime(body["from"], "%Y-%m-%d").date()
        date_to = datetime.datetime.strptime(body["to"], "%Y-%m-%d").date()
    except Exception:
        return jsonify({"error": "from/to phai dang YYYY-MM-DD"}), 400
    rows, err = db_downloader.fetch_calllog(d, date_from, date_to, log=log)
    if err:
        return jsonify({"error": err}), 502
    items = []
    for r in rows:
        path = r.get("RecFilePath") or ""
        name = path.replace("/", "\\").rsplit("\\", 1)[-1]
        meta = protocol.parse_recording_name(name)
        ex = db.c.execute("SELECT id FROM recordings WHERE device_id=? AND remote_path=?",
                          (did, path)).fetchone()
        items.append({
            **meta,
            "filename": name,
            "filesize": int(r.get("FileSize", 0) or 0),
            "rec_time": int(r.get("RecTime", 0) or 0),
            "downloadable": name.lower().endswith(".wav") and int(r.get("FileSize", 0) or 0) > 0,
            "downloaded": bool(ex),
            "rec_id": ex[0] if ex else None,
            "cdr": {k: r.get(k) for k in CDR_FIELDS},
        })
    items.sort(key=lambda x: x.get("call_ts", 0), reverse=True)
    return jsonify({"total": len(items), "items": items})

@app.route("/api/devices/<int:did>/fetch", methods=["POST"])
def api_fetch_one(did):
    """Download one recording (by its CDR row) on demand; store + return rec_id."""
    from artech import db_downloader
    d = db.get_device(did)
    if not d:
        return jsonify({"error": "not found"}), 404
    cdr = request.get_json(force=True).get("cdr") or {}
    if not cdr.get("RecFilePath"):
        return jsonify({"error": "thieu RecFilePath"}), 400
    rid, err = db_downloader.fetch_one(db, d, cdr, REC_ROOT, log=log)
    if err:
        return jsonify({"ok": False, "error": err}), 502
    return jsonify({"ok": True, "rec_id": rid})

@app.route("/api/recordings")
def api_recordings():
    import datetime
    did = request.args.get("device", type=int)
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(200, request.args.get("page_size", 50, type=int))
    df = request.args.get("from")   # YYYY-MM-DD (call date, inclusive)
    dt = request.args.get("to")     # YYYY-MM-DD (call date, inclusive)
    ts_from = ts_to = None
    try:
        if df:
            ts_from = int(datetime.datetime.strptime(df, "%Y-%m-%d").timestamp())
        if dt:
            ts_to = int((datetime.datetime.strptime(dt, "%Y-%m-%d") + datetime.timedelta(days=1)).timestamp())
    except Exception:
        pass
    total = db.count_recordings_filtered(did, ts_from, ts_to)
    recs = db.list_recordings(did, page_size, (page - 1) * page_size, ts_from, ts_to)
    for r in recs:
        r.update(protocol.parse_recording_name(r["filename"]))
    return jsonify({"total": total, "page": page, "page_size": page_size,
                    "pages": (total + page_size - 1) // page_size, "items": recs})

@app.route("/api/recordings/<int:rid>/play")
def api_play(rid):
    """Transcode ADPCM->PCM WAV on the fly so any browser can play it."""
    r = db.get_recording(rid)
    if not r or not os.path.exists(r["path"]):
        abort(404)
    raw = open(r["path"], "rb").read()
    try:
        ch, sr, pcm = protocol.ima_adpcm_to_pcm16(raw)
        wav = protocol.pcm16_to_wav(ch, sr, pcm)
    except Exception:
        wav = raw  # fall back to original
    return Response(wav, mimetype="audio/wav")

@app.route("/api/recordings/<int:rid>/download")
def api_rec_download(rid):
    r = db.get_recording(rid)
    if not r or not os.path.exists(r["path"]):
        abort(404)
    return send_file(r["path"], as_attachment=True, download_name=r["filename"])


# ------------------------------------------------------------------ logs + update
@app.route("/api/logs")
def api_logs():
    return jsonify(_log_lines[-200:])

ZIP_URL = GITHUB_REPO + "/archive/refs/heads/main.zip"

def _update_via_git():
    out = subprocess.check_output(["git", "-C", BASE, "pull", "--ff-only"],
                                  stderr=subprocess.STDOUT, timeout=60).decode("utf-8", "ignore")
    return "git pull:\n" + out

def _https_get(url, dest):
    """Download url to dest, tolerating corporate TLS interception / missing CA bundle."""
    import urllib.request, ssl
    # 1) try with certifi's CA bundle if available
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(url, context=ctx, timeout=60) as r, open(dest, "wb") as f:
            f.write(r.read())
        return
    except Exception:
        pass
    # 2) try the system default
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
            f.write(r.read())
        return
    except Exception as e:
        log("update: TLS verify that bai (%s) - thu che do khong xac thuc" % e)
    # 3) last resort: unverified (internal tool, GitHub source)
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(url, context=ctx, timeout=60) as r, open(dest, "wb") as f:
        f.write(r.read())


def _update_via_zip():
    """Download repo zip from GitHub and overwrite code files (keep data/, recordings/)."""
    import zipfile, tempfile, shutil
    log("update: tai zip tu %s" % ZIP_URL)
    tmp = tempfile.mkdtemp()
    zp = os.path.join(tmp, "repo.zip")
    _https_get(ZIP_URL, zp)
    with zipfile.ZipFile(zp) as z:
        z.extractall(tmp)
    # find extracted root (ArtechCMS-main)
    root = next(os.path.join(tmp, d) for d in os.listdir(tmp)
                if os.path.isdir(os.path.join(tmp, d)) and d.lower().startswith("artechcms"))
    copied = 0
    for dirpath, _, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel.startswith("data") or rel.startswith("recordings") or ".git" in rel:
            continue
        for f in files:
            dst = os.path.join(BASE, rel, f) if rel != "." else os.path.join(BASE, f)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(dirpath, f), dst)
            copied += 1
    shutil.rmtree(tmp, ignore_errors=True)
    return "Da tai ban moi tu GitHub (%d file). Khoi dong lai..." % copied

@app.route("/api/update", methods=["POST"])
def api_update():
    """Update from GitHub: git pull if it's a git clone, else download the zip."""
    is_git = os.path.isdir(os.path.join(BASE, ".git"))
    has_git = False
    try:
        subprocess.check_output(["git", "--version"], stderr=subprocess.STDOUT, timeout=10)
        has_git = True
    except Exception:
        has_git = False
    try:
        if is_git and has_git:
            out = _update_via_git()
        else:
            reason = "khong phai git repo" if not is_git else "khong co git"
            log("update: %s -> dung zip" % reason)
            out = _update_via_zip()
        log("update OK")
        threading.Timer(1.0, lambda: os._exit(3)).start()  # launcher restarts
        return jsonify({"ok": True, "output": out})
    except Exception as e:
        log("update LOI: %s" % e)
        return jsonify({"ok": False, "output": "Loi update: %s" % e}), 500


def main():
    port = int(db.get_setting("port", os.environ.get("ARTECHCMS_PORT", "8787")))
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log("ArtechCMS on http://0.0.0.0:%d" % port)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
