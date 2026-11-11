#!/usr/bin/env python3
"""
Artech voice logger protocol (TCP 6066) - reverse-engineered.

Verified against live captures of the PhoneRec/CMS client:
  * Login frame  : header 40 01 01 00 + 320-byte struct (UTF-16LE fields).
  * Record list  : device streams CDR records delimited by DELIM + u32 index.
  * File download : audio is IMA ADPCM (WAVE fmt 17), mono 8000 Hz, streamed
                    in 4096-byte chunks, each chunk prefixed by CHUNK_MARK.
                    Each file framed as  DELIM u32_index HDR u32_size 00 00 <payload>.

Pure sockets + pure-Python IMA ADPCM decoder (no scapy / no ffmpeg needed).
"""
import socket, struct, time, re, os, datetime

PORT = 6066
DELIM      = bytes.fromhex("0d0009363a00002d3a0000")   # record delimiter; u32 index follows
HDR        = bytes.fromhex("06000c0a")                 # file header: + u32 payload size + 00 00
CHUNK_MARK = bytes.fromhex("06100c0700100000")         # inline 4096-chunk marker (device->client)
CHUNK_REQ  = bytes.fromhex("06000c0700100000")         # client->device: request next 0x1000 bytes
DAY_SELECT = bytes.fromhex("04000c130000")             # client->device: prepare/select
DAY_QUERY  = bytes.fromhex("42000c11")                 # + SYSTEMTIME(day): query a day's records


# ---------------------------------------------------------------- login
def build_login(user, pw, ver, client_ip, mac, device_ip):
    # Frame = 2-byte length (0x0140 = 320 bytes FOLLOW) + 320-byte payload = 322 bytes total.
    b = bytearray(322)
    struct.pack_into("<HH", b, 0, 0x0140, 0x0001)      # length=320, cmd=01 sub=00
    def w(off, s):
        x = (s or "").encode("utf-16-le")
        b[off:off+len(x)] = x
    w(0x04, user); w(0x24, pw); w(0x44, ver)
    w(0x64, client_ip); w(0x84, mac); w(0xC0, device_ip)
    return bytes(b)


def systemtime(y, mo, da, h=0, mi=0, s=0, ms=0):
    dow = datetime.date(y, mo, da).isoweekday() % 7
    return struct.pack("<8H", y, mo, dow, da, h, mi, s, ms)


# ---------------------------------------------------------------- IMA ADPCM decode
_IDX = [-1,-1,-1,-1,2,4,6,8,-1,-1,-1,-1,2,4,6,8]
_STEP = [7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,73,80,88,97,
         107,118,130,143,157,173,190,209,230,253,279,307,337,371,408,449,494,544,598,658,724,
         796,876,963,1060,1166,1282,1411,1552,1707,1878,2066,2272,2499,2749,3024,3327,3660,
         4026,4428,4871,5358,5894,6484,7132,7845,8630,9493,10442,11487,12635,13899,15289,
         16818,18500,20350,22385,24623,27086,29794,32767]


def _decode_block(block, ch):
    out = [[] for _ in range(ch)]
    pred = [0]*ch; idx = [0]*ch; o = 0
    for c in range(ch):
        s, i = struct.unpack_from("<hB", block, o); o += 4
        pred[c] = s; idx[c] = min(88, i); out[c].append(s)
    data = block[o:]
    words = len(data) // (4*ch) if ch else 0
    pos = 0
    def step(c, nib):
        st = _STEP[idx[c]]; diff = st >> 3
        if nib & 1: diff += st >> 2
        if nib & 2: diff += st >> 1
        if nib & 4: diff += st
        if nib & 8: diff = -diff
        p = max(-32768, min(32767, pred[c] + diff)); pred[c] = p
        idx[c] = max(0, min(88, idx[c] + _IDX[nib])); return p
    for _ in range(words):
        for c in range(ch):
            four = data[pos:pos+4]; pos += 4
            for byte in four:
                out[c].append(step(c, byte & 0x0F)); out[c].append(step(c, byte >> 4))
    return out


