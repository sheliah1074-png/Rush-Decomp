#!/usr/bin/env python3
"""Build a synthetic, self-contained DS cartridge image for the test suite.

Why this exists
---------------
Every part of this toolchain parses a retail cartridge image, and the
toolchain's own correctness cannot be checked against a copyrighted ROM in
continuous integration.  So the tests build a *fake* one instead: a complete,
structurally valid ``.nds`` whose every field is generated here from code
written for this repository.

Nothing in the output comes from any commercial game.  The ARM9, ARM7 and
overlay programs below are a handful of arithmetic routines; the banner titles
are this project's own name; the logo bitmap is a neutral checkerboard rather
than any manufacturer's artwork.  The image is deliberately built to exercise
the awkward corners:

* an ARM9 that is **statically compressed** -- ``0x30`` marker plus the
  12-byte footer, which is the form the DS BIOS expects
* an ARM9 that mixes **ARM and Thumb** functions and calls between them
* an ARM7 left **uncompressed**, so both paths through the loader are covered
* three ARM9 overlays, one of which is **LZ11**-compressed and flagged in the
  ``ram_size`` field rather than the ``file_id`` field
* a Nitro file system with **nested directories**, an empty file, and a file
  whose FAT entry carries the top-byte flag some SDK revisions set
* a v1 banner with all six titles populated and correct CRCs
* header and logo CRCs that actually verify

Run it directly to write the image somewhere::

    python3 tests/make_test_rom.py build/selftest.nds

or import :func:`build_test_rom` to get the bytes.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from rushdecomp import lz77  # noqa: E402
from rushdecomp.arm9 import (  # noqa: E402
    ARM9_STATIC_MARKER,
    FOOTER_MAGIC_BASE,
    FOOTER_SIZE,
    footer_magic_for,
)
from rushdecomp.crc import crc16  # noqa: E402
from rushdecomp.header import HEADER_SIZE  # noqa: E402
from rushdecomp.util import p16, p32  # noqa: E402

try:  # pragma: no cover
    import keystone
except ImportError:  # pragma: no cover
    keystone = None  # type: ignore[assignment]

__all__ = ["build_test_rom", "TEST_ROM_GAME_CODE", "main"]

TEST_ROM_GAME_CODE = "TSTR"
TEST_ROM_TITLE = "RUSHDECOMP"

ARM9_RAM_ADDRESS = 0x02000000
ARM9_ENTRY_ADDRESS = 0x02000000
ARM7_RAM_ADDRESS = 0x02380000
ARM7_ENTRY_ADDRESS = 0x02380000
OVERLAY_RAM_BASE = 0x02100000


# --------------------------------------------------------------------------
# The synthetic programs.  Original code written for this repository: a few
# integer routines, no relation to any shipped game.
# --------------------------------------------------------------------------
_ARM9_ARM_SOURCE = """
push {{r4, r5, lr}}
mov r4, r0
mov r5, r1
add r4, r4, r5
sub r4, r4, #1
cmp r4, #0
beq {done}
blx {thumb_helper}
mov r0, r4
pop {{r4, r5, pc}}
push {{lr}}
mov r0, #0
pop {{pc}}
"""

_ARM9_THUMB_SOURCE = """
push {r4, lr}
movs r4, #0x21
adds r4, r4, #1
mov r0, r4
lsls r0, r0, #2
pop {r4, pc}
"""

_ARM7_SOURCE = """
push {r4, lr}
mov r4, #0x4000000
ldr r0, [r4, #0x180]
mov r1, #1
str r1, [r4, #0x180]
mov r0, #0
pop {r4, pc}
"""

_OVERLAY_SOURCES = (
    """
push {{r4, lr}}
mov r4, r0
mul r0, r4, r4
pop {{r4, pc}}
""",
    """
push {{r4, r5, lr}}
mov r4, r0
mov r5, #0
loop_{n}:
add r5, r5, r4
subs r4, r4, #1
bne loop_{n}
mov r0, r5
pop {{r4, r5, pc}}
""",
    """
push {{lr}}
mov r0, r0, lsr #1
mov r1, r1, lsl #1
orr r0, r0, r1
pop {{pc}}
""",
)


def _assemble(source: str, address: int, *, thumb: bool = False) -> bytes:
    """Assemble one of the synthetic programs with Keystone."""
    if keystone is None:
        raise RuntimeError(
            "keystone-engine is required to build the self-test ROM; "
            "install it with `pip install -r requirements.txt`"
        )
    mode = keystone.KS_MODE_THUMB if thumb else keystone.KS_MODE_ARM
    engine = keystone.Ks(keystone.KS_ARCH_ARM, mode | keystone.KS_MODE_LITTLE_ENDIAN)
    encoded, count = engine.asm(source, addr=address)
    if not encoded:
        raise RuntimeError(f"nothing assembled from:\n{source}")
    _ = count
    return bytes(encoded)


def _build_arm9() -> bytes:
    """ARM9 payload: an ARM entry function that calls into a Thumb helper."""
    thumb_at = ARM9_RAM_ADDRESS + 0x40
    done_at = ARM9_RAM_ADDRESS + 0x30
    arm = _assemble(
        _ARM9_ARM_SOURCE.format(thumb_helper=thumb_at, done=done_at),
        ARM9_RAM_ADDRESS,
    )
    thumb = _assemble(_ARM9_THUMB_SOURCE, thumb_at, thumb=True)
    payload = bytearray(0x80)
    payload[0 : len(arm)] = arm
    payload[len(arm) : len(arm) + len(thumb)] = b""  # padding stays zero
    payload[0x40 : 0x40 + len(thumb)] = thumb
    return bytes(payload)


def _statically_compress(payload: bytes) -> bytes:
    """Pack an ARM9 the way the DS BIOS expects to find it.

    ``0x30`` marker instead of ``0x10``, then the 12-byte footer whose third
    word is the ``0x5F030000 - (size >> 9)`` check the entry stub verifies.
    """
    body = bytearray(lz77.compress_lz10(payload))
    if not body:
        raise RuntimeError("LZ10 produced an empty stream")
    body[0] = ARM9_STATIC_MARKER
    decompressed = len(payload)
    compressed = len(body) + FOOTER_SIZE
    magic = footer_magic_for(decompressed, arithmetic=True)
    if not FOOTER_MAGIC_BASE:
        raise RuntimeError("footer magic base is unset")
    return bytes(body) + p32(decompressed) + p32(compressed) + p32(magic)


def _build_overlays() -> list[dict]:
    """Three overlays; the second is LZ11-packed and flagged in ``ram_size``."""
    out = []
    for index, template in enumerate(_OVERLAY_SOURCES):
        source = template.format(n=index)
        payload = _assemble(source, OVERLAY_RAM_BASE + index * 0x100)
        payload += bytes((-len(payload)) % 4)
        entry = {
            "overlay_id": index,
            "ram_address": OVERLAY_RAM_BASE + index * 0x100,
            "payload": payload,
            "compressed": index == 1,
            "flag_in_ram_size": index == 1,
            "static_init_start": OVERLAY_RAM_BASE + index * 0x100,
            "static_init_end": OVERLAY_RAM_BASE + index * 0x100 + 8
            if index == 0
            else OVERLAY_RAM_BASE + index * 0x100,
        }
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# Nitro file system
# --------------------------------------------------------------------------
_TEST_FILES: list[tuple[str, bytes]] = [
    ("selftest.txt", b"Rush-Decomp synthetic self-test image. Not a retail ROM.\n"),
    ("gfx/checkerboard.bin", bytes((i * 7) & 0xFF for i in range(256))),
    ("gfx/palette.bin", bytes(range(32))),
    ("sound/blank.bin", b""),
    ("script/init.bin", bytes([0xE9, 0x2D, 0x40, 0x10] * 16)),
    ("deep/nested/dir/leaf.bin", b"leaf"),
]


def _encode_name(name: str) -> bytes:
    raw = name.encode("utf-8")
    if not 0 < len(raw) < 0x80:
        raise ValueError(f"FNT name {name!r} must be 1..127 bytes")
    return bytes((len(raw),)) + raw


def _build_fnt(paths: list[str]) -> tuple[bytes, dict[str, tuple[int, int]]]:
    """Build an FNT blob and return it with ``{path: (dir_index, file_id)}``.

    Directory descriptors come first, one per directory, then the record
    listings.  Directory IDs are 0xF000 + index, with the root at index 0.
    """
    tree: dict[int, dict] = {0: {"dirs": {}, "files": []}}
    for path in paths:
        parts = [piece for piece in path.split("/") if piece]
        current = 0
        for piece in parts[:-1]:
            children = tree[current]["dirs"]
            if piece not in children:
                new_index = len(tree)
                tree[new_index] = {"dirs": {}, "files": []}
                children[piece] = new_index
            current = children[piece]
        tree[current]["files"].append((parts[-1], path))

    # Assign file IDs in a stable, depth-first order.
    order: list[int] = []

    def visit(index: int) -> None:
        order.append(index)
        for name in sorted(tree[index]["dirs"]):
            visit(tree[index]["dirs"][name])

    visit(0)
    first_file_id_of: dict[int, int] = {}
    next_file_id = 0
    assignments: dict[str, tuple[int, int]] = {}
    for index in order:
        first_file_id_of[index] = next_file_id
        for _name, path in tree[index]["files"]:
            assignments[path] = (index, next_file_id)
            next_file_id += 1

    # Listing offsets: the descriptor block is 8 bytes per directory.
    listing_start = len(tree) * 8
    listing_offsets: dict[int, int] = {}
    cursor = listing_start
    for index in order:
        listing_offsets[index] = cursor
        for name, _path in tree[index]["files"]:
            cursor += len(_encode_name(name)) + 2
        for name in sorted(tree[index]["dirs"]):
            cursor += len(_encode_name(name)) + 2
        cursor += 1  # terminator

    descriptors = bytearray()
    for index in range(len(tree)):
        parent = 0xF000
        for candidate, node in tree.items():
            if index in node["dirs"].values():
                parent = 0xF000 + candidate
                break
        descriptors += (
            p32(listing_offsets[index])
            + p16(first_file_id_of[index])
            + p16(parent if index else 0xF000)
        )

    listing = bytearray()
    for index in order:
        for name, path in tree[index]["files"]:
            file_id = assignments[path][1]
            listing += _encode_name(name) + p16(file_id)
        for name in sorted(tree[index]["dirs"]):
            child = tree[index]["dirs"][name]
            encoded_name = name.encode("utf-8")
            # High bit on the length byte marks a directory record.
            listing += bytes((0x80 | len(encoded_name),)) + encoded_name
            listing += p16(0xF000 + child)
        listing += b"\x00"

    return bytes(descriptors) + bytes(listing), assignments


def _build_fat(entries: list[tuple[int, int, int]]) -> bytes:
    """``entries`` is ``[(file_id, start, end), ...]``."""
    by_id: dict[int, bytes] = {}
    for file_id, start, end in entries:
        raw_start = start
        if file_id == 3:
            # Exercise the top-byte flag some SDK revisions set on the start
            # word; the parser has to mask it off and still find the file.
            raw_start = start | (0x01 << 24)
        by_id[file_id] = p32(raw_start) + p32(end)
    count = max(by_id) + 1 if by_id else 0
    return b"".join(by_id.get(i, p32(0) + p32(0)) for i in range(count))


# --------------------------------------------------------------------------
# Banner
# --------------------------------------------------------------------------
_BANNER_TITLES = {
    0x240: "Rush-Decomp 自己テスト",
    0x340: "Rush-Decomp Self-Test",
    0x440: "Image d'auto-test",
    0x540: "Selbsttest-Bild",
    0x640: "Immagine di autotest",
    0x740: "Imagen de autoprueba",
}


def _build_banner() -> bytes:
    banner = bytearray(0x840)
    banner[0:2] = p16(1)  # version 1
    # 32x32 4-bpp icon: a checkerboard, deliberately not anyone's artwork.
    # 4 tile columns x 4 tile rows x 32 bytes = 0x200, filling 0x020..0x21F.
    for tile_row in range(4):
        for tile_col in range(4):
            base = 0x020 + (tile_row * 4 + tile_col) * 32
            for pixel_row in range(8):
                for byte_index in range(4):
                    on = (tile_row + tile_col + pixel_row + byte_index) % 2 == 0
                    nibble = 1 if on else 2
                    banner[base + pixel_row * 4 + byte_index] = nibble | (nibble << 4)
    # BGR555 palette at 0x220: index 0 unused, 1 = white, 2 = dark blue.
    palette = (0x0000, 0x7FFF, 0x4010) + (0x0000,) * 13
    for index, colour in enumerate(palette):
        banner[0x220 + index * 2 : 0x222 + index * 2] = p16(colour)
    for offset, text in _BANNER_TITLES.items():
        encoded = text.encode("utf-16-le")
        # Truncate on a code-unit boundary; a half character leaves a dangling
        # byte that the reader decodes as junk past the real title.
        encoded = encoded[: 0x100 - 2]
        if len(encoded) % 2:
            encoded = encoded[:-1]
        banner[offset : offset + len(encoded)] = encoded
    # Sign through the same code path that verifies, so the covered ranges are
    # defined in exactly one place and the self-test proves internal
    # consistency rather than a guess about the field layout.
    from rushdecomp.banner import Banner

    signed = Banner.parse(bytes(banner))
    signed.sign()
    return signed.raw


def _build_logo() -> bytes:
    """A neutral 156-byte placeholder standing in for the manufacturer logo."""
    return bytes(((i * 37) ^ 0x5A) & 0xFF for i in range(156))


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------
def _align(value: int, boundary: int = 4) -> int:
    rem = value % boundary
    return value if rem == 0 else value + (boundary - rem)


def build_test_rom(*, game_code: str = TEST_ROM_GAME_CODE) -> bytes:
    """Return a complete synthetic ``.nds`` image."""
    arm9_payload = _build_arm9()
    arm9_media = _statically_compress(arm9_payload)

    arm7_payload = _assemble(_ARM7_SOURCE, ARM7_RAM_ADDRESS)
    arm7_payload += bytes((-len(arm7_payload)) % 4)

    overlays = _build_overlays()

    banner = _build_banner()

    # Overlay payloads become Nitro files, so build the file list first.
    file_paths = [path for path, _ in _TEST_FILES]
    file_blobs = [blob for _, blob in _TEST_FILES]
    overlay_paths = [f"overlay_{o['overlay_id']:03d}.bin" for o in overlays]
    all_paths = file_paths + overlay_paths
    all_blobs = file_blobs + [
        lz77.compress(o["payload"], lz77.Marker.LZ11) if o["compressed"] else o["payload"]
        for o in overlays
    ]

    fnt, assignments = _build_fnt(all_paths)

    # Lay the image out: header, secure area, ARM9, ARM7, Y9 table, FNT, FAT,
    # then the file payloads, then the banner.  Offsets are what a real card
    # would use, rounded to 4 (0x200 for the big regions).
    cursor = HEADER_SIZE + 0x2000  # header + secure area
    cursor = _align(cursor, 0x200)

    arm9_offset = cursor
    cursor = _align(arm9_offset + len(arm9_media), 0x200)

    arm7_offset = cursor
    cursor = _align(arm7_offset + len(arm7_payload), 0x200)

    y9_offset = cursor
    # Reserve the space now; the descriptors are filled in below once the file
    # IDs have been assigned, because each overlay's payload is itself a file.
    y9_blob = b"\x00" * (len(overlays) * 0x20)
    cursor = _align(y9_offset + len(y9_blob), 4)

    fnt_offset = cursor
    cursor = _align(fnt_offset + len(fnt), 4)

    fat_count = max(assignments[path][1] for path in all_paths) + 1
    fat_size = fat_count * 8
    fat_offset = cursor
    cursor = _align(fat_offset + fat_size, 4)

    file_entries: list[tuple[int, int, int]] = []
    for path, blob in zip(all_paths, all_blobs):
        file_id = assignments[path][1]
        start = cursor
        end = start + len(blob)
        file_entries.append((file_id, start, end))
        cursor = _align(end, 4)

    banner_offset = _align(cursor, 0x200)
    cursor = banner_offset + len(banner)
    total_used = _align(cursor, 0x200)

    fat_blob = _build_fat(file_entries)
    assert len(fat_blob) == fat_size, (len(fat_blob), fat_size)

    # Now that every overlay payload has a file ID, fill the Y9 descriptors in.
    y9_blob = b"".join(
        _overlay_entry(o, assignments[path][1])
        for o, path in zip(overlays, overlay_paths)
    )
    assert len(y9_blob) == len(overlays) * 0x20

    image = bytearray(total_used)

    header = bytearray(HEADER_SIZE)
    header[0x000:0x00C] = TEST_ROM_TITLE.encode("ascii").ljust(12, b"\x00")
    header[0x00C:0x010] = game_code.encode("ascii").ljust(4, b"\x00")
    header[0x010:0x012] = b"RD"
    header[0x012] = 0x00  # unit code: NDS
    header[0x013] = 0x00  # encryption seed select
    header[0x014] = 0x09  # device capacity: 128 KiB << 9 = 64 MiB
    header[0x01E] = 0xFF  # region: all
    header[0x01F] = 0x00  # rom version
    header[0x020] = 0x00  # autostart

    header[0x024:0x028] = p32(arm9_offset)
    header[0x028:0x02C] = p32(ARM9_ENTRY_ADDRESS)
    header[0x02C:0x030] = p32(ARM9_RAM_ADDRESS)
    header[0x030:0x034] = p32(len(arm9_media))

    header[0x034:0x038] = p32(arm7_offset)
    header[0x038:0x03C] = p32(ARM7_ENTRY_ADDRESS)
    header[0x03C:0x040] = p32(ARM7_RAM_ADDRESS)
    header[0x040:0x044] = p32(len(arm7_payload))

    header[0x044:0x048] = p32(fnt_offset)
    header[0x048:0x04C] = p32(len(fnt))
    header[0x04C:0x050] = p32(fat_offset)
    header[0x050:0x054] = p32(fat_size)

    header[0x054:0x058] = p32(y9_offset)
    header[0x058:0x05C] = p32(len(y9_blob))
    header[0x05C:0x060] = p32(0)
    header[0x060:0x064] = p32(0)

    header[0x064:0x068] = p32(0x00416657)  # normal card control
    header[0x068:0x06C] = p32(0x081808F8)  # secure card control
    header[0x06C:0x070] = p32(banner_offset)
    header[0x070:0x072] = p16(0)  # secure area CRC (left clear: no real stub)
    header[0x072:0x074] = p16(0xD7FE)  # secure area delay
    header[0x074:0x078] = p32(0)
    header[0x078:0x07C] = p32(0)
    header[0x07C:0x084] = p32(0) + p32(0)
    header[0x084:0x088] = p32(total_used)
    header[0x088:0x08C] = p32(HEADER_SIZE)

    logo = _build_logo()
    header[0x0C0:0x15C] = logo
    header[0x15C:0x15E] = p16(crc16(logo))
    header[0x15E:0x160] = p16(crc16(bytes(header[:0x15E])))

    image[0:HEADER_SIZE] = header
    # Secure area: a recognisable plaintext stub so the parser reports
    # "already-decrypted" rather than demanding a KEY1 table nobody has.
    image[0x4000:0x4008] = b"encryObj"
    for offset in range(0x4008, 0x6000, 4):
        image[offset : offset + 4] = p32(0xE1A00000)  # mov r0, r0

    image[arm9_offset : arm9_offset + len(arm9_media)] = arm9_media
    image[arm7_offset : arm7_offset + len(arm7_payload)] = arm7_payload
    image[y9_offset : y9_offset + len(y9_blob)] = y9_blob
    image[fnt_offset : fnt_offset + len(fnt)] = fnt
    image[fat_offset : fat_offset + len(fat_blob)] = fat_blob
    for (_file_id, start, end), blob in zip(file_entries, all_blobs):
        image[start:end] = blob
    image[banner_offset : banner_offset + len(banner)] = banner

    return bytes(image)


def _overlay_entry(overlay: dict, file_id: int) -> bytes:
    """Serialise one 32-byte Y9 descriptor.

    The compression flag is written into whichever field the caller asked for.
    Real cartridges are seen with it in the file-ID word and with it in the
    RAM-size word, so the parser checks both and the generator exercises one of
    each -- which is the only way to know the reader is not silently depending
    on a single spelling.
    """
    ram_size = len(overlay["payload"]) & 0x00FFFFFF
    raw_file = file_id & 0xFFFF
    if overlay["compressed"]:
        if overlay["flag_in_ram_size"]:
            ram_size |= 1 << 24
        else:
            raw_file |= 1 << 24
    return (
        p32(overlay["overlay_id"])
        + p32(overlay["ram_address"])
        + p32(ram_size)
        + p32(0)  # bss size
        + p32(overlay["static_init_start"])
        + p32(overlay["static_init_end"])
        + p32(raw_file)
        + p32(0)
    )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: ``make_test_rom.py [output.nds]``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    destination = args[0] if args else os.path.join("build", "selftest.nds")
    image = build_test_rom()
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    with open(destination, "wb") as handle:
        handle.write(image)
    print(
        f"wrote {destination}: {len(image)} bytes "
        f"(0x{len(image):x}), sha256 "
        f"{__import__('hashlib').sha256(image).hexdigest()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
