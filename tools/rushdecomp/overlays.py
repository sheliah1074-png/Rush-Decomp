"""Overlay tables (Y9 / Y7) and the overlay files they point at.

Overlays are the DS's answer to not having enough RAM for a whole game at once.
A table of 32-byte descriptors lives at the header's ARM9/ARM7 overlay offset;
each descriptor names a file in the Nitro file system, the RAM address and size
it loads to, its BSS size, and the address range of static initialisers that
``OS_InitOverlay`` runs after loading.  The game then swaps overlays in and out
as the player moves between levels -- for Sonic Rush that is how the per-zone
code and the boss routines share a single small window of ARM9 RAM.

Descriptor layout (32 bytes):

=====  =====================================================
0x00   Overlay ID
0x04   RAM address
0x08   RAM size (bits 0-23) | flags (bit 24 = compressed)
0x0C   BSS size
0x10   Static init start address
0x14   Static init end address
0x18   File ID (bits 0-15) | compression flag (bit 24)
0x1C   Reserved / file size when compressed
=====  =====================================================

The two places the compressed flag is documented are genuinely both used by
different SDK revisions, so :class:`Overlay` exposes ``compressed`` after
checking both and reports which one carried it.
"""

from __future__ import annotations

import dataclasses
from typing import Iterator, Optional, Sequence

from .util import u32

__all__ = ["Overlay", "OverlayTable", "OVERLAY_ENTRY_SIZE"]

OVERLAY_ENTRY_SIZE = 0x20

#: RAM size field masks.
_SIZE_MASK = 0x00FFFFFF
_SIZE_FLAG_SHIFT = 24


@dataclasses.dataclass
class Overlay:
    """One 32-byte overlay table entry."""

    overlay_id: int
    ram_address: int
    ram_size: int
    bss_size: int
    static_init_start: int
    static_init_end: int
    file_id: int
    reserved: int
    #: Which field carried the compression flag: ``"file_id"``, ``"ram_size"``
    #: or ``None``.  Recorded so a rebuild can put it back in the same place.
    compressed_flag_source: Optional[str] = None
    #: Index into the owning table, for stable naming of dumps.
    index: int = 0

    @classmethod
    def parse(cls, data: bytes, offset: int = 0, index: int = 0) -> "Overlay":
        if offset + OVERLAY_ENTRY_SIZE > len(data):
            raise ValueError(
                f"overlay entry {index} at 0x{offset:x} runs past the table"
            )
        raw_size = u32(data, offset + 0x08)
        raw_file = u32(data, offset + 0x18)
        compressed_by_file = bool((raw_file >> _SIZE_FLAG_SHIFT) & 1)
        compressed_by_size = bool((raw_size >> _SIZE_FLAG_SHIFT) & 1)
        if compressed_by_file:
            source: Optional[str] = "file_id"
        elif compressed_by_size:
            source = "ram_size"
        else:
            source = None
        return cls(
            overlay_id=u32(data, offset + 0x00),
            ram_address=u32(data, offset + 0x04),
            ram_size=raw_size & _SIZE_MASK,
            bss_size=u32(data, offset + 0x0C),
            static_init_start=u32(data, offset + 0x10),
            static_init_end=u32(data, offset + 0x14),
            file_id=raw_file & 0xFFFF,
            reserved=u32(data, offset + 0x1C),
            compressed_flag_source=source,
            index=index,
        )

    @property
    def compressed(self) -> bool:
        return self.compressed_flag_source is not None

    @property
    def static_init_size(self) -> int:
        """Bytes of static initialiser code, or 0 when the overlay has none."""
        if self.static_init_end <= self.static_init_start:
            return 0
        return self.static_init_end - self.static_init_start

    @property
    def total_ram_size(self) -> int:
        """RAM footprint including BSS."""
        return self.ram_size + self.bss_size

    def to_bytes(self) -> bytes:
        """Serialise back to the exact 32 bytes this entry was parsed from."""
        raw_size = self.ram_size & _SIZE_MASK
        raw_file = self.file_id & 0xFFFF
        if self.compressed_flag_source == "ram_size":
            raw_size |= 1 << _SIZE_FLAG_SHIFT
        elif self.compressed_flag_source == "file_id":
            raw_file |= 1 << _SIZE_FLAG_SHIFT

        def w(value: int) -> bytes:
            return bytes(
                (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF)
            )

        return (
            w(self.overlay_id)
            + w(self.ram_address)
            + w(raw_size)
            + w(self.bss_size)
            + w(self.static_init_start)
            + w(self.static_init_end)
            + w(raw_file)
            + w(self.reserved)
        )

    def label(self, prefix: str = "overlay") -> str:
        """Stable file/symbol name, e.g. ``overlay_007``."""
        return f"{prefix}_{self.overlay_id:03d}"


