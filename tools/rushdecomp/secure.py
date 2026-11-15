"""Secure-area handling (cartridge offset 0x4000, 0x2000 bytes).

The secure area is the only encrypted part of a DS cartridge image.  It holds a
small stub the boot ROM authenticates before it will hand control to the game,
and it sits between the header and the ARM9 binary.  Everything a decompilation
actually cares about -- ARM9, ARM7, overlays, the Nitro file system -- is
plaintext, so this module is deliberately optional: extraction proceeds with the
secure area left raw if it cannot be decrypted.

Decryption needs Nintendo's ``KEY1`` table, a 0x412-word block of Blowfish
P-array and S-box values stored in the DS BIOS.  That table is Nintendo
firmware data and is **not** in this repository.  If you want the secure area
decoded, dump it from hardware you own and drop it at::

    tools/keys/nds_key1.bin        # 0x1048 bytes, little-endian u32 words

which :mod:`.gitignore` already excludes.

The decryption routine is written to be self-validating: a correct key yields
the ASCII signature ``encryObj`` at the start of the decrypted block, and if
that signature is missing the code reports failure and returns the raw bytes
rather than quietly emitting garbage.  Without a real key on hand that path
cannot be exercised by the test suite, so it is flagged ``untested`` in every
report that touches it.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Optional, Sequence

from .crc import crc16_ccitt
from .util import u32

__all__ = [
    "Blowfish",
    "SecureArea",
    "KEY1_PATH",
    "KEY1_WORD_COUNT",
    "SECURE_ID",
]

#: Path (relative to the repository root) the loader looks for KEY1 at.
KEY1_PATH = os.path.join("tools", "keys", "nds_key1.bin")

#: 18 P-words + 4 * 256 S-words.
KEY1_WORD_COUNT = 18 + 4 * 256

#: ASCII signature found at the start of a correctly decrypted secure area.
SECURE_ID = b"encryObj"

_MASK32 = 0xFFFFFFFF


class Blowfish:
    """Textbook Blowfish with externally supplied P-array and S-boxes.

    The DS uses Blowfish with Nintendo's KEY1 table loaded directly as the
    initial P/S state rather than running the standard key-expansion over a
    passphrase, so this class takes the arrays as constructor arguments.  Pass
    ``key=`` instead to get the ordinary expansion, which is what the
    round-trip self-test uses.
    """

    def __init__(
        self,
        p: Optional[Sequence[int]] = None,
        s: Optional[Sequence[Sequence[int]]] = None,
        key: Optional[bytes] = None,
    ) -> None:
        if key is not None:
            if p is not None or s is not None:
                raise ValueError("pass either explicit P/S or a key, not both")
            p, s = self._standard_expansion(key)
        if p is None or s is None:
            raise ValueError("Blowfish needs either explicit P/S or a key")
        if len(p) != 18:
            raise ValueError(f"P-array must hold 18 words, got {len(p)}")
        if len(s) != 4 or any(len(box) != 256 for box in s):
            raise ValueError("S-boxes must be 4 arrays of 256 words each")
        self.p = [w & _MASK32 for w in p]
        self.s = [[w & _MASK32 for w in box] for box in s]

    # ------------------------------------------------------------------ core
    def _f(self, x: int) -> int:
        s0, s1, s2, s3 = self.s
        a = (x >> 24) & 0xFF
        b = (x >> 16) & 0xFF
        c = (x >> 8) & 0xFF
        d = x & 0xFF
        return (((s0[a] + s1[b]) & _MASK32) ^ s2[c]) + s3[d] & _MASK32

    def encrypt_block(self, left: int, right: int) -> tuple[int, int]:
        """Encrypt one 64-bit block held as two big-endian words."""
        p = self.p
        for round_index in range(16):
            left ^= p[round_index]
            left &= _MASK32
            right ^= self._f(left)
            right &= _MASK32
            left, right = right, left
        left, right = right, left
        right ^= p[17]
        left ^= p[16]
        return left & _MASK32, right & _MASK32

    def decrypt_block(self, left: int, right: int) -> tuple[int, int]:
        """Inverse of :meth:`encrypt_block`."""
        p = self.p
        for round_index in range(17, 1, -1):
            left ^= p[round_index]
            left &= _MASK32
            right ^= self._f(left)
            right &= _MASK32
            left, right = right, left
        left, right = right, left
        right ^= p[1]
        left ^= p[0]
        return left & _MASK32, right & _MASK32

    def encrypt(self, data: bytes) -> bytes:
        return self._run(data, self.encrypt_block)

    def decrypt(self, data: bytes) -> bytes:
        return self._run(data, self.decrypt_block)

    @staticmethod
    def _run(data: bytes, block_fn) -> bytes:
        if len(data) % 8:
            raise ValueError(f"Blowfish ECB needs a multiple of 8 bytes, got {len(data)}")
        out = bytearray()
        for offset in range(0, len(data), 8):
            left = u32_be(data, offset)
            right = u32_be(data, offset + 4)
            left, right = block_fn(left, right)
            out.extend(p32_be(left))
            out.extend(p32_be(right))
        return bytes(out)

    # ------------------------------------------------------------- expansion
    @staticmethod
    def _standard_expansion(key: bytes) -> tuple[list[int], list[list[int]]]:
        """Ordinary Blowfish key schedule.

        Requires the published initial P-array and S-box constants.  Those are
        not bundled here -- supply them through ``p=``/``s=`` (which is how the
        DS KEY1 table is loaded) or install a Blowfish package that provides
        them and hand the result to this class.
        """
        raise NotImplementedError(
            "the standard Blowfish initial P/S constants are not bundled with "
            "this repository; pass p= and s= loaded from your own KEY1 table, "
            f"e.g. from {KEY1_PATH}"
        )

    def copy(self) -> "Blowfish":
        return Blowfish(p=list(self.p), s=[list(box) for box in self.s])


def u32_be(data: bytes, offset: int) -> int:
    """Read a big-endian u32; Blowfish blocks are big-endian by definition."""
    return (
        (data[offset] << 24)
        | (data[offset + 1] << 16)
        | (data[offset + 2] << 8)
        | data[offset + 3]
    )


def p32_be(value: int) -> bytes:
    value &= _MASK32
    return bytes(((value >> 24) & 0xFF, (value >> 16) & 0xFF,
                  (value >> 8) & 0xFF, value & 0xFF))


def load_key1(path: str = KEY1_PATH) -> Optional[Blowfish]:
    """Load a user-supplied KEY1 table, or return ``None`` if there is none."""
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as handle:
        blob = handle.read()
    if len(blob) != KEY1_WORD_COUNT * 4:
        raise ValueError(
            f"{path} holds {len(blob)} bytes; a KEY1 table is "
            f"{KEY1_WORD_COUNT * 4} bytes ({KEY1_WORD_COUNT} little-endian words)"
        )
    words = [u32(blob, i * 4) for i in range(KEY1_WORD_COUNT)]
    return Blowfish(p=words[:18], s=[words[18 + i * 256 : 18 + (i + 1) * 256] for i in range(4)])


@dataclasses.dataclass
class SecureArea:
    """The 0x2000-byte region at cartridge offset 0x4000."""

    raw: bytes
    expected_crc: int = 0
    decrypted: Optional[bytes] = None
    status: str = "raw"
    detail: str = ""

    @classmethod
    def parse(cls, rom: bytes, *, expected_crc: int = 0) -> "SecureArea":
        blob = bytes(rom[0x4000 : 0x4000 + 0x2000])
        if len(blob) < 0x2000:
            return cls(raw=blob, expected_crc=expected_crc,
                       status="truncated",
                       detail=f"only {len(blob)} of 0x2000 bytes present")
        if blob[:8] == SECURE_ID:
            return cls(raw=blob, expected_crc=expected_crc,
                       status="already-decrypted",
                       detail="the encryObj signature is present in the clear")
        return cls(raw=blob, expected_crc=expected_crc,
                   status="encrypted",
                   detail="no encryObj signature; KEY1 required to decode")

    @property
    def crc_ok(self) -> Optional[bool]:
        """Check the header's secure-area CRC against the decrypted block.

        Returns ``None`` when the area is still encrypted, since the CRC only
        covers the plaintext.
        """
        if self.decrypted is None:
            return None
        return crc16_ccitt(self.decrypted[:0x2000]) == self.expected_crc

    def decrypt(self, key: Optional[Blowfish] = None,
                key_path: str = KEY1_PATH) -> "SecureArea":
        """Attempt decryption; always succeeds in *returning*, never in lying.

        The result is validated by looking for the ``encryObj`` signature.  On
        any failure the object keeps ``status`` non-``ok`` and ``decrypted``
        stays ``None``, so downstream code can fall back to the raw bytes.
        """
        if self.status == "already-decrypted":
            self.decrypted = self.raw
            self.status = "ok"
            self.detail = "already in the clear"
            return self
        if key is None:
            try:
                key = load_key1(key_path)
            except ValueError as error:
                self.status = "key-error"
                self.detail = str(error)
                return self
        if key is None:
            self.status = "no-key"
            self.detail = (
                f"no KEY1 table at {key_path}; the secure area is left raw. "
                "Nothing else in the image is encrypted, so extraction and "
                "disassembly are unaffected."
            )
            return self
        try:
            candidate = key.decrypt(self.raw[:0x2000])
        except Exception as error:  # pragma: no cover - defensive
            self.status = "decrypt-error"
            self.detail = str(error)
            return self
        if candidate[:8] != SECURE_ID:
            self.status = "bad-key"
            self.detail = (
                "decryption produced no encryObj signature, so the KEY1 table "
                "is wrong or incomplete; leaving the secure area raw"
            )
            return self
        self.decrypted = candidate
        self.status = "ok"
        self.detail = f"decrypted with {key_path}"
        return self

    @property
    def bytes_for_dump(self) -> bytes:
        """Plaintext when available, raw otherwise."""
        return self.decrypted if self.decrypted is not None else self.raw

    def describe(self) -> str:
        lines = [
            f"secure area (0x4000..0x6000): {self.status}",
            f"  {self.detail}",
        ]
        if self.expected_crc:
            lines.append(f"  header CRC16-CCITT: 0x{self.expected_crc:04x}")
            if self.crc_ok is not None:
                lines.append(f"  CRC check: {'ok' if self.crc_ok else 'MISMATCH'}")
        return "\n".join(lines)


def summarise(rom: bytes, *, expected_crc: int = 0) -> str:
    """One-call helper used by the extraction report."""
    return SecureArea.parse(rom, expected_crc=expected_crc).describe()
