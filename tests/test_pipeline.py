#!/usr/bin/env python3
"""End-to-end tests for the Rush-Decomp toolchain.

Runs against the synthetic image built by :mod:`make_test_rom`, never against a
retail cartridge.  That is the point: these tests can run in CI, on a fresh
clone, with no copyrighted data present anywhere.

Runnable three ways::

    make test                          # via the Makefile
    python3 tests/test_pipeline.py     # standalone, no dependencies
    pytest tests/test_pipeline.py      # if pytest is installed

The standalone runner exists so that ``make test`` works on a machine that has
the two pip dependencies but not pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)

from rushdecomp import lz77  # noqa: E402
from rushdecomp.arm9 import (  # noqa: E402
    ARM9_STATIC_MARKER,
    ARM9Image,
    footer_magic_for,
    verify_footer_magic,
)
from rushdecomp.banner import Banner  # noqa: E402
from rushdecomp.crc import crc16, crc16_ccitt  # noqa: E402
from rushdecomp.header import Header, looks_like_nds  # noqa: E402
from rushdecomp.rom import NDSRom, RomError  # noqa: E402
from rushdecomp.util import p32  # noqa: E402

from make_test_rom import build_test_rom  # noqa: E402

CAPSTONE = True
try:
    import capstone  # noqa: F401
except ImportError:
    CAPSTONE = False

KEYSTONE = True
try:
    import keystone  # noqa: F401
except ImportError:
    KEYSTONE = False


def _have(name: str, present: bool):
    return unittest.skipUnless(present, f"{name} is not installed")


class Lz77Test(unittest.TestCase):
    """The compression layer, on its own terms."""

    PATTERNS = {
        "empty": b"",
        "single": b"\x41",
        "zeros": bytes(4096),
        "ramp": bytes(range(256)) * 4,
        "repeated": b"abcabcabc" * 200,
        "ones": b"\xff" * 2048,
        "mixed": (b"\x00" * 100 + b"pattern!" * 50) * 8,
    }

    def test_round_trip_both_schemes(self):
        for label, pattern in self.PATTERNS.items():
            for marker in (lz77.Marker.LZ10, lz77.Marker.LZ11):
                with self.subTest(pattern=label, marker=hex(marker)):
                    packed = lz77.compress(pattern, marker)
                    if not pattern:
                        self.assertEqual(bytes(packed), b"")
                        continue
                    self.assertEqual(packed[0], marker)
                    self.assertEqual(bytes(lz77.decompress(packed, marker)), pattern)

    def test_recompression_is_stable(self):
        """Re-encoding what we decoded must reproduce our own encoder output."""
        for label, pattern in self.PATTERNS.items():
            if not pattern:
                continue
            for marker in (lz77.Marker.LZ10, lz77.Marker.LZ11):
                with self.subTest(pattern=label, marker=hex(marker)):
                    packed = lz77.compress(pattern, marker)
                    self.assertTrue(
                        lz77.recompression_matches(packed, pattern, marker),
                        "encoder is not deterministic",
                    )

    def test_compressed_is_smaller_for_compressible_input(self):
        """Each scheme is bound by its own maximum run length, not by ours.

        LZ10 encodes at most 18 bytes per two-byte reference, so 4096 identical
        bytes cannot go below roughly 490.  LZ11 reaches 272 bytes per
        three-byte reference and collapses the same input to a handful of
        bytes.  Asserting one bound for both would just be asserting the wrong
        thing about LZ10.
        """
        pattern = self.PATTERNS["zeros"]
        lz10 = lz77.compress(pattern, lz77.Marker.LZ10)
        lz11 = lz77.compress(pattern, lz77.Marker.LZ11)
        self.assertLess(len(lz10), len(pattern) // 4)
        self.assertLess(len(lz11), 64)
        self.assertLess(len(lz11), len(lz10) // 10)

    def test_probe_rejects_non_lz(self):
        self.assertFalse(lz77.probe(b"\xe9\x2d\x40\x10").compressed)
        self.assertFalse(lz77.probe(b"\x00\x00\x00\x00").compressed)
        self.assertFalse(lz77.probe(b"\x10").compressed)

    def test_probe_accepts_arm9_marker_as_lz10(self):
        packed = bytearray(lz77.compress(b"\x00" * 512, lz77.Marker.LZ10))
        packed[0] = ARM9_STATIC_MARKER
        info = lz77.probe(bytes(packed))
        self.assertTrue(info.compressed)
        self.assertEqual(info.marker, lz77.Marker.LZ10)

    def test_truncated_stream_raises(self):
        packed = lz77.compress(bytes(2048), lz77.Marker.LZ10)
        with self.assertRaises(lz77.LZError):
            lz77.decompress(packed[:-3], lz77.Marker.LZ10)

    def test_bad_distance_raises(self):
        """A reference reaching past the start of the output must be refused."""
        bogus = bytearray(lz77.header_of(lz77.Marker.LZ10, 16))
        bogus += bytes((0x80, 0xFF, 0xFF))  # flag=reference, disp far too big
        with self.assertRaises(lz77.LZError):
            lz77.decompress(bytes(bogus), lz77.Marker.LZ10)


class CrcTest(unittest.TestCase):
    def test_crc16_known_values(self):
        """Pin the polynomial *and* Nintendo's initial value, separately.

        Reflected poly 0xA001 with ``init=0`` is CRC-16/ARC, whose check value
        over "123456789" is 0xBB3D -- that confirms the table.  Nintendo uses
        ``init=0xFFFF``, which is CRC-16/MODBUS and gives 0x4B37.  Checking both
        means a future edit cannot silently swap the initial value and still
        pass by having the polynomial right.
        """
        self.assertEqual(crc16(b"123456789", init=0x0000), 0xBB3D)
        self.assertEqual(crc16(b"123456789"), 0x4B37)
        self.assertEqual(crc16(b""), 0xFFFF)

    def test_crc16_ccitt_known_value(self):
        # CRC-16/CCITT-FALSE over "123456789" is 0x29B1.
        self.assertEqual(crc16_ccitt(b"123456789"), 0x29B1)


class Arm9FooterTest(unittest.TestCase):
    def test_footer_magic_both_spellings(self):
        size = 0x1234
        arithmetic = footer_magic_for(size, arithmetic=True)
        logical = footer_magic_for(size, arithmetic=False)
        self.assertEqual(verify_footer_magic(arithmetic, size), "arithmetic")
        self.assertEqual(verify_footer_magic(logical, size), "logical")
        self.assertIsNone(verify_footer_magic(arithmetic + 1, size))

    def test_footer_magic_rejects_wrong_size(self):
        magic = footer_magic_for(0x1000)
        self.assertIsNone(verify_footer_magic(magic, 0x2000))

    def test_round_trip_through_arm9image(self):
        payload = bytes((i * 31 + 7) & 0xFF for i in range(4096))
        payload = payload[:2048] + b"\x00" * 1024 + payload[3072:]
        body = bytearray(lz77.compress(payload, lz77.Marker.LZ10))
        body[0] = ARM9_STATIC_MARKER
        media = bytes(body) + p32(len(payload)) + p32(len(body) + 12)
        media += p32(footer_magic_for(len(payload)))
        image = ARM9Image.parse(media)
        self.assertTrue(image.compressed)
        self.assertTrue(image.footer_present)
        self.assertEqual(image.footer_decompressed_size, len(payload))
        self.assertEqual(image.footer_compressed_size, len(media))
        self.assertEqual(image.payload, payload)
        self.assertEqual(ARM9Image.parse(image.recompress()).payload, payload)

    def test_uncompressed_arm9_passes_through(self):
        payload = b"\xe9\x2d\x40\x10" + bytes(60)
        image = ARM9Image.parse(payload)
        self.assertFalse(image.compressed)
        self.assertEqual(image.payload, payload)

    def test_arm7_starting_with_0x10_is_not_treated_as_lz10(self):
        """``push {r4, lr}`` begins with 0x10 -- the LZ10 marker.

        This is the collision that makes byte-sniffing unsafe for the ARM7,
        which the BIOS loads raw.  It must survive untouched.
        """
        payload = bytes.fromhex("10402de9" "0130a0e3" "1eff2fe1")
        info = lz77.probe(payload)
        self.assertTrue(info.compressed, "sniffing does see a marker")
        with self.assertRaises(lz77.LZError):
            lz77.decompress(payload, lz77.Marker.LZ10)


class HeaderTest(unittest.TestCase):
    def test_parse_and_reserialise(self):
        image = build_test_rom()
        header = Header.parse(image)
        self.assertEqual(header.game_code, "TSTR")
        self.assertEqual(bytes(header.to_bytes()), image[:0x4000])
        self.assertTrue(all(header.verify_crcs().values()))

    def test_sign_recomputes_crc(self):
        image = bytearray(build_test_rom())
        header = Header.parse(bytes(image))
        header.rom_version = 7
        header.sign()
        updated = header.to_bytes()
        self.assertEqual(updated[0x01F], 7)
        self.assertEqual(crc16(bytes(updated[:0x15E])), header.header_crc)

    def test_field_diff(self):
        image = build_test_rom()
        a = Header.parse(image)
        b = Header.parse(image)
        b.rom_version = 3
        diff = a.field_diff(b)
        self.assertTrue(any("rom_version" in line for line in diff))

    def test_looks_like_nds_rejects_junk(self):
        self.assertIsNotNone(looks_like_nds(b"too short"))
        self.assertIsNotNone(looks_like_nds(bytes(0x400)))
        self.assertIsNone(looks_like_nds(build_test_rom()))

    def test_open_rejects_junk(self):
        with self.assertRaises(RomError):
            NDSRom(bytes(0x400))


class BannerTest(unittest.TestCase):
    def test_parse_and_titles(self):
        image = build_test_rom()
        rom = NDSRom(image)
        banner = rom.banner
        self.assertEqual(banner.version, 1)
        self.assertEqual(banner.titles["english"], "Rush-Decomp Self-Test")
        self.assertIn("Rush-Decomp", banner.titles["japanese"])
        self.assertNotIn("\u1222", banner.titles["japanese"])

    def test_sign_then_verify(self):
        image = build_test_rom()
        banner = Banner.parse(NDSRom(image).banner.raw)
        banner.sign()
        checks = banner.verify_crcs()
        self.assertTrue(checks)
        self.assertTrue(all(checks.values()))

    def test_icon_geometry(self):
        """32x32 at 4bpp is 0x200 bytes; a wrong size walks into the titles."""
        image = build_test_rom()
        banner = NDSRom(image).banner
        self.assertEqual(len(banner.icon), 0x200)
        rows = banner.decode_icon()
        self.assertEqual(len(rows), 32)
        self.assertEqual(len(rows[0]), 32)

    def test_bad_version_rejected(self):
        blob = bytearray(0x840)
        blob[0:2] = (99).to_bytes(2, "little")
        with self.assertRaises(ValueError):
            Banner.parse(bytes(blob))


class RomStructureTest(unittest.TestCase):
    """The whole image, parsed as one object."""

    @classmethod
    def setUpClass(cls):
        cls.image = build_test_rom()
        cls.rom = NDSRom(cls.image, path="<synthetic>")

    def test_arm9_is_footer_compressed(self):
        self.assertTrue(self.rom.arm9.compressed)
        self.assertEqual(self.rom.arm9.compression_source, "footer")
        self.assertGreater(
            self.rom.arm9.payload_size, self.rom.arm9.size_on_media
        )

    def test_arm7_is_raw(self):
        """The ARM7 must never be guessed at; the BIOS loads it uncompressed."""
        self.assertFalse(self.rom.arm7.compressed)
        self.assertEqual(self.rom.arm7.compression_source, "none")
        self.assertEqual(self.rom.arm7.payload, self.rom.arm7.raw)
        self.assertEqual(self.rom.arm7.payload[0], 0x10, "starts with push {r4, lr}")

    def test_overlays(self):
        table = self.rom.arm9_overlays
        self.assertEqual(len(table), 3)
        self.assertEqual([o.overlay_id for o in table], [0, 1, 2])
        self.assertEqual([o.file_id for o in table], [1, 2, 3])
        self.assertTrue(table[1].compressed)
        self.assertEqual(table[1].compressed_flag_source, "ram_size")
        self.assertFalse(table[0].compressed)
        for overlay in table:
            payload = self.rom.overlay_payload(overlay)
            self.assertEqual(len(payload), overlay.ram_size)

    def test_overlay_table_round_trips(self):
        table = self.rom.arm9_overlays
        h = self.rom.header
        original = self.image[h.arm9_overlay_offset:
                              h.arm9_overlay_offset + h.arm9_overlay_size]
        self.assertEqual(table.to_bytes(), original)

    def test_file_system(self):
        fs = self.rom.filesystem
        paths = {entry.path for entry in fs}
        self.assertIn("selftest.txt", paths)
        self.assertIn("gfx/checkerboard.bin", paths)
        self.assertIn("deep/nested/dir/leaf.bin", paths)
        self.assertEqual(fs.read(self.image, fs.get("deep/nested/dir/leaf.bin")), b"leaf")
        self.assertEqual(fs.read(self.image, fs.get("sound/blank.bin")), b"")

    def test_fat_top_byte_flag_is_masked(self):
        """File ID 3 carries a flag in the top byte of its start word."""
        entry = self.rom.filesystem.fat[3]
        self.assertTrue(entry.has_flags)
        self.assertEqual(entry.flags, 1)
        self.assertLess(entry.start, 1 << 24)
        self.assertGreater(entry.size, 0)

    def test_no_parser_warnings(self):
        self.assertEqual(self.rom.filesystem.warnings, [])
        self.assertEqual(self.rom.arm9_overlays.sanity_report(), [])

    def test_secure_area_is_plaintext_stub(self):
        secure = self.rom.secure_area
        self.assertEqual(secure.status, "already-decrypted")
        self.assertEqual(secure.raw[:8], b"encryObj")

    def test_provenance_is_complete(self):
        record = self.rom.provenance()
        self.assertEqual(record["game_code"], "TSTR")
        self.assertEqual(record["components"]["arm9_overlays"], 3)
        self.assertEqual(record["components"]["files"], 9)
        self.assertNotIn("error", record["components"]["arm9"])
        self.assertNotIn("error", record["components"]["arm7"])


@_have("capstone", CAPSTONE)
@_have("keystone", KEYSTONE)
class RoundTripTest(unittest.TestCase):
    """The property the whole project rests on: disasm -> asm -> same bytes."""

    @classmethod
    def setUpClass(cls):
        cls.rom = NDSRom(build_test_rom(), path="<synthetic>")

    def components(self):
        yield "arm9", self.rom.arm9
        yield "arm7", self.rom.arm7
        for overlay in self.rom.arm9_overlays:
            yield overlay.label(), self.rom.overlay_image(overlay)

    def test_every_component_round_trips(self):
        from rushdecomp.assembler import round_trip

        for name, component in self.components():
            with self.subTest(component=name):
                ok, report, rebuilt = round_trip(
                    component.payload,
                    component.ram_address,
                    entry_address=component.entry_address,
                    name=name,
                )
                self.assertTrue(
                    ok,
                    f"{name} did not round-trip:\n{report.describe()}",
                )
                self.assertEqual(bytes(rebuilt), component.payload)
                self.assertEqual(report.failures, [])

    def test_disassembly_is_deterministic(self):
        """Two runs over the same bytes must produce identical text."""
        from rushdecomp.disasm import Disassembler

        for name, component in self.components():
            with self.subTest(component=name):
                outputs = []
                for _ in range(2):
                    d = Disassembler(
                        component.payload, component.ram_address,
                        entry_address=component.entry_address, name=name,
                    )
                    outputs.append(d.render(d.disassemble()))
                self.assertEqual(outputs[0], outputs[1])

    def test_arm9_modes_are_both_present(self):
        """The synthetic ARM9 mixes ARM and Thumb and calls between them."""
        from rushdecomp.disasm import Disassembler, Mode

        component = self.rom.arm9
        d = Disassembler(
            component.payload, component.ram_address,
            entry_address=component.entry_address, name="arm9",
        )
        result = d.disassemble()
        modes = {ins.mode for ins in result.instructions}
        self.assertIn(Mode.ARM, modes)
        self.assertIn(Mode.THUMB, modes)
        self.assertEqual(result.stats["unresolved_tails"] >= 0, True)


@_have("capstone", CAPSTONE)
@_have("keystone", KEYSTONE)
class CliTest(unittest.TestCase):
    """The CLI verbs, driven as a user would drive them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rom_path = os.path.join(self.tmp.name, "selftest.nds")
        with open(self.rom_path, "wb") as handle:
            handle.write(build_test_rom())

    def run_cli(self, *args, cwd=None):
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "sr"), *args],
            cwd=cwd or self.tmp.name,
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "tools")},
        )
        return result

    def test_full_pipeline(self):
        self.assertEqual(self.run_cli("extract", "--rom", self.rom_path).returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "data", "arm9.bin")))

        for name in ("arm9", "arm7"):
            dis = self.run_cli("disasm", name)
            self.assertEqual(dis.returncode, 0, dis.stderr)
            verify = self.run_cli("verify", name, "--strict")
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self.assertIn("MATCH", verify.stdout)

        rebuild = self.run_cli("rebuild", "--rom", self.rom_path, "--keep-going")
        self.assertEqual(rebuild.returncode, 0, rebuild.stdout + rebuild.stderr)
        self.assertIn("byte-identical", rebuild.stdout)

        rebuilt_path = os.path.join(self.tmp.name, "build", "rebuilt.nds")
        with open(rebuilt_path, "rb") as handle:
            self.assertEqual(handle.read(), open(self.rom_path, "rb").read())

    def test_progress(self):
        self.run_cli("extract", "--rom", self.rom_path)
        self.run_cli("disasm", "arm9")
        out = self.run_cli("progress", "--json", "build/progress.json")
        self.assertEqual(out.returncode, 0, out.stderr)
        record_path = os.path.join(self.tmp.name, "build", "progress.json")
        self.assertTrue(os.path.isfile(record_path))
        with open(record_path, encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertIn("arm9", record["images"])
        self.assertEqual(record["matched_bytes"], 0)

    def test_dumpfs(self):
        out = self.run_cli("dumpfs", "--rom", self.rom_path)
        self.assertEqual(out.returncode, 0, out.stderr)
        leaf = os.path.join(self.tmp.name, "fsdump", "deep", "nested", "dir", "leaf.bin")
        self.assertTrue(os.path.isfile(leaf))
        with open(leaf, "rb") as handle:
            self.assertEqual(handle.read(), b"leaf")

    def test_report(self):
        out = self.run_cli("report", "--rom", self.rom_path)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("TSTR", out.stdout)
        self.assertIn("RUSHDECOMP", out.stdout)

    def test_missing_rom_explains_itself(self):
        out = self.run_cli("report", "--rom", os.path.join(self.tmp.name, "nope.nds"))
        self.assertNotEqual(out.returncode, 0)
        combined = out.stdout + out.stderr
        self.assertIn("never contains a ROM", combined)
        self.assertIn("docs/ROM_SETUP.md", combined)


class RepoHygieneTest(unittest.TestCase):
    """The rule that matters most: no ROM-derived bytes in git."""

    def test_gitignore_covers_the_dangerous_paths(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as handle:
            rules = handle.read()
        for required in ("baserom/", "*.nds", "data/", "asm/", "fsdump/", "build/"):
            self.assertIn(required, rules, f".gitignore must exclude {required}")

    def test_no_tracked_file_is_a_rom_or_derived_from_one(self):
        listing = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        if listing.returncode != 0:
            self.skipTest("not a git checkout")
        tracked = [line for line in listing.stdout.splitlines() if line]
        forbidden_suffixes = (".nds", ".srl", ".bin", ".ppm")
        allowed = {
            os.path.join("tests", "data", "testrom.nds"),
        }
        offenders = [
            path for path in tracked
            if path.endswith(forbidden_suffixes) and path not in allowed
        ]
        self.assertEqual(offenders, [], "tracked files must not include ROM data")

    def test_no_tracked_file_is_large(self):
        listing = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        if listing.returncode != 0:
            self.skipTest("not a git checkout")
        for path in listing.stdout.splitlines():
            if not path:
                continue
            full = os.path.join(ROOT, path)
            if os.path.isfile(full):
                self.assertLess(
                    os.path.getsize(full), 1 << 20,
                    f"{path} is over 1 MiB; generated data belongs outside git",
                )


class ProgressTest(unittest.TestCase):
    def test_annotations_are_collected_and_validated(self):
        from rushdecomp.progress import load_progress

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            asm = os.path.join(tmp, "asm")
            os.makedirs(src)
            os.makedirs(asm)
            with open(os.path.join(asm, "arm9.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "stats": {"code_bytes": 4096, "image_bytes": 8192},
                    "functions": [
                        {"start": 0x02000000, "end": 0x02000040,
                         "mode": "arm", "origin": "entry"},
                    ],
                    "labels": [{"name": "func_02000000", "kind": "function"}],
                }, handle)
            with open(os.path.join(src, "boot.c"), "w", encoding="utf-8") as handle:
                handle.write(
                    "/* @rush_match arm9 0x40 func_02000000 */\n"
                    "/* @rush_match arm9 999 func_deadbeef */\n"
                    "/* @rush_match arm9 8 func_02000000 */\n"
                )
            progress = load_progress(
                asm_dir=asm, src_dir=src, image_sizes=[("arm9", 8192)]
            )
            self.assertEqual(progress.matched_bytes, 0x40 + 999 + 8)
            self.assertEqual(progress.images["arm9"].disassembled_bytes, 4096)
            self.assertEqual(progress.images["arm9"].functions_total, 1)
            # Both bad annotations must be reported: an unknown symbol, and a
            # byte count that disagrees with the disassembly.
            joined = " ".join(progress.images["arm9"].problems)
            self.assertIn("func_deadbeef", joined)
            self.assertIn("8 bytes", joined)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    outcome = runner.run(suite)
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
