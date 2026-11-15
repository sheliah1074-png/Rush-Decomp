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
from rushdecomp.header import (  # noqa: E402
    Header,
    declared_extents,
    looks_like_cartridge_prefix,
    looks_like_nds,
    required_length,
    size_report,
)
from rushdecomp.rom import NDSRom, RomError, diagnose  # noqa: E402
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


class SizeReportTest(unittest.TestCase):
    """Trim vs. truncation -- a complete dump versus one with a hole in it.

    A retail Sonic Rush card is 64 MiB, and dumps in the wild are very often
    *trimmed*: the trailing padding removed, data intact.  That looks identical
    at a glance to a dump that is genuinely missing its tail, and only the
    second one produces a disassembly with a gap in it.  These tests pin the
    distinction down.
    """

    @classmethod
    def setUpClass(cls):
        cls.image = build_test_rom()
        cls.rom = NDSRom(cls.image, path="<synthetic>")

    def test_a_complete_but_trimmed_image_is_not_called_truncated(self):
        text = "\n".join(self.rom.size_report())
        self.assertIn("trimmed", text)
        self.assertNotIn("TRUNCATED", text)

    def test_truncation_is_detected_even_though_parsing_refuses_the_image(self):
        """The whole point: the diagnosis must not need a parseable image."""
        data = self.image[: -0x1000]
        with self.assertRaises(RomError):
            NDSRom(data)
        text = "\n".join(diagnose(data, path="truncated.nds"))
        self.assertIn("TRUNCATED", text)
        self.assertIn("ARM7 extent", text, "the rejection reason is kept")

    def test_truncation_quotes_the_exact_shortfall(self):
        data = self.image[: -0x1000]
        need, _why = required_length(Header.parse(data))
        text = "\n".join(diagnose(data))
        self.assertIn(f"0x{need - len(data):x}", text)

    def test_a_full_dump_is_recognised(self):
        data = bytearray(self.image)
        data[0x014] = 0  # capacity code 0 -> 0x20000, small enough to pad out
        data += bytes(0x20000 - len(data))
        text = "\n".join(size_report(Header.parse(bytes(data)), len(data)))
        self.assertIn("full untrimmed dump", text)

    def test_an_over_long_image_is_flagged(self):
        data = bytearray(self.image)
        data[0x014] = 0
        data += bytes(0x20000 - len(data) + 0x200)
        text = "\n".join(size_report(Header.parse(bytes(data)), len(data)))
        self.assertIn("LONGER than capacity", text)

    def test_the_fat_end_outranks_the_header_extents(self):
        """Nitro files sit past every table, so the FAT is the real end of data."""
        header = Header.parse(self.image)
        alone, _why = required_length(header)
        with_fat, why = required_length(header, data_end=alone + 0x9000)
        self.assertEqual(with_fat, alone + 0x9000)
        self.assertIn("Nitro file", why)

    def test_absent_regions_do_not_invent_extents(self):
        """A zero-length table must not report an extent ending at its offset."""
        header = Header.parse(self.image)
        extents = declared_extents(header)
        for name, start, end in extents:
            self.assertGreater(end, start, name)
        self.assertIn("ARM9", {name for name, _s, _e in extents})

    def test_too_small_to_diagnose(self):
        text = "\n".join(diagnose(b"\x00" * 0x40))
        self.assertIn("at least 0x160", text)
        self.assertIn("header itself is absent", text)


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

    def test_report_diagnoses_a_truncated_image(self):
        """A dump with its tail missing must say how much, not just fail."""
        truncated = os.path.join(self.tmp.name, "truncated.nds")
        with open(truncated, "wb") as handle:
            handle.write(build_test_rom()[:-0x1000])
        out = self.run_cli("report", "--rom", truncated)
        self.assertEqual(out.returncode, 1)
        self.assertIn("TRUNCATED", out.stdout)
        self.assertIn("header-only diagnosis", out.stdout)
        self.assertIn("ARM7 extent", out.stderr)

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
    """The rule that matters most: no cartridge image in git.

    The policy is a line between the cartridge and work derived from it.  Derived
    work -- disassembly, converted art, symbol maps -- is tracked, and is the
    point of the project.  What must never be tracked is the image itself or its
    verbatim contents.  That is a narrower claim than "nothing derived", and it
    needs a narrower set of checks to enforce.
    """

    #: Paths that must stay ignored: the cartridge and its verbatim contents.
    IGNORED = ("baserom/", "*.nds", "data/", "build/", "fsdump/", "tools/keys/")

    #: Paths that must stay committable: the work product.
    TRACKED = (
        os.path.join("asm", "arm9.s"),
        os.path.join("asm", "arm9.json"),
        os.path.join("assets", "symbols", "arm9.txt"),
        os.path.join("src", "example", "example.c"),
    )

    #: Original code produced by make_test_rom, never a retail dump.
    ALLOWED_IMAGES = {os.path.join("tests", "data", "testrom.nds")}

    def tracked_files(self):
        listing = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        if listing.returncode != 0:
            self.skipTest("not a git checkout")
        return [line for line in listing.stdout.splitlines() if line]

    def test_gitignore_covers_the_cartridge_and_its_verbatim_contents(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as handle:
            rules = handle.read()
        for required in self.IGNORED:
            self.assertIn(required, rules, f".gitignore must exclude {required}")

    def test_gitignore_does_not_swallow_the_work_product(self):
        """The mirror of the test above, and the one that fails silently.

        A rule broad enough to keep a ROM out can easily keep the disassembly out
        as well, and then the repository looks immaculate while containing no
        work at all.  Both directions have to be pinned.
        """
        for path in self.TRACKED:
            probe = subprocess.run(
                ["git", "check-ignore", "-q", path], cwd=ROOT, timeout=60,
            )
            self.assertNotEqual(probe.returncode, 0, f"{path} must be committable")

    def test_no_tracked_file_is_named_like_a_cartridge_image(self):
        forbidden = (".nds", ".srl", ".dsg", ".nd")
        offenders = [
            path for path in self.tracked_files()
            if path.endswith(forbidden) and path not in self.ALLOWED_IMAGES
        ]
        self.assertEqual(offenders, [], "tracked files must not include a cartridge image")

    def test_no_tracked_file_is_a_renamed_cartridge_image(self):
        """Extension rules are trivia.  This one reads the bytes instead."""
        offenders = []
        for path in self.tracked_files():
            if path in self.ALLOWED_IMAGES:
                continue
            full = os.path.join(ROOT, path)
            if not os.path.isfile(full):
                continue
            with open(full, "rb") as handle:
                prefix = handle.read(0x4000)
            reason = looks_like_cartridge_prefix(prefix, file_size=os.path.getsize(full))
            if reason:
                offenders.append(f"{path}: {reason}")
        self.assertEqual(offenders, [], "no tracked file may open with a cartridge header")

    def test_no_tracked_binary_file_is_large(self):
        """Binary blobs stay small; text is exempt, because disassembly is text.

        A full ARM9 listing legitimately runs to tens of megabytes.  A *binary*
        that large does not: it is either a cartridge image or raw extraction
        output, and both belong under baserom/ or data/, which are ignored.
        """
        for path in self.tracked_files():
            full = os.path.join(ROOT, path)
            if not os.path.isfile(full):
                continue
            with open(full, "rb") as handle:
                probe = handle.read(4096)
            try:
                probe.decode("utf-8")
            except UnicodeDecodeError:
                self.assertLess(
                    os.path.getsize(full), 1 << 20,
                    f"{path} is a binary file over 1 MiB; raw extraction output "
                    "belongs in data/, which is gitignored",
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
    # Honour `test_pipeline.py ClassName[.method]` selectors.  Loading the whole
    # module unconditionally would make CI's `RepoHygieneTest`-only job silently
    # run everything instead, which is how a targeted check stops being one.
    loader = unittest.defaultTestLoader
    if len(sys.argv) > 1:
        suite = loader.loadTestsFromNames(sys.argv[1:], module=sys.modules[__name__])
    else:
        suite = loader.loadTestsFromModule(sys.modules[__name__])
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
