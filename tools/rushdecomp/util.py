"""Common helpers used across the Rush-Decomp toolchain.

Nothing in here knows anything about Sonic Rush specifically; these are the
plumbing primitives (little-endian integer access, alignment, checksums of our
own artifacts) shared by the format parsers.
"""

from __future__ import annotations

import hashlib
import os
from typing import Iterable, Iterator, Sequence

__all__ = [
    "u16",
    "u32",
    "u64",
    "p16",
    "p32",
    "align_up",
    "align_down",
    "is_aligned",
    "chunk",
    "hexdigest",
    "read_file",
    "write_file",
    "ensure_dir",
    "human_size",
    "hexdump",
]


# --------------------------------------------------------------------------
# Little-endian integer access.  The DS is an ARM machine and every on-media
# structure it uses is little-endian, so we never bother with struct.unpack
# for the simple scalar cases.
# --------------------------------------------------------------------------
def u16(buf: bytes | bytearray, off: int = 0) -> int:
    """Read an unsigned little-endian 16-bit value."""
    return buf[off] | (buf[off + 1] << 8)


def u32(buf: bytes | bytearray, off: int = 0) -> int:
    """Read an unsigned little-endian 32-bit value."""
    return (
        buf[off]
        | (buf[off + 1] << 8)
        | (buf[off + 2] << 16)
        | (buf[off + 3] << 24)
    )


def u64(buf: bytes | bytearray, off: int = 0) -> int:
    """Read an unsigned little-endian 64-bit value."""
    lo = u32(buf, off)
    hi = u32(buf, off + 4)
    return lo | (hi << 32)


def p16(value: int) -> bytes:
    """Pack an unsigned value as little-endian 16-bit."""
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


def p32(value: int) -> bytes:
    """Pack an unsigned value as little-endian 32-bit."""
    return bytes(
        (
            value & 0xFF,
            (value >> 8) & 0xFF,
            (value >> 16) & 0xFF,
            (value >> 24) & 0xFF,
        )
    )


# --------------------------------------------------------------------------
# Alignment.  Nitro binaries and the FAT/overlay tables are padded to 4-byte
# (frequently 0x200-byte) boundaries, and getting this wrong is the single
# most common source of a non-matching rebuild.
# --------------------------------------------------------------------------
def align_up(value: int, boundary: int) -> int:
    if boundary <= 0:
        raise ValueError("boundary must be positive")
    rem = value % boundary
    return value if rem == 0 else value + (boundary - rem)


def align_down(value: int, boundary: int) -> int:
    if boundary <= 0:
        raise ValueError("boundary must be positive")
    return value - (value % boundary)


def is_aligned(value: int, boundary: int) -> bool:
    return value % boundary == 0


def chunk(seq: Sequence, size: int) -> Iterator[Sequence]:
    """Yield fixed-size slices of *seq*; the final slice may be short."""
    if size <= 0:
        raise ValueError("size must be positive")
    for start in range(0, len(seq), size):
        yield seq[start : start + size]


# --------------------------------------------------------------------------
# Digests / IO
# --------------------------------------------------------------------------
def hexdigest(data: bytes, algo: str = "sha256") -> str:
    return hashlib.new(algo, data).hexdigest()


def read_file(path: str | os.PathLike) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def write_file(path: str | os.PathLike, data: bytes) -> None:
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    tmp = f"{os.fspath(path)}.tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, os.fspath(path))


def ensure_dir(path: str | os.PathLike) -> None:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GiB"


_HEX_CHARS = "0123456789abcdef"


def hexdump(
    data: bytes,
    base: int = 0,
    limit: int | None = None,
    width: int = 16,
) -> str:
    """Classic offset / hex / ASCII dump, used all over the debug output."""
    if limit is not None:
        data = data[:limit]
    lines: list[str] = []
    for off in range(0, len(data), width):
        row = data[off : off + width]
        hexes = " ".join(f"{b:02x}" for b in row)
        hexes = f"{hexes[: width * 3 - 1]:<{width * 3 - 1}}"
        printable = "".join(
            chr(b) if 0x20 <= b < 0x7F else "." for b in row
        )
        lines.append(f"{base + off:08x}  {hexes}  |{printable}|")
    return "\n".join(lines)


def flatten(items: Iterable[Iterable]) -> list:
    out: list = []
    for item in items:
        out.extend(item)
    return out