def ima_adpcm_to_pcm16(wav_bytes):
    """Return (channels, samplerate, pcm16_bytes_interleaved) from an Artech ADPCM WAV."""
    raw = wav_bytes
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("not a WAV")
    o = 12; fmt = data = None
    while o + 8 <= len(raw):
        cid = raw[o:o+4]; sz = struct.unpack_from("<I", raw, o+4)[0]; body = raw[o+8:o+8+sz]
        if cid == b"fmt ": fmt = body
        elif cid == b"data": data = body
        o += 8 + sz + (sz & 1)
    if fmt is None or data is None:
        raise ValueError("missing fmt/data")
    afmt, ch, sr, br, ba, bps = struct.unpack_from("<HHIIHH", fmt, 0)
    if afmt != 17:
        # already PCM: return data as-is
        if afmt == 1:
            return ch, sr, data
        raise ValueError("unsupported audio format %d" % afmt)
    pcm = [[] for _ in range(ch)]
    for bo in range(0, len(data), ba):
        block = data[bo:bo+ba]
        if len(block) < 4*ch: break
        dec = _decode_block(block, ch)
        for c in range(ch): pcm[c].extend(dec[c])
    n = min(len(c) for c in pcm) if pcm else 0
    inter = bytearray(n*ch*2)
    mv = memoryview(inter)
    for i in range(n):
        for c in range(ch):
            struct.pack_into("<h", mv, (i*ch+c)*2, pcm[c][i])
    return ch, sr, bytes(inter)


def pcm16_to_wav(ch, sr, pcm):
    import io, wave
    buf = io.BytesIO(); w = wave.open(buf, "wb")
    w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm); w.close()
    return buf.getvalue()


# ---------------------------------------------------------------- extract files from a device stream
def extract_recordings(stream):
    """Yield dicts {index, wav_bytes} for every RIFF/WAVE file in a device->client stream."""
    for m in re.finditer(b"RIFF", stream):
        r = m.start()
        if stream[r+8:r+12] != b"WAVE":
            continue
        d = stream.rfind(DELIM, max(0, r-64), r)
        idx = struct.unpack_from("<I", stream, d+len(DELIM))[0] if d >= 0 else None
        h = stream.rfind(HDR, max(0, r-64), r)
        if h >= 0:
            psize = struct.unpack_from("<I", stream, h+4)[0]
            payload = stream[h+8:h+8+psize]
            clean = payload.replace(CHUNK_MARK, b"")
            if clean[:4] != b"RIFF":
                clean = stream[r:r+psize].replace(CHUNK_MARK, b"")
        else:
            clean = stream[r:].replace(CHUNK_MARK, b"")
        if clean[:4] != b"RIFF" or len(clean) < 44:
            continue
        riffsz = struct.unpack_from("<I", clean, 4)[0] + 8
        yield {"index": idx, "wav": clean[:riffsz]}


# ---------------------------------------------------------------- live download
def _recv_idle(sock, idle=2.0, hard=180.0):
    sock.setblocking(False)
    buf = bytearray(); t0 = last = time.time()
    while True:
        try:
            d = sock.recv(65535)
            if d: buf += d; last = time.time()
            else: break
        except BlockingIOError:
            now = time.time()
            if now - last > idle or now - t0 > hard: break
            time.sleep(0.05)
        except Exception:
            break
    return bytes(buf)


def download_stream(device, dates, log=lambda *a: None, idle=2.0):
    """
    Connect, login, query each date in `dates` (list of datetime.date), pull all
    chunks, return the raw device->client byte stream (parse with extract_recordings).
    `device` is a dict with host/port/user/password/version/client_ip/mac.
    """
    host = device["host"]; port = int(device.get("port", PORT))
    login = build_login(device.get("user", "admin"), device.get("password", "1111"),
                        device.get("version", "150116 1.0.0.5"),
                        device.get("client_ip", ""), device.get("mac", ""), host)
    log("connect %s:%d user=%s pass_len=%d" % (host, port, device.get("user", "admin"),
                                               len(device.get("password", ""))))
    s = socket.create_connection((host, port), timeout=10)
    try:
        s.sendall(login); time.sleep(0.3)
        buf = bytearray(_recv_idle(s, idle=1.0))
        if buf[:4] == bytes.fromhex("62000102"):
            log("login OK (%d bytes)" % len(buf))
        elif not buf:
            log("CANH BAO: khong co phan hoi login - co the sai port giao thuc hoac thieu client_ip/mac")
        else:
            log("CANH BAO: login header khac thuong %s - xem /test de debug" % buf[:4].hex())
        for dt in dates:
            s.sendall(DAY_SELECT)
            s.sendall(DAY_QUERY + systemtime(dt.year, dt.month, dt.day))
            time.sleep(0.2)
            buf += _recv_idle(s, idle=1.0)
            # pull chunks until the device stops sending
            empties = 0
            for _ in range(200000):
                try: s.sendall(CHUNK_REQ)
                except Exception: break
                chunk = _recv_idle(s, idle=0.6, hard=8.0)
                if chunk:
                    buf += chunk; empties = 0
                else:
                    empties += 1
                    if empties >= 2: break
            log("date %s: stream now %d bytes" % (dt, len(buf)))
        return bytes(buf)
    finally:
        try: s.close()
        except Exception: pass


