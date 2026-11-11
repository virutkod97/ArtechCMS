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
    d = db.get_device(did)
    if not d:
        return jsonify({"error": "not found"}), 404
    def job():
        downloader.run_device(db, d, REC_ROOT, log=log)
    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True, "msg": "download started"})

@app.route("/api/devices/<int:did>/test", methods=["POST"])
def api_test_device(did):
    d = db.get_device(did)
    if not d:
        return jsonify({"error": "not found"}), 404
    method = (d.get("method") or "ftp").lower()
    if method == "ftp":
        from artech import ftp_downloader
        user = d.get("ftp_user") or d.get("user", "admin")
        pw = d.get("ftp_pass") or d.get("password", "1111")
        log("[test ftp %s:%s] user=%s" % (d["host"], d.get("ftp_port", 21), user))
        ok, msg = ftp_downloader.probe(d["host"], d.get("ftp_port", 21), user, pw)
        log("[test ftp %s] %s" % (d["host"], msg))
        return jsonify({"ok": ok, "msg": msg})
    res = protocol.login_probe(d, log=log)
    return jsonify(res)


# ------------------------------------------------------------------ recordings API
@app.route("/api/recordings")
def api_recordings():
    did = request.args.get("device", type=int)
    limit = request.args.get("limit", 500, type=int)
    offset = request.args.get("offset", 0, type=int)
    recs = db.list_recordings(did, limit, offset)
    return jsonify({"total": db.count_recordings(did), "items": recs})

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

def _update_via_zip():
    """Download repo zip from GitHub and overwrite code files (keep data/, recordings/)."""
    import urllib.request, zipfile, tempfile, shutil
    log("update: tai zip tu %s" % ZIP_URL)
    tmp = tempfile.mkdtemp()
    zp = os.path.join(tmp, "repo.zip")
    urllib.request.urlretrieve(ZIP_URL, zp)
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
