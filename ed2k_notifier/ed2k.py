"""High-speed ED2K hash calculator and link generator.

Uses C-accelerated MD4 from pycryptodome when available (hundreds of MB/s on
multi-gigabyte video files), with a built-in pure-Python RFC 1320 MD4 fallback.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

ED2K_CHUNK_SIZE = 9_728_000  # 9500 * 1024 bytes
READ_BUFFER_SIZE = 1_048_576  # 1 MiB streaming buffer
EMPTY_MD4_DIGEST = bytes.fromhex("31d6cfe0d16ae931b73c59d7e0c089c0")

try:
    from Crypto.Hash import MD4 as _CryptoMD4

    HAS_FAST_MD4 = True
except ImportError:
    _CryptoMD4 = None
    HAS_FAST_MD4 = False


class _PurePythonMD4:
    """RFC 1320 MD4 implementation used only as a fallback if pycryptodome is missing."""

    def __init__(self, data: bytes = b"") -> None:
        self._a = 0x67452301
        self._b = 0xEFCDAB89
        self._c = 0x98BADCFE
        self._d = 0x10325476
        self._count = 0
        self._buffer = bytearray()
        if data:
            self.update(data)

    @staticmethod
    def _lrot(value: int, bits: int) -> int:
        return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF

    def _process_block(self, block: bytes | bytearray | memoryview) -> None:
        x = struct.unpack("<16I", block)
        a, b, c, d = self._a, self._b, self._c, self._d
        lrot = self._lrot

        # Round 1
        s1 = (3, 7, 11, 19)
        for i in range(16):
            k = i
            s = s1[i % 4]
            if i % 4 == 0:
                a = lrot((a + ((b & c) | (~b & d)) + x[k]) & 0xFFFFFFFF, s)
            elif i % 4 == 1:
                d = lrot((d + ((a & b) | (~a & c)) + x[k]) & 0xFFFFFFFF, s)
            elif i % 4 == 2:
                c = lrot((c + ((d & a) | (~d & b)) + x[k]) & 0xFFFFFFFF, s)
            else:
                b = lrot((b + ((c & d) | (~c & a)) + x[k]) & 0xFFFFFFFF, s)

        # Round 2
        s2 = (3, 5, 9, 13)
        for i in range(16):
            k = (i % 4) * 4 + (i // 4)
            s = s2[i % 4]
            if i % 4 == 0:
                a = lrot((a + ((b & c) | (b & d) | (c & d)) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)
            elif i % 4 == 1:
                d = lrot((d + ((a & b) | (a & c) | (b & c)) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)
            elif i % 4 == 2:
                c = lrot((c + ((d & a) | (d & b) | (a & b)) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)
            else:
                b = lrot((b + ((c & d) | (c & a) | (d & a)) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)

        # Round 3
        s3 = (3, 9, 11, 15)
        k3 = (0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15)
        for i in range(16):
            k = k3[i]
            s = s3[i % 4]
            if i % 4 == 0:
                a = lrot((a + (b ^ c ^ d) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            elif i % 4 == 1:
                d = lrot((d + (a ^ b ^ c) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            elif i % 4 == 2:
                c = lrot((c + (d ^ a ^ b) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            else:
                b = lrot((b + (c ^ d ^ a) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)

        self._a = (self._a + a) & 0xFFFFFFFF
        self._b = (self._b + b) & 0xFFFFFFFF
        self._c = (self._c + c) & 0xFFFFFFFF
        self._d = (self._d + d) & 0xFFFFFFFF

    def update(self, data: bytes | bytearray | memoryview) -> None:
        if not data:
            return
        self._count += len(data)
        self._buffer.extend(data)
        with memoryview(self._buffer) as mv:
            offset = 0
            total = len(self._buffer)
            while total - offset >= 64:
                self._process_block(mv[offset : offset + 64])
                offset += 64
        if offset:
            del self._buffer[:offset]

    def digest(self) -> bytes:
        # Operate on a copy so digest() is non-destructive
        clone = _PurePythonMD4()
        clone._a, clone._b, clone._c, clone._d = self._a, self._b, self._c, self._d
        clone._count = self._count
        clone._buffer = bytearray(self._buffer)

        bit_len = clone._count * 8
        clone._buffer.append(0x80)
        while len(clone._buffer) % 64 != 56:
            clone._buffer.append(0x00)
        clone._buffer.extend(struct.pack("<Q", bit_len))

        mv = memoryview(clone._buffer)
        for offset in range(0, len(clone._buffer), 64):
            clone._process_block(mv[offset : offset + 64])
        return struct.pack("<4I", clone._a, clone._b, clone._c, clone._d)

    def hexdigest(self) -> str:
        return self.digest().hex()


def new_md4(data: bytes = b""):
    """Return a new MD4 hash object (fast C implementation if available)."""
    if _CryptoMD4 is not None:
        return _CryptoMD4.new(data)
    return _PurePythonMD4(data)


class HashCancelledError(Exception):
    """Raised when ED2K hashing is cancelled by the caller."""


@dataclass
class ED2KResult:
    """Result of an ED2K hash calculation."""

    file_path: str
    file_name: str
    file_size: int
    ed2k_hash: str
    ed2k_link: str
    elapsed_seconds: float
    mode: str = "blue"

    @property
    def speed_mb_s(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return (self.file_size / (1024 * 1024)) / self.elapsed_seconds


def format_file_size(num_bytes: int) -> str:
    """Format byte count into human-readable string."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def encode_ed2k_filename(filename: str, urlencode_non_ascii: bool = False) -> str:
    """Encode filename safely for inclusion in an ed2k:// link."""
    if urlencode_non_ascii:
        return quote(filename, safe=".-_()[]")

    # Keep Unicode (e.g., Chinese titles) readable & compact while escaping
    # ED2K delimiters and URL-unsafe characters.
    escape_chars = set(" |%#?/\\<>\"'\r\n\t")
    out: list[str] = []
    for ch in filename:
        if ch in escape_chars or ord(ch) < 0x20:
            out.append(quote(ch, safe=""))
        else:
            out.append(ch)
    return "".join(out)


