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
import os, glob, sqlite3, time
from . import protocol


def _rows(db_folder, log):
    """Yield CdrTable rows (dicts) from every CDR*.db in the folder, newest day first."""
    files = sorted(glob.glob(os.path.join(db_folder, "CDR*.db")), reverse=True)
    if not files:
        log("khong thay file CDR*.db trong %s" % db_folder)
    for f in files:
        try:
            c = sqlite3.connect(f); c.row_factory = sqlite3.Row
            for r in c.execute("SELECT * FROM CdrTable ORDER BY AutoID"):
                yield dict(r)
            c.close()
        except Exception as e:
            log("doc %s loi: %s" % (os.path.basename(f), e))


def _duration(wav):
    try:
        ch, sr, pcm = protocol.ima_adpcm_to_pcm16(wav)
        return round(len(pcm) / 2 / ch / sr, 2)
    except Exception:
        return 0.0


def run_device_db(db, device, rec_root, log=lambda *a: None):
    """Read the device's CDR DBs and download new recordings over 6066."""
    did = device["id"]
    folder = device.get("db_folder") or ""
    tag = "[db6066 %s]" % device["host"]
    if not folder or not os.path.isdir(folder):
        msg = "db_folder khong hop le: %r" % folder
        db.set_device_status(did, msg); log("%s %s" % (tag, msg)); return 0, msg

    try:
        conn = protocol.login_and_handshake(device, log=lambda *a: log(tag, *a))
    except Exception as e:
        msg = "login loi: %s" % e
        db.set_device_status(did, msg); log("%s %s" % (tag, msg)); return 0, msg

    dev_dir = os.path.join(rec_root, "dev%d" % did)
    os.makedirs(dev_dir, exist_ok=True)
    new = 0; seen = 0; skipped = 0
    try:
        for row in _rows(folder, lambda *a: log(tag, *a)):
            path = row.get("RecFilePath") or ""
            if not path.lower().endswith(".wav") or int(row.get("FileSize", 0) or 0) <= 0:
                skipped += 1; continue           # incomplete (.tmp) or empty
            seen += 1
            if db.has_ftp_path(did, path):
                continue
            try:
                wav = protocol.download_file(conn, row, log=lambda *a: log(tag, *a))
            except Exception as e:
                log("%s tai loi %s: %s" % (tag, path, e))
                # a broken connection: reconnect once
                try:
                    conn = protocol.login_and_handshake(device, log=lambda *a: log(tag, *a))
                    wav = protocol.download_file(conn, row)
                except Exception as e2:
                    log("%s tai lai that bai: %s" % (tag, e2)); break
            if not wav:
                continue
            name = os.path.basename(path)
            local = os.path.join(dev_dir, name)
            if os.path.exists(local):
                local = os.path.join(dev_dir, "%d_%s" % (row.get("AutoID", 0), name))
            with open(local, "wb") as f:
                f.write(wav)
            db.add_recording_ftp(did, path, name, local, len(wav), _duration(wav))
            new += 1
            log("%s tai %s (%d bytes)" % (tag, name, len(wav)))
    finally:
        try: conn.s.close()
        except Exception: pass
    msg = "db6066 ok: %d moi / %d wav / %d bo qua @ %s" % (new, seen, skipped, time.strftime("%H:%M:%S"))
    db.set_device_status(did, msg); log("%s %s" % (tag, msg))
    return new, msg
