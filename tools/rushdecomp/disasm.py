"""ARM/Thumb disassembly of DS binaries into GAS-syntax ``.s`` files.

A DS executable image is a flat blob with no symbol table, no section headers
and no reliable marker separating code from data.  Recovering structure from it
is the actual work of a decompilation, and it is heuristic.  This module states
its heuristics plainly so that, when the output is wrong, the reason is
discoverable rather than mysterious.

Approach
--------
1. **Seeded recursive sweep.**  Start at the header's entry address and follow
   every direct branch and call whose target lands inside the image.  Bytes
   reached this way are code with high confidence.

2. **Mode tracking.**  ARM and Thumb decode differently, so every worklist
   entry carries the mode it must be decoded in.

   * ``b``/``bl <imm>`` in ARM stay in ARM mode.
   * ``blx <imm>`` in ARM switches to Thumb -- the encoding carries an H bit
     rather than an address bit 0, so the printed immediate always reads even.
   * Thumb ``bl``/``blx <imm>`` targets are Thumb.
   * ``bx``/``blx <reg>`` cannot be resolved statically.  Those sites are
     recorded as *unresolved tails* and reported, because they are exactly
     where a jump table or a mode switch hides.

3. **Terminator discipline.**  An unconditional ``b``, ``bx``, ``pop {..., pc}``
   or ``ldr pc, ...`` ends a basic block: the sweep must *not* fall through
   into the next word.  Falling through is what silently walks a function's
   epilogue into whatever padding follows it.

4. **Padding guard.**  Cartridge images pad between sections with zeros or
   ``0xFF``.  A zero word decodes as ``andeq r0, r0, r0`` -- a perfectly valid
   ARM encoding -- so without a guard the sweep happily consumes megabytes of
   filler as code and then walks straight into a Thumb function from the wrong
   side.  Runs of ``0x00``/``0xFF`` are refused as instruction starts.

5. **Prologue scan.**  Once the sweep stalls, scan for unreached function
   prologues (``push {..., lr}`` in either mode, ``mov ip, sp``) and seed a
   second sweep from each.  This recovers functions that are only ever called
   indirectly.

6. **Window classification.**  For runs no prologue explains, score a window
   decoded as ARM against the same window decoded as Thumb and take the clearly
   better mode.  The threshold is deliberately high and the address must be
   word-aligned, because the cost of a false positive is data being rendered as
   instructions.

7. **Address-ordered emission.**  Code and data are interleaved in address
   order, so the ``.s`` file reassembles into a byte-exact copy of the payload.
   Emitting all code first and all data second would look tidier and produce a
   completely different binary.

Output
------
GAS syntax that ``arm-none-eabi-as`` and Keystone both accept: ``.arm``/
``.thumb`` directives at each mode change, ``.thumb_func`` on Thumb entry
points, local labels for branch targets, globals for functions, and
``.word``/``.byte`` for data.  A JSON sidecar records every label and its mode
so the assembler can rebuild without re-deriving anything.

This module is a pure function from bytes to text.  It reads no copyrighted
data of its own and writes nothing outside the paths it is given.
"""

from __future__ import annotations

import dataclasses
import heapq
import json
import os
import re
from typing import Iterator, NamedTuple, Optional

try:  # pragma: no cover
    import capstone
except ImportError:  # pragma: no cover
    capstone = None  # type: ignore[assignment]

__all__ = [
    "Disassembler",
    "DisassemblyResult",
    "Instruction",
    "Label",
    "Mode",
    "UnresolvedTail",
    "FunctionInfo",
    "require_capstone",
    "write_sidecar",
]


def require_capstone() -> None:
    """Raise a helpful error when the optional dependency is missing."""
    if capstone is None:
        raise RuntimeError(
            "capstone is required for disassembly; install it with "
            "`pip install -r requirements.txt` (or `make deps`)"
        )


class Mode:
    ARM = "arm"
    THUMB = "thumb"


@dataclasses.dataclass
class Instruction:
    """One decoded instruction."""

    address: int
    size: int
    mnemonic: str
    op_str: str
    mode: str
    raw: bytes

    @property
    def end(self) -> int:
        return self.address + self.size


@dataclasses.dataclass
class Label:
    """A named address in the output."""

    address: int
    name: str
    mode: str
    kind: str  # "function" | "branch_target" | "data"
    reached_from: Optional[int] = None

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class UnresolvedTail:
    """An indirect branch or return whose target could not be resolved."""

    address: int
    mnemonic: str
    op_str: str
    mode: str
    reason: str