class OverlayTable:
    """The complete Y9 or Y7 table."""

    def __init__(self, entries: Sequence[Overlay], *, rom_offset: int = 0,
                 processor: str = "arm9") -> None:
        self.entries: list[Overlay] = list(entries)
        self.rom_offset = rom_offset
        self.processor = processor

    @classmethod
    def parse(cls, data: bytes, *, rom_offset: int = 0,
              processor: str = "arm9") -> "OverlayTable":
        if len(data) % OVERLAY_ENTRY_SIZE:
            raise ValueError(
                f"{processor} overlay table is 0x{len(data):x} bytes, which is "
                f"not a multiple of the 0x{OVERLAY_ENTRY_SIZE:x}-byte entry size"
            )
        entries = [
            Overlay.parse(data, off, index)
            for index, off in enumerate(range(0, len(data), OVERLAY_ENTRY_SIZE))
        ]
        return cls(entries, rom_offset=rom_offset, processor=processor)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[Overlay]:
        return iter(self.entries)

    def __getitem__(self, item: int) -> Overlay:
        return self.entries[item]

    def by_id(self, overlay_id: int) -> Optional[Overlay]:
        for entry in self.entries:
            if entry.overlay_id == overlay_id:
                return entry
        return None

    def file_ids(self) -> list[int]:
        """FAT indices this table references, in table order."""
        return [entry.file_id for entry in self.entries]

    def to_bytes(self) -> bytes:
        return b"".join(entry.to_bytes() for entry in self.entries)

    def describe(self) -> str:
        head = (
            f"{self.processor.upper()} overlay table at rom 0x{self.rom_offset:08x}: "
            f"{len(self.entries)} entries, "
            f"{sum(e.ram_size for e in self.entries) / 1024:.1f} KiB of code"
        )
        rows = [
            f"{'id':>4}  {'ram address':>10}  {'ram size':>9}  {'bss':>8}  "
            f"{'file id':>7}  {'cmp':>3}  {'static init'}"
        ]
        for entry in self.entries:
            init = (
                f"0x{entry.static_init_start:08x}-0x{entry.static_init_end:08x}"
                if entry.static_init_size
                else "--"
            )
            rows.append(
                f"{entry.overlay_id:>4}  0x{entry.ram_address:08x}  "
                f"0x{entry.ram_size:07x}  0x{entry.bss_size:06x}  "
                f"{entry.file_id:>7}  {'yes' if entry.compressed else ' no':>3}  {init}"
            )
        return "\n".join([head, *rows])

    def sanity_report(self) -> list[str]:
        """Flag table contents that usually mean a bad offset or a bad dump."""
        problems: list[str] = []
        seen_ids: set[int] = set()
        for entry in self.entries:
            if entry.overlay_id in seen_ids:
                problems.append(f"duplicate overlay ID {entry.overlay_id}")
            seen_ids.add(entry.overlay_id)
            if entry.ram_size == 0:
                problems.append(f"overlay {entry.overlay_id} has zero RAM size")
            if entry.ram_size and not entry.ram_size % 4 == 0:
                problems.append(
                    f"overlay {entry.overlay_id} RAM size 0x{entry.ram_size:x} "
                    "is not 4-byte aligned"
                )
            if entry.static_init_size and entry.static_init_size % 4:
                problems.append(
                    f"overlay {entry.overlay_id} static init range is not word aligned"
                )
        return problems
