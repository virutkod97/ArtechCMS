"""Encrypt device passwords at rest.
Windows: DPAPI (bound to the Windows account running the app).
Other OS: XOR stream keyed by a random local key file (obfuscation fallback)."""
import os, sys, base64, hashlib, ctypes

PREFIX = "enc:"
_KEYFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "secret.key")


def _dpapi(data, protect):
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inb = BLOB(len(data), buf)
    outb = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(outb)):
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(outb.pbData, outb.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outb.pbData)


def _key():
    if not os.path.exists(_KEYFILE):
        os.makedirs(os.path.dirname(_KEYFILE), exist_ok=True)
        with open(_KEYFILE, "wb") as f:
            f.write(os.urandom(32))
    return open(_KEYFILE, "rb").read()


def _xor(data):
    key = _key(); out = bytearray(); n = 0
    while len(out) < len(data):
        out += hashlib.sha256(key + n.to_bytes(4, "little")).digest(); n += 1
    return bytes(a ^ b for a, b in zip(data, out))


def encrypt(plain):
    if plain is None or plain == "" or str(plain).startswith(PREFIX):
        return plain
    raw = str(plain).encode("utf-8")
    blob = _dpapi(raw, True) if sys.platform == "win32" else _xor(raw)
    return PREFIX + base64.b64encode(blob).decode("ascii")


def decrypt(stored):
    if not stored or not str(stored).startswith(PREFIX):
        return stored or ""          # legacy plaintext row
    blob = base64.b64decode(stored[len(PREFIX):])
    try:
        raw = _dpapi(blob, False) if sys.platform == "win32" else _xor(blob)
        return raw.decode("utf-8")
    except Exception:
        return ""
