"""The Nitro ROM file system: FNT (names) plus FAT (extents).

A DS cartridge image carries a flat FAT of ``(start, end)`` pairs and a separate
hierarchical FNT that gives each FAT index a path.  The two are joined by file
ID: the FNT record for a file ends in a 16-bit index into the FAT.  Directories
are numbered from 0xF000 upward, and the root directory descriptor is always
the first 8 bytes of the FNT.

FAT entry, 8 bytes::

    0x00  u32  start offset in the cartridge image
    0x04  u32  end offset (exclusive)

A few SDK revisions stash flags in the top byte of the start word, so
:class:`FATEntry` keeps both the raw value and the masked offset and reports
which one it used.

FNT records:

* Directory descriptor (8 bytes, one per directory, starting with the root)::

      0x00  u32  offset of the first record of this directory's listing
      0x04  u16  first file ID inside this directory
      0x06  u16  parent directory ID (0xF000 for the root)

* Listing records, read from that offset until a zero length byte:

  * ``len & 0x80`` set -> directory: ``len & 0x7F`` name bytes, then u16 dir ID
  * otherwise -> file: ``len`` name bytes, then u16 file ID

Overlays have no FNT entry at all; their file IDs come from the overlay tables
and are usually allocated after the file-system range.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Iterator, Optional

from .util import u16, u32

__all__ = ["FATEntry", "FileEntry", "NitroFileSystem", "ROOT_DIR_ID", "FAT_ENTRY_SIZE"]

ROOT_DIR_ID = 0xF000
FAT_ENTRY_SIZE = 8

#: Directory descriptors live at ``0x08 * (dir_id - ROOT_DIR_ID)`` in the FNT.
_DIR_DESCRIPTOR_SIZE = 8


@dataclasses.dataclass
class FATEntry:
    """One 8-byte FAT record."""

    index: int
    raw_start: int
    raw_end: int

    @property
    def start(self) -> int:
        """Cartridge-image offset of the file's first byte."""
        # Some SDK builds OR a "this file is an overlay" flag into the top byte.
        return self.raw_start & 0x00FFFFFF if self.has_flags else self.raw_start

    @property
    def end(self) -> int:
        return self.raw_end

    @property
    def has_flags(self) -> bool:
        return bool(self.raw_start & 0xFF000000)

    @property
    def flags(self) -> int:
        return (self.raw_start & 0xFF000000) >> 24

    @property
    def size(self) -> int:
        """File length; zero-length and inverted entries report 0."""
        if self.end <= self.start:
            return 0
        return self.end - self.start

    def to_bytes(self) -> bytes:
        def w(value: int) -> bytes:
            return bytes(
                (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF)
            )

        return w(self.raw_start) + w(self.raw_end)


@dataclasses.dataclass
class FileEntry:
    """A file or directory recovered from the FNT."""

    file_id: int
    path: str
    is_directory: bool = False
    directory_id: int = 0

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def directory(self) -> str:
        return os.path.dirname(self.path)

    @property
    def extension(self) -> str:
        _, ext = os.path.splitext(self.name)
        return ext.lower()

    @property
    def safe_name(self) -> str:
        """Path with anything awkward replaced, for use as an on-disk name."""
        cleaned = "".join(c if (c.isalnum() or c in "._-/") else "_" for c in self.path)
        return cleaned.lstrip("/")


