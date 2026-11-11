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

        head = reply[:4]
        log("%s nhan %d bytes, header=%s" % (tag, len(reply), head.hex()))
        log("%s reply hex: %s" % (tag, _hex(reply, 80)))

        # 4) interpret
        if head == bytes.fromhex("62000102"):
            # pull any readable UTF-16LE username echoed back
            try:
                name = reply[4:24].decode("utf-16-le", "ignore").split("\x00")[0]
            except Exception:
                name = ""
            log("%s LOGIN OK (echo user=%r)" % (tag, name))
            return {"ok": True, "stage": "ok", "bytes": len(reply),
                    "reply_hex": head.hex(), "msg": "Login OK" + (" (user=%s)" % name if name else "")}

        # unexpected header: decode cmd/sub if it looks framed (<u16 len><u8 cmd><u8 sub>)
        info = ""
        if len(reply) >= 4:
            ln = struct.unpack_from("<H", reply, 0)[0]
            info = " (frame len=%d cmd=%02x sub=%02x)" % (ln, reply[2], reply[3])
        log("%s LOGIN BI TU CHOI/khac thuong: header=%s%s" % (tag, head.hex(), info))
        log("%s -> thuong do sai user/pass, hoac thieu client_ip/mac/version dung" % tag)
        return {"ok": False, "stage": "bad_reply", "bytes": len(reply),
                "reply_hex": _hex(reply, 40),
                "msg": "Thiet bi tra loi khac thuong (header %s)%s - xem tab Log" % (head.hex(), info)}
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
