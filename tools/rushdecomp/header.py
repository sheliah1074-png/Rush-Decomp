"""The DS cartridge header -- the first 0x4000 bytes of every ``.nds`` image.

Layout follows the published cartridge specification.  Only the first 0x160
bytes are meaningful on a retail NDS card; 0x160..0x17F is multiboot/debug
scratch, 0x180..0x1BF is the DSi extension block, and the rest up to the 0x4000
header size is padding.  Immediately after the header sits the 0x2000-byte
secure area at 0x4000.

The two CRCs at 0x15C/0x15E are what a flash cart checks before it will boot
an image, so a rebuild has to get them right; :func:`Header.sign` recomputes
them from the current field values.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from .crc import crc16
from .util import p16, u16, u32, u64

__all__ = [
    "Header",
    "HEADER_SIZE",
    "SECURE_AREA_OFFSET",
    "SECURE_AREA_SIZE",
    "declared_extents",
    "required_length",
    "size_report",
    "looks_like_nds",
]

HEADER_SIZE = 0x4000
SECURE_AREA_OFFSET = 0x4000
SECURE_AREA_SIZE = 0x2000

#: Offsets of every scalar field, so that a rebuild can be diffed field by
#: field against the base ROM instead of as one opaque blob.
FIELD_OFFSETS = {
    "game_title": 0x000,
    "game_code": 0x00C,
    "maker_code": 0x010,
    "unit_code": 0x012,
    "encryption_seed_select": 0x013,
    "device_capacity": 0x014,
    "region": 0x01E,
    "rom_version": 0x01F,
    "autostart": 0x020,
    "arm9_rom_offset": 0x024,
    "arm9_entry_address": 0x028,
    "arm9_ram_address": 0x02C,
    "arm9_size": 0x030,
    "arm7_rom_offset": 0x034,
    "arm7_entry_address": 0x038,
    "arm7_ram_address": 0x03C,
    "arm7_size": 0x040,
    "fnt_offset": 0x044,
    "fnt_size": 0x048,
    "fat_offset": 0x04C,
    "fat_size": 0x050,
    "arm9_overlay_offset": 0x054,
    "arm9_overlay_size": 0x058,
    "arm7_overlay_offset": 0x05C,
    "arm7_overlay_size": 0x060,
    "normal_card_control": 0x064,
    "secure_card_control": 0x068,
    "banner_offset": 0x06C,
    "secure_area_crc": 0x070,
    "secure_area_delay": 0x072,
    "arm9_auto_load": 0x074,
    "arm7_auto_load": 0x078,
    "secure_disable": 0x07C,
    "total_used_rom_size": 0x084,
    "header_size": 0x088,
    "logo_crc": 0x15C,
    "header_crc": 0x15E,
}


def _decode_string(raw: bytes) -> str:
    """Decode a fixed-width, NUL-padded ASCII field."""
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


@dataclasses.dataclass
class Header:
    """Parsed representation of the 0x4000-byte cartridge header."""

    raw: bytes

    # Identity ------------------------------------------------------------
    game_title: str = ""
    game_code: str = ""
    maker_code: str = ""
    unit_code: int = 0
    encryption_seed_select: int = 0
    device_capacity: int = 0
    region: int = 0
    rom_version: int = 0
    autostart: int = 0

    # ARM9 ------------------------------------------------------------------
    arm9_rom_offset: int = 0
    arm9_entry_address: int = 0
    arm9_ram_address: int = 0
    arm9_size: int = 0

    # ARM7 ------------------------------------------------------------------
    arm7_rom_offset: int = 0
    arm7_entry_address: int = 0
    arm7_ram_address: int = 0
    arm7_size: int = 0

    # File system -----------------------------------------------------------
    fnt_offset: int = 0
    fnt_size: int = 0
    fat_offset: int = 0
    fat_size: int = 0
    banner_offset: int = 0

    # Overlays --------------------------------------------------------------
    arm9_overlay_offset: int = 0
    arm9_overlay_size: int = 0
    arm7_overlay_offset: int = 0
    arm7_overlay_size: int = 0

    # Card control / secure area -------------------------------------------
    normal_card_control: int = 0
    secure_card_control: int = 0
    secure_area_crc: int = 0
    secure_area_delay: int = 0
    arm9_auto_load: int = 0
    arm7_auto_load: int = 0
    secure_disable: int = 0

    # Extent ----------------------------------------------------------------
    total_used_rom_size: int = 0
    header_size: int = HEADER_SIZE
    logo_crc: int = 0
    header_crc: int = 0

    # ------------------------------------------------------------------ ctor
    @classmethod
    def parse(cls, data: bytes) -> "Header":
        """Parse a header from the start of a cartridge image.

        Only 0x160 bytes are read; the surrounding padding is retained in
        ``raw`` so that a rebuild reproduces it unchanged.
        """
        if len(data) < 0x160:
            raise ValueError(
                f"file is {len(data)} bytes; a DS cartridge image needs at "
                "least 0x160 for its header"
            )
        raw = bytes(data[:HEADER_SIZE]) if len(data) >= HEADER_SIZE else bytes(data)
        padded = raw + bytes(HEADER_SIZE - len(raw))

        def s(off: int, width: int) -> str:
            return _decode_string(data[off : off + width])

        header = cls(raw=padded)
        header.game_title = s(0x000, 12)
        header.game_code = s(0x00C, 4)
        header.maker_code = s(0x010, 2)
        header.unit_code = data[0x012]
        header.encryption_seed_select = data[0x013]
        header.device_capacity = data[0x014]
        header.region = data[0x01E]
        header.rom_version = data[0x01F]
        header.autostart = data[0x020]

        header.arm9_rom_offset = u32(data, 0x024)
        header.arm9_entry_address = u32(data, 0x028)
        header.arm9_ram_address = u32(data, 0x02C)
        header.arm9_size = u32(data, 0x030)

        header.arm7_rom_offset = u32(data, 0x034)
        header.arm7_entry_address = u32(data, 0x038)
        header.arm7_ram_address = u32(data, 0x03C)
        header.arm7_size = u32(data, 0x040)

        header.fnt_offset = u32(data, 0x044)
        header.fnt_size = u32(data, 0x048)
        header.fat_offset = u32(data, 0x04C)
        header.fat_size = u32(data, 0x050)

        header.arm9_overlay_offset = u32(data, 0x054)
        header.arm9_overlay_size = u32(data, 0x058)
        header.arm7_overlay_offset = u32(data, 0x05C)
        header.arm7_overlay_size = u32(data, 0x060)

        header.normal_card_control = u32(data, 0x064)
        header.secure_card_control = u32(data, 0x068)
        header.banner_offset = u32(data, 0x06C)
        header.secure_area_crc = u16(data, 0x070)
        header.secure_area_delay = u16(data, 0x072)
        header.arm9_auto_load = u32(data, 0x074)
        header.arm7_auto_load = u32(data, 0x078)
        header.secure_disable = u64(data, 0x07C)

        header.total_used_rom_size = u32(data, 0x084)
        header.header_size = u32(data, 0x088) or HEADER_SIZE
        header.logo_crc = u16(data, 0x15C)
        header.header_crc = u16(data, 0x15E)
        return header

    # ------------------------------------------------------------- derived
    @property
    def rom_capacity(self) -> int:
        """Cartridge capacity in bytes, from the ``device_capacity`` exponent."""
        return 0x20000 << (self.device_capacity & 0x1F)

    @property
    def logo(self) -> bytes:
        """The 156-byte Nintendo logo bitmap at 0x0C0."""
        return self.raw[0x0C0 : 0x15C]

    @property
    def reserved_08c(self) -> bytes:
        """Reserved run at 0x08C, checked for zero on a retail image."""
        return self.raw[0x08C : 0x0C0]

    @property
    def arm9_is_compressed(self) -> bool:
        """True when the ARM9 uses the static-compression marker convention.

        The definitive test is the marker byte at ``arm9_rom_offset`` (0x30) and
        the 12-byte footer, which :mod:`rushdecomp.arm9` inspects.  This
        property is a cheap pre-check used by reporting code.
        """
        return self.arm9_size > 12

    @property
    def banner_size(self) -> int:
        """Banner length implied by its version field."""
        return 0x840

    @property
    def secure_area_end(self) -> int:
        return SECURE_AREA_OFFSET + SECURE_AREA_SIZE

    def describe(self) -> str:
        """Human-readable multi-line summary, used by ``sr-extract --report``."""
        lines = [
            f"Game title            : {self.game_title!r}",
            f"Game code             : {self.game_code!r}",
            f"Maker code            : {self.maker_code!r}",
            f"Unit code             : {self.unit_code} "
            f"({'NDS' if self.unit_code == 0 else 'DSi-enhanced' if self.unit_code == 2 else 'DSi-exclusive'})",
            f"Device capacity       : {self.device_capacity} "
            f"(0x{self.rom_capacity:x} = {self.rom_capacity // 0x100000} MiB)",
            f"Region                : {self.region}",
            f"ROM version           : {self.rom_version}",
            f"Autostart             : 0x{self.autostart:02x}",
            "",
            f"ARM9  rom 0x{self.arm9_rom_offset:08x}  size 0x{self.arm9_size:08x}"
            f"  entry 0x{self.arm9_entry_address:08x}  ram 0x{self.arm9_ram_address:08x}",
            f"ARM7  rom 0x{self.arm7_rom_offset:08x}  size 0x{self.arm7_size:08x}"
            f"  entry 0x{self.arm7_entry_address:08x}  ram 0x{self.arm7_ram_address:08x}",
            f"FNT   rom 0x{self.fnt_offset:08x}  size 0x{self.fnt_size:08x}",
            f"FAT   rom 0x{self.fat_offset:08x}  size 0x{self.fat_size:08x}"
            f"  ({self.fat_size // 8} entries)",
            f"Y9    rom 0x{self.arm9_overlay_offset:08x}  size 0x{self.arm9_overlay_size:08x}"
            f"  ({self.arm9_overlay_size // 32} overlays)",
            f"Y7    rom 0x{self.arm7_overlay_offset:08x}  size 0x{self.arm7_overlay_size:08x}"
            f"  ({self.arm7_overlay_size // 32} overlays)",
            f"Banner rom 0x{self.banner_offset:08x}",
            f"Header size         : 0x{self.header_size:08x}",
            f"Logo CRC16          : 0x{self.logo_crc:04x}",
            f"Header CRC16        : 0x{self.header_crc:04x}",
        ]
        return "\n".join(lines)

    # ---------------------------------------------------------------- write
    def to_bytes(self) -> bytearray:
        """Serialise back to 0x4000 bytes, preserving all padding verbatim."""
        out = bytearray(self.raw[:HEADER_SIZE])
        out += bytes(HEADER_SIZE - len(out))

        def put_str(off: int, width: int, value: str) -> None:
            encoded = value.encode("ascii", errors="replace")[:width]
            out[off : off + width] = encoded + bytes(width - len(encoded))

        put_str(0x000, 12, self.game_title)
        put_str(0x00C, 4, self.game_code)
        put_str(0x010, 2, self.maker_code)
        out[0x012] = self.unit_code & 0xFF
        out[0x013] = self.encryption_seed_select & 0xFF
        out[0x014] = self.device_capacity & 0xFF
        out[0x01E] = self.region & 0xFF
        out[0x01F] = self.rom_version & 0xFF
        out[0x020] = self.autostart & 0xFF

        def put32(off: int, value: int) -> None:
            out[off : off + 4] = bytes(
                (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF)
            )

        put32(0x024, self.arm9_rom_offset)
        put32(0x028, self.arm9_entry_address)
        put32(0x02C, self.arm9_ram_address)
        put32(0x030, self.arm9_size)
        put32(0x034, self.arm7_rom_offset)
        put32(0x038, self.arm7_entry_address)
        put32(0x03C, self.arm7_ram_address)
        put32(0x040, self.arm7_size)
        put32(0x044, self.fnt_offset)
        put32(0x048, self.fnt_size)
        put32(0x04C, self.fat_offset)
        put32(0x050, self.fat_size)
        put32(0x054, self.arm9_overlay_offset)
        put32(0x058, self.arm9_overlay_size)
        put32(0x05C, self.arm7_overlay_offset)
        put32(0x060, self.arm7_overlay_size)
        put32(0x064, self.normal_card_control)
        put32(0x068, self.secure_card_control)
        put32(0x06C, self.banner_offset)
        out[0x070 : 0x072] = p16(self.secure_area_crc)
        out[0x072 : 0x074] = p16(self.secure_area_delay)
        put32(0x074, self.arm9_auto_load)
        put32(0x078, self.arm7_auto_load)
        out[0x07C : 0x084] = bytes(
            (self.secure_disable >> (8 * i)) & 0xFF for i in range(8)
        )
        put32(0x084, self.total_used_rom_size)
        put32(0x088, self.header_size)
        out[0x15C : 0x15E] = p16(self.logo_crc)
        out[0x15E : 0x160] = p16(self.header_crc)
        return out

    def sign(self, *, recompute_logo: bool = False) -> None:
        """Recompute the header (and optionally logo) CRC16 in place."""
        if recompute_logo:
            self.logo_crc = crc16(self.raw[0x0C0 : 0x15C])
        blob = self.to_bytes()
        self.header_crc = crc16(bytes(blob[:0x15E]))
        blob[0x15E : 0x160] = p16(self.header_crc)
        self.raw = bytes(blob)

    def verify_crcs(self) -> dict[str, bool]:
        """Check the stored CRCs against freshly computed ones."""
        blob = bytes(self.to_bytes())
        return {
            "header_crc": crc16(blob[:0x15E]) == self.header_crc,
            "logo_crc": crc16(blob[0x0C0 : 0x15C]) == self.logo_crc,
        }

    def field_diff(self, other: "Header") -> list[str]:
        """Field-by-field comparison, used to explain a non-matching rebuild."""
        out = []
        for field in dataclasses.fields(self):
            if field.name == "raw":
                continue
            a, b = getattr(self, field.name), getattr(other, field.name)
            if a != b:
                off = FIELD_OFFSETS.get(field.name)
                where = f"0x{off:03x}" if off is not None else "    "
                out.append(f"  {where}  {field.name}: {a!r} -> {b!r}")
        return out


def looks_like_nds(data: bytes) -> Optional[str]:
    """Return ``None`` if *data* plausibly is a DS image, else a reason string.

    This is the guard that stops someone pointing the toolchain at a save file
    or a homebrew blob and getting a confusing traceback three modules later.
    """
    if len(data) < 0x160:
        return f"only {len(data)} bytes; a DS image needs at least 0x160"
    unit = data[0x012]
    if unit not in (0x00, 0x02, 0x03):
        return f"unit code 0x{unit:02x} is not a DS/DSi value"
    capacity = data[0x014]
    if capacity > 0x0F:
        return f"device capacity code 0x{capacity:02x} is out of range"
    arm9_off = u32(data, 0x024)
    arm9_size = u32(data, 0x030)
    if arm9_off < 0x4000 or arm9_off + arm9_size > len(data):
        return (
            f"ARM9 extent 0x{arm9_off:x}..0x{arm9_off + arm9_size:x} does not "
            f"fit inside a {len(data):#x}-byte file"
        )
    arm7_off = u32(data, 0x034)
    arm7_size = u32(data, 0x040)
    if arm7_off + arm7_size > len(data):
        return "ARM7 extent runs past the end of the file"
    return None


def declared_extents(header: Header) -> list[tuple[str, int, int]]:
    """Every ``(name, start, end)`` region the header itself points at.

    Zero-length regions are dropped rather than reported as ending at their
    offset, so an absent overlay table cannot invent an extent or drag a
    maximum down to zero.
    """
    candidates = [
        ("header", 0, header.header_size or HEADER_SIZE),
        ("secure area", SECURE_AREA_OFFSET, header.secure_area_end),
        ("ARM9", header.arm9_rom_offset, header.arm9_rom_offset + header.arm9_size),
        ("ARM7", header.arm7_rom_offset, header.arm7_rom_offset + header.arm7_size),
        ("FNT", header.fnt_offset, header.fnt_offset + header.fnt_size),
        ("FAT", header.fat_offset, header.fat_offset + header.fat_size),
        (
            "Y9 overlays",
            header.arm9_overlay_offset,
            header.arm9_overlay_offset + header.arm9_overlay_size,
        ),
        (
            "Y7 overlays",
            header.arm7_overlay_offset,
            header.arm7_overlay_offset + header.arm7_overlay_size,
        ),
        ("banner", header.banner_offset, header.banner_offset + header.banner_size),
    ]
    return [(name, start, end) for name, start, end in candidates if end > start]


def required_length(header: Header, data_end: Optional[int] = None) -> tuple[int, str]:
    """The shortest image that still holds everything the tables reference.

    *data_end* lets a caller that parsed the FAT contribute the true end of the
    last Nitro file, which usually sits past every header-declared region and is
    the bound a trimming tool actually cuts at.  Returns ``(length, why)``.
    """
    extents = declared_extents(header)
    best_name, best_end = "header", HEADER_SIZE
    for name, _start, end in extents:
        if end > best_end:
            best_name, best_end = name, end
    if data_end is not None and data_end > best_end:
        return data_end, "last Nitro file in the FAT"
    return best_end, f"end of {best_name}"


def size_report(
    header: Header, actual_length: int, *, data_end: Optional[int] = None
) -> list[str]:
    """Classify an image's length against the header's own size claims.

    "Smaller than the cartridge" is ambiguous on its own: a *trimmed* dump has
    had its trailing padding removed and is complete, while a *truncated* dump
    is silently missing data and will produce a disassembly with a hole in it.
    Both look identical at a glance.  This separates them by asking what the
    image's own tables require, rather than by trusting ``total_used_rom_size``
    (0x084) alone -- homebrew and some bad dumps leave that field zero or
    stale, and it is not covered by the header CRC.
    """
    capacity = header.rom_capacity
    used = header.total_used_rom_size
    need, why = required_length(header, data_end)

    lines = [
        f"Cartridge capacity    : 0x{capacity:08x} ({capacity / 1048576:.0f} MiB)",
        f"Image length          : 0x{actual_length:08x} ({actual_length / 1048576:.2f} MiB)",
        f"Total used ROM size   : 0x{used:08x}"
        + ("" if used else "  (unset; common on homebrew)"),
        f"Data required by      : 0x{need:08x}  ({why})",
    ]

    if actual_length < need:
        lines.append(
            f"Verdict               : TRUNCATED -- 0x{need - actual_length:x} "
            f"({need - actual_length:,} bytes) of referenced data is missing; "
            "extraction will be incomplete"
        )
        return lines

    if used and actual_length < used:
        lines.append(
            f"Verdict               : SHORT of the header's own 'used' field by "
            f"0x{used - actual_length:x} bytes, but every referenced region is "
            "present; the field is likely stale"
        )
        return lines

    if actual_length == capacity:
        lines.append("Verdict               : full untrimmed dump")
    elif actual_length > capacity:
        lines.append(
            f"Verdict               : LONGER than capacity by 0x{actual_length - capacity:x} "
            "bytes -- the device_capacity code or the image is wrong"
        )
    else:
        lines.append(
            f"Verdict               : trimmed -- {capacity - actual_length:,} bytes of "
            f"trailing padding removed, all referenced data present "
            f"(0x{actual_length - need:x} of slack past it)"
        )
    return lines