def _hex(b, n=64):
    return b[:n].hex() + (" ...(+%d)" % (len(b) - n) if len(b) > n else "")


# ---------------------------------------------------------------- frame I/O + file download (via CDR DB)
def frame(cmd, sub, payload=b""):
    """Build a wire frame: u16 length (= 2 + len(payload)) + cmd + sub + payload."""
    body = bytes([cmd, sub]) + payload
    return struct.pack("<H", len(body)) + body


def build_cdr_request(row):
    """
    Build the 1008-byte 0c/06 download-request payload from a CDR DB row (dict with
    keys: AutoID, RecFilePath, RecType, RecChannel, RingTime, RecTime, TalkTime,
    FileSize, PhoneNumber, SystemID, SystemName). Verified byte-exact vs captures.
    """
    import datetime
    b = bytearray(1008)
    struct.pack_into("<I", b, 0, int(row.get("AutoID", 0)))
    ts = row["RecFilePath"].rsplit("\\", 1)[-1][:14]        # YYYYMMDDHHMMSS from filename
    dt = datetime.datetime.strptime(ts, "%Y%m%d%H%M%S")
    dow = dt.isoweekday() % 7
    struct.pack_into("<8H", b, 8, dt.year, dt.month, dow, dt.day, dt.hour, dt.minute, dt.second, 0)
    struct.pack_into("<I", b, 24, (int(row.get("RecType", 0)) & 0xff) | ((int(row.get("RecChannel", 0)) & 0xff) << 8))
    struct.pack_into("<I", b, 28, int(row.get("RingTime", 0) or 0))
    struct.pack_into("<I", b, 32, int(row.get("RecTime", 0) or 0))
    struct.pack_into("<I", b, 36, int(row.get("TalkTime", 0) or 0))
    struct.pack_into("<I", b, 40, int(row.get("FileSize", 0) or 0))
    def w(off, s):
        x = (s or "").encode("utf-16-le"); b[off:off+len(x)] = x
    w(44, str(row.get("PhoneNumber", "") or ""))
    w(384, row["RecFilePath"]); b[784] = 0x20
    w(848, str(row.get("SystemID", "") or ""))
    w(880, str(row.get("SystemName", "") or ""))
    return bytes(b)


class FrameConn:
    """Buffered frame reader/writer over a socket."""
    def __init__(self, sock):
        self.s = sock; self.buf = bytearray()

    def send(self, cmd, sub, payload=b""):
        self.s.sendall(frame(cmd, sub, payload))

    def _fill(self, timeout=8.0):
        self.s.settimeout(timeout)
        d = self.s.recv(65535)
        if not d:
            raise ConnectionError("closed")
        self.buf += d

    def read_frame(self, timeout=8.0):
        """Return (cmd, sub, payload) for the next complete frame, or raise on close/timeout."""
        while True:
            if len(self.buf) >= 2:
                ln = struct.unpack_from("<H", self.buf, 0)[0]
                if len(self.buf) >= 2 + ln:
                    body = bytes(self.buf[2:2+ln]); del self.buf[:2+ln]
                    return body[0], body[1], body[2:]
            self._fill(timeout)

    def drain(self, idle=0.4, hard=3.0):
        """Discard any buffered + in-flight bytes until the socket goes quiet.
        Call between files so one transfer's trailing frames can't leak into the next."""
        self.buf.clear()
        self.s.setblocking(False)
        t0 = last = time.time()
        try:
            while True:
                try:
                    d = self.s.recv(65535)
                    if d:
                        last = time.time()
                    else:
                        break
                except (BlockingIOError, BrokenPipeError, OSError):
                    now = time.time()
                    if now - last > idle or now - t0 > hard:
                        break
                    time.sleep(0.03)
        finally:
            try: self.s.setblocking(True)
            except Exception: pass
        self.buf.clear()


