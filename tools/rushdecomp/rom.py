"""Top-level cartridge image: one object that knows where everything lives.

:class:`NDSRom` is what the CLI tools work against.  It parses the header once,
then lazily resolves the ARM9, ARM7, both overlay tables, the Nitro file system,
the banner and the secure area.  Nothing is decompressed or disassembled until
asked, so opening a 64 MiB image stays instant.

The class also owns the *provenance* record: which bytes came from where, and
their digests.  That record is what lets ``make verify`` prove a rebuild is byte
identical to the base ROM without anyone diffing a 64 MiB blob by eye.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Iterator, Optional

from . import lz77
from .arm9 import ARM9Image
from .banner import Banner
from .filesystem import FileEntry, NitroFileSystem
from .header import Header, looks_like_nds
from .header import size_report as header_size_report
from .overlays import Overlay, OverlayTable
from .secure import SecureArea
from .util import hexdigest, read_file

__all__ = ["NDSRom", "BinaryImage", "RomError", "diagnose"]


class RomError(RuntimeError):
    """Raised when an image cannot be parsed at all."""


def diagnose(data: bytes, *, path: Optional[str] = None) -> list[str]:
    """Explain an image that :class:`NDSRom` refuses to parse.

    ``looks_like_nds`` rejects a truncated dump before anything else runs, which
    is the right safety behaviour for extraction but the wrong one for
    reporting: an image with its tail missing is exactly when someone needs to
    be told *how much* is missing, and whether it is a harmless trim or a real
    truncation.  This reads only the header, so it works on any image of at
    least 0x160 bytes, valid or not.
    """
    label = path or "<image>"
    if len(data) < 0x160:
        return [
            f"{label}: {len(data)} bytes; a DS cartridge image needs at least 0x160",
            "nothing further can be determined -- the header itself is absent",
        ]

    lines = [f"{label}: header-only diagnosis (image could not be fully parsed)"]
    reason = looks_like_nds(data)
    if reason:
        lines.append(f"  rejected because: {reason}")
    try:
        parsed = Header.parse(data)
    except (ValueError, IndexError) as error:
        lines.append(f"  header unreadable: {error}")
        return lines
    lines += [f"  {line}" for line in header_size_report(parsed, len(data))]
    return lines


@dataclasses.dataclass
class BinaryImage:
    """An executable image: where it lives on the card and where it runs."""

    name: str
    rom_offset: int
    entry_address: int
    ram_address: int
    size_on_media: int
    raw: bytes
    #: Decompressed payload.  Equal to ``raw`` when the image is not packed.
    payload: bytes
    compressed: bool = False
    #: ``"arm9"``, ``"arm7"`` or ``"overlay"``.
    kind: str = "arm9"
    #: Extra bookkeeping for the ARM9 static-compression footer.
    footer: Optional[dict] = None
    #: What decided ``compressed``: ``"footer"``, ``"overlay-flag"`` or
    #: ``"none"``.  Recorded because guessing from the first byte alone is
    #: actively wrong -- see :meth:`NDSRom._unpack`.
    compression_source: str = "none"

    @property
    def base_address(self) -> int:
        """Address the payload's first byte executes at."""
        return self.ram_address

    @property
    def payload_size(self) -> int:
        return len(self.payload)

    def digest(self) -> str:
        return hexdigest(self.payload)

    def describe(self) -> str:
        lines = [
            f"{self.name} ({self.kind})",
            f"  on media   : 0x{self.rom_offset:08x}  size 0x{self.size_on_media:08x}",
            f"  loads to   : 0x{self.ram_address:08x}  entry 0x{self.entry_address:08x}",
            f"  payload    : 0x{len(self.payload):08x} bytes"
            + (f"  (compressed {len(self.raw) / max(len(self.payload), 1):.3f})"
               if self.compressed else ""),
            f"  sha256     : {self.digest()}",
        ]
        return "\n".join(lines)


