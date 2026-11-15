"""Command line interface for the Rush-Decomp toolchain.

One entry point, several verbs, each mapping to a step a contributor actually
performs::

    sr report    baserom/sonic_rush.nds          # what is this image?
    sr extract   baserom/sonic_rush.nds          # split it into data/
    sr dumpfs    baserom/sonic_rush.nds          # unpack the Nitro file system
    sr disasm    arm9                            # data/arm9.bin -> asm/arm9.s
    sr verify    arm9                            # disasm -> assemble -> compare
    sr rebuild                                   # data/ + asm/ -> build/*.nds
    sr progress                                  # how much is matched?

Every verb reads only from paths the user supplies and writes only inside the
repository's generated directories, all of which ``.gitignore`` excludes.  No
verb uploads, downloads or embeds anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from . import lz77
from .assembler import assemble_file, round_trip
from .disasm import Disassembler, Mode, require_capstone, write_sidecar
from .progress import load_progress, save_progress
from .rom import BinaryImage, NDSRom, RomError, diagnose
from .util import ensure_dir, hexdigest, human_size, read_file, write_file

__all__ = ["main", "build_parser"]

DEFAULT_ROM = os.path.join("baserom", "sonic_rush.nds")
DATA_DIR = "data"
ASM_DIR = "asm"
BUILD_DIR = "build"
FSDUMP_DIR = "fsdump"


def _say(message: str = "") -> None:
    print(message, flush=True)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr, flush=True)
    return 1


def _resolve_rom(path: Optional[str]) -> str:
    candidate = path or DEFAULT_ROM
    if os.path.isfile(candidate):
        return candidate
    raise SystemExit(
        f"error: no cartridge image at {candidate!r}\n"
        "\n"
        "This project never contains a ROM.  Dump your own cartridge and put it\n"
        "at that path (or pass one explicitly).  See docs/ROM_SETUP.md for how\n"
        "to dump, and how to confirm you have the right revision.\n"
    )


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def cmd_report(args: argparse.Namespace) -> int:
    """Print a full structural report of the cartridge image."""
    path = _resolve_rom(args.rom)
    try:
        rom = NDSRom.open(path)
    except RomError as error:
        # A malformed or truncated dump still has to explain itself.  How much
        # data is missing -- and whether it is a harmless trim or a real
        # truncation -- matters more than the fact that parsing stopped.
        _fail(str(error))
        try:
            _say()
            _say("\n".join(diagnose(read_file(path), path=path)))
        except OSError as io_error:
            _fail(f"cannot re-read {path} for diagnosis: {io_error}")
        return 1
    _say(rom.report())
    if args.json:
        _say()
        _say(json.dumps(rom.provenance(), indent=1, sort_keys=True))
    return 0


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------
def _image_paths(name: str) -> tuple[str, str]:
    return os.path.join(DATA_DIR, f"{name}.bin"), os.path.join(DATA_DIR, f"{name}.json")


def cmd_extract(args: argparse.Namespace) -> int:
    """Split the image into its executable components under ``data/``."""
    rom = NDSRom.open(_resolve_rom(args.rom))
    ensure_dir(DATA_DIR)
    manifest: dict = {
        "source_image": os.path.abspath(rom.path or args.rom),
        "source_sha256": rom.digest(),
        "source_size": len(rom.data),
        "game_code": rom.game_code,
        "game_title": rom.header.game_title,
        "components": {},
    }

    def emit(image: BinaryImage) -> None:
        bin_path, meta_path = _image_paths(image.name)
        write_file(bin_path, image.payload)
        meta = {
            "name": image.name,
            "kind": image.kind,
            "rom_offset": image.rom_offset,
            "size_on_media": image.size_on_media,
            "entry_address": image.entry_address,
            "ram_address": image.ram_address,
            "compressed": image.compressed,
            "compression_source": image.compression_source,
            "payload_size": image.payload_size,
            "payload_sha256": hexdigest(image.payload),
            "raw_sha256": hexdigest(image.raw),
            "footer": image.footer,
        }
        write_file(meta_path, json.dumps(meta, indent=1, sort_keys=True).encode() + b"\n")
        manifest["components"][image.name] = meta
        _say(
            f"  {image.name:<16} {human_size(image.payload_size):>10}  "
            f"-> {bin_path}"
            + ("  (decompressed)" if image.compressed else "")
        )

    _say(f"extracting {rom.path}")
    emit(rom.arm9)
    emit(rom.arm7)

    for processor in ("arm9", "arm7"):
        table = rom.arm9_overlays if processor == "arm9" else rom.arm7_overlays
        if not len(table):
            continue
        _say(f"  {processor} overlays: {len(table)}")
        for overlay in table:
            try:
                image = rom.overlay_image(overlay, processor)
            except (RomError, IndexError, lz77.LZError) as error:
                _say(f"    overlay {overlay.overlay_id}: SKIPPED ({error})")
                manifest["components"][f"overlay_{overlay.overlay_id:03d}"] = {
                    "error": str(error)
                }
                continue
            image.name = f"{processor}_{image.name}"
            emit(image)

    if args.banner:
        write_file(os.path.join(DATA_DIR, "banner.bin"), rom.banner.raw)
        write_file(
            os.path.join(DATA_DIR, "banner.ppm"), rom.banner.icon_as_ppm()
        )
        manifest["components"]["banner"] = {
            "version": rom.banner.version,
            "sha256": hexdigest(rom.banner.raw),
        }
        _say(f"  banner           {human_size(len(rom.banner.raw)):>10}  -> "
             f"{DATA_DIR}/banner.bin")

    if args.secure:
        secure = rom.secure_area
        write_file(os.path.join(DATA_DIR, "secure_area.bin"), secure.bytes_for_dump)
        manifest["components"]["secure_area"] = {
            "status": secure.status,
            "detail": secure.detail,
        }
        _say(f"  secure_area      {human_size(len(secure.raw)):>10}  -> "
             f"{DATA_DIR}/secure_area.bin  [{secure.status}]")

    write_file(
        os.path.join(DATA_DIR, "manifest.json"),
        json.dumps(manifest, indent=1, sort_keys=True).encode() + b"\n",
    )
    _say(f"\nmanifest -> {DATA_DIR}/manifest.json")
    _say(
        "\nnote: everything just written is derived from your cartridge dump and\n"
        "      is excluded from git.  Do not commit it."
    )
    return 0


# --------------------------------------------------------------------------
# dumpfs
# --------------------------------------------------------------------------
def cmd_dumpfs(args: argparse.Namespace) -> int:
    """Unpack the Nitro ROM file system into ``fsdump/``."""
    rom = NDSRom.open(_resolve_rom(args.rom))
    fs = rom.filesystem
    ensure_dir(FSDUMP_DIR)
    count = 0
    total = 0
    for entry in fs:
        if args.filter and args.filter.lower() not in entry.path.lower():
            continue
        blob = fs.read(rom.data, entry)
        destination = os.path.join(FSDUMP_DIR, entry.safe_name)
        write_file(destination, blob)
        count += 1
        total += len(blob)
    listing = fs.describe(max_rows=args.limit)
    write_file(os.path.join(FSDUMP_DIR, "_listing.txt"), listing.encode() + b"\n")
    _say(f"wrote {count} files ({human_size(total)}) under {FSDUMP_DIR}/")
    if args.list:
        _say()
        _say(listing)
    return 0


# --------------------------------------------------------------------------
# disasm
# --------------------------------------------------------------------------
def _load_image(name: str) -> tuple[bytes, dict]:
    bin_path, meta_path = _image_paths(name)
    if not os.path.isfile(bin_path):
        raise SystemExit(
            f"error: {bin_path} does not exist -- run `sr extract` first"
        )
    with open(bin_path, "rb") as handle:
        payload = handle.read()
    meta: dict = {}
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
    return payload, meta


def cmd_disasm(args: argparse.Namespace) -> int:
    """Disassemble one extracted component into ``asm/<name>.s``."""
    require_capstone()
    payload, meta = _load_image(args.name)
    base = meta.get("ram_address") or args.base or 0x02000000
    entry = meta.get("entry_address") or args.entry
    name = args.name

    _say(
        f"disassembling {name}: {human_size(len(payload))} at "
        f"0x{base:08x}, entry "
        f"{'0x%08x' % entry if entry else '(none)'}"
    )
    disassembler = Disassembler(
        payload, base,
        entry_address=entry,
        name=name,
        scan_prologues=not args.no_prologues,
        scan_windows=not args.no_windows,
    )
    result = disassembler.disassemble()
    ensure_dir(ASM_DIR)
    asm_path = os.path.join(ASM_DIR, f"{name}.s")
    write_file(asm_path, disassembler.render(result).encode() + b"\n")
    write_sidecar(result, os.path.join(ASM_DIR, f"{name}.json"))
    _say(result.summary(len(payload)))
    _say(f"-> {asm_path}")
    return 0


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------
def cmd_verify(args: argparse.Namespace) -> int:
    """Disassemble, reassemble, and compare byte for byte."""
    require_capstone()
    payload, meta = _load_image(args.name)
    base = meta.get("ram_address") or args.base or 0x02000000
    entry = meta.get("entry_address") or args.entry
    ok, report, rebuilt = round_trip(
        payload, base, entry_address=entry, name=args.name
    )
    if args.output:
        write_file(args.output, bytes(rebuilt))
    _say(f"{args.name}: {'MATCH' if ok else 'MISMATCH'}")
    _say(report.describe())
    if args.strict and not ok:
        return 1
    return 0 if ok else (1 if args.strict else 0)


# --------------------------------------------------------------------------
# rebuild
# --------------------------------------------------------------------------
def cmd_rebuild(args: argparse.Namespace) -> int:
    """Reassemble a cartridge image from ``asm/`` and compare it to the base ROM.

    This is the check that makes a decompilation meaningful.  It reads the
    generated ``.s`` for every component, reassembles each one, substitutes the
    result back into a copy of the base image at the right offset (recompressing
    the ARM9 if the original was compressed), and compares the whole image
    against the base ROM byte for byte.

    A component whose recompression cannot reproduce the original bytes falls
    back to the pristine compressed blob from ``data/``.  That keeps the rebuild
    honest: it reports which components matched from source and which had to be
    carried over, instead of quietly claiming a match it did not earn.
    """
    require_capstone()
    rom = NDSRom.open(_resolve_rom(args.rom))
    ensure_dir(BUILD_DIR)
    image = bytearray(rom.data)
    report_lines: list[str] = []
    all_ok = True

    components: list[tuple[str, BinaryImage]] = [("arm9", rom.arm9), ("arm7", rom.arm7)]
    for processor in ("arm9", "arm7"):
        table = rom.arm9_overlays if processor == "arm9" else rom.arm7_overlays
        for overlay in table:
            try:
                components.append(
                    (f"{processor}_{overlay.label()}", rom.overlay_image(overlay, processor))
                )
            except (RomError, IndexError, lz77.LZError):
                continue

    for name, component in components:
        asm_path = os.path.join(ASM_DIR, f"{name}.s")
        if not os.path.isfile(asm_path):
            report_lines.append(f"  {name:<20} no asm/ file -- not rebuilt")
            continue
        rebuilt, report = assemble_file(
            asm_path, base_address=component.ram_address,
            start_mode=Mode.THUMB if component.entry_address & 0x1 else Mode.ARM,
        )
        if bytes(rebuilt) != component.payload:
            all_ok = False
            report_lines.append(
                f"  {name:<20} MISMATCH  {report.match_ratio * 100:.2f}% of bytes, "
                f"{len(report.failures)} assembly failures"
            )
            if not args.keep_going:
                _say("\n".join(report_lines))
                return 1
            continue

        # Put the rebuilt payload back where the original was.
        if component.compressed and component.kind == "arm9":
            from .arm9 import ARM9Image

            original = ARM9Image.parse(component.raw)
            packed = original.recompress()
            if len(packed) != len(component.raw):
                report_lines.append(
                    f"  {name:<20} matched, but recompression changed the size "
                    f"(0x{len(component.raw):x} -> 0x{len(packed):x}); "
                    "carrying the original bytes forward"
                )
                packed = component.raw
            else:
                report_lines.append(f"  {name:<20} matched (recompressed)")
            blob = packed
        elif component.compressed:
            marker = lz77.probe(component.raw).marker
            packed = lz77.compress(component.payload, marker)
            if packed != component.raw:
                if lz77.recompression_matches(component.raw, component.payload, marker):
                    report_lines.append(f"  {name:<20} matched (recompressed)")
                else:
                    report_lines.append(
                        f"  {name:<20} matched, but recompression differs; "
                        "carrying the original bytes forward"
                    )
                    packed = component.raw
            else:
                report_lines.append(f"  {name:<20} matched (recompressed)")
            blob = bytes(packed)
        else:
            blob = bytes(rebuilt)
            report_lines.append(f"  {name:<20} matched")

        if component.kind == "overlay":
            continue  # overlays live in the file system; handled by dumpfs
        offset = component.rom_offset
        if len(blob) != component.size_on_media:
            report_lines.append(
                f"  {name:<20} size changed on media; rebuild needs a "
                "re-laid-out image (not implemented)"
            )
            all_ok = False
            continue
        image[offset : offset + len(blob)] = blob

    output = os.path.join(BUILD_DIR, "rebuilt.nds")
    write_file(output, bytes(image))
    _say("rebuild report:")
    _say("\n".join(report_lines))
    _say()
    if bytes(image) == rom.data:
        _say(f"byte-identical to the base ROM: {output}")
        _say(f"sha256 {hexdigest(bytes(image))}")
        return 0
    _say(f"DIFFERS from the base ROM: {output}")
    _say(f"rebuilt sha256 {hexdigest(bytes(image))}")
    _say(f"base    sha256 {rom.digest()}")
    differing = sum(1 for a, b in zip(image, rom.data) if a != b)
    _say(f"differing bytes: {differing} of {len(rom.data)}")
    return 0 if all_ok and args.allow_diff else 1


# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------
def cmd_progress(args: argparse.Namespace) -> int:
    """Report how much of the binary is expressed as C."""
    sizes: list[tuple[str, int]] = []
    if os.path.isdir(DATA_DIR):
        for filename in sorted(os.listdir(DATA_DIR)):
            if filename.endswith(".json") and filename != "manifest.json":
                path = os.path.join(DATA_DIR, filename)
                try:
                    with open(path, encoding="utf-8") as handle:
                        meta = json.load(handle)
                except (OSError, json.JSONDecodeError):
                    continue
                name = os.path.splitext(filename)[0]
                sizes.append((name, meta.get("payload_size", 0)))
    progress = load_progress(asm_dir=ASM_DIR, src_dir="src", image_sizes=sizes)
    _say(progress.report())
    if args.json:
        save_progress(progress, args.json)
        _say(f"\nwrote {args.json}")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sr",
        description="Rush-Decomp toolchain for Nintendo DS cartridge images.",
        epilog=(
            "This project contains no ROM and no data derived from one. "
            "Supply your own dump; see docs/ROM_SETUP.md."
        ),
    )
    parser.add_argument("--version", action="version", version="Rush-Decomp tools 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_rom(p: argparse.ArgumentParser) -> None:
        p.add_argument("--rom", default=None, help=f"cartridge image (default {DEFAULT_ROM})")

    p = sub.add_parser("report", help="print a structural report of the image")
    add_rom(p)
    p.add_argument("--json", action="store_true", help="append the provenance record")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("extract", help="split the image into data/")
    add_rom(p)
    p.add_argument("--banner", action="store_true", help="also dump the icon/title banner")
    p.add_argument("--secure", action="store_true", help="also dump the secure area")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("dumpfs", help="unpack the Nitro file system into fsdump/")
    add_rom(p)
    p.add_argument("--filter", default=None, help="only paths containing this substring")
    p.add_argument("--limit", type=int, default=60, help="rows in the printed listing")
    p.add_argument("--list", action="store_true", help="print the listing")
    p.set_defaults(func=cmd_dumpfs)

    for verb, func, helptext in (
        ("disasm", cmd_disasm, "disassemble one component into asm/"),
        ("verify", cmd_verify, "disassemble, reassemble and compare byte for byte"),
    ):
        p = sub.add_parser(verb, help=helptext)
        p.add_argument("name", help="component name, e.g. arm9, arm7, arm9_overlay_003")
        p.add_argument("--base", type=lambda v: int(v, 0), default=None,
                       help="override the base address")
        p.add_argument("--entry", type=lambda v: int(v, 0), default=None,
                       help="override the entry address")
        p.add_argument("--no-prologues", action="store_true",
                       help="skip the prologue scan (disasm only)")
        p.add_argument("--no-windows", action="store_true",
                       help="skip window classification (disasm only)")
        p.add_argument("--output", default=None, help="write reassembled bytes here")
        p.add_argument("--strict", action="store_true", help="exit non-zero on mismatch")
        p.set_defaults(func=func)

    p = sub.add_parser("rebuild", help="reassemble asm/ into a cartridge image")
    add_rom(p)
    p.add_argument("--keep-going", action="store_true",
                   help="continue past the first mismatching component")
    p.add_argument("--allow-diff", action="store_true",
                   help="exit zero even if the image differs")
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser("progress", help="report matching progress")
    p.add_argument("--json", default=None, help="also write the record to this path")
    p.set_defaults(func=cmd_progress)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RomError as error:
        return _fail(str(error))
    except lz77.LZError as error:
        return _fail(f"LZ77: {error}")
    except RuntimeError as error:
        return _fail(str(error))
    except KeyboardInterrupt:
        return _fail("interrupted")


if __name__ == "__main__":
    raise SystemExit(main())
