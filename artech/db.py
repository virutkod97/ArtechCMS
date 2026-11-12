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
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL,
            full_name TEXT DEFAULT '',
            is_admin INTEGER DEFAULT 0,
            can_manage INTEGER DEFAULT 0,
            all_devices INTEGER DEFAULT 0,
            created INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_devices(
            user_id INTEGER NOT NULL,
            device_id INTEGER NOT NULL,
            UNIQUE(user_id, device_id)
        );
        """)
        self.c.commit()
        self._migrate()

    def _migrate(self):
        cols = {r["name"] for r in self.c.execute("PRAGMA table_info(devices)")}
        add = {
            "method": "TEXT DEFAULT 'db6066'",
            "ftp_port": "INTEGER DEFAULT 21",
            "ftp_user": "TEXT DEFAULT ''",
            "ftp_pass": "TEXT DEFAULT ''",
            "ftp_root": "TEXT DEFAULT '/'",
            "db_folder": "TEXT DEFAULT ''",
        }
        for name, decl in add.items():
            if name not in cols:
                self.c.execute("ALTER TABLE devices ADD COLUMN %s %s" % (name, decl))
        rcols = {r["name"] for r in self.c.execute("PRAGMA table_info(recordings)")}
        if "remote_path" not in rcols:
            self.c.execute("ALTER TABLE recordings ADD COLUMN remote_path TEXT DEFAULT ''")
        if "call_ts" not in rcols:
            self.c.execute("ALTER TABLE recordings ADD COLUMN call_ts INTEGER DEFAULT 0")
            self.c.commit()
            self._backfill_call_ts()
        self.c.commit()

    def _backfill_call_ts(self):
        from . import protocol
        rows = self.c.execute("SELECT id, filename FROM recordings WHERE call_ts=0 OR call_ts IS NULL").fetchall()
        for r in rows:
            ts = protocol.parse_recording_name(r["filename"]).get("call_ts", 0)
            if ts:
                self.c.execute("UPDATE recordings SET call_ts=? WHERE id=?", (ts, r["id"]))
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
                client_ip,mac,enabled,interval_min,lookback_days,method,ftp_port,ftp_user,ftp_pass,ftp_root,db_folder)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["name"], d["host"], int(d.get("port", 6066)), d.get("user", "admin"),
                 secret.encrypt(d.get("password", "")), d.get("version", "150116 1.0.0.5"),
                 d.get("client_ip", ""), d.get("mac", ""), int(d.get("enabled", 1)),
                 int(d.get("interval_min", 15)), int(d.get("lookback_days", 1)),
                 d.get("method", "db6066"), int(d.get("ftp_port", 21) or 21),
                 d.get("ftp_user", ""), secret.encrypt(d.get("ftp_pass", "")), d.get("ftp_root", "/"),
                 d.get("db_folder", "")))
            self.c.commit(); return cur.lastrowid

    def update_device(self, did, d):
        fields = ["name","host","port","user","password","version","client_ip","mac",
                  "enabled","interval_min","lookback_days","method","ftp_port",
                  "ftp_user","ftp_pass","ftp_root","db_folder"]
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

    def _call_ts(self, filename):
        from . import protocol
        return protocol.parse_recording_name(filename).get("call_ts", 0)

    def add_recording(self, did, idx, filename, path, nbytes, duration):
        with _LOCK:
            try:
                self.c.execute("""INSERT INTO recordings(device_id,cdr_index,filename,path,bytes,duration,created,call_ts)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (did, idx, filename, path, nbytes, duration, int(time.time()), self._call_ts(filename)))
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
            self.c.execute("""INSERT INTO recordings(device_id,cdr_index,remote_path,filename,path,bytes,duration,created,call_ts)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (did, None, remote_path, filename, path, nbytes, duration, int(time.time()), self._call_ts(filename)))
            self.c.commit(); return True

    def _rec_where(self, did, ts_from, ts_to, allowed_ids=None):
        where = []; args = []
        if did:
            where.append("r.device_id=?"); args.append(did)
        if allowed_ids is not None:
            if not allowed_ids:
                where.append("0"); 
            else:
                where.append("r.device_id IN (%s)" % ",".join("?" * len(allowed_ids)))
                args += [int(x) for x in allowed_ids]
        if ts_from:
            where.append("r.call_ts>=?"); args.append(int(ts_from))
        if ts_to:
            where.append("r.call_ts<?"); args.append(int(ts_to))
        return (" WHERE " + " AND ".join(where)) if where else "", args

    def list_recordings(self, did=None, limit=50, offset=0, ts_from=None, ts_to=None, allowed_ids=None):
        w, args = self._rec_where(did, ts_from, ts_to, allowed_ids)
        q = ("SELECT r.*, d.name AS device_name FROM recordings r "
             "JOIN devices d ON d.id=r.device_id" + w +
             " ORDER BY r.call_ts DESC, r.id DESC LIMIT ? OFFSET ?")
        return [dict(r) for r in self.c.execute(q, args + [limit, offset]).fetchall()]

    def count_recordings_filtered(self, did=None, ts_from=None, ts_to=None, allowed_ids=None):
        w, args = self._rec_where(did, ts_from, ts_to, allowed_ids)
        return self.c.execute("SELECT COUNT(*) FROM recordings r" + w, args).fetchone()[0]

    def get_recording(self, rid):
        r = self.c.execute("SELECT * FROM recordings WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def count_recordings(self, did=None):
        if did:
            return self.c.execute("SELECT COUNT(*) FROM recordings WHERE device_id=?", (did,)).fetchone()[0]
        return self.c.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]

    # ---------------------------------------------------------------- users
    def count_users(self):
        return self.c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def list_users(self):
        out=[]
        for u in self.c.execute("SELECT * FROM users ORDER BY id"):
            d=dict(u); d.pop("pw_hash",None)
            d["device_ids"]=self.user_device_ids(d["id"])
            out.append(d)
        return out

    def get_user(self, uid):
        r=self.c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None

    def get_user_by_name(self, name):
        r=self.c.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        return dict(r) if r else None

    def create_user(self, username, pw_hash, full_name="", is_admin=0, can_manage=0, all_devices=0, device_ids=None):
        with _LOCK:
            cur=self.c.execute("""INSERT INTO users(username,pw_hash,full_name,is_admin,can_manage,all_devices,created)
                VALUES(?,?,?,?,?,?,?)""",(username,pw_hash,full_name,int(is_admin),int(can_manage),int(all_devices),int(time.time())))
            uid=cur.lastrowid; self.c.commit()
        self.set_user_devices(uid, device_ids or [])
        return uid

    def update_user(self, uid, fields):
        cols=["username","pw_hash","full_name","is_admin","can_manage","all_devices"]
        sets=", ".join("%s=?"%c for c in cols if c in fields)
        vals=[fields[c] for c in cols if c in fields]
        with _LOCK:
            if sets:
                self.c.execute("UPDATE users SET %s WHERE id=?"%sets, vals+[uid]); self.c.commit()
        if "device_ids" in fields:
            self.set_user_devices(uid, fields["device_ids"] or [])

    def delete_user(self, uid):
        with _LOCK:
            self.c.execute("DELETE FROM users WHERE id=?", (uid,))
            self.c.execute("DELETE FROM user_devices WHERE user_id=?", (uid,))
            self.c.commit()

    def user_device_ids(self, uid):
        return [r[0] for r in self.c.execute("SELECT device_id FROM user_devices WHERE user_id=?", (uid,))]

    def set_user_devices(self, uid, device_ids):
        with _LOCK:
            self.c.execute("DELETE FROM user_devices WHERE user_id=?", (uid,))
            for d in device_ids:
                try: self.c.execute("INSERT INTO user_devices(user_id,device_id) VALUES(?,?)", (uid,int(d)))
                except Exception: pass
            self.c.commit()

    def user_can_access(self, user, device_id):
        if not user: return False
        if user.get("is_admin") or user.get("all_devices"): return True
        return int(device_id) in set(self.user_device_ids(user["id"]))
