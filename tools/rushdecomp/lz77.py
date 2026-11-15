"""Nintendo's LZ77 family, as used by the DS BIOS and by Sonic Rush.

Three markers appear in a DS cartridge image:

``0x10``
    LZ10 -- the classic 3..18-byte, 12-bit-distance scheme the GBA BIOS already
    knew.  Nitro's ``LZDecompressUnwrap`` handles it, and it is what most
    overlay and file-system blobs use.

``0x11``
    LZ11 -- the extended scheme introduced with the DS.  Same 4096-byte window,
    but a three-way reference encoding that covers lengths 2..16 in two bytes,
    17..272 in three, and 273.. up in four.

``0x30``
    Not a compression type at all.  When the ARM9 binary is *statically*
    compressed the loader overwrites the leading ``0x10`` with ``0x30`` and
    appends a 12-byte footer.  The BIOS ARM9 entry stub keys off that ``0x30``
    to decide whether to call ``DecompressARM9``.  :mod:`rushdecomp.arm9` deals
    with the footer; this module decompresses the payload once the marker has
    been normalised.

The decoders are exact.  The encoders are greedy longest-match over a 3-byte
hash chain and reproduce the reference output for the overwhelming majority of
inputs, but a different vendor encoder may pick a different match on a tie, so
:func:`recompression_matches` exists for the build to check before it trusts a
regenerated blob.
"""

from __future__ import annotations

from typing import NamedTuple

from .util import u32

__all__ = [
    "LZError",
    "Marker",
    "ProbeResult",
    "probe",
    "is_compressed",
    "decompressed_size",
    "decompress",
    "decompress_auto",
    "compress",
    "compress_lz10",
    "compress_lz11",
    "header_of",
    "recompression_matches",
]

#: Size of the LZ77 sliding window, shared by both schemes.
WINDOW_SIZE = 0x1000

#: LZ10 encodes a length in 4 bits plus a bias of 3.
LZ10_MIN_LENGTH = 3
LZ10_MAX_LENGTH = 0x12  # 18

#: LZ11 splits lengths across three reference encodings.
LZ11_MIN_LENGTH = 2
LZ11_MID_MAX = 0x110  # 272:  ind == 0, three bytes
LZ11_LONG_MAX = 0x10110  # ind == 1, four bytes

_HASH_BITS = 15
_HASH_SIZE = 1 << _HASH_BITS
_HASH_SHIFT = 5

#: How far down a hash chain to look.  The Nitro encoder is exhaustive, but a
#: bounded search keeps a 4 MB ARM9 compressible in seconds instead of hours.
#: Raise it with ``--deep-search`` when a rebuild fails to match.
DEFAULT_SEARCH_DEPTH = 128


class LZError(ValueError):
    """Raised when a compressed stream is malformed or runs off its input."""


class Marker:
    LZ10 = 0x10
    LZ11 = 0x11
    #: ARM9 statically-compressed marker; the payload underneath is LZ10.
    LZ10_ARM9_STATIC = 0x30


class ProbeResult(NamedTuple):
    """Outcome of sniffing a buffer for a Nintendo LZ77 stream."""

    compressed: bool
    marker: int
    decompressed_size: int
    header_size: int


_NOT_COMPRESSED = ProbeResult(False, 0, 0, 0)


def probe(data: bytes | bytearray) -> ProbeResult:
    """Decide whether *data* begins with a Nintendo LZ77 stream.

    ``0x30`` is reported as :attr:`Marker.LZ10` because that is what the bytes
    behind it actually are; callers that need to know it was the ARM9 static
    form should inspect ``data[0]`` themselves.
    """
    if len(data) < 4:
        return ProbeResult(False, 0, len(data), 0)
    raw_marker = data[0]
    marker = Marker.LZ10 if raw_marker == Marker.LZ10_ARM9_STATIC else raw_marker
    if marker not in (Marker.LZ10, Marker.LZ11):
        return ProbeResult(False, raw_marker, len(data), 0)
    size = data[1] | (data[2] << 8) | (data[3] << 16)
    if size == 0:
        return ProbeResult(False, raw_marker, len(data), 0)
    return ProbeResult(True, marker, size, 4)


def is_compressed(data: bytes | bytearray) -> bool:
    return probe(data).compressed


def decompressed_size(data: bytes | bytearray) -> int:
    """Size the stream expands to, or ``len(data)`` if it is not packed."""
    info = probe(data)
    return info.decompressed_size if info.compressed else len(data)


