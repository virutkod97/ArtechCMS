#!/usr/bin/env python3
"""
ArtechCMS - web management for Artech voice loggers.
Flask app: multi-device management, scheduled auto-download, browser playback.
"""
import os, io, json, time, threading, subprocess, sys
from flask import (Flask, request, jsonify, send_file, Response,
                   render_template, abort, session, redirect, url_for)
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
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

# ------------------------------------------------------------------ auth / users
def _session_secret():
    sk = db.get_setting("session_secret")
    if not sk:
        sk = os.urandom(24).hex(); db.set_setting("session_secret", sk)
    return sk

app.secret_key = _session_secret()
app.config.update(PERMANENT_SESSION_LIFETIME=7*24*3600)

if db.count_users() == 0:                       # first run: default admin
    db.create_user("admin", generate_password_hash("admin"), full_name="",
                   is_admin=1, can_manage=1, all_devices=1)

def current_user():
    uid = session.get("uid")
    return db.get_user(uid) if uid else None

OPEN_PATHS = ("/login", "/logout", "/static/", "/favicon.ico")

@app.before_request
def _guard():
    path = request.path
    if any(path == p or path.startswith(p) for p in OPEN_PATHS):
        return
    if not current_user():
        if path.startswith("/api/"):
            return jsonify({"error": "unauthorized", "login": True}), 401
        return redirect(url_for("login", next=path))

def login_required(fn):
    @wraps(fn)
    def w(*a, **k):
        if not current_user():
            return jsonify({"error": "unauthorized"}), 401
        return fn(*a, **k)
    return w

def admin_required(fn):
    @wraps(fn)
    def w(*a, **k):
        u = current_user()
        if not u or not u.get("is_admin"):
            return jsonify({"error": "Chỉ quản trị viên được phép."}), 403
        return fn(*a, **k)
    return w

def require_device(did):
    """abort 403 if the current user can't access this device."""
    if not db.user_can_access(current_user(), did):
        abort(403, "Không có quyền với thiết bị này.")

def require_manage():
    u = current_user()
    if not (u and (u.get("is_admin") or u.get("can_manage"))):
        abort(403, "Không có quyền thêm/sửa/xóa thiết bị.")

@app.errorhandler(403)
@app.errorhandler(404)
def _api_err(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": getattr(e, "description", str(e))}), e.code
    return e


_log_lines = []
def log(*a):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), " ".join(str(x) for x in a))
    _log_lines.append(line)
    del _log_lines[:-400]
    print(line, flush=True)


# ------------------------------------------------------------------ per-device busy lock
# The device allows only a few concurrent 6066 sessions, so only ONE operation
# (download / call-log / fetch / test) may talk to a given device at a time.
_dev_locks = {}
_dev_locks_guard = threading.Lock()

def _dev_lock(did):
    with _dev_locks_guard:
        lk = _dev_locks.get(did)
        if lk is None:
            lk = _dev_locks[did] = threading.Lock()
        return lk

def device_busy(did):
    return _dev_lock(did).locked()

# stop requests for running download jobs (checked between/during transfers)
_stop_flags = {}

def _stop_flag(did):
    with _dev_locks_guard:
        ev = _stop_flags.get(did)
        if ev is None:
            ev = _stop_flags[did] = threading.Event()
        return ev


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
                if now - int(d["last_run"] or 0) < interval:
                    continue
                lk = _dev_lock(d["id"])
                if not lk.acquire(blocking=False):
                    log("bo qua chu ky #%d %s: thiet bi dang ban" % (d["id"], d["name"]))
                    db.set_device_status(d["id"], "Bỏ qua chu kỳ: thiết bị đang bận")  # bump last_run so it retries next interval
                    continue
                try:
                    log("auto-run device #%d %s" % (d["id"], d["name"]))
                    ev = _stop_flag(d["id"]); ev.clear()
                    downloader.run_device(db, d, REC_ROOT, log=log, should_stop=ev.is_set)
                finally:
                    lk.release()
        except Exception as e:
            log("scheduler error: %s" % e)
        _sched_stop.wait(20)


# ------------------------------------------------------------------ pages
@app.route("/")
def index():
    return render_template("index.html", repo=GITHUB_REPO)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = (request.form.get("username") or "").strip()
        pw = request.form.get("password") or ""
        u = db.get_user_by_name(name)
        if u and check_password_hash(u["pw_hash"], pw):
            session.permanent = True; session["uid"] = u["id"]
            nxt = request.args.get("next") or "/"
            if not nxt.startswith("/"): nxt = "/"
            return redirect(nxt)
        return render_template("login.html", error="Sai tài khoản hoặc mật khẩu."), 401
    if current_user():
        return redirect("/")
    return render_template("login.html", error="")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/api/me")
