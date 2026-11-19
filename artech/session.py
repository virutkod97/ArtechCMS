"""
Persistent per-device login session.

Once ArtechCMS logs in to a device it keeps that connection open and sends a
periodic keepalive, so the device's single login slot stays held and other
clients (PhoneRec, etc.) get "busy" (01/05) instead of stealing the session.

Socket access is serialised by `lock`: every operation (download / call-log /
fetch / test) runs inside `with session.lock:`, and the keepalive thread only
touches the socket when no operation holds the lock. The operation's own
traffic keeps the session alive while it runs; the keepalive covers idle time.
"""
import threading
import time
from . import protocol

KEEPALIVE_SECS = 8.0
KEEPALIVE_FRAME = (0x07, 0x00, b"\x00\x00")   # what the vendor client sends to hold the session


class DeviceSession:
    def __init__(self, device_getter, log=lambda *a: None):
        self.device_getter = device_getter      # callable -> fresh device dict
        self.log = log
        self.conn = None
        self.lock = threading.Lock()             # serialises socket use (ops + keepalive)
        self._ka_stop = threading.Event()
        self._ka_thread = None

    # ---- connection (caller must hold self.lock) ----
    def get(self):
        """Return a live, logged-in FrameConn, reconnecting if needed."""
        if self.conn is None:
            self.conn = protocol.login_and_handshake(self.device_getter(),
                                                      log=lambda *a: self.log(*a))
            self.log("đã giữ phiên đăng nhập")
        return self.conn

    def reset(self):
        if self.conn is not None:
            try: self.conn.s.close()
            except Exception: pass
        self.conn = None

    def is_open(self):
        return self.conn is not None

    # ---- keepalive ----
    def start_keepalive(self):
        if self._ka_thread and self._ka_thread.is_alive():
            return
        self._ka_stop.clear()
        self._ka_thread = threading.Thread(target=self._ka_loop, daemon=True)
        self._ka_thread.start()

    def stop(self):
        self._ka_stop.set()
        with self.lock:
            self.reset()

    def _ka_loop(self):
        while not self._ka_stop.wait(KEEPALIVE_SECS):
            # only when no operation is using the socket
            if not self.lock.acquire(blocking=False):
                continue
            try:
                if self.conn is not None:
                    try:
                        self.conn.send(*KEEPALIVE_FRAME)
                        self.conn.drain(idle=0.3, hard=1.0)   # swallow heartbeats/replies
                    except Exception as e:
                        self.log("keepalive lỗi, đóng phiên: %s" % e)
                        self.reset()
            finally:
                self.lock.release()


class SessionManager:
    """Holds one DeviceSession per device id."""
    def __init__(self, device_getter, log=lambda *a: None):
        self.device_getter = device_getter       # callable(did) -> device dict
        self.log = log
        self._sessions = {}
        self._guard = threading.Lock()

    def get(self, did):
        with self._guard:
            s = self._sessions.get(did)
            if s is None:
                s = self._sessions[did] = DeviceSession(
                    lambda: self.device_getter(did),
                    log=lambda *a, _did=did: self.log(*a))
                s.start_keepalive()
            return s

    def drop(self, did):
        """Close and forget a device's session (on edit/delete)."""
        with self._guard:
            s = self._sessions.pop(did, None)
        if s:
            s.stop()