def build_ed2k_link(
    filename: str,
    file_size: int,
    ed2k_hash: str,
    uppercase_hash: bool = True,
    urlencode_non_ascii: bool = False,
) -> str:
    """Construct a standard ed2k://|file|...|/ link."""
    safe_name = encode_ed2k_filename(filename, urlencode_non_ascii=urlencode_non_ascii)
    hash_str = ed2k_hash.upper() if uppercase_hash else ed2k_hash.lower()
    return f"ed2k://|file|{safe_name}|{file_size}|{hash_str}|/"


def calculate_ed2k_bytes(data: bytes, mode: str = "blue") -> str:
    """Calculate ED2K hash (lowercase hex) directly from an in-memory bytes object."""
    if mode not in ("blue", "red"):
        raise ValueError(f"Unsupported ED2K mode: {mode!r}")

    total_size = len(data)
    if total_size == 0:
        return EMPTY_MD4_DIGEST.hex()

    if total_size < ED2K_CHUNK_SIZE:
        return new_md4(data).hexdigest()

    chunk_digests: list[bytes] = []
    for offset in range(0, total_size, ED2K_CHUNK_SIZE):
        chunk = data[offset : offset + ED2K_CHUNK_SIZE]
        chunk_digests.append(new_md4(chunk).digest())

    if total_size % ED2K_CHUNK_SIZE == 0:
        if mode == "blue":
            chunk_digests.append(EMPTY_MD4_DIGEST)
        elif len(chunk_digests) == 1:
            return chunk_digests[0].hex()

    return new_md4(b"".join(chunk_digests)).hexdigest()