# --------------------------------------------------------------------------
# Decompression
# --------------------------------------------------------------------------
def decompress(data: bytes | bytearray, marker: int) -> bytearray:
    """Decompress a Nintendo LZ77 stream whose marker byte is already known."""
    if marker == Marker.LZ10_ARM9_STATIC:
        marker = Marker.LZ10
    if marker == Marker.LZ10:
        return _decompress_lz10(data)
    if marker == Marker.LZ11:
        return _decompress_lz11(data)
    raise LZError(f"unsupported LZ77 marker 0x{marker:02x}")


def decompress_auto(data: bytes | bytearray) -> tuple[bytearray, ProbeResult]:
    """Sniff *data* and decompress it if it carries an LZ77 marker."""
    info = probe(data)
    if not info.compressed:
        return bytearray(data), info
    return decompress(data, info.marker), info


def _decompress_lz10(data: bytes | bytearray) -> bytearray:
    limit = len(data)
    if limit < 4:
        raise LZError("LZ10 stream truncated: missing 4-byte header")
    out_size = data[1] | (data[2] << 8) | (data[3] << 16)
    out = bytearray(out_size)
    pos = 0
    src = 4

    while pos < out_size:
        if src >= limit:
            raise LZError(
                f"LZ10 stream ended early at output offset 0x{pos:x} "
                f"(wanted 0x{out_size:x})"
            )
        flags = data[src]
        src += 1
        for _ in range(8):
            if pos >= out_size:
                break
            if flags & 0x80:
                if src + 1 >= limit:
                    raise LZError(f"LZ10 reference truncated at src 0x{src:x}")
                ref = data[src] | (data[src + 1] << 8)
                src += 2
                length = (ref >> 12) + 3
                disp = (ref & 0x0FFF) + 1
                if disp > pos:
                    raise LZError(
                        f"LZ10 reference at src 0x{src - 2:x} reaches 0x{disp:x} "
                        f"bytes back, past the start of the output (pos 0x{pos:x})"
                    )
                start = pos - disp
                end = min(pos + length, out_size)
                # Byte-at-a-time on purpose: overlapping references are legal
                # and are exactly how LZ77 spells a run-length fill.
                while pos < end:
                    out[pos] = out[start]
                    pos += 1
                    start += 1
            else:
                if src >= limit:
                    raise LZError(f"LZ10 literal truncated at src 0x{src:x}")
                out[pos] = data[src]
                src += 1
                pos += 1
            flags = (flags << 1) & 0xFF
    return out


def _decompress_lz11(data: bytes | bytearray) -> bytearray:
    limit = len(data)
    if limit < 4:
        raise LZError("LZ11 stream truncated: missing 4-byte header")
    out_size = data[1] | (data[2] << 8) | (data[3] << 16)
    out = bytearray(out_size)
    pos = 0
    src = 4

    def need(count: int, what: str) -> None:
        if src + count > limit:
            raise LZError(
                f"LZ11 {what} reference truncated at src 0x{src:x} "
                f"(need {count} bytes, have {limit - src})"
            )

    while pos < out_size:
        if src >= limit:
            raise LZError(
                f"LZ11 stream ended early at output offset 0x{pos:x} "
                f"(wanted 0x{out_size:x})"
            )
        flags = data[src]
        src += 1
        for _ in range(8):
            if pos >= out_size:
                break
            if not flags & 0x80:
                if src >= limit:
                    raise LZError(f"LZ11 literal truncated at src 0x{src:x}")
                out[pos] = data[src]
                src += 1
                pos += 1
                flags = (flags << 1) & 0xFF
                continue

            b0 = data[src]
            src += 1
            category = b0 >> 4
            if category == 0:
                # Three bytes, length 17..272.
                need(2, "medium")
                b1 = data[src]
                b2 = data[src + 1]
                src += 2
                length = ((b0 & 0x0F) << 4 | b1 >> 4) + 0x11
                disp = ((b1 & 0x0F) << 8 | b2) + 1
            elif category == 1:
                # Four bytes, length 273 and up.
                need(3, "long")
                b1 = data[src]
                b2 = data[src + 1]
                b3 = data[src + 2]
                src += 3
                length = ((b0 & 0x0F) << 12 | b1 << 4 | b2 >> 4) + 0x111
                disp = ((b2 & 0x0F) << 8 | b3) + 1
            else:
                # Two bytes, length 2..16.
                need(1, "short")
                b1 = data[src]
                src += 1
                length = category + 1
                disp = ((b0 & 0x0F) << 8 | b1) + 1

            if disp > pos:
                raise LZError(
                    f"LZ11 reference at src 0x{src:x} reaches 0x{disp:x} bytes "
                    f"back, past the start of the output (pos 0x{pos:x})"
                )
            start = pos - disp
            end = min(pos + length, out_size)
            while pos < end:
                out[pos] = out[start]
                pos += 1
                start += 1
            flags = (flags << 1) & 0xFF
    return out


