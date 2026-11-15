"""CRC routines used by the Nintendo DS cartridge format.

Every checksum in a DS cartridge image -- the header CRC, the logo CRC, the
banner CRCs and the Nitro file-system CRCs -- is the same plain CRC-16/ARC
(poly 0xA001 reflected, init 0xFFFF, no final xor).  There is exactly one
exception worth knowing about: the secure area, which uses a CRC-16-CCITT
variant folded into the Blowfish key schedule.  Both are implemented here so
that a rebuild can reproduce either one byte for byte.
"""

from __future__ import annotations

__all__ = ["crc16", "crc16_ccitt", "crc_table"]

_CRC_POLY_REFLECTED = 0xA001


def crc_table() -> tuple[int, ...]:
    """Pre-compute the 256-entry reflected CRC-16/ARC table."""
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ _CRC_POLY_REFLECTED
            else:
                crc >>= 1
        table.append(crc & 0xFFFF)
    return tuple(table)


_TABLE = crc_table()


def crc16(data: bytes | bytearray, init: int = 0xFFFF) -> int:
    """CRC-16/ARC.  Used for the header, logo and banner checksums."""
    crc = init & 0xFFFF
    for byte in data:
        crc = (_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)) & 0xFFFF
    return crc


def crc16_ccitt(data: bytes | bytearray, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, non-reflected).

    This is the variant the DS secure-area check uses, and the one you need if
    you ever re-sign a cartridge image yourself.
    """
    crc = init & 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def banner_crcs(banner: bytes) -> dict[str, tuple[int, int, bool]]:
    """Check the up-to-four CRC16s stored in an icon/title banner.

    Returns ``{version: (field_offset, stored, ok)}``.  A v1 banner only
    carries the first entry; DSi-era v3/v4 banners carry all four, each
    covering a progressively longer prefix of the banner.
    """
    ranges = {1: (0x002, 0x020), 2: (0x004, 0x140), 3: (0x006, 0x120), 4: (0x008, 0x1240)}
    results: dict[str, tuple[int, int, bool]] = {}
    for version, (field, end) in ranges.items():
        if len(banner) < field + 2:
            continue
        stored = banner[field] | (banner[field + 1] << 8)
        results[version] = (field, stored, crc16(banner[:end]) == stored)
    return results