def download_by_request(conn, request_payload):
    """
    One file transfer: 0c/06 request -> 0c/0a (size) -> pull 0c/07 chunks -> 0c/08.
    Sends exactly one chunk request per received chunk (no request-ahead), ignores
    interleaved heartbeats, and drains trailing frames so the next file starts clean.
    """
    conn.drain()                                           # start clean
    conn.send(0x0c, 0x06, request_payload)
    size = None; data = bytearray(); requested = False
    for _ in range(500000):
        # ask for the next chunk only when we still need data and the previous
        # request has been answered (strict 1 request : 1 chunk)
        if (size is None or len(data) < size) and not requested:
            conn.send(0x0c, 0x07, bytes.fromhex("00100000"))
            requested = True
        cmd, sub, pl = conn.read_frame()
        if cmd == 0x0c and sub == 0x0a:
            size = struct.unpack_from("<I", pl, 0)[0]
            requested = False                              # 0c/0a is not a chunk
            if size == 0:
                break
        elif cmd == 0x0c and sub == 0x07:
            data += pl[4:]                                 # strip 4-byte chunk prefix
            requested = False
            if size is not None and len(data) >= size:
                break
        elif cmd == 0x0c and sub == 0x08:
            break
        # else: heartbeat/status -> ignore, keep waiting for the answer
    conn.send(0x0c, 0x08, bytes.fromhex("0000"))            # ack/close file
    conn.drain()                                            # discard the device's end-ack + any stragglers
    return bytes(data)


def download_file(conn, row, log=lambda *a: None):
    """Download one recording. Returns WAV bytes (starts with RIFF) or None."""
    data = download_by_request(conn, build_cdr_request(row))
    if data[:4] != b"RIFF":
        log("download_file: khong nhan RIFF cho %s (got %d bytes)" % (row.get("RecFilePath"), len(data)))
        return None
    riffsz = struct.unpack_from("<I", data, 4)[0] + 8
    return bytes(data[:riffsz])


def build_path_request(remote_path, when=None):
    """Build a 0c/06 request for an arbitrary device path (e.g. a CDR .db file).
    Only the path matters for the device to locate the file; metadata is minimal."""
    import datetime
    b = bytearray(1008)
    dt = when or datetime.datetime.now()
    struct.pack_into("<8H", b, 8, dt.year, dt.month, dt.isoweekday() % 7, dt.day, 0, 0, 0, 0)
    p = remote_path.encode("utf-16-le"); b[384:384+len(p)] = p
    b[784] = 0x20
    return bytes(b)


def download_path(conn, remote_path, when=None):
    """Download an arbitrary file (e.g. the device's daily CDR DB) by path. Raw bytes."""
    return download_by_request(conn, build_path_request(remote_path, when))


def login_and_handshake(device, log=lambda *a: None):
    """Connect + login. Returns a logged-in FrameConn (device-info frames are drained
    lazily; download_file ignores any non-download frames)."""
    host = device["host"]; port = int(device.get("port", PORT))
    s = socket.create_connection((host, port), timeout=10)
    conn = FrameConn(s)
    # build_login() returns the full 322-byte frame; strip its 4-byte len+cmd+sub header
    conn.send(0x01, 0x00, build_login(device.get("user", "admin"), device.get("password", ""),
              device.get("version", "150116 1.0.0.5"), device.get("client_ip", ""),
              device.get("mac", ""), host)[4:])
    t0 = time.time()
    while time.time() - t0 < 6:
        cmd, sub, pl = conn.read_frame(timeout=6)
        if cmd == 0x01 and sub == 0x02:
            log("login OK")
            return conn
        if cmd == 0x01 and sub == 0x05:
            raise ConnectionError("login bi tu choi (01/05) - thiet bi da du ket noi; "
                                  "dong PhoneRec/client khac roi thu lai")
    raise ConnectionError("no login reply (01/02)")


