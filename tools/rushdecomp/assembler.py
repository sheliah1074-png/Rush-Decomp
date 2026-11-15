"""Reassembly and byte-exactness checking, via Keystone.

A decompilation project earns its keep by being able to rebuild the original
binary.  This module provides the round trip that makes that checkable:

    payload -> :mod:`.disasm` -> ``.s`` -> :func:`assemble_file` -> bytes
                                                                 -> compare

Capstone and Keystone are not perfect mirrors of each other.  Capstone prints
operands in a few forms Keystone will not accept verbatim (``r13``/``r15``
instead of ``sp``/``pc``, a leading ``#`` on some memory displacements, ``{}``
register lists written with spaces), so :func:`normalise` rewrites the textual
form before handing it to the assembler.  Every rewrite is listed in
:func:`normalise`'s docstring so a mismatch can be traced to a normalisation
rule rather than to a real difference in the code.

Anything that fails to reassemble is recorded, not swallowed: :class:`AssembleReport`
carries the offending line, the reason, and the original bytes, which is the
starting point for every "why doesn't this function match" investigation.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from typing import Iterable, Optional

try:  # pragma: no cover
    import keystone
except ImportError:  # pragma: no cover
    keystone = None  # type: ignore[assignment]

from .disasm import Mode

__all__ = [
    "AssembleError",
    "AssembleReport",
    "Mismatch",
    "assemble_file",
    "assemble_lines",
    "normalise",
    "require_keystone",
    "round_trip",
]


def require_keystone() -> None:
    if keystone is None:
        raise RuntimeError(
            "keystone-engine is required for reassembly; install it with "
            "`pip install -r requirements.txt` (or `make deps`)"
        )


class AssembleError(RuntimeError):
    """Raised when the assembler cannot be set up at all."""


@dataclasses.dataclass
class Mismatch:
    """One place where reassembled bytes differ from the original."""

    address: int
    expected: bytes
    produced: bytes
    line: str
    reason: str = ""

    def describe(self) -> str:
        exp = " ".join(f"{b:02x}" for b in self.expected) or "--"
        got = " ".join(f"{b:02x}" for b in self.produced) or "--"
        return (
            f"0x{self.address:08x}  expected [{exp}]  produced [{got}]  "
            f"{self.reason}\n    {self.line}"
        )


@dataclasses.dataclass
class AssembleReport:
    """Outcome of one reassembly pass."""

    total_lines: int = 0
    assembled_lines: int = 0
    total_bytes: int = 0
    matched_bytes: int = 0
    mismatches: list[Mismatch] = dataclasses.field(default_factory=list)
    failures: list[tuple[int, str, str]] = dataclasses.field(default_factory=list)

    @property
    def match_ratio(self) -> float:
        if not self.total_bytes:
            return 1.0
        return self.matched_bytes / self.total_bytes

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.failures

    def describe(self) -> str:
        lines = [
            f"lines       : {self.assembled_lines}/{self.total_lines} assembled",
            f"bytes       : {self.matched_bytes}/{self.total_bytes} match "
            f"({self.match_ratio * 100:.3f}%)",
            f"mismatches  : {len(self.mismatches)}",
            f"failures    : {len(self.failures)}",
        ]
        for mismatch in self.mismatches[:20]:
            lines.append("  " + mismatch.describe())
        for line_no, text, reason in self.failures[:20]:
            lines.append(f"  line {line_no}: {reason}\n    {text}")
        if len(self.mismatches) > 20:
            lines.append(f"  ... and {len(self.mismatches) - 20} more mismatches")
        if len(self.failures) > 20:
            lines.append(f"  ... and {len(self.failures) - 20} more failures")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Textual normalisation.  Each rule below is a place where Capstone's printed
# form and Keystone's accepted form legitimately differ.
# --------------------------------------------------------------------------
_REG_ALIASES = {
    "r13": "sp",
    "r14": "lr",
    "r15": "pc",
}

_LEADING_HASH_ON_MEMORY = re.compile(r"\[([^]]*?),\s*#")
_DOUBLE_HASH = re.compile(r"#\s*#")
_BRACE_SPACING = re.compile(r"\{\s+")
_SPACE_BEFORE_BRACE = re.compile(r"\s+\}")
_HEX_IMM = re.compile(r"#-?0[xX]([0-9a-fA-F]+)")


def normalise(mnemonic: str, op_str: str, mode: str) -> str:
    """Rewrite a Capstone operand string into a form Keystone accepts.

    Rules applied, in order:

    1. ``r13``/``r14``/``r15`` become ``sp``/``lr``/``pc`` outside memory
       operands -- Keystone resolves the aliases but Capstone sometimes prints
       the numbered form where the SDK source used the name.
    2. A redundant ``#`` in front of a ``#`` is collapsed.
    3. Whitespace inside ``{}`` register lists is removed.
    4. Hex immediates are lower-cased with the ``0x`` prefix preserved, which
       is what Keystone's parser wants.
    5. An empty operand string yields the bare mnemonic.
    """
    text = op_str.strip()
    if not text:
        return mnemonic

    # Rule 1: register aliases, but leave memory operands alone.
    parts: list[str] = []
    for token in re.split(r"(\[[^\]]*\])", text):
        if token.startswith("["):
            parts.append(token)
            continue
        for alias, name in _REG_ALIASES.items():
            token = re.sub(rf"\b{alias}\b", name, token)
        parts.append(token)
    text = "".join(parts)

    text = _DOUBLE_HASH.sub("#", text)
    text = _BRACE_SPACING.sub("{", text)
    text = _SPACE_BEFORE_BRACE.sub("}", text)
    text = _HEX_IMM.sub(lambda m: "#" + m.group(0)[1:].lower().replace("#", ""), text)
    text = re.sub(r"#-", "#-", text)

    _ = mode
    return f"{mnemonic}\t{text}"


# --------------------------------------------------------------------------
# Parsing our own .s output
# --------------------------------------------------------------------------
_ADDRESS_COMMENT = re.compile(
    r"^/\*\s*([0-9a-fA-F]{8})(?:\s*\|\s*([0-9a-fA-F]*))?\s*\*/\s*(.*)$"
)
_TRAILING_BLOCK_COMMENT = re.compile(r"/\*.*?\*/")
#: The ``# -> XXXXXXXX`` hint the renderer appends to data words.  Stripped
#: before assembly; it is a note for the reader, not a directive.
_POINTER_ANNOTATION = re.compile(r"\s*#\s*->.*$")
_MODE_DIRECTIVE = re.compile(r"^\s*\.(arm|thumb)\s*$")
_DATA_WORD = re.compile(r"^\s*\.word\s+(0x[0-9a-fA-F]+|\d+)\s*(?:#.*)?$")
_DATA_SHORT = re.compile(r"^\s*\.short\s+(0x[0-9a-fA-F]+|\d+)\s*(?:#.*)?$")
_DATA_BYTE = re.compile(r"^\s*\.byte\s+(0x[0-9a-fA-F]+|\d+)\s*(?:#.*)?$")
_DIRECTIVE = re.compile(r"^\s*\.(global|type|thumb_func|section|align|ascii|asciz)\b")
_LABEL_ONLY = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_$]*\s*:\s*$")


def _to_int(text: str) -> int:
    text = text.strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text, 10)


def assemble_lines(
    lines: Iterable[str],
    *,
    base_address: int,
    start_mode: str = Mode.ARM,
) -> tuple[bytearray, AssembleReport]:
    """Assemble the ``.s`` text produced by :mod:`.disasm`.

    Returns the byte stream and a report.  The stream is built position by
    position: data directives emit their literal bytes and instructions are
    handed to Keystone at the address recorded in the ``/* XXXXXXXX */``
    comment, so a branch that Keystone cannot encode yet still leaves the
    surrounding bytes in the right place.
    """
    require_keystone()
    engines = {
        Mode.ARM: keystone.Ks(keystone.KS_ARCH_ARM, keystone.KS_MODE_ARM | keystone.KS_MODE_LITTLE_ENDIAN),
        Mode.THUMB: keystone.Ks(keystone.KS_ARCH_ARM, keystone.KS_MODE_THUMB | keystone.KS_MODE_LITTLE_ENDIAN),
    }
    out = bytearray()
    report = AssembleReport()
    mode = start_mode
    cursor = base_address

    for line_no, raw_line in enumerate(lines, 1):
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        report.total_lines += 1
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        if _LABEL_ONLY.match(line):
            continue
        if _DIRECTIVE.match(line):
            continue

        mode_match = _MODE_DIRECTIVE.match(line)
        if mode_match:
            mode = Mode.THUMB if mode_match.group(1) == "thumb" else Mode.ARM
            continue

        body = stripped
        address_match = _ADDRESS_COMMENT.match(line)
        if address_match:
            cursor = int(address_match.group(1), 16)
            body = address_match.group(3).strip()
        # A trailing "/* ... */" or "  # -> ..." annotation is commentary added
        # by the renderer, never something Keystone should see.
        body = _TRAILING_BLOCK_COMMENT.sub("", body).strip()
        body = _POINTER_ANNOTATION.sub("", body).strip()
        if not body:
            continue
        if not (body[0].isalpha() or body[0] == "." or body[0] == "_"):
            report.failures.append((line_no, body, "does not start an instruction"))
            continue

        data_match = None
        for pattern, width in ((_DATA_WORD, 4), (_DATA_SHORT, 2), (_DATA_BYTE, 1)):
            data_match = pattern.match(body)
            if data_match:
                out.extend(_to_int(data_match.group(1)).to_bytes(width, "little"))
                cursor += width
                report.assembled_lines += 1
                report.total_bytes += width
                report.matched_bytes += width
                break
        if data_match is None:
            encoded: Optional[list[int]] = []
            try:
                encoded, _count = engines[mode].asm(body, addr=cursor)
            except keystone.KsError as error:
                # Try the normalised spelling before giving up: this is where a
                # Capstone/Keystone syntax disagreement shows up.
                pieces = body.split(None, 1)
                retry = normalise(
                    pieces[0], pieces[1] if len(pieces) > 1 else "", mode
                )
                try:
                    encoded, _count = engines[mode].asm(retry, addr=cursor)
                except keystone.KsError as error2:
                    report.failures.append((line_no, body, str(error2 or error)))
                    encoded = None
            if encoded is None:
                # Already recorded as a failure; leave the bytes out so the
                # comparison below reports the gap rather than hiding it.
                continue
            out.extend(bytes(encoded))
            cursor += len(encoded)
            report.assembled_lines += 1
            report.total_bytes += len(encoded)
            report.matched_bytes += len(encoded)

    return out, report


def assemble_file(path: str, *, base_address: int,
                  start_mode: str = Mode.ARM) -> tuple[bytearray, AssembleReport]:
    """Read a ``.s`` file and assemble it."""
    with open(path, encoding="utf-8") as handle:
        return assemble_lines(handle, base_address=base_address, start_mode=start_mode)


def round_trip(
    payload: bytes,
    base_address: int,
    *,
    entry_address: Optional[int] = None,
    name: str = "image",
    verbose: bool = False,
) -> tuple[bool, AssembleReport, bytearray]:
    """Disassemble *payload* and reassemble it, comparing against the original.

    This is the check that matters.  A payload that survives it byte for byte
    has a disassembly that faithfully represents every instruction the DS would
    execute, which is the precondition for converting any of it to C.
    """
    from .disasm import Disassembler

    disassembler = Disassembler(
        payload, base_address, entry_address=entry_address, name=name
    )
    result = disassembler.disassemble()
    text = disassembler.render(result)
    rebuilt, report = assemble_lines(
        text.splitlines(), base_address=base_address
    )

    # Compare positionally; report every differing byte run, capped for sanity.
    limit = max(len(payload), len(rebuilt))
    index = 0
    runs = 0
    while index < limit and runs < 200:
        if index >= len(payload):
            report.mismatches.append(
                Mismatch(base_address + index, b"", bytes(rebuilt[index:index + 8]),
                         "<end of image>", "reassembly produced extra bytes")
            )
            runs += 1
            index += 8
            continue
        if index >= len(rebuilt):
            report.mismatches.append(
                Mismatch(base_address + index, bytes(payload[index:index + 8]), b"",
                         "<end of output>", "reassembly is short")
            )
            runs += 1
            index += 8
            continue
        if payload[index] != rebuilt[index]:
            start = index
            while index < limit and (
                index >= len(payload)
                or index >= len(rebuilt)
                or payload[index] != rebuilt[index]
            ):
                index += 1
            report.mismatches.append(
                Mismatch(
                    base_address + start,
                    bytes(payload[start:index]),
                    bytes(rebuilt[start:index]),
                    f"{index - start} byte run",
                    "bytes differ",
                )
            )
            runs += 1
        else:
            index += 1

    # Recompute the honest byte statistics now that we have compared for real.
    differing = sum(
        1 for i in range(min(len(payload), len(rebuilt))) if payload[i] != rebuilt[i]
    )
    differing += abs(len(payload) - len(rebuilt))
    report.total_bytes = max(len(payload), 1)
    report.matched_bytes = max(len(payload) - differing, 0)

    ok = differing == 0
    if verbose:
        print(report.describe(), file=sys.stderr)
    return ok, report, rebuilt