class FunctionInfo(NamedTuple):
    start: int
    end: int
    mode: str
    origin: str  # "entry" | "direct" | "prologue" | "window"


# --------------------------------------------------------------------------
# Encoding predicates
# --------------------------------------------------------------------------
#: Mnemonics that are almost never real code in a bulk sweep of a game image.
_SUSPICIOUS = frozenset(
    {"udf", ".inst", "cdp2", "ldc2", "stc2", "mcr2", "mrc2", "mcrr2", "mrrc2"}
)

#: Instruction-level bonus/penalty weights for the window classifier.  These
#: are the shapes real compiled Nitro code is made of.
_COMMON_MNEMONICS = frozenset(
    {
        "push", "pop", "mov", "movs", "mvn", "ldr", "str", "ldrb", "strb",
        "ldrh", "strh", "ldrsb", "ldrsh", "add", "adds", "sub", "subs",
        "cmp", "cmn", "tst", "teq", "and", "ands", "orr", "ors", "eor",
        "eors", "bic", "bics", "lsl", "lsls", "lsr", "lsrs", "asr", "asrs",
        "ror", "mul", "muls", "mla", "b", "bl", "blx", "bx", "ldmia",
        "stmia", "stmdb", "ldmdb", "nop", "neg", "negs", "adc", "adcs",
        "sbc", "sbcs", "rsb", "rsbs", "movw", "movt", "ldr.w", "str.w",
        "add.w", "sub.w", "cbz", "cbnz", "sxth", "uxth", "sxtb", "uxtb",
        "rev", "clz", "umull", "smull",
    }
)

_PROLOGUE_SCAN_LIMIT = 1 << 22

#: Window-classifier thresholds.  ``MIN_SCORE`` rejects a mode outright;
#: ``MARGIN`` requires one mode to beat the other by a clear amount before we
#: treat an unexplained run as code at all.
WINDOW_MIN_SCORE = 1.30
WINDOW_MARGIN = 0.35


def _arm_is_push_lr(word: int) -> bool:
    """``push {..., lr}`` / ``stmdb sp!, {..., lr}``."""
    return (word & 0xFFFF0000) == 0xE92D0000 and bool(word & (1 << 14))


def _arm_is_stmdb_sp(word: int) -> bool:
    """Any ``stmdb sp!`` -- a stack frame being opened."""
    return (word & 0xFFFF0000) == 0xE92D0000


def _arm_is_mov_ip_sp(word: int) -> bool:
    """``mov ip, sp`` -- Nitro's stack-frame idiom."""
    return word == 0xE1A0C00D


def _thumb_is_push_lr(half: int) -> bool:
    """``push {..., lr}`` in Thumb."""
    return (half & 0xFF00) == 0xB500


_IMM_TARGET = re.compile(r"#?(0x[0-9a-fA-F]+)\s*$")


def _immediate_target(mnemonic: str, op_str: str) -> Optional[int]:
    """Extract a branch target from an operand string.

    Only a trailing bare immediate counts.  Anything with a register or a
    memory operand -- ``bx r4``, ``ldr pc, [r0]``, ``b [pc, r1]`` -- yields
    ``None``, which the caller records as an unresolved tail.
    """
    text = op_str.strip()
    if not text:
        return None
    # Reject anything that mentions a register or a bracket.
    if "[" in text or "!" in text:
        return None
    if re.search(r"\br\d+\b|\bsp\b|\blr\b|\bpc\b", text):
        return None
    match = _IMM_TARGET.search(text)
    if not match:
        return None
    return int(match.group(1), 16)


#: Mnemonics after which control never falls through to the next word.
_UNCONDITIONAL_TERMINATORS = frozenset({"bx", "b", "blx", "ret"})


