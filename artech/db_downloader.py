"""
DB-driven 6066 downloader (the reliable path).

The Artech PC software keeps a per-device folder of daily SQLite CDRs
(CDR<YYYYMMDD>.db, table CdrTable with RecFilePath + metadata). ArtechCMS reads
those, then downloads each recording from the device over TCP 6066 using the
exact 0c/06 request built from the CDR row (verified byte-exact vs captures).

Set the device's `db_folder` to that folder (named by SystemID, e.g. 55260439).
Only completed recordings are fetched (.wav with FileSize>0; .tmp skipped).
Dedup is by RecFilePath.
"""
import os, glob, sqlite3, time, tempfile, datetime
from . import protocol

MAX_FAILS_IN_ROW = 5


def _rows_from_folder(db_folder, log):
    """Yield CdrTable rows from every CDR*.db in a local folder (needs the vendor software)."""
    files = sorted(glob.glob(os.path.join(db_folder, "CDR*.db")), reverse=True)
    if not files:
        log("khong thay file CDR*.db trong %s" % db_folder)
    for f in files:
        yield from _rows_from_file(f, log)


def _rows_from_file(path, log):
    try:
        c = sqlite3.connect(path); c.row_factory = sqlite3.Row
        for r in c.execute("SELECT * FROM CdrTable ORDER BY AutoID"):
            yield dict(r)
        c.close()
    except Exception as e:
        log("doc %s loi: %s" % (os.path.basename(path), e))


class _Session:
    """One device connection that can be thrown away and reopened after an error."""
    def __init__(self, device, log):
        self.device = device; self.log = log; self.conn = None

    def get(self):
        if self.conn is None:
            self.conn = protocol.login_and_handshake(self.device, log=self.log)
        return self.conn

    def reset(self):
        if self.conn is not None:
            try: self.conn.s.close()
            except Exception: pass
        self.conn = None

    close = reset


def _rows_from_device_dates(sess, dates, log, should_stop=None):
    """Pull the device's CDR DBs via 6066 for an explicit list of dates.
    A failed day is retried once on a fresh connection, then skipped."""
    tmp = tempfile.mkdtemp(prefix="artechdb_")
    for d in dates:
        if should_stop and should_stop():
            raise protocol.Cancelled()
        # device path: \Hard Disk\Sqlite\<YYYY>\<YYYYMM>\CDR<YYYYMMDD>.db
        rp = "\\Hard Disk\\Sqlite\\%04d\\%04d%02d\\CDR%04d%02d%02d.db" % (
            d.year, d.year, d.month, d.year, d.month, d.day)
        raw = None
        for attempt in (1, 2):
            try:
                raw = protocol.download_path(sess.get(), rp, when=datetime.datetime(d.year, d.month, d.day),
                                             should_stop=should_stop)
                break
            except protocol.Cancelled:
                raise
            except Exception as e:
                log("ngay %s: keo DB loi (lan %d): %s" % (d, attempt, e))
                sess.reset()
        if raw is None:
            continue
        if not raw or raw[:16].find(b"SQLite format 3") != 0:
            log("ngay %s: khong co DB hoac khong phai sqlite (%d bytes)" % (d, len(raw)))
            continue
        local = os.path.join(tmp, "CDR%04d%02d%02d.db" % (d.year, d.month, d.day))
        open(local, "wb").write(raw)
        log("ngay %s: keo DB %d bytes" % (d, len(raw)))
        yield from _rows_from_file(local, log)


def _duration(wav):
    return protocol.wav_duration(wav)


def fetch_calllog(device, date_from, date_to, log=lambda *a: None):
    """
    Live call log: pull the device's CDR DBs for [date_from, date_to] via 6066 and
    return the raw CDR rows (NO audio download). date_* are datetime.date.
    Returns (rows, error_or_None).
    """
    tag = "[calllog %s]" % device["host"]
    days = (date_to - date_from).days
    if days < 0:
        return [], "khoang ngay khong hop le"
    dates = [date_to - datetime.timedelta(days=i) for i in range(days + 1)]
    sess = _Session(device, lambda *a: log(tag, *a))
    try:
        sess.get()
    except Exception as e:
        return [], "Không đăng nhập được: %s" % e
    try:
        rows = list(_rows_from_device_dates(sess, dates, lambda *a: log(tag, *a)))
    except Exception as e:
        rows = []; log("%s keo DB loi: %s" % (tag, e))
    finally:
        sess.close()
    log("%s lay %d ban ghi (%s..%s)" % (tag, len(rows), date_from, date_to))
    return rows, None


def fetch_one(db, device, cdr_row, rec_root, log=lambda *a: None):
    """
    Download ONE recording (given its CDR row dict) via 6066 and store it.
    Returns (rec_id, error_or_None). Uses its own short-lived connection.
    """
    did = device["id"]
    tag = "[fetch %s]" % device["host"]
    path = cdr_row.get("RecFilePath") or ""
    if not path.lower().endswith(".wav"):
        return None, "khong phai file wav"
    # already downloaded?
    existing = db.c.execute("SELECT id FROM recordings WHERE device_id=? AND remote_path=?",
                            (did, path)).fetchone()
    if existing:
        return existing[0], None
    try:
        conn = protocol.login_and_handshake(device, log=lambda *a: log(tag, *a))
    except Exception as e:
        return None, "login loi: %s" % e
    try:
        wav = protocol.download_file(conn, cdr_row, log=lambda *a: log(tag, *a))
    except Exception as e:
        return None, "tai loi: %s" % e
    finally:
        try: conn.s.close()
        except Exception: pass
    if not wav:
        return None, "khong tai duoc file (0 bytes)"
    dev_dir = os.path.join(rec_root, "dev%d" % did)
    os.makedirs(dev_dir, exist_ok=True)
    name = path.replace("/", "\\").rsplit("\\", 1)[-1]
    local = os.path.join(dev_dir, name)
    if os.path.exists(local):
        local = os.path.join(dev_dir, "%s_%s" % (cdr_row.get("AutoID", 0), name))
    with open(local, "wb") as f:
        f.write(wav)
    db.add_recording_ftp(did, path, name, local, len(wav), _duration(wav))
    rid = db.c.execute("SELECT id FROM recordings WHERE device_id=? AND remote_path=?",
                       (did, path)).fetchone()[0]
    log("%s tai %s (%d bytes) -> rec#%d" % (tag, name, len(wav), rid))
    return rid, None


