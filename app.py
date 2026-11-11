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
    import socket
    try:
        s = socket.create_connection((d["host"], int(d["port"])), timeout=5)
        s.sendall(protocol.build_login(d["user"], d["password"], d["version"],
                                       d.get("client_ip", ""), d.get("mac", ""), d["host"]))
        reply = protocol._recv_idle(s, idle=1.0, hard=5.0); s.close()
        ok = reply[:4] == bytes.fromhex("62000102")
        return jsonify({"ok": ok, "bytes": len(reply),
                        "msg": "login reply OK" if ok else "connected, unexpected reply"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


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

@app.route("/api/update", methods=["POST"])
def api_update():
    """git pull from GitHub, then exit so the launcher restarts."""
    try:
        out = subprocess.check_output(["git", "-C", BASE, "pull", "--ff-only"],
                                      stderr=subprocess.STDOUT, timeout=60).decode("utf-8", "ignore")
        log("update: %s" % out.strip().replace("\n", " | "))
        threading.Timer(1.0, lambda: os._exit(3)).start()  # launcher restarts (see start script)
        return jsonify({"ok": True, "output": out})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "output": e.output.decode("utf-8", "ignore")}), 500
    except Exception as e:
        return jsonify({"ok": False, "output": str(e)}), 500


def main():
    port = int(db.get_setting("port", os.environ.get("ARTECHCMS_PORT", "8787")))
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log("ArtechCMS on http://0.0.0.0:%d" % port)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