def api_me():
    u = current_user()
    if not u:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"id": u["id"], "username": u["username"], "full_name": u.get("full_name", ""),
                    "is_admin": bool(u["is_admin"]), "can_manage": bool(u["can_manage"]),
                    "all_devices": bool(u["all_devices"])})

@app.route("/api/me/password", methods=["POST"])
def api_change_password():
    u = current_user()
    b = request.get_json(force=True)
    if not check_password_hash(u["pw_hash"], b.get("old") or ""):
        return jsonify({"error": "Mật khẩu hiện tại không đúng."}), 400
    if len(b.get("new") or "") < 4:
        return jsonify({"error": "Mật khẩu mới tối thiểu 4 ký tự."}), 400
    db.update_user(u["id"], {"pw_hash": generate_password_hash(b["new"])})
    return jsonify({"ok": True})

# ---- user management (admin only)
@app.route("/api/users", methods=["GET"])
@admin_required
def api_users():
    return jsonify(db.list_users())

@app.route("/api/users", methods=["POST"])
@admin_required
def api_add_user():
    b = request.get_json(force=True)
    name = (b.get("username") or "").strip()
    if not name or not (b.get("password") or ""):
        return jsonify({"error": "Thiếu tên đăng nhập hoặc mật khẩu."}), 400
    if db.get_user_by_name(name):
        return jsonify({"error": "Tên đăng nhập đã tồn tại."}), 400
    uid = db.create_user(name, generate_password_hash(b["password"]), full_name=b.get("full_name", ""),
                         is_admin=int(bool(b.get("is_admin"))), can_manage=int(bool(b.get("can_manage"))),
                         all_devices=int(bool(b.get("all_devices"))), device_ids=b.get("device_ids") or [])
    return jsonify({"id": uid})

@app.route("/api/users/<int:uid>", methods=["PUT"])
@admin_required
def api_update_user(uid):
    b = request.get_json(force=True)
    target = db.get_user(uid)
    if not target:
        return jsonify({"error": "not found"}), 404
    fields = {}
    for k in ("full_name", "is_admin", "can_manage", "all_devices"):
        if k in b: fields[k] = int(bool(b[k])) if k != "full_name" else b[k]
    if b.get("username"):
        other = db.get_user_by_name(b["username"].strip())
        if other and other["id"] != uid:
            return jsonify({"error": "Tên đăng nhập đã tồn tại."}), 400
        fields["username"] = b["username"].strip()
    if b.get("password"):
        fields["pw_hash"] = generate_password_hash(b["password"])
    if "device_ids" in b:
        fields["device_ids"] = b["device_ids"] or []
    # never let the last admin lose admin
    if target["is_admin"] and fields.get("is_admin") == 0:
        admins = [x for x in db.list_users() if x["is_admin"]]
        if len(admins) <= 1:
            return jsonify({"error": "Phải còn ít nhất một quản trị viên."}), 400
    db.update_user(uid, fields)
    return jsonify({"ok": True})

@app.route("/api/users/<int:uid>", methods=["DELETE"])
@admin_required
def api_delete_user(uid):
    u = current_user()
    if uid == u["id"]:
        return jsonify({"error": "Không thể tự xóa tài khoản đang đăng nhập."}), 400
    target = db.get_user(uid)
    if target and target["is_admin"]:
        admins = [x for x in db.list_users() if x["is_admin"]]
        if len(admins) <= 1:
            return jsonify({"error": "Phải còn ít nhất một quản trị viên."}), 400
    db.delete_user(uid)
    return jsonify({"ok": True})


# ------------------------------------------------------------------ device API
@app.route("/api/devices", methods=["GET"])
def api_devices():
    u = current_user()
    devs = db.list_devices()
    out = []
    for d in devs:
        if not db.user_can_access(u, d["id"]):
            continue
        d.pop("password", None)
        d.pop("ftp_pass", None)
        d["recordings"] = db.count_recordings(d["id"])
        d["busy"] = device_busy(d["id"])
        out.append(d)
    return jsonify(out)

@app.route("/api/devices", methods=["POST"])
def api_add_device():
    require_manage()
    d = request.get_json(force=True)
    if not d.get("name") or not d.get("host"):
        return jsonify({"error": "name and host required"}), 400
    did = db.add_device(d)
    u = current_user()
    if u and not u.get("is_admin") and not u.get("all_devices"):
        db.set_user_devices(u["id"], db.user_device_ids(u["id"]) + [did])   # creator keeps access
    return jsonify({"id": did})

