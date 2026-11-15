"""The statically-compressed ARM9 binary and its 12-byte footer.

An NDS ARM9 is loaded and started before the Nitro SDK's own decompression code
is available, so a game that wants its ARM9 packed has to rely on the BIOS
doing it.  The convention the BIOS entry stub implements is:

1. The first byte of the ARM9 image is ``0x30`` instead of the usual LZ77
   ``0x10``.  ``0x30`` is not a valid LZ77 marker, which is the point: nothing
   that decompresses generically will touch it by accident.
2. Twelve bytes are appended after the compressed payload:

   =====  ==========================================================
   +0x00  u32  decompressed size, **excluding** this footer
   +0x04  u32  compressed size, **including** this footer
   +0x08  u32  verification value, ``0x5F030000 - (decomp >> 9)``
   =====  ==========================================================

   The third word exists so the BIOS can tell "this really is a statically
   compressed ARM9" from "the first byte happens to be 0x30".  The shift is an
   *arithmetic* right shift of ``-decompressed_size``, i.e. sign-extending.

3. ``DecompressARM9`` expands the payload in place at the ARM9 RAM address,
   backwards from the top, because the decompressed image is larger than the
   compressed one and would otherwise overwrite the source it is reading.

The verification word has two spellings in the wild -- the arithmetic form above
and a plain ``0x5F030000 - (decomp >> 9)`` with a logical shift -- because
different SDK revisions emitted different ones.  :func:`footer_magic_for` tries
both and :class:`ARM9Image` records which one matched, so that a rebuild writes
back the same spelling the original used.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from .lz77 import Marker, compress, decompress, probe
from .util import p32, u32

__all__ = [
    "ARM9Image",
    "ARM9_STATIC_MARKER",
    "FOOTER_SIZE",
    "FOOTER_MAGIC_BASE",
    "footer_magic_for",
    "verify_footer_magic",
]

ARM9_STATIC_MARKER = 0x30
FOOTER_SIZE = 12
FOOTER_MAGIC_BASE = 0x5F030000


def footer_magic_for(decompressed_size: int, *, arithmetic: bool = True) -> int:
    """Compute the footer's third word for a given decompressed size.

    ``arithmetic=True`` gives the sign-extending form used by the mainstream
    SDK; ``False`` gives the logical-shift variant some homebrew toolchains
    emit.
    """
    size = decompressed_size & 0xFFFFFFFF
    if arithmetic:
        # (uint32)(-(int32)size) >> 9, arithmetic, i.e. sign extending.
        negated = (-size) & 0xFFFFFFFF
        if negated & 0x80000000:
            shifted = (negated >> 9) | 0x7F8000  # keep the sign bits set
            shifted &= 0xFFFFFFFF
        else:
            shifted = negated >> 9
        return (FOOTER_MAGIC_BASE - shifted) & 0xFFFFFFFF
    return (FOOTER_MAGIC_BASE - (size >> 9)) & 0xFFFFFFFF


def verify_footer_magic(stored: int, decompressed_size: int) -> Optional[str]:
    """Return which spelling the stored magic matches, or ``None``."""
    if stored == footer_magic_for(decompressed_size, arithmetic=True):
        return "arithmetic"
    if stored == footer_magic_for(decompressed_size, arithmetic=False):
        return "logical"
    return None


@dataclasses.dataclass
class ARM9Image:
    """An ARM9 binary as it sits in the cartridge image, footer included."""

    #: Exactly the bytes the header's ``arm9_size`` covers.
    raw: bytes
    compressed: bool = False
    footer_present: bool = False
    footer_decompressed_size: int = 0
    footer_compressed_size: int = 0
    footer_magic: int = 0
    footer_magic_style: Optional[str] = None
    marker: int = 0
    #: Decompressed payload; equals ``raw`` when the image is not compressed.
    payload: bytes = b""

    @classmethod
    def parse(cls, raw: bytes) -> "ARM9Image":
        """Classify and unpack an ARM9 image.

        The footer is only trusted when all three of its words are consistent
        with each other *and* with the length of the buffer, because a
        false-positive footer would silently corrupt the decompression.
        """
        image = cls(raw=bytes(raw))
        if not raw:
            image.payload = b""
            return image

        image.marker = raw[0]
        if image.marker == ARM9_STATIC_MARKER and len(raw) > FOOTER_SIZE:
            footer_at = len(raw) - FOOTER_SIZE
            dec_size = u32(raw, footer_at)
            comp_size = u32(raw, footer_at + 4)
            magic = u32(raw, footer_at + 8)
            style = verify_footer_magic(magic, dec_size)
            if (
                style is not None
                and comp_size == len(raw)
                and 0 < dec_size < (1 << 24)
            ):
                image.compressed = True
                image.footer_present = True
                image.footer_decompressed_size = dec_size
                image.footer_compressed_size = comp_size
                image.footer_magic = magic
                image.footer_magic_style = style
                body = bytearray(raw[:footer_at])
                body[0] = Marker.LZ10
                info = probe(body)
                if info.compressed and info.decompressed_size == dec_size:
                    image.payload = bytes(decompress(body, Marker.LZ10))
                else:
                    # Header says compressed, payload says otherwise.  Keep the
                    # bytes as-is and let the caller see the inconsistency
                    # rather than raising halfway through an extraction run.
                    image.compressed = False
                    image.footer_present = False
                    image.payload = bytes(raw)
                return image

        info = probe(raw)
        if info.compressed:
            image.compressed = True
            image.payload = bytes(decompress(raw, info.marker))
        else:
            image.payload = bytes(raw)
        return image

    # ------------------------------------------------------------------ util
    @property
    def payload_size(self) -> int:
        return len(self.payload)

    @property
    def compression_ratio(self) -> float:
        if not self.payload:
            return 1.0
        return len(self.raw) / len(self.payload)

    def recompress(self, *, arithmetic_footer: Optional[bool] = None,
                   search_depth: int = 128) -> bytes:
        """Rebuild the on-media image, footer and all.

        ``arithmetic_footer`` defaults to whichever spelling the parsed image
        used, so a rebuild that changed nothing but a byte of code still
        produces a footer identical in form to the original.
        """
        if not self.compressed:
            return bytes(self.payload)
        style = arithmetic_footer
        if style is None:
            style = self.footer_magic_style != "logical"
        body = bytearray(compress(self.payload, Marker.LZ10, search_depth=search_depth))
        if not body:
            return b""
        body[0] = ARM9_STATIC_MARKER
        dec_size = len(self.payload)
        comp_size = len(body) + FOOTER_SIZE
        magic = footer_magic_for(dec_size, arithmetic=style)
        return bytes(body) + p32(dec_size) + p32(comp_size) + p32(magic)

    def describe(self) -> str:
        lines = [
            f"raw size              : 0x{len(self.raw):x} ({len(self.raw)} bytes)",
            f"marker byte           : 0x{self.marker:02x}"
            + ("  (ARM9 static-compression form)" if self.marker == ARM9_STATIC_MARKER else ""),
            f"compressed            : {self.compressed}",
        ]
        if self.footer_present:
            lines += [
                f"footer decompressed   : 0x{self.footer_decompressed_size:x}",
                f"footer compressed     : 0x{self.footer_compressed_size:x}",
                f"footer magic          : 0x{self.footer_magic:08x} "
                f"({self.footer_magic_style} shift)",
            ]
        if self.compressed:
            lines.append(
                f"payload size          : 0x{len(self.payload):x} "
                f"(ratio {self.compression_ratio:.3f})"
            )
        return "\n".join(lines)