def calculate_ed2k_file(
    file_path: str | Path,
    mode: str = "blue",
    uppercase_hash: bool = True,
    urlencode_non_ascii: bool = False,
    progress_callback: Optional[Callable[[int, int, float], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> ED2KResult:
    """Stream-calculate the ED2K hash and link for a file on disk.

    Args:
        file_path: Path to the target file.
        mode: "blue" (eMule / modern standard) or "red" (legacy eDonkey2000).
        uppercase_hash: Format hex hash in uppercase.
        urlencode_non_ascii: Percent-encode non-ASCII characters in the link filename.
        progress_callback: Optional callback(bytes_done, total_bytes, speed_mb_s).
        cancel_check: Optional callable returning True if hashing should abort.
    """
    if mode not in ("blue", "red"):
        raise ValueError(f"Unsupported ED2K mode: {mode!r}")

    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    file_size = os.path.getsize(path)
    start_time = time.perf_counter()

    if file_size == 0:
        final_hex = EMPTY_MD4_DIGEST.hex()
        elapsed = time.perf_counter() - start_time
        if progress_callback:
            progress_callback(0, 0, 0.0)
        hash_out = final_hex.upper() if uppercase_hash else final_hex.lower()
        return ED2KResult(
            file_path=str(path),
            file_name=path.name,
            file_size=0,
            ed2k_hash=hash_out,
            ed2k_link=build_ed2k_link(
                path.name,
                0,
                hash_out,
                uppercase_hash=uppercase_hash,
                urlencode_non_ascii=urlencode_non_ascii,
            ),
            elapsed_seconds=elapsed,
            mode=mode,
        )

    chunk_digests: list[bytes] = []
    bytes_read_total = 0
    last_report_time = start_time

    with path.open("rb") as f:
        while bytes_read_total < file_size:
            chunk_remaining = min(ED2K_CHUNK_SIZE, file_size - bytes_read_total)
            hasher = new_md4()
            while chunk_remaining > 0:
                if cancel_check and cancel_check():
                    raise HashCancelledError("ED2K calculation cancelled by user.")

                to_read = min(READ_BUFFER_SIZE, chunk_remaining)
                buf = f.read(to_read)
                if not buf:
                    break
                hasher.update(buf)
                read_len = len(buf)
                chunk_remaining -= read_len
                bytes_read_total += read_len

                if progress_callback:
                    now = time.perf_counter()
                    if now - last_report_time >= 0.1 or bytes_read_total >= file_size:
                        elapsed_so_far = max(now - start_time, 1e-6)
                        speed = (bytes_read_total / (1024 * 1024)) / elapsed_so_far
                        progress_callback(bytes_read_total, file_size, speed)
                        last_report_time = now

            chunk_digests.append(hasher.digest())

    if file_size < ED2K_CHUNK_SIZE:
        final_hex = chunk_digests[0].hex()
    elif file_size % ED2K_CHUNK_SIZE == 0:
        if mode == "blue":
            chunk_digests.append(EMPTY_MD4_DIGEST)
            final_hex = new_md4(b"".join(chunk_digests)).hexdigest()
        elif len(chunk_digests) == 1:
            final_hex = chunk_digests[0].hex()
        else:
            final_hex = new_md4(b"".join(chunk_digests)).hexdigest()
    else:
        final_hex = new_md4(b"".join(chunk_digests)).hexdigest()

    elapsed = time.perf_counter() - start_time
    hash_out = final_hex.upper() if uppercase_hash else final_hex.lower()
    link = build_ed2k_link(
        path.name,
        file_size,
        hash_out,
        uppercase_hash=uppercase_hash,
        urlencode_non_ascii=urlencode_non_ascii,
    )
    return ED2KResult(
        file_path=str(path),
        file_name=path.name,
        file_size=file_size,
        ed2k_hash=hash_out,
        ed2k_link=link,
        elapsed_seconds=elapsed,
        mode=mode,
    )