class NDSRom:
    """A parsed Nintendo DS cartridge image."""

    def __init__(self, data: bytes, *, path: Optional[str] = None) -> None:
        reason = looks_like_nds(data)
        if reason is not None:
            raise RomError(f"{path or '<image>'}: {reason}")
        self.data = bytes(data)
        self.path = path
        self.header = Header.parse(self.data)
        self._arm9: Optional[BinaryImage] = None
        self._arm7: Optional[BinaryImage] = None
        self._arm9_overlays: Optional[OverlayTable] = None
        self._arm7_overlays: Optional[OverlayTable] = None
        self._fs: Optional[NitroFileSystem] = None
        self._banner: Optional[Banner] = None
        self._secure: Optional[SecureArea] = None

    # ------------------------------------------------------------------ open
    @classmethod
    def open(cls, path: str | os.PathLike) -> "NDSRom":
        """Read and parse a ``.nds`` file."""
        path = os.fspath(path)
        if not os.path.isfile(path):
            raise RomError(f"no such file: {path}")
        data = read_file(path)
        try:
            return cls(data, path=path)
        except RomError:
            raise
        except Exception as error:
            raise RomError(f"{path}: could not be parsed as a DS image ({error})") from error

    # ------------------------------------------------------------ processors
    def _unpack(self, offset: int, size: int, *, name: str, kind: str,
                entry: int, ram: int,
                compression: str = "none") -> BinaryImage:
        """Slice an executable image out of the card and unpack it if needed.

        ``compression`` decides how packing is determined, and the distinction
        matters more than it looks:

        ``"arm9-footer"``
            Trust only the ARM9 static-compression convention: a ``0x30``
            marker *and* a 12-byte footer whose three words agree with each
            other and with the buffer length.  Nothing else counts.

        ``"none"``
            The image is stored raw.  This is the correct default for the ARM7,
            which the BIOS loads without decompressing.

        Guessing from the leading byte instead is a trap.  ``push {r4, lr}``
        assembles to ``10 40 2D E9``, so a perfectly ordinary ARM7 begins with
        ``0x10`` -- the LZ10 marker -- and a sniffing loader will "decompress"
        it into nonsense.  The same collision hits any function starting with
        ``push {r4-r7}`` (``0x10``) or ``and`` (``0x11``).  Compression is
        therefore always taken from an authoritative source: the ARM9 footer,
        or an overlay table's flag.
        """
        if offset + size > len(self.data):
            raise RomError(
                f"{name} extent 0x{offset:x}..0x{offset + size:x} runs past the end "
                f"of a 0x{len(self.data):x}-byte image"
            )
        raw = self.data[offset : offset + size]
        footer: Optional[dict] = None
        source = "none"

        if compression == "arm9-footer":
            image = ARM9Image.parse(raw)
            # ARM9Image also honours a bare 0x10/0x11 marker, which the BIOS
            # would not; require the footer before believing it.
            if image.footer_present:
                payload = image.payload
                compressed = True
                source = "footer"
                footer = {
                    "decompressed_size": image.footer_decompressed_size,
                    "compressed_size": image.footer_compressed_size,
                    "magic": image.footer_magic,
                    "magic_style": image.footer_magic_style,
                }
            else:
                payload = bytes(raw)
                compressed = False
        elif compression == "overlay-flag":
            payload, info = lz77.decompress_auto(raw)
            compressed = info.compressed
            source = "overlay-flag" if compressed else "none"
        elif compression == "none":
            payload = bytes(raw)
            compressed = False
        else:
            raise RomError(f"unknown compression policy {compression!r}")

        return BinaryImage(
            name=name, rom_offset=offset, entry_address=entry, ram_address=ram,
            size_on_media=size, raw=bytes(raw), payload=bytes(payload),
            compressed=compressed, kind=kind, footer=footer,
            compression_source=source,
        )

    @property
    def arm9(self) -> BinaryImage:
        """The ARM9 image, decompressed if the card stored it compressed."""
        if self._arm9 is None:
            h = self.header
            self._arm9 = self._unpack(
                h.arm9_rom_offset, h.arm9_size, name="arm9", kind="arm9",
                entry=h.arm9_entry_address, ram=h.arm9_ram_address,
                compression="arm9-footer",
            )
        return self._arm9

    @property
    def arm7(self) -> BinaryImage:
        """The ARM7 image.  Holds Wi-Fi, sound and the Nitro sound driver.

        The BIOS copies the ARM7 to its RAM address and jumps straight at it;
        there is no decompression step, so a retail ARM7 is always raw.
        """
        if self._arm7 is None:
            h = self.header
            self._arm7 = self._unpack(
                h.arm7_rom_offset, h.arm7_size, name="arm7", kind="arm7",
                entry=h.arm7_entry_address, ram=h.arm7_ram_address,
                compression="none",
            )
        return self._arm7

    # -------------------------------------------------------------- overlays
    def _table(self, offset: int, size: int, processor: str) -> OverlayTable:
        if size == 0:
            return OverlayTable([], rom_offset=offset, processor=processor)
        if offset + size > len(self.data):
            raise RomError(
                f"{processor} overlay table at 0x{offset:x} size 0x{size:x} "
                "runs past the end of the image"
            )
        return OverlayTable.parse(
            self.data[offset : offset + size], rom_offset=offset, processor=processor
        )

    @property
    def arm9_overlays(self) -> OverlayTable:
        if self._arm9_overlays is None:
            h = self.header
            self._arm9_overlays = self._table(
                h.arm9_overlay_offset, h.arm9_overlay_size, "arm9"
            )
        return self._arm9_overlays

    @property
    def arm7_overlays(self) -> OverlayTable:
        if self._arm7_overlays is None:
            h = self.header
            self._arm7_overlays = self._table(
                h.arm7_overlay_offset, h.arm7_overlay_size, "arm7"
            )
        return self._arm7_overlays

    def overlay_payload(self, overlay: Overlay) -> bytes:
        """Read an overlay's file and decompress it if the flag says so."""
        blob = self.filesystem.read_by_id(self.data, overlay.file_id)
        if overlay.compressed:
            unpacked, info = lz77.decompress_auto(blob)
            if not info.compressed:
                raise RomError(
                    f"overlay {overlay.overlay_id} is flagged compressed but its "
                    "file carries no LZ77 marker"
                )
            return bytes(unpacked)
        return blob

    def overlay_image(self, overlay: Overlay, processor: str = "arm9") -> BinaryImage:
        """Wrap an overlay in a :class:`BinaryImage` for the disassembler."""
        payload = self.overlay_payload(overlay)
        return BinaryImage(
            name=overlay.label(),
            rom_offset=0,
            entry_address=overlay.ram_address,
            ram_address=overlay.ram_address,
            size_on_media=len(payload),
            raw=payload,
            payload=payload,
            compressed=False,
            kind="overlay",
        )

    def iter_overlay_images(self, processor: str = "arm9") -> Iterator[BinaryImage]:
        table = self.arm9_overlays if processor == "arm9" else self.arm7_overlays
        for overlay in table:
            try:
                yield self.overlay_image(overlay, processor)
            except (RomError, IndexError, lz77.LZError):
                # A bad overlay should not abort a whole extraction run; the
                # caller sees it missing and the report explains why.
                continue

    # ------------------------------------------------------------ file system
    @property
    def filesystem(self) -> NitroFileSystem:
        if self._fs is None:
            h = self.header
            self._fs = NitroFileSystem.parse(
                self.data,
                fnt_offset=h.fnt_offset, fnt_size=h.fnt_size,
                fat_offset=h.fat_offset, fat_size=h.fat_size,
            )
        return self._fs

    def read_file(self, path_or_entry: str | FileEntry) -> bytes:
        entry = (
            path_or_entry
            if isinstance(path_or_entry, FileEntry)
            else self.filesystem.get(path_or_entry)
        )
        if entry is None:
            raise RomError(f"no file {path_or_entry!r} in the Nitro file system")
        return self.filesystem.read(self.data, entry)

    # ------------------------------------------------------------------ misc
    @property
    def banner(self) -> Banner:
        if self._banner is None:
            offset = self.header.banner_offset
            if offset == 0 or offset + 0x20 > len(self.data):
                raise RomError(f"banner offset 0x{offset:x} is outside the image")
            # Read generously and let Banner trim to its version's real length.
            self._banner = Banner.parse(self.data[offset : offset + 0x1240])
        return self._banner

    @property
    def secure_area(self) -> SecureArea:
        if self._secure is None:
            self._secure = SecureArea.parse(
                self.data, expected_crc=self.header.secure_area_crc
            )
        return self._secure

    @property
    def game_code(self) -> str:
        return self.header.game_code

    def is_sonic_rush(self) -> bool:
        """True when the image looks like a retail Sonic Rush card.

        Sonic Rush shipped as ``SRSE`` (US), ``SRSP`` (Europe) and ``SRJJ``
        (Japan) under maker code ``8P`` for Sega.  The check is a convenience
        for the report, not a gate -- any DS image extracts fine.
        """
        h = self.header
        return h.game_code in ("SRSE", "SRSP", "SRJJ") or h.game_title.startswith("SONIC RUSH")

    def digest(self) -> str:
        return hexdigest(self.data)

    def provenance(self) -> dict:
        """Digests of every component, for ``make verify`` to compare against."""
        record: dict = {
            "image_sha256": self.digest(),
            "image_size": len(self.data),
            "game_code": self.game_code,
            "game_title": self.header.game_title,
            "components": {},
        }
        try:
            record["components"]["arm9"] = {
                "payload_sha256": self.arm9.digest(),
                "payload_size": self.arm9.payload_size,
                "compressed": self.arm9.compressed,
            }
        except RomError as error:
            record["components"]["arm9"] = {"error": str(error)}
        try:
            record["components"]["arm7"] = {
                "payload_sha256": self.arm7.digest(),
                "payload_size": self.arm7.payload_size,
                "compressed": self.arm7.compressed,
            }
        except RomError as error:
            record["components"]["arm7"] = {"error": str(error)}
        record["components"]["arm9_overlays"] = len(self.arm9_overlays)
        record["components"]["arm7_overlays"] = len(self.arm7_overlays)
        try:
            record["components"]["files"] = len(self.filesystem)
        except (RomError, IndexError) as error:
            record["components"]["files"] = {"error": str(error)}
        return record

    def _fat_data_end(self) -> Optional[int]:
        """End of the last Nitro file, or ``None`` when the FAT is unreadable.

        This is the authoritative "last byte of real data" on a retail card:
        files sit after every header-declared table, so a trimming tool cuts
        here and not at any offset the header records.
        """
        try:
            ends = [entry.end for entry in self.filesystem.fat if entry.end > 0]
        except (RomError, IndexError, AttributeError):
            return None
        return max(ends) if ends else None

    def size_report(self) -> list[str]:
        """Trim/truncation classification for this image; see :func:`header.size_report`."""
        return header_size_report(
            self.header, len(self.data), data_end=self._fat_data_end()
        )

    def report(self) -> str:
        """The full extraction report printed by ``sr-extract --report``."""
        parts = [
            "=" * 72,
            "Nintendo DS cartridge image report",
            "=" * 72,
            f"file      : {self.path or '<in memory>'}",
            f"size      : 0x{len(self.data):x} ({len(self.data) / 1048576:.2f} MiB)",
            f"sha256    : {self.digest()}",
            f"md5       : {hexdigest(self.data, 'md5')}",
            f"identified: {'Sonic Rush' if self.is_sonic_rush() else 'not a recognised Sonic Rush card'}",
            "",
            "-" * 72,
            "Header",
            "-" * 72,
            self.header.describe(),
            "",
            *self.size_report(),
            "",
        ]
        for label, fn in (
            ("ARM9", lambda: self.arm9),
            ("ARM7", lambda: self.arm7),
        ):
            parts += ["-" * 72, label, "-" * 72]
            try:
                parts.append(fn().describe())
            except (RomError, lz77.LZError) as error:
                parts.append(f"  UNREADABLE: {error}")
            parts.append("")

        for label, table in (
            ("ARM9 overlays", self.arm9_overlays),
            ("ARM7 overlays", self.arm7_overlays),
        ):
            parts += ["-" * 72, label, "-" * 72]
            if len(table):
                parts.append(table.describe())
                for problem in table.sanity_report():
                    parts.append(f"  warning: {problem}")
            else:
                parts.append("  (none)")
            parts.append("")

        parts += ["-" * 72, "Nitro file system", "-" * 72]
        try:
            parts.append(self.filesystem.describe(max_rows=40))
            parts.append("")
            parts.append("  by extension:")
            for ext, (count, total) in self.filesystem.extension_histogram().items():
                parts.append(f"    {ext:<10} {count:>4} files  {total / 1024:>10.1f} KiB")
        except (RomError, IndexError) as error:
            parts.append(f"  UNREADABLE: {error}")
        parts.append("")

        parts += ["-" * 72, "Banner", "-" * 72]
        try:
            parts.append(self.banner.describe())
        except (RomError, ValueError) as error:
            parts.append(f"  UNREADABLE: {error}")
        parts.append("")

        parts += ["-" * 72, "Secure area", "-" * 72]
        parts.append(self.secure_area.describe())
        return "\n".join(parts)