class NitroFileSystem:
    """Parsed FNT + FAT pair, joined into a path-indexed file list."""

    def __init__(
        self,
        fat: list[FATEntry],
        files: list[FileEntry],
        *,
        fnt_offset: int = 0,
        fat_offset: int = 0,
        warnings: Optional[list[str]] = None,
    ) -> None:
        self.fat = fat
        self.files = files
        self.fnt_offset = fnt_offset
        self.fat_offset = fat_offset
        self.warnings = warnings if warnings is not None else []
        self._by_path = {entry.path: entry for entry in files if not entry.is_directory}
        self._by_id = {entry.file_id: entry for entry in files if not entry.is_directory}

    # ------------------------------------------------------------------ ctor
    @classmethod
    def parse(cls, rom: bytes, *, fnt_offset: int, fnt_size: int,
              fat_offset: int, fat_size: int) -> "NitroFileSystem":
        warnings: list[str] = []
        fat = cls._parse_fat(rom, fat_offset, fat_size, warnings)
        files = cls._parse_fnt(rom, fnt_offset, fnt_size, warnings)
        # Drop files whose ID has no FAT entry, but keep them visible as a
        # warning: it usually means the dump was taken from a re-signed image.
        valid: list[FileEntry] = []
        for entry in files:
            if entry.is_directory:
                valid.append(entry)
                continue
            if entry.file_id >= len(fat):
                warnings.append(
                    f"FNT file {entry.path!r} has ID {entry.file_id} but the FAT "
                    f"only holds {len(fat)} entries"
                )
                continue
            valid.append(entry)
        return cls(
            fat, valid,
            fnt_offset=fnt_offset, fat_offset=fat_offset, warnings=warnings,
        )

    @staticmethod
    def _parse_fat(rom: bytes, offset: int, size: int,
                   warnings: list[str]) -> list[FATEntry]:
        if size % FAT_ENTRY_SIZE:
            warnings.append(
                f"FAT size 0x{size:x} is not a multiple of {FAT_ENTRY_SIZE}; "
                "trailing bytes ignored"
            )
        count = size // FAT_ENTRY_SIZE
        entries: list[FATEntry] = []
        for index in range(count):
            at = offset + index * FAT_ENTRY_SIZE
            if at + FAT_ENTRY_SIZE > len(rom):
                warnings.append(
                    f"FAT truncated after {index} of {count} entries "
                    f"(image is 0x{len(rom):x} bytes)"
                )
                break
            entries.append(
                FATEntry(index=index, raw_start=u32(rom, at), raw_end=u32(rom, at + 4))
            )
        return entries

    @staticmethod
    def _parse_fnt(rom: bytes, offset: int, size: int,
                   warnings: list[str]) -> list[FileEntry]:
        if size < _DIR_DESCRIPTOR_SIZE:
            warnings.append(f"FNT size 0x{size:x} is too small to hold a root descriptor")
            return []
        if offset + size > len(rom):
            warnings.append(
                f"FNT extent 0x{offset:x}..0x{offset + size:x} exceeds the image "
                f"(0x{len(rom):x} bytes); parsing what is present"
            )
        blob = rom[offset : offset + size]

        root_first_record = u32(blob, 0x00)
        root_first_file_id = u16(blob, 0x04)
        root_parent = u16(blob, 0x06)
        if root_parent != ROOT_DIR_ID:
            warnings.append(
                f"FNT root parent is 0x{root_parent:04x}, expected 0x{ROOT_DIR_ID:04x}"
            )
        dir_count = root_first_record // _DIR_DESCRIPTOR_SIZE
        if dir_count == 0:
            warnings.append("FNT root descriptor points into itself")
            dir_count = 1

        descriptors: list[tuple[int, int, int]] = []
        for index in range(dir_count):
            at = index * _DIR_DESCRIPTOR_SIZE
            if at + _DIR_DESCRIPTOR_SIZE > len(blob):
                break
            descriptors.append(
                (u32(blob, at), u16(blob, at + 4), u16(blob, at + 6))
            )

        files: list[FileEntry] = []
        files.append(
            FileEntry(file_id=-1, path="", is_directory=True, directory_id=ROOT_DIR_ID)
        )

        visited: set[int] = set()

        def walk(dir_index: int, path: str) -> None:
            dir_id = ROOT_DIR_ID + dir_index
            if dir_id in visited:
                warnings.append(f"FNT directory 0x{dir_id:04x} is referenced twice")
                return
            visited.add(dir_id)
            if dir_index >= len(descriptors):
                return
            record_offset = descriptors[dir_index][0]
            if record_offset >= len(blob):
                warnings.append(
                    f"FNT directory 0x{dir_id:04x} listing starts at 0x{record_offset:x}, "
                    f"outside the 0x{len(blob):x}-byte table"
                )
                return
            pos = record_offset
            while pos < len(blob):
                length = blob[pos]
                pos += 1
                if length == 0:
                    break
                is_dir = bool(length & 0x80)
                name_len = length & 0x7F
                if pos + name_len + 2 > len(blob):
                    warnings.append(
                        f"FNT record at 0x{pos - 1:x} is truncated "
                        f"(name length {name_len})"
                    )
                    return
                name = blob[pos : pos + name_len].decode("utf-8", errors="replace")
                pos += name_len
                ident = u16(blob, pos)
                pos += 2
                child = f"{path}/{name}" if path else name
                if is_dir:
                    files.append(
                        FileEntry(
                            file_id=-1, path=child, is_directory=True,
                            directory_id=ident,
                        )
                    )
                    walk(ident - ROOT_DIR_ID, child)
                else:
                    files.append(FileEntry(file_id=ident, path=child))

        walk(0, "")
        _ = root_first_file_id
        return files

    # ------------------------------------------------------------- lookups
    def __iter__(self) -> Iterator[FileEntry]:
        return iter(entry for entry in self.files if not entry.is_directory)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    @property
    def directories(self) -> list[FileEntry]:
        return [entry for entry in self.files if entry.is_directory]

    def get(self, path: str) -> Optional[FileEntry]:
        return self._by_path.get(path.lstrip("/"))

    def by_file_id(self, file_id: int) -> Optional[FileEntry]:
        return self._by_id.get(file_id)

    def find(self, pattern: str) -> list[FileEntry]:
        """Case-insensitive substring search over paths."""
        needle = pattern.lower()
        return [e for e in self if needle in e.path.lower()]

    def read(self, rom: bytes, entry: FileEntry) -> bytes:
        """Slice *entry*'s bytes out of the raw cartridge image."""
        if entry.is_directory:
            raise ValueError(f"{entry.path!r} is a directory")
        if entry.file_id >= len(self.fat):
            raise IndexError(
                f"{entry.path!r} has file ID {entry.file_id} but the FAT holds "
                f"{len(self.fat)} entries"
            )
        slot = self.fat[entry.file_id]
        if slot.size == 0:
            return b""
        end = min(slot.end, len(rom))
        return bytes(rom[slot.start : end])

    def read_by_id(self, rom: bytes, file_id: int) -> bytes:
        """Read a file by FAT index -- the path overlays use, since they have none."""
        if file_id >= len(self.fat):
            raise IndexError(
                f"file ID {file_id} is past the end of a {len(self.fat)}-entry FAT"
            )
        slot = self.fat[file_id]
        return bytes(rom[slot.start : min(slot.end, len(rom))])

    def describe(self, *, max_rows: Optional[int] = None) -> str:
        """Readable table of the file system, biggest files first."""
        rows = sorted(self, key=lambda e: self.fat[e.file_id].size, reverse=True)
        if max_rows is not None:
            rows = rows[:max_rows]
        lines = [
            f"{len(self.files)} FNT records ({len(self)} files, "
            f"{len(self.directories)} directories), {len(self.fat)} FAT entries",
            f"{'id':>5}  {'size':>9}  {'rom offset':>10}  path",
        ]
        total = 0
        for entry in rows:
            slot = self.fat[entry.file_id]
            total += slot.size
            lines.append(
                f"{entry.file_id:>5}  0x{slot.size:07x}  0x{slot.start:08x}  {entry.path}"
            )
        lines.append(f"\ntotal file bytes: 0x{total:x} ({total / 1048576:.2f} MiB)")
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        return "\n".join(lines)

    def extension_histogram(self) -> dict[str, tuple[int, int]]:
        """``{extension: (file_count, total_bytes)}`` -- a quick look at what
        asset formats a game actually ships, before writing any decoder."""
        hist: dict[str, list[int]] = {}
        for entry in self:
            slot = self.fat[entry.file_id]
            bucket = hist.setdefault(entry.extension or "<none>", [0, 0])
            bucket[0] += 1
            bucket[1] += slot.size
        return {k: (v[0], v[1]) for k, v in sorted(hist.items())}
