"""The icon/title banner (``icon_title.bin`` in Nitro terms).

The banner is what the DS firmware shows on the save-game menu: a 48x32 4-bpp
icon, its 16-colour palette, and up to eight localised titles in UTF-16LE.  It
sits at the offset named by header field 0x06C and is *not* part of the ARM9 or
the Nitro file system, which is why it needs its own extractor.

Banner versions:

=====  ==========  =========  ==========================
ver    size        extra      languages
=====  ==========  =========  ==========================
1      0x0840      --         JP EN FR DE IT ES
2      0x0940      ZH         + Chinese
3      0x0A40      KO         + Korean
4      0x1240      DSi        + DSi titles, 8-frame anim
=====  ==========  =========  ==========================

The icon is 32x32 pixels at 4 bpp, which is 0x200 bytes -- the field size is
what pins the geometry, and getting it wrong silently walks the icon into the
palette and the titles.  The palette is 16 BGR555 entries at 0x220, and the
titles start at 0x240, eight bytes short of which lands you in the icon.

Sonic Rush shipped before v2 banners existed, so a retail dump carries a v1
banner; the version field is still read rather than assumed, so that DSi
re-releases and homebrew are handled too.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from .crc import crc16
from .util import p16, u16

__all__ = ["Banner", "BANNER_VERSION_SIZES", "TITLE_SLOTS"]

BANNER_VERSION_SIZES = {1: 0x840, 2: 0x940, 3: 0xA40, 4: 0x1240}

#: ``(offset, language)`` for each title slot, in banner order.
TITLE_SLOTS: tuple[tuple[int, str], ...] = (
    (0x240, "japanese"),
    (0x340, "english"),
    (0x440, "french"),
    (0x540, "german"),
    (0x540 + 0x100, "italian"),
    (0x740, "spanish"),
    (0x840, "chinese"),
    (0x940, "korean"),
)

_TITLE_LEN = 128  # UTF-16LE code units per title

#: Icon is 32x32 at 4 bpp: 32 tiles of 8x8, two pixels per byte.
ICON_OFFSET = 0x020
ICON_SIZE = 0x200
PALETTE_OFFSET = 0x220
PALETTE_ENTRIES = 16
TITLE_OFFSET = 0x240


@dataclasses.dataclass
class Banner:
    """Parsed icon/title banner."""

    raw: bytes
    version: int = 1
    crc_v1: int = 0
    crc_v2: int = 0
    crc_v3: int = 0
    crc_dsi: int = 0
    reserved: bytes = b""
    icon: bytes = b""
    palette: tuple[int, ...] = ()
    titles: dict[str, str] = dataclasses.field(default_factory=dict)

    @classmethod
    def parse(cls, data: bytes) -> "Banner":
        """Parse a banner from the bytes found at the header's banner offset."""
        if len(data) < 0x20:
            raise ValueError(f"banner needs at least 0x20 bytes, got {len(data)}")
        version = u16(data, 0x000)
        if version == 0:
            # Some homebrew leaves the version field clear; treat as v1.
            version = 1
        if version not in BANNER_VERSION_SIZES:
            raise ValueError(f"unknown banner version {version}")
        want = BANNER_VERSION_SIZES[version]
        if len(data) < want:
            raise ValueError(
                f"banner version {version} needs 0x{want:x} bytes, got 0x{len(data):x}"
            )
        raw = bytes(data[:want])

        titles: dict[str, str] = {}
        for offset, language in TITLE_SLOTS:
            if offset + 0x100 > len(raw):
                continue
            if version >= 2 and language in ("chinese",):
                pass  # present
            elif version < 2 and language in ("chinese", "korean"):
                continue
            blob = raw[offset : offset + _TITLE_LEN * 2]
            text = blob.decode("utf-16-le", errors="replace")
            titles[language] = text.split("\x00", 1)[0].strip()

        return cls(
            raw=raw,
            version=version,
            crc_v1=u16(raw, 0x002),
            crc_v2=u16(raw, 0x004) if len(raw) > 0x006 else 0,
            crc_v3=u16(raw, 0x006) if len(raw) > 0x008 else 0,
            crc_dsi=u16(raw, 0x008) if len(raw) > 0x00A else 0,
            reserved=raw[0x00A : 0x020],
            icon=raw[ICON_OFFSET : ICON_OFFSET + ICON_SIZE],
            palette=tuple(u16(raw, PALETTE_OFFSET + i * 2) for i in range(16)),
            titles=titles,
        )

    # ------------------------------------------------------------------ art
    def decode_icon(self) -> Optional[list[list[tuple[int, int, int]]]]:
        """Expand the 4-bpp tiled icon into rows of ``(r, g, b)`` pixels.

        The icon is stored as 32 8x8 tiles in raster order (4 across, 8 down)
        with two pixels per byte, low nibble first -- the same layout the GBA
        used for 4-bpp object tiles.  Colour index 0 is transparent by
        convention; the palette entry itself is ignored.
        """
        if len(self.icon) < ICON_SIZE or len(self.palette) < PALETTE_ENTRIES:
            return None
        rows: list[list[tuple[int, int, int]]] = []
        for tile_row in range(4):
            for pixel_row in range(8):
                row: list[tuple[int, int, int]] = []
                for tile_col in range(4):
                    base = (tile_row * 4 + tile_col) * 32 + pixel_row * 4
                    for byte_index in range(4):
                        byte = self.icon[base + byte_index]
                        for nibble in (byte & 0x0F, byte >> 4):
                            colour = self.palette[nibble]
                            # BGR555: 5 bits each, MSB is a transparency hint
                            # on some firmwares and is dropped here.
                            row.append(
                                (
                                    ((colour & 0x1F) * 255) // 31,
                                    (((colour >> 5) & 0x1F) * 255) // 31,
                                    (((colour >> 10) & 0x1F) * 255) // 31,
                                )
                            )
                rows.append(row)
        return rows

    def icon_as_ppm(self) -> bytes:
        """Render the icon to a binary PPM, so a dump can be eyeballed."""
        rows = self.decode_icon()
        if rows is None:
            raise ValueError("banner has no usable icon data")
        height = len(rows)
        width = len(rows[0])
        out = bytearray(f"P6\n{width} {height}\n255\n".encode("ascii"))
        for row in rows:
            for r, g, b in row:
                out.extend((r, g, b))
        return bytes(out)

    # ---------------------------------------------------------------- check
    #: Byte ranges each stored CRC covers, keyed by banner version.
    #:
    #: Each range *includes* the offset the CRC itself lives at, so the field
    #: must be zeroed before computing -- otherwise the checksum depends on its
    #: own value and never verifies.
    #:
    #: These are the ranges reported by established DS tooling.  Treat
    #: :meth:`verify_crcs` as advisory on a retail dump until it has been
    #: confirmed against a known-good banner; :meth:`sign` and
    #: :meth:`verify_crcs` share this one table, so they can never disagree
    #: with each other even if the values here need correcting.
    CRC_RANGES: tuple[tuple[int, int, int], ...] = (
        # (version, field offset, exclusive end of the covered range)
        (1, 0x002, 0x200),
        (2, 0x004, 0x340),
        (3, 0x006, 0x440),
        (4, 0x008, 0x1240),
    )

    def crc_range(self, version: int) -> Optional[tuple[int, int]]:
        for entry_version, field, end in self.CRC_RANGES:
            if entry_version == version:
                return field, end
        return None

    def compute_crc(self, version: int) -> Optional[int]:
        """Recompute one stored CRC, with its own field zeroed first."""
        bounds = self.crc_range(version)
        if bounds is None:
            return None
        field, end = bounds
        if end > len(self.raw) or field + 2 > len(self.raw):
            return None
        blob = bytearray(self.raw[:end])
        blob[field : field + 2] = b"\x00\x00"
        return crc16(bytes(blob))

    def stored_crc(self, version: int) -> int:
        bounds = self.crc_range(version)
        if bounds is None or bounds[0] + 2 > len(self.raw):
            return 0
        return u16(self.raw, bounds[0])

    def verify_crcs(self) -> dict[str, bool]:
        """Recompute each stored CRC that this banner version actually uses."""
        result: dict[str, bool] = {}
        names = {1: "crc_v1", 2: "crc_v2", 3: "crc_v3", 4: "crc_dsi"}
        for version in range(1, self.version + 1):
            stored = self.stored_crc(version)
            computed = self.compute_crc(version)
            if computed is None or stored == 0:
                continue
            result[names[version]] = stored == computed
        return result

    def sign(self) -> None:
        """Recompute whichever CRC fields this banner version actually uses.

        Fields are filled in ascending order and each is zeroed while the
        *later* ones are computed, matching how the firmware reads them.
        """
        blob = bytearray(self.raw)
        names = {1: "crc_v1", 2: "crc_v2", 3: "crc_v3", 4: "crc_dsi"}
        for version in range(1, self.version + 1):
            bounds = self.crc_range(version)
            if bounds is None:
                continue
            field, end = bounds
            if end > len(blob):
                continue
            blob[field : field + 2] = b"\x00\x00"
            blob[field : field + 2] = p16(crc16(bytes(blob[:end])))
        self.raw = bytes(blob)
        self.crc_v1 = u16(self.raw, 0x002)
        self.crc_v2 = u16(self.raw, 0x004) if len(self.raw) > 0x006 else 0
        self.crc_v3 = u16(self.raw, 0x006) if len(self.raw) > 0x008 else 0
        self.crc_dsi = u16(self.raw, 0x008) if len(self.raw) > 0x00A else 0
        _ = names

    def describe(self) -> str:
        lines = [
            f"Banner version : {self.version} (0x{len(self.raw):x} bytes)",
            f"CRC16 v1       : 0x{self.crc_v1:04x}",
        ]
        if self.crc_v2:
            lines.append(f"CRC16 v2       : 0x{self.crc_v2:04x}")
        if self.crc_v3:
            lines.append(f"CRC16 v3       : 0x{self.crc_v3:04x}")
        checks = self.verify_crcs()
        for name, ok in checks.items():
            lines.append(f"  {name:8s} {'ok' if ok else 'MISMATCH'}")
        for language, text in self.titles.items():
            if text:
                lines.append(f"Title[{language:8s}] : {text}")
        return "\n".join(lines)