class DisassemblyResult:
    """Everything one disassembly pass produced."""

    def __init__(self, name: str, base_address: int) -> None:
        self.name = name
        self.base_address = base_address
        self.instructions: list[Instruction] = []
        self.labels: dict[int, Label] = {}
        self.functions: list[FunctionInfo] = []
        self.unresolved_tails: list[UnresolvedTail] = []
        self.data_ranges: list[tuple[int, int]] = []
        self.stats: dict[str, int] = {}

    @property
    def instruction_count(self) -> int:
        return len(self.instructions)

    @property
    def code_bytes(self) -> int:
        return sum(ins.size for ins in self.instructions)

    @property
    def data_bytes(self) -> int:
        return sum(end - start for start, end in self.data_ranges)

    def function_at(self, address: int) -> Optional[FunctionInfo]:
        for function in self.functions:
            if function.start == address:
                return function
        return None

    def coverage(self, total_bytes: int) -> float:
        if not total_bytes:
            return 0.0
        return (self.code_bytes + self.data_bytes) / total_bytes

    def summary(self, total_bytes: int) -> str:
        arm = sum(1 for ins in self.instructions if ins.mode == Mode.ARM)
        thumb = self.instruction_count - arm
        return "\n".join(
            [
                f"{self.name}: {self.instruction_count} instructions, "
                f"{len(self.functions)} functions, "
                f"{self.code_bytes / max(total_bytes, 1) * 100:.1f}% of "
                f"0x{total_bytes:x} bytes classified as code",
                f"  arm   : {arm}",
                f"  thumb : {thumb}",
                f"  data  : {self.data_bytes} bytes in {len(self.data_ranges)} runs",
                f"  unresolved indirect branches: {len(self.unresolved_tails)}",
                f"  coverage: {self.coverage(total_bytes) * 100:.1f}%",
            ]
        )