# --------------------------------------------------------------------------
# Compression
# --------------------------------------------------------------------------
def compress(data: bytes | bytearray, marker: int = Marker.LZ10, *,
             search_depth: int = DEFAULT_SEARCH_DEPTH) -> bytearray:
    """Compress with the scheme named by *marker*."""
    if marker in (Marker.LZ10, Marker.LZ10_ARM9_STATIC):
        return compress_lz10(data, search_depth=search_depth)
    if marker == Marker.LZ11:
        return compress_lz11(data, search_depth=search_depth)
    raise LZError(f"unsupported LZ77 marker 0x{marker:02x}")


def header_of(marker: int, size: int) -> bytes:
    """Build the 4-byte LZ77 header describing *size* decompressed bytes."""
    if size >= 1 << 24:
        raise LZError("size does not fit the 24-bit LZ77 header field")
    if marker == Marker.LZ10_ARM9_STATIC:
        marker = Marker.LZ10
    return bytes((marker & 0xFF, size & 0xFF, (size >> 8) & 0xFF, (size >> 16) & 0xFF))


def compress_lz10(data: bytes | bytearray, *,
                  search_depth: int = DEFAULT_SEARCH_DEPTH) -> bytearray:
    """Greedy LZ10 encoder.

    Output layout matches the Nitro encoder: a 4-byte header, then blocks of a
    single flag byte followed by up to eight items.  A block that runs out of
    input keeps the unused flag bits cleared and emits no trailing bytes.
    """
    size = len(data)
    if size >= 1 << 24:
        raise LZError(f"LZ10 cannot encode 0x{size:x} bytes (24-bit size field)")
    if size == 0:
        return bytearray()

    out = bytearray(header_of(Marker.LZ10, size))
    finder = _MatchFinder(data, search_depth)
    pos = 0

    while pos < size:
        flag_pos = len(out)
        out.append(0)
        flags = 0
        for bit in range(8):
            if pos >= size:
                break
            length, disp = finder.find(pos, LZ10_MAX_LENGTH)
            if length >= LZ10_MIN_LENGTH:
                flags |= 1 << (7 - bit)
                ref = ((length - 3) << 12) | (disp - 1)
                out.append(ref & 0xFF)
                out.append((ref >> 8) & 0xFF)
                pos += length
            else:
                out.append(data[pos])
                pos += 1
        out[flag_pos] = flags
    return out


def compress_lz11(data: bytes | bytearray, *,
                  search_depth: int = DEFAULT_SEARCH_DEPTH) -> bytearray:
    """Greedy LZ11 encoder with the same block layout as :func:`compress_lz10`."""
    size = len(data)
    if size >= 1 << 24:
        raise LZError(f"LZ11 cannot encode 0x{size:x} bytes (24-bit size field)")
    if size == 0:
        return bytearray()

    out = bytearray(header_of(Marker.LZ11, size))
    finder = _MatchFinder(data, search_depth)
    pos = 0
    max_length = min(LZ11_LONG_MAX, size)

    while pos < size:
        flag_pos = len(out)
        out.append(0)
        flags = 0
        for bit in range(8):
            if pos >= size:
                break
            length, disp = finder.find(pos, max_length)
            if length >= LZ11_MIN_LENGTH:
                flags |= 1 << (7 - bit)
                out.extend(encode_lz11_reference(length, disp))
                pos += length
            else:
                out.append(data[pos])
                pos += 1
        out[flag_pos] = flags
    return out


def encode_lz11_reference(length: int, disp: int) -> bytes:
    """Encode one LZ11 back-reference into its two-, three- or four-byte form."""
    if not 0 < disp <= WINDOW_SIZE:
        raise LZError(f"LZ11 distance 0x{disp:x} outside the 4096-byte window")
    d = disp - 1

    if length <= 0x10:
        if length < LZ11_MIN_LENGTH:
            raise LZError(f"LZ11 cannot encode a {length}-byte reference")
        # ind == length - 1, i.e. 2..15; two bytes total.
        return bytes((
            ((length - 1) << 4) | (d >> 8),
            d & 0xFF,
        ))
    if length <= LZ11_MID_MAX:
        # ind == 0; nl = length - 0x11 in 12 bits; three bytes total.
        nl = length - 0x11
        return bytes((
            (nl >> 4) & 0x0F,
            ((nl & 0x0F) << 4) | (d >> 8),
            d & 0xFF,
        ))
    if length <= LZ11_LONG_MAX:
        # ind == 1; nl = length - 0x111 in 16 bits; four bytes total.
        nl = length - 0x111
        return bytes((
            0x10 | ((nl >> 12) & 0x0F),
            (nl >> 4) & 0xFF,
            ((nl & 0x0F) << 4) | (d >> 8),
            d & 0xFF,
        ))
    raise LZError(f"LZ11 length {length} exceeds 0x{LZ11_LONG_MAX:x}")