@app.route("/api/devices/<int:did>", methods=["PUT"])
def api_update_device(did):
    require_manage(); require_device(did)
    db.update_device(did, request.get_json(force=True))
    return jsonify({"ok": True})

@app.route("/api/devices/<int:did>", methods=["DELETE"])
def api_delete_device(did):
    require_manage(); require_device(did)
    db.delete_device(did)
    return jsonify({"ok": True})

@app.route("/api/devices/<int:did>/download", methods=["POST"])
def api_download_now(did):
    import datetime
    require_device(did)
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
    lk = _dev_lock(did)
    if not lk.acquire(blocking=False):
        return jsonify({"ok": False, "busy": True,
                        "msg": "Thiết bị đang bận (có lệnh khác đang chạy). Thử lại sau."}), 409
    ev = _stop_flag(did); ev.clear()
    def job():
        try:
            downloader.run_device(db, d, REC_ROOT, log=log, dates=dates, should_stop=ev.is_set)
        except Exception as e:
            log("loi job tai #%d: %s" % (did, e))
        finally:
            lk.release()
    threading.Thread(target=job, daemon=True).start()
    rng = ("%s đến %s" % (dates[0].strftime("%d/%m/%Y"), dates[-1].strftime("%d/%m/%Y"))) if dates else ("mặc định (%d ngày)" % int(d.get("lookback_days", 1) or 1))
    return jsonify({"ok": True, "msg": "Đã bắt đầu tải: %s" % rng})

@app.route("/api/devices/<int:did>/stop", methods=["POST"])
def api_stop(did):
    """Ask the running download job of this device to stop."""
    require_device(did)
    if not device_busy(did):
        return jsonify({"ok": False, "msg": "Thiết bị không có lệnh nào đang chạy."})
    _stop_flag(did).set()
    log("yeu cau dung lenh dang chay cua thiet bi #%d" % did)
    return jsonify({"ok": True, "msg": "Đã gửi yêu cầu dừng. Lệnh sẽ dừng trong vài giây."})

@app.route("/api/devices/<int:did>/test", methods=["POST"])
def api_test_device(did):
    require_device(did)
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
    lk = _dev_lock(did)
    if not lk.acquire(blocking=False):
        return jsonify({"ok": False, "busy": True, "msg": "Thiết bị đang bận (có lệnh khác đang chạy). Thử lại sau."}), 409
    try:
        res = protocol.login_probe(d, log=log)
    finally:
        lk.release()
    return jsonify(res)


# ------------------------------------------------------------------ recordings API
CDR_FIELDS = ["AutoID", "RecFilePath", "RecType", "RecChannel", "RingTime",
              "RecTime", "TalkTime", "FileSize", "PhoneNumber", "SystemID", "SystemName"]

@app.route("/api/devices/<int:did>/calllog", methods=["POST"])
def api_calllog(did):
    """Pull the device's call log (CDR) for a date range, live from the device."""
    import datetime
    require_device(did)
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
    lk = _dev_lock(did)
    if not lk.acquire(blocking=False):
        return jsonify({"error": "Thiết bị đang bận (có lệnh khác đang chạy). Thử lại sau.", "busy": True}), 409
    try:
        rows, err = db_downloader.fetch_calllog(d, date_from, date_to, log=log)
    finally:
        lk.release()
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
    require_device(did)
    from artech import db_downloader
    d = db.get_device(did)
    if not d:
        return jsonify({"error": "not found"}), 404
    cdr = request.get_json(force=True).get("cdr") or {}
    if not cdr.get("RecFilePath"):
        return jsonify({"error": "thieu RecFilePath"}), 400
    lk = _dev_lock(did)
    if not lk.acquire(blocking=False):
        return jsonify({"ok": False, "busy": True, "error": "Thiết bị đang bận. Thử lại sau."}), 409
    try:
        rid, err = db_downloader.fetch_one(db, d, cdr, REC_ROOT, log=log)
    finally:
        lk.release()
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
    u = current_user()
    allowed = None
    if not (u.get("is_admin") or u.get("all_devices")):
        allowed = db.user_device_ids(u["id"])
    if did is not None:
        require_device(did)
    total = db.count_recordings_filtered(did, ts_from, ts_to, allowed)
    recs = db.list_recordings(did, page_size, (page - 1) * page_size, ts_from, ts_to, allowed)
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
    require_device(r["device_id"])
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
    require_device(r["device_id"])
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
@admin_required
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
