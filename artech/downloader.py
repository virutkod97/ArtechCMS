"""Per-device download job: pull stream, extract WAVs, dedup by CDR index, store."""
import os, time, datetime, struct, traceback
from . import protocol


def _duration(wav):
    try:
        ch, sr, pcm = protocol.ima_adpcm_to_pcm16(wav)
        return round(len(pcm) / 2 / ch / sr, 2)
    except Exception:
        return 0.0


def run_device(db, device, rec_root, log=lambda *a: None, dates=None, should_stop=None):
    """Dispatch by method. `dates` (list of datetime.date) overrides the lookback window."""
    method = (device.get("method") or "db6066").lower()
    if method == "ftp":
        from . import ftp_downloader
        return ftp_downloader.run_device_ftp(db, device, rec_root, log=log)
    if method == "db6066":
        from . import db_downloader
        return db_downloader.run_device_db(db, device, rec_root, log=log, dates=dates, should_stop=should_stop)
    return run_device_net(db, device, rec_root, log=log)


def run_device_net(db, device, rec_root, log=lambda *a: None):
    """Download via the proprietary 6066 protocol (best-effort). Returns (new, msg)."""
    did = device["id"]
    lookback = int(device.get("lookback_days", 1) or 1)
    today = datetime.date.today()
    dates = [today - datetime.timedelta(days=i) for i in range(lookback)]
    try:
        stream = protocol.download_stream(device, dates, log=log)
    except Exception as e:
        msg = "connect/download error: %s" % e
        db.set_device_status(did, msg); log(msg)
        return 0, msg

    dev_dir = os.path.join(rec_root, "dev%d" % did)
    os.makedirs(dev_dir, exist_ok=True)
    new = 0; total = 0
    for rec in protocol.extract_recordings(stream):
        total += 1
        idx = rec["index"]
        if idx is not None and db.has_index(did, idx):
            continue
        name = ("%d.wav" % idx) if idx is not None else ("rec_%d.wav" % int(time.time()*1000))
        path = os.path.join(dev_dir, name)
        try:
            with open(path, "wb") as f:
                f.write(rec["wav"])
            dur = _duration(rec["wav"])
            if db.add_recording(did, idx, name, path, len(rec["wav"]), dur):
                new += 1
        except Exception as e:
            log("save error %s: %s" % (name, e))
    msg = "ok: %d new / %d seen @ %s" % (new, total, time.strftime("%H:%M:%S"))
    db.set_device_status(did, msg); log(msg)
    return new, msg