class Disassembler:
    """Recursive-descent ARM/Thumb disassembler for a flat DS binary image."""

    def __init__(
        self,
        payload: bytes,
        base_address: int,
        *,
        entry_address: Optional[int] = None,
        name: str = "image",
        scan_prologues: bool = True,
        scan_windows: bool = True,
        window_size: int = 32,
    ) -> None:
        require_capstone()
        self.payload = bytes(payload)
        self.base_address = base_address & ~0x1
        self.entry_address = entry_address
        self.name = name
        self.scan_prologues = scan_prologues
        self.scan_windows = scan_windows
        self.window_size = window_size

        little = capstone.CS_MODE_LITTLE_ENDIAN
        self._arm = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM | little)
        self._thumb = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | little)
        for engine in (self._arm, self._thumb):
            engine.detail = False
            engine.skipdata = False

        self._end = self.base_address + len(self.payload)
        self._mode_at: dict[int, str] = {}
        self._visited: set[int] = set()
        self._ins_by_addr: dict[int, Instruction] = {}
        self._block_starts: set[int] = set()

    # ------------------------------------------------------------------ util
    def _offset(self, address: int) -> int:
        return address - self.base_address

    def _in_range(self, address: int) -> bool:
        return self.base_address <= address < self._end

    def _engine(self, mode: str):
        return self._thumb if mode == Mode.THUMB else self._arm

    def _decode_one(self, address: int, mode: str) -> Optional[Instruction]:
        if not self._in_range(address):
            return None
        offset = self._offset(address)
        need = 2 if mode == Mode.THUMB else 4
        if offset + need > len(self.payload):
            return None
        blob = self.payload[offset : offset + need]
        for ins in self._engine(mode).disasm(bytes(blob), address):
            if ins.address != address:
                return None
            return Instruction(
                address=ins.address, size=ins.size, mnemonic=ins.mnemonic,
                op_str=ins.op_str, mode=mode, raw=bytes(ins.bytes),
            )
        return None

    def _decode_stream(self, address: int, mode: str, count: int) -> list[Instruction]:
        if not self._in_range(address):
            return []
        offset = self._offset(address)
        blob = self.payload[offset : offset + count * 4 + 4]
        out: list[Instruction] = []
        for ins in self._engine(mode).disasm(bytes(blob), address, count=count):
            out.append(
                Instruction(
                    address=ins.address, size=ins.size, mnemonic=ins.mnemonic,
                    op_str=ins.op_str, mode=mode, raw=bytes(ins.bytes),
                )
            )
        return out

    def _is_padding(self, address: int, mode: str) -> bool:
        """True when *address* opens a run of filler rather than an instruction.

        A zero word is a legal ``andeq r0, r0, r0``, so this guard is the only
        thing stopping the sweep from consuming megabytes of inter-section
        padding as code -- and then walking into the next section from the
        wrong side, decoding Thumb bytes as ARM.
        """
        offset = self._offset(address)
        width = 4 if mode == Mode.ARM else 2
        window = self.payload[offset : offset + width]
        if len(window) < width:
            return True
        return all(byte in (0x00, 0xFF) for byte in window)

    # ------------------------------------------------------------- sweeping
    def _sweep(self, start: int, mode: str, origin: str,
               result: DisassemblyResult) -> Optional[FunctionInfo]:
        """Walk code from *start*, following direct branches, honouring mode.

        The worklist is a min-heap keyed on address, which makes the sweep
        deterministic and visits blocks in ascending order.  That matters for
        reproducibility: two runs over the same bytes must produce identical
        ``.s`` text, or ``make verify`` cannot compare against a checked-in
        baseline.
        """
        if start in self._visited or not self._in_range(start):
            return None
        if self._is_padding(start, mode):
            return None

        function_start = start
        function_end = start
        heap: list[tuple[int, int, str]] = [(start, 0, mode)]
        counter = 1
        steps = 0

        while heap:
            address, _order, addr_mode = heapq.heappop(heap)
            steps += 1
            if steps > 8_000_000:  # pragma: no cover - runaway guard
                break
            if not self._in_range(address) or address in self._visited:
                continue
            if self._is_padding(address, addr_mode):
                continue

            claimed = self._mode_at.get(address)
            if claimed is not None and claimed != addr_mode:
                # Another path already decoded these bytes in the other mode.
                # Do not fight it; the report surfaces the conflict.
                result.unresolved_tails.append(
                    UnresolvedTail(
                        address=address, mnemonic="<mode conflict>",
                        op_str="", mode=addr_mode,
                        reason=f"already decoded as {claimed}",
                    )
                )
                continue
            self._mode_at[address] = addr_mode

            instruction = self._decode_one(address, addr_mode)
            if instruction is None:
                continue
            if instruction.address != address or instruction.size not in (2, 4):
                continue

            self._visited.add(address)
            self._ins_by_addr[address] = instruction
            self._block_starts.add(function_start)
            result.instructions.append(instruction)
            function_end = max(function_end, instruction.end)

            mnemonic = instruction.mnemonic
            base_mnemonic = mnemonic.split(".")[0]
            falls_through = True
            resolved_target: Optional[int] = None

            if base_mnemonic in ("b", "bl", "blx", "bx", "cbz", "cbnz"):
                raw_target = _immediate_target(mnemonic, instruction.op_str)
                if raw_target is not None and self._in_range(raw_target & ~0x1):
                    target = raw_target & ~0x1
                    resolved_target = target
                    if base_mnemonic == "blx" and addr_mode == Mode.ARM:
                        target_mode = Mode.THUMB
                    elif raw_target & 0x1:
                        target_mode = Mode.THUMB
                    elif addr_mode == Mode.THUMB:
                        target_mode = Mode.THUMB
                    else:
                        target_mode = Mode.ARM
                    if target not in self._visited:
                        self._label(result, target, target_mode, "branch_target",
                                    reached_from=instruction.address)
                    heapq.heappush(heap, (target, counter, target_mode))
                    counter += 1
                else:
                    result.unresolved_tails.append(
                        UnresolvedTail(
                            address=instruction.address, mnemonic=mnemonic,
                            op_str=instruction.op_str, mode=addr_mode,
                            reason=(
                                "indirect branch through a register"
                                if raw_target is None
                                else "branch target outside the image"
                            ),
                        )
                    )

                if base_mnemonic in ("bl", "blx") and raw_target is not None:
                    # A call returns; keep falling through.
                    falls_through = True
                elif base_mnemonic in ("b", "bx", "cbz", "cbnz"):
                    # Unconditional jump, or an indirect tail we cannot follow.
                    # Either way the next word is not part of this block.
                    falls_through = False
                elif base_mnemonic == "blx" and raw_target is None:
                    falls_through = False
            elif base_mnemonic == "pop" and re.search(r"\bpc\b", instruction.op_str):
                falls_through = False
            elif base_mnemonic in ("ldr", "mov", "add") and re.search(
                r"\bpc\s*,", instruction.op_str
            ):
                falls_through = False

            if falls_through:
                nxt = instruction.end
                if self._in_range(nxt) and nxt not in self._visited:
                    heapq.heappush(heap, (nxt, counter, addr_mode))
                    counter += 1

        if function_end > function_start:
            info = FunctionInfo(function_start, function_end, mode, origin)
            result.functions.append(info)
            self._label(result, function_start, mode, "function")
            return info
        return None

    def _label(self, result: DisassemblyResult, address: int, mode: str,
               kind: str, *, reached_from: Optional[int] = None) -> Label:
        existing = result.labels.get(address)
        if existing is not None:
            if existing.kind == "branch_target" and kind == "function":
                existing.kind = "function"
                existing.mode = mode
            if reached_from is not None and existing.reached_from is None:
                existing.reached_from = reached_from
            return existing
        if kind == "function":
            name = f"func_{address:08x}"
        elif kind == "branch_target":
            name = f"loc_{address:08x}"
        else:
            name = f"dat_{address:08x}"
        label = Label(address=address, name=name, mode=mode, kind=kind,
                      reached_from=reached_from)
        result.labels[address] = label
        return label

    # --------------------------------------------------------- classification
    def _score_window(self, address: int, mode: str) -> float:
        decoded = self._decode_stream(address, mode, max(self.window_size // 2, 4))
        if not decoded:
            return 0.0
        covered = sum(ins.size for ins in decoded)
        if covered < self.window_size * 0.75:
            return 0.0
        score = covered / float(self.window_size)
        for ins in decoded:
            base = ins.mnemonic.split(".")[0]
            if base in _SUSPICIOUS:
                score -= 0.40
            elif base in _COMMON_MNEMONICS:
                score += 0.10
            else:
                score -= 0.05
        return score

    def _classify(self, address: int) -> Optional[str]:
        """Pick a mode for an unexplained run, or ``None`` if it looks like data."""
        arm = self._score_window(address, Mode.ARM)
        thumb = self._score_window(address, Mode.THUMB)
        if arm >= WINDOW_MIN_SCORE and arm - thumb >= WINDOW_MARGIN:
            return Mode.ARM
        if thumb >= WINDOW_MIN_SCORE and thumb - arm >= WINDOW_MARGIN:
            return Mode.THUMB
        return None

    def _find_prologues(self) -> Iterator[tuple[int, str]]:
        """Yield ``(address, mode)`` for every function prologue in the image."""
        limit = min(len(self.payload), _PROLOGUE_SCAN_LIMIT)
        payload = self.payload
        base = self.base_address
        offset = 0
        while offset + 4 <= limit:
            word = (
                payload[offset]
                | (payload[offset + 1] << 8)
                | (payload[offset + 2] << 16)
                | (payload[offset + 3] << 24)
            )
            if _arm_is_push_lr(word) or _arm_is_mov_ip_sp(word):
                yield base + offset, Mode.ARM
            half = payload[offset] | (payload[offset + 1] << 8)
            if _thumb_is_push_lr(half):
                yield base + offset, Mode.THUMB
            offset += 2

    # ------------------------------------------------------------------ entry
    def disassemble(self) -> DisassemblyResult:
        """Run the full pass and return the collected result."""
        result = DisassemblyResult(self.name, self.base_address)

        if self.entry_address is not None and self._in_range(self.entry_address):
            entry = self.entry_address & ~0x1
            entry_mode = Mode.THUMB if self.entry_address & 0x1 else Mode.ARM
        else:
            entry, entry_mode = self.base_address, Mode.ARM
        self._sweep(entry, entry_mode, "entry", result)

        if self.scan_prologues:
            for address, mode in self._find_prologues():
                if address in self._visited:
                    continue
                self._sweep(address, mode, "prologue", result)

        if self.scan_windows:
            address = self.base_address
            while address < self._end:
                if address in self._visited:
                    address += self._ins_by_addr[address].size
                    continue
                if (address - self.base_address) % 4 == 0:
                    mode = self._classify(address)
                    if mode is not None:
                        self._sweep(address, mode, "window", result)
                address += 4 if address not in self._visited else 2

        self._collect_data(result)
        result.instructions.sort(key=lambda ins: ins.address)
        result.functions.sort()
        result.stats = {
            "instructions": result.instruction_count,
            "functions": len(result.functions),
            "code_bytes": result.code_bytes,
            "data_bytes": result.data_bytes,
            "image_bytes": len(self.payload),
            "unresolved_tails": len(result.unresolved_tails),
            "labels": len(result.labels),
        }
        return result

    def _collect_data(self, result: DisassemblyResult) -> None:
        """Turn every unvisited run into a labelled data range."""
        address = self.base_address
        end = self._end
        while address < end:
            if address in self._visited:
                address += self._ins_by_addr[address].size
                continue
            start = address
            while address < end and address not in self._visited:
                address += 1
            result.data_ranges.append((start, address))
            self._label(result, start, Mode.ARM, "data")

    # ----------------------------------------------------------------- render
    def render(self, result: DisassemblyResult, *, symbol_prefix: str = "") -> str:
        """Emit the ``.s`` text, code and data interleaved in address order.

        Address order is not cosmetic: the section is a flat byte stream, so
        emitting code first and data second would reassemble into a completely
        different binary.
        """
        header = [
            "# ------------------------------------------------------------",
            f"# {result.name} -- generated by Rush-Decomp",
            f"# base address 0x{result.base_address:08x}   "
            f"size 0x{len(self.payload):x}",
            "#",
            "# Generated file.  Do not edit by hand: convert the functions you",
            "# are working on into C under src/ and delete them from here.",
            "# ------------------------------------------------------------",
            "",
            f'.section .{result.name}, "ax", %progbits',
            "",
        ]

        body: list[str] = []
        current_mode: Optional[str] = None
        data_map = dict(result.data_ranges)
        ins_index = 0
        instructions = result.instructions
        address = self.base_address
        end = self._end

        def set_mode(mode: str) -> None:
            nonlocal current_mode
            if mode != current_mode:
                current_mode = mode
                body.append("    .thumb" if mode == Mode.THUMB else "    .arm")

        while address < end:
            label = result.labels.get(address)
            instruction = (
                instructions[ins_index]
                if ins_index < len(instructions)
                and instructions[ins_index].address == address
                else None
            )

            if instruction is not None:
                if label is not None:
                    if label.kind == "function":
                        body.append("")
                        body.append(f"{symbol_prefix}{label.name}:")
                        body.append(f"    .global {symbol_prefix}{label.name}")
                        body.append(f"    .type {symbol_prefix}{label.name}, %function")
                        if instruction.mode == Mode.THUMB:
                            # GAS needs this to set bit 0 of the symbol value,
                            # or a relocated call lands in ARM mode.
                            body.append("    .thumb_func")
                    else:
                        body.append(f"{symbol_prefix}{label.name}:")
                set_mode(instruction.mode)
                body.append(
                    f"/* {instruction.address:08x} | {_hex_comment(instruction.raw)} */    "
                    f"{instruction.mnemonic}\t{instruction.op_str}".rstrip()
                )
                address += instruction.size
                ins_index += 1
                continue

            if instruction is None and ins_index < len(instructions) and \
                    instructions[ins_index].address < address:
                ins_index += 1
                continue

            # Data run.
            run_end = data_map.get(address)
            if run_end is None:
                # Should not happen: every unvisited byte is in a data range.
                run_end = min(address + 4, end)
            if label is not None and label.kind == "data":
                body.append("")
                body.append(f"{symbol_prefix}{label.name}:")
                body.append("    .arm")
                current_mode = Mode.ARM
            at = address
            while at + 4 <= run_end:
                word = _read_u32(self.payload, self._offset(at))
                body.append(
                    f"/* {at:08x} | */    .word 0x{word:08x}"
                    f"{_pointer_note(word, self.base_address, self._end)}"
                )
                at += 4
            while at < run_end:
                body.append(
                    f"/* {at:08x} | */    .byte "
                    f"0x{self.payload[self._offset(at)]:02x}"
                )
                at += 1
            address = run_end

        footer = ["", "# " + result.summary(len(self.payload)).replace("\n", "\n# "), ""]
        return "\n".join(header + body + footer)


def _read_u32(payload: bytes, offset: int) -> int:
    return (
        payload[offset]
        | (payload[offset + 1] << 8)
        | (payload[offset + 2] << 16)
        | (payload[offset + 3] << 24)
    )


def _hex_comment(raw: bytes) -> str:
    return "".join(f"{b:02x}" for b in reversed(raw)).rjust(8, " ")


def _pointer_note(word: int, base: int, end: int) -> str:
    """Annotate a data word that looks like a pointer into the image."""
    if base <= word < end:
        return f"  # -> {word:08x}"
    if word & 0x1 and base <= (word & ~0x1) < end:
        return f"  # -> {word & ~0x1:08x} (thumb)"
    return ""


def write_sidecar(result: DisassemblyResult, path: str) -> None:
    """Persist labels and stats next to the ``.s`` for the assembler to reuse."""
    payload = {
        "name": result.name,
        "base_address": result.base_address,
        "stats": result.stats,
        "labels": [
            label.to_json()
            for label in sorted(result.labels.values(), key=lambda item: item.address)
        ],
        "functions": [
            {"start": f.start, "end": f.end, "mode": f.mode, "origin": f.origin}
            for f in sorted(result.functions)
        ],
        "unresolved_tails": [
            dataclasses.asdict(tail) for tail in result.unresolved_tails[:2000]
        ],
        "data_ranges": [{"start": s, "end": e} for s, e in result.data_ranges],
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1)