class _MatchFinder:
    """Sliding-window match search over a 3-byte hash chain.

    A naive "scan the whole 4096-byte window at every position" search is
    O(n * window) and, in Python, would take hours on a multi-megabyte ARM9.
    Hashing three bytes at a time and walking the resulting chain (bounded by
    ``search_depth``) brings a 4 MB binary down to a few seconds while still
    finding the long matches that actually matter for ratio.
    """

    __slots__ = ("_data", "_size", "_heads", "_prev", "_depth")

    def __init__(self, data: bytes | bytearray, search_depth: int) -> None:
        self._data = bytes(data)
        self._size = len(data)
        self._heads = [-1] * _HASH_SIZE
        self._prev = [0] * max(self._size, 1)
        self._depth = max(1, search_depth)

    def _hash3(self, pos: int) -> int:
        d = self._data
        return ((d[pos] << _HASH_SHIFT * 2) ^ (d[pos + 1] << _HASH_SHIFT) ^ d[pos + 2]) & (_HASH_SIZE - 1)

    def find(self, pos: int, max_length: int) -> tuple[int, int]:
        """Longest back-reference at *pos*, capped at *max_length*.

        Returns ``(0, 0)`` when nothing of the minimum length is available.
        """
        data = self._data
        size = self._size
        if pos + LZ10_MIN_LENGTH > size:
            self._advance(pos)
            return 0, 0

        cap = min(max_length, size - pos, WINDOW_SIZE)
        if cap < LZ10_MIN_LENGTH:
            self._advance(pos)
            return 0, 0

        h = self._hash3(pos)
        head = self._heads[h]
        prev = self._prev

        best_len = 0
        best_disp = 0
        candidate = head
        walked = 0
        # Nearest-first walk, so on a length tie the smaller distance wins --
        # the same tie-break the Nitro encoder applies.
        while candidate >= 0 and walked < self._depth:
            disp = pos - candidate
            if disp > WINDOW_SIZE:
                break
            length = 0
            if data[candidate] == data[pos]:
                while length < cap and data[candidate + length] == data[pos + length]:
                    length += 1
                if length > best_len:
                    best_len = length
                    best_disp = disp
                    if best_len == cap:
                        break
            candidate = prev[candidate]
            walked += 1

        self._advance(pos, h)
        if best_len < LZ10_MIN_LENGTH:
            return 0, 0
        return best_len, best_disp

    def _advance(self, pos: int, precomputed_hash: int | None = None) -> None:
        """Insert *pos* into the hash chain."""
        if pos + 2 >= self._size:
            return
        h = self._hash3(pos) if precomputed_hash is None else precomputed_hash
        self._prev[pos] = self._heads[h]
        self._heads[h] = pos


def recompression_matches(
    original: bytes | bytearray,
    data: bytes | bytearray,
    marker: int,
    *,
    search_depth: int = DEFAULT_SEARCH_DEPTH,
) -> bool:
    """True when re-encoding *data* reproduces *original* byte for byte.

    The build calls this before it trusts a regenerated blob.  When it returns
    False the Makefile falls back to linking the pristine compressed bytes from
    ``data/`` instead, which keeps the rebuild matching without anyone having
    to reverse-engineer Sega's exact compressor settings.
    """
    try:
        return bytes(compress(data, marker, search_depth=search_depth)) == bytes(original)
    except LZError:
        return False


def _selfcheck() -> None:
    """Round-trip every encoder against its decoder on synthetic patterns."""
    import os
    import random

    rng = random.Random(0xC0FFEE)
    patterns = [
        b"",
        b"\x00",
        b"\x00" * 4096,
        bytes(range(256)) * 4,
        os.urandom(1024),
        b"abcabcabc" * 200,
        bytes(rng.randrange(2) for _ in range(2048)),
        bytes(rng.randrange(256) for _ in range(8192)),
    ]
    for pattern in patterns:
        for marker in (Marker.LZ10, Marker.LZ11):
            packed = compress(pattern, marker)
            if pattern:
                assert probe(packed).compressed, (marker, len(pattern))
                unpacked = decompress(packed, marker)
                assert unpacked == pattern, (marker, len(pattern))
                assert recompression_matches(packed, pattern, marker)
    return None


if __name__ == "__main__":  # pragma: no cover
    _selfcheck()
    print("lz77 self-check: OK")
