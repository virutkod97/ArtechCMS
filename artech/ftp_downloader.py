"""
FTP-based downloader for Artech voice loggers.

Many Artech loggers expose an FTP server for SD-card access (the product line
is advertised as "FTP ... SD card recorder"). This is far more reliable than
the proprietary 6066 download, so ArtechCMS prefers it when available.

Walks the recording tree, downloads new .wav files, dedups by remote path.
"""
import os, ftplib, time, posixpath


def _walk(ftp, root, log, depth=0, max_depth=8):
    """Yield full remote paths of .wav files under root."""
    if depth > max_depth:
        return
    try:
        entries = []
        ftp.retrlines("LIST " + root, entries.append)
    except Exception as e:
        log("FTP LIST loi %s: %s" % (root, e))
        return
    for line in entries:
        parts = line.split(maxsplit=8)
        if len(parts) < 9:
            continue
        name = parts[8]
        if name in (".", ".."):
            continue
        is_dir = line[0] == "d"
        full = posixpath.join(root, name)
        if is_dir:
            yield from _walk(ftp, full, log, depth + 1, max_depth)
        elif name.lower().endswith(".wav"):
            yield full


def probe(host, port=21, user="admin", password="1111", timeout=8):
    """Return (ok, msg) for an FTP login test."""
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, int(port), timeout=timeout)
        ftp.login(user, password)
        pwd = ftp.pwd()
        root = []
        try:
            ftp.retrlines("LIST", root.append)
        except Exception:
            pass
        ftp.quit()
        return True, "FTP OK (pwd=%s, %d muc goc)" % (pwd, len(root))
    except Exception as e:
        return False, "FTP loi: %s" % e


def run_device_ftp(db, device, rec_root, log=lambda *a: None):
    """Download new .wav via FTP. device needs: host, ftp_port, ftp_user, ftp_pass, ftp_root."""
    did = device["id"]
    host = device["host"]
    port = int(device.get("ftp_port", 21) or 21)
    user = device.get("ftp_user") or device.get("user", "admin")
    pw = device.get("ftp_pass") or device.get("password", "1111")
    root = device.get("ftp_root") or "/"
    tag = "[ftp %s]" % host
    log("%s connect port=%d user=%s root=%s" % (tag, port, user, root))
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=15)
        ftp.login(user, pw)
    except Exception as e:
        msg = "FTP connect loi: %s" % e
        db.set_device_status(did, msg); log("%s %s" % (tag, msg))
        return 0, msg

    dev_dir = os.path.join(rec_root, "dev%d" % did)
    os.makedirs(dev_dir, exist_ok=True)
    new = 0; seen = 0
    try:
        for remote in _walk(ftp, root, lambda *a: log(tag, *a)):
            seen += 1
            key = remote.replace("/", "_").replace("\\", "_").lstrip("_")
            if db.has_ftp_path(did, remote):
                continue
            local = os.path.join(dev_dir, os.path.basename(remote))
            # avoid name clash across folders
            if os.path.exists(local):
                local = os.path.join(dev_dir, key)
            try:
                with open(local, "wb") as f:
                    ftp.retrbinary("RETR " + remote, f.write)
                nbytes = os.path.getsize(local)
                dur = _duration(local)
                db.add_recording_ftp(did, remote, os.path.basename(local), local, nbytes, dur)
                new += 1
                log("%s tai %s (%d bytes)" % (tag, os.path.basename(remote), nbytes))
            except Exception as e:
                log("%s RETR loi %s: %s" % (tag, remote, e))
    finally:
        try: ftp.quit()
        except Exception: pass
    msg = "ftp ok: %d moi / %d tren server @ %s" % (new, seen, time.strftime("%H:%M:%S"))
    db.set_device_status(did, msg); log("%s %s" % (tag, msg))
    return new, msg


def _duration(path):
    from . import protocol
    try:
        raw = open(path, "rb").read()
        ch, sr, pcm = protocol.ima_adpcm_to_pcm16(raw)
        return round(len(pcm) / 2 / ch / sr, 2)
    except Exception:
        return 0.0