def run_device_db(db, device, rec_root, log=lambda *a: None, dates=None, should_stop=None):
    """Pull the device's CDR DBs and download new recordings over 6066.
    `dates` (list of datetime.date) overrides the default lookback window.

    Never blocks forever: each transfer is abandoned after protocol.STALL seconds
    without progress, a failed file is retried once on a fresh connection and then
    skipped, and the job stops after MAX_FAILS_IN_ROW consecutive failures or when
    should_stop() turns true."""
    did = device["id"]
    folder = device.get("db_folder") or ""
    lookback = int(device.get("lookback_days", 1) or 1)
    tag = "[db6066 %s]" % device["host"]
    tlog = lambda *a: log(tag, *a)
    use_local = bool(folder and os.path.isdir(folder))
    stop = should_stop or (lambda: False)
    sess = _Session(device, tlog)

    try:
        sess.get()
    except Exception as e:
        msg = "Không đăng nhập được: %s" % e
        db.set_device_status(did, msg); tlog(msg); return 0, msg

    new = failed = seen = skipped = 0
    stopped = False
    try:
        # 1) collect CDR rows first
        db.set_device_status(did, "Đang lấy danh sách cuộc gọi")
        if use_local:
            tlog("nguon CDR: thu muc local %s" % folder)
            rows = list(_rows_from_folder(folder, tlog))
        else:
            if dates is None:
                today = datetime.date.today()
                dates = [today - datetime.timedelta(days=i) for i in range(lookback)]
            tlog("nguon CDR: keo tu thiet bi (%s..%s)" % (min(dates), max(dates)))
            rows = list(_rows_from_device_dates(sess, dates, tlog, should_stop=stop))

        # 2) work out what still needs downloading
        todo = []
        for row in rows:
            path = row.get("RecFilePath") or ""
            if not path.lower().endswith(".wav") or int(row.get("FileSize", 0) or 0) <= 0:
                skipped += 1; continue           # incomplete (.tmp) or empty
            seen += 1
            if not db.has_ftp_path(did, path):
                todo.append(row)
        tlog("can tai %d file moi (%d file tren thiet bi, %d da co)" % (len(todo), seen, seen - len(todo)))

        # 3) download
        dev_dir = os.path.join(rec_root, "dev%d" % did)
        os.makedirs(dev_dir, exist_ok=True)
        fails_in_row = 0
        for n, row in enumerate(todo, 1):
            if stop():
                raise protocol.Cancelled()
            path = row["RecFilePath"]
            name = path.replace("/", "\\").rsplit("\\", 1)[-1]   # Windows path, split cross-platform
            db.set_device_status(did, "Đang tải %d/%d" % (n, len(todo)))
            mark = [0]
            def progress(got, size, _name=name, _mark=mark):
                if got - _mark[0] >= 2 * 1024 * 1024:           # log every 2 MB on big files
                    _mark[0] = got
                    tlog("  %s: %.1f/%.1f MB" % (_name, got / 1048576.0, (size or 0) / 1048576.0))
            wav = None
            for attempt in (1, 2):
                try:
                    wav = protocol.download_file(sess.get(), row, log=tlog, should_stop=stop, progress=progress)
                    if wav:
                        break
                    sess.reset()                                 # unexpected content: start clean
                except protocol.Cancelled:
                    raise
                except Exception as e:
                    tlog("loi tai %s (lan %d): %s" % (name, attempt, e))
                    sess.reset()
            if not wav:
                failed += 1; fails_in_row += 1
                tlog("bo qua %s (%d/%d)" % (name, n, len(todo)))
                if fails_in_row >= MAX_FAILS_IN_ROW:
                    tlog("dung: %d file loi lien tiep, thiet bi co the dang gap su co" % fails_in_row)
                    break
                continue
            fails_in_row = 0
            local = os.path.join(dev_dir, name)
            if os.path.exists(local):
                local = os.path.join(dev_dir, "%s_%s" % (row.get("AutoID", 0), name))
            with open(local, "wb") as f:
                f.write(wav)
            db.add_recording_ftp(did, path, name, local, len(wav), _duration(wav))
            new += 1
            tlog("tai %s (%d bytes) [%d/%d]" % (name, len(wav), n, len(todo)))
    except protocol.Cancelled:
        stopped = True
        tlog("da dung theo yeu cau")
    except Exception as e:
        tlog("loi: %s" % e)
        failed += 1
    finally:
        sess.close()
    msg = "Tải %d file mới" % new
    if failed:
        msg += ", %d file lỗi" % failed
    msg += ", %d file trên thiết bị" % seen
    if stopped:
        msg += " (đã dừng)"
    db.set_device_status(did, msg); tlog(msg)
    return new, msg