def login_probe(device, log=lambda *a: None):
    """
    Try to log in and report, in detail, why it did or didn't work.
    Returns a dict: {ok, msg, stage, bytes, reply_hex, reply_cmd, reply_text}.
    Logs every step so the Log tab shows exactly where login fails.
    """
    host = device["host"]; port = int(device.get("port", PORT))
    user = device.get("user", "admin"); pw = device.get("password", "")
    tag = "[test %s:%d]" % (host, port)

    # 1) TCP connect
    log("%s connecting..." % tag)
    try:
        s = socket.create_connection((host, port), timeout=6)
    except socket.timeout:
        log("%s TCP TIMEOUT - khong toi duoc thiet bi (firewall? sai IP? app gate?)" % tag)
        return {"ok": False, "stage": "connect", "msg": "TCP timeout - khong ket noi duoc %s:%d" % (host, port)}
    except OSError as e:
        log("%s TCP ERROR: %s" % (tag, e))
        return {"ok": False, "stage": "connect", "msg": "TCP loi: %s" % e}
    log("%s TCP connected" % tag)

    try:
        # 2) send login
        frame = build_login(user, pw, device.get("version", "150116 1.0.0.5"),
                           device.get("client_ip", ""), device.get("mac", ""), host)
        log("%s gui login: user=%s pass_len=%d ver=%r client_ip=%r mac=%r (frame %d bytes)"
            % (tag, user, len(pw), device.get("version", ""), device.get("client_ip", ""),
               device.get("mac", ""), len(frame)))
        s.sendall(frame)

        # 3) read reply
        reply = _recv_idle(s, idle=1.2, hard=6.0)
        if not reply:
            log("%s KHONG CO PHAN HOI - thiet bi nhan ket noi nhung im lang "
                "(sai port giao thuc? firmware khac? can client_ip/mac dung?)" % tag)
            return {"ok": False, "stage": "no_reply", "bytes": 0,
                    "msg": "Ket noi duoc nhung thiet bi khong tra loi login"}

        log("%s nhan %d bytes, header=%s" % (tag, len(reply), reply[:4].hex()))
        log("%s reply hex: %s" % (tag, _hex(reply, 80)))

        # 4) parse ALL frames and look for the login result (01/02 OK, 01/05 busy).
        #    The device interleaves heartbeat (15/00) and info frames in any order.
        seen = []
        buf = reply; off = 0
        login_frame = None
        while off + 2 <= len(buf):
            ln = struct.unpack_from("<H", buf, off)[0]
            if ln < 2 or off + 2 + ln > len(buf):
                break
            cmd, sub = buf[off+2], buf[off+3]
            seen.append("%02x/%02x" % (cmd, sub))
            if cmd == 0x01 and sub in (0x02, 0x05):
                login_frame = (cmd, sub, bytes(buf[off+4:off+2+ln])); break
            off += 2 + ln
        log("%s frames: %s" % (tag, " ".join(seen[:12]) + (" ..." if len(seen) > 12 else "")))

        if login_frame and login_frame[1] == 0x02:
            pl = login_frame[2]
            try:
                name = pl[:20].decode("utf-16-le", "ignore").split("\x00")[0]
            except Exception:
                name = ""
            log("%s LOGIN OK (echo user=%r)" % (tag, name))
            return {"ok": True, "stage": "ok", "bytes": len(reply),
                    "msg": "Login OK" + (" (user=%s)" % name if name else "")}
        if login_frame and login_frame[1] == 0x05:
            log("%s LOGIN BI TU CHOI (01/05) - thiet bi da du so ket noi cho phep" % tag)
            log("%s -> dong PhoneRec va client khac, doi ~30s roi thu lai" % tag)
            return {"ok": False, "stage": "busy", "bytes": len(reply),
                    "msg": "Thiet bi tu choi dang nhap (dang co phien khac). Dong PhoneRec/client khac, doi ~30s roi Test lai."}
        if seen and all(f.startswith("15/") or f.startswith("09/") or f.startswith("08/") for f in seen):
            # only heartbeat/status seen and no explicit 01/02 — device is streaming, treat as logged-in
            log("%s nhan heartbeat/status (chua thay 01/02 ro rang) - coi nhu da dang nhap" % tag)
            return {"ok": True, "stage": "ok_heartbeat", "bytes": len(reply),
                    "msg": "Ket noi OK (thiet bi dang day du lieu status)"}

        # frames received but no 01/02 and not pure heartbeat
        log("%s khong thay frame login 01/02 trong phan hoi" % tag)
        return {"ok": False, "stage": "bad_reply", "bytes": len(reply),
                "msg": "Thiet bi tra loi nhung khong co frame login 01/02 (frames: %s) - xem tab Log"
                       % " ".join(seen[:8])}
    finally:
        try: s.close()
        except Exception: pass


def replay_stream(host, seq_bytes, port=PORT, idle=2.0):
    """Replay a captured client sequence verbatim; return device->client stream."""
    s = socket.create_connection((host, port), timeout=10)
    try:
        s.sendall(seq_bytes)
        return _recv_idle(s, idle=idle)
    finally:
        try: s.close()
        except Exception: pass
