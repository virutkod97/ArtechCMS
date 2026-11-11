"""SQLite storage for ArtechCMS: devices, recordings, settings."""
import sqlite3, os, threading, time
from . import secret

_LOCK = threading.Lock()


def _conn(path):
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


class DB:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.c = _conn(path)
        self._init()

    def _init(self):
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS devices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER DEFAULT 6066,
            user TEXT DEFAULT 'admin',
            password TEXT DEFAULT '',
            version TEXT DEFAULT '150116 1.0.0.5',
            client_ip TEXT DEFAULT '',
            mac TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            interval_min INTEGER DEFAULT 15,
            lookback_days INTEGER DEFAULT 1,
            last_run INTEGER DEFAULT 0,
            last_status TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS recordings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL,
            cdr_index INTEGER,
            filename TEXT NOT NULL,
            path TEXT NOT NULL,
            bytes INTEGER DEFAULT 0,
            duration REAL DEFAULT 0,
            created INTEGER DEFAULT 0,
            UNIQUE(device_id, cdr_index)
        );
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        """)
        self.c.commit()
        self._migrate()

    def _migrate(self):
        cols = {r["name"] for r in self.c.execute("PRAGMA table_info(devices)")}
        add = {
            "method": "TEXT DEFAULT 'ftp'",
            "ftp_port": "INTEGER DEFAULT 21",
            "ftp_user": "TEXT DEFAULT ''",
            "ftp_pass": "TEXT DEFAULT ''",
            "ftp_root": "TEXT DEFAULT '/'",
        }
        for name, decl in add.items():
            if name not in cols:
                self.c.execute("ALTER TABLE devices ADD COLUMN %s %s" % (name, decl))
        rcols = {r["name"] for r in self.c.execute("PRAGMA table_info(recordings)")}
        if "remote_path" not in rcols:
            self.c.execute("ALTER TABLE recordings ADD COLUMN remote_path TEXT DEFAULT ''")
        self.c.commit()

    # ---- settings
    def get_setting(self, k, default=None):
        r = self.c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set_setting(self, k, v):
        with _LOCK:
            self.c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=?",
                           (k, str(v), str(v)))
            self.c.commit()

    # ---- devices
    def list_devices(self):
        out = [dict(r) for r in self.c.execute("SELECT * FROM devices ORDER BY id").fetchall()]
        for d in out:
            d["password"] = secret.decrypt(d.get("password"))
            d["ftp_pass"] = secret.decrypt(d.get("ftp_pass"))
        return out

    def get_device(self, did):
        r = self.c.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
        if not r: return None
        d = dict(r)
        d["password"] = secret.decrypt(d.get("password"))
        d["ftp_pass"] = secret.decrypt(d.get("ftp_pass"))
        return d

    def add_device(self, d):
        with _LOCK:
            cur = self.c.execute("""INSERT INTO devices(name,host,port,user,password,version,
                client_ip,mac,enabled,interval_min,lookback_days,method,ftp_port,ftp_user,ftp_pass,ftp_root)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["name"], d["host"], int(d.get("port", 6066)), d.get("user", "admin"),
                 secret.encrypt(d.get("password", "")), d.get("version", "150116 1.0.0.5"),
                 d.get("client_ip", ""), d.get("mac", ""), int(d.get("enabled", 1)),
                 int(d.get("interval_min", 15)), int(d.get("lookback_days", 1)),
                 d.get("method", "ftp"), int(d.get("ftp_port", 21) or 21),
                 d.get("ftp_user", ""), secret.encrypt(d.get("ftp_pass", "")), d.get("ftp_root", "/")))
            self.c.commit(); return cur.lastrowid

    def update_device(self, did, d):
        fields = ["name","host","port","user","password","version","client_ip","mac",
                  "enabled","interval_min","lookback_days","method","ftp_port",
                  "ftp_user","ftp_pass","ftp_root"]
        d = dict(d)
        if not d.get("password"): d.pop("password", None)      # blank = keep current
        else: d["password"] = secret.encrypt(d["password"])
        if "ftp_pass" in d:
            if not d["ftp_pass"]: d.pop("ftp_pass", None)      # blank = keep current
            else: d["ftp_pass"] = secret.encrypt(d["ftp_pass"])
        sets = ", ".join("%s=?" % f for f in fields if f in d)
        vals = [d[f] for f in fields if f in d]
        if not sets: return
        with _LOCK:
            self.c.execute("UPDATE devices SET %s WHERE id=?" % sets, vals + [did])
            self.c.commit()

    def delete_device(self, did):
        with _LOCK:
            self.c.execute("DELETE FROM devices WHERE id=?", (did,))
            self.c.execute("DELETE FROM recordings WHERE device_id=?", (did,))
            self.c.commit()

    def set_device_status(self, did, status):
        with _LOCK:
            self.c.execute("UPDATE devices SET last_run=?, last_status=? WHERE id=?",
                           (int(time.time()), status, did))
            self.c.commit()

    # ---- recordings
    def has_index(self, did, idx):
        if idx is None: return False
        r = self.c.execute("SELECT 1 FROM recordings WHERE device_id=? AND cdr_index=?",
                           (did, idx)).fetchone()
        return r is not None

    def add_recording(self, did, idx, filename, path, nbytes, duration):
        with _LOCK:
            try:
                self.c.execute("""INSERT INTO recordings(device_id,cdr_index,filename,path,bytes,duration,created)
                    VALUES(?,?,?,?,?,?,?)""",
                    (did, idx, filename, path, nbytes, duration, int(time.time())))
                self.c.commit(); return True
            except sqlite3.IntegrityError:
                return False

    # ---- FTP dedup (by remote path)
    def has_ftp_path(self, did, remote_path):
        r = self.c.execute("SELECT 1 FROM recordings WHERE device_id=? AND remote_path=?",
                           (did, remote_path)).fetchone()
        return r is not None

    def add_recording_ftp(self, did, remote_path, filename, path, nbytes, duration):
        with _LOCK:
            self.c.execute("""INSERT INTO recordings(device_id,cdr_index,remote_path,filename,path,bytes,duration,created)
                VALUES(?,?,?,?,?,?,?,?)""",
                (did, None, remote_path, filename, path, nbytes, duration, int(time.time())))
            self.c.commit(); return True

    def list_recordings(self, did=None, limit=500, offset=0):
        q = "SELECT r.*, d.name AS device_name FROM recordings r JOIN devices d ON d.id=r.device_id"
        args = []
        if did:
            q += " WHERE r.device_id=?"; args.append(did)
        q += " ORDER BY r.created DESC, r.id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [dict(r) for r in self.c.execute(q, args).fetchall()]

    def get_recording(self, rid):
        r = self.c.execute("SELECT * FROM recordings WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def count_recordings(self, did=None):
        if did:
            return self.c.execute("SELECT COUNT(*) FROM recordings WHERE device_id=?", (did,)).fetchone()[0]
        return self.c.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
