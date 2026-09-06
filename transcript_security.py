"""Local encrypted transcript storage and per-recording sharing tokens."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import tempfile
from ctypes import wintypes
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT = "autoscript.transcript.aes256gcm.v2"
AAD = FORMAT.encode("ascii")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


# ctypes otherwise assumes 32-bit integer arguments.  On 64-bit Windows that
# truncates the DATA_BLOB pointers passed to DPAPI and can terminate the
# process while saving a token.  Declare the native signatures explicitly.
_CryptProtectData = ctypes.windll.crypt32.CryptProtectData
_CryptProtectData.argtypes = (
    ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob),
)
_CryptProtectData.restype = wintypes.BOOL
_CryptUnprotectData = ctypes.windll.crypt32.CryptUnprotectData
_CryptUnprotectData.argtypes = (
    ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob),
)
_CryptUnprotectData.restype = wintypes.BOOL
_LocalFree = ctypes.windll.kernel32.LocalFree
_LocalFree.argtypes = (ctypes.c_void_p,)
_LocalFree.restype = ctypes.c_void_p


def _protect(value: str) -> str:
    raw = value.encode("utf-8")
    source_buffer = ctypes.create_string_buffer(raw)
    source = _Blob(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _Blob()
    if not _CryptProtectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return base64.b64encode(ctypes.string_at(target.pbData, target.cbData)).decode("ascii")
    finally:
        _LocalFree(target.pbData)


def _unprotect(value: str) -> str:
    raw = base64.b64decode(value.encode("ascii"))
    source_buffer = ctypes.create_string_buffer(raw)
    source = _Blob(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _Blob()
    if not _CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        _LocalFree(target.pbData)


def new_token() -> str:
    """A 256-bit URL-safe secret intended for manual sharing."""
    return secrets.token_urlsafe(32)


def _key(token: str, salt: bytes) -> bytes:
    if not token or len(token) < 32:
        raise ValueError("A valid transcript token is required")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"AutoScript per-recording transcript v2").derive(token.encode("utf-8"))


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".autoscript-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def encrypt(path: Path, text: str, token: str, transcript_id: str) -> None:
    salt, nonce = os.urandom(32), os.urandom(12)
    ciphertext = AESGCM(_key(token, salt)).encrypt(nonce, text.encode("utf-8"), AAD)
    _atomic(path, json.dumps({"format": FORMAT, "id": transcript_id, "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ciphertext).decode()}).encode("utf-8"))


def decrypt(path: Path, token: str) -> str:
    value = json.loads(Path(path).read_bytes())
    if value.get("format") != FORMAT:
        raise ValueError("This is not a current AutoScript encrypted transcript")
    salt, nonce, ciphertext = (base64.b64decode(value[name], validate=True) for name in ("salt", "nonce", "ciphertext"))
    return AESGCM(_key(token, salt)).decrypt(nonce, ciphertext, AAD).decode("utf-8")


class EncryptedLog:
    def __init__(self, path: Path, token: str, transcript_id: str, header: str):
        self.path, self.token, self.transcript_id, self.text = path, token, transcript_id, header
        encrypt(path, header, token, transcript_id)

    def append(self, line: str) -> None:
        self.text += line + "\n"
        encrypt(self.path, self.text, self.token, self.transcript_id)


class TokenVault:
    def __init__(self, path: Path):
        self.path = path

    def save(self, transcript_id: str, token: str) -> None:
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            values = {}
        values[transcript_id] = _protect(token)
        _atomic(self.path, json.dumps(values).encode("utf-8"))

    def get(self, transcript_id: str) -> str | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")).get(transcript_id)
            return _unprotect(value) if value else None
        except (OSError, ValueError):
            return None


def transcript_id(path: Path) -> str:
    try:
        return json.loads(Path(path).read_bytes()).get("id", "")
    except (OSError, ValueError):
        return ""
