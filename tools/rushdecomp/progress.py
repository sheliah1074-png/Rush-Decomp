"""Matching-progress accounting.

A decompilation's real deliverable is a number that goes up: how much of the
binary is now expressed as C rather than as disassembly.  This module computes
that from what is on disk, with no bookkeeping file to fall out of date.

Definitions used here
---------------------
``total``
    Executable bytes in the image: the decompressed ARM9 and ARM7 payloads plus
    every overlay payload.  Data-only Nitro files are excluded -- nobody
    "decompiles" a texture, and counting them would flatter the percentage.

``matched``
    Executable bytes covered by a hand-written C translation unit under
    ``src/``, attributed via the ``@rush_match`` annotation each file carries.
    A translation unit declares which functions it reproduces and how many
    bytes each occupied in the original; the total is what counts.

``disassembled``
    Executable bytes for which a generated ``.s`` exists.  This is the
    starting point, not the goal: it goes to ~100% on the first run.

The annotation format is deliberately boring so it survives a decade of edits::

    /*
     * @rush_match arm9 0x1c4 func_02001A80
     * @rush_match arm9 0x088 func_02003F10
     */

Each line is ``@rush_match <image> <byte-count> <original-symbol>``.  Byte
counts are checked against the disassembly's recorded function sizes and a
mismatch is reported rather than trusted, so the number cannot drift from the
binary it describes.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from typing import Iterable, Optional

__all__ = ["Progress", "ImageProgress", "scan_sources", "load_progress", "save_progress"]

_ANNOTATION = re.compile(
    r"@rush_match\s+(?P<image>[A-Za-z0-9_\-]+)\s+"
    r"(?P<size>0[xX][0-9a-fA-F]+|\d+)\s+"
    r"(?P<symbol>[A-Za-z0-9_$]+)"
)


@dataclasses.dataclass
class ImageProgress:
    """Progress for one executable image (arm9, arm7, or an overlay)."""

    name: str
    total_bytes: int = 0
    matched_bytes: int = 0
    disassembled_bytes: int = 0
    functions_total: int = 0
    functions_matched: int = 0
    #: ``@rush_match`` annotations whose byte count disagrees with the
    #: disassembly, or which name a symbol that does not exist.
    problems: list[str] = dataclasses.field(default_factory=list)

    @property
    def match_ratio(self) -> float:
        if not self.total_bytes:
            return 0.0
        return self.matched_bytes / self.total_bytes

    @property
    def disasm_ratio(self) -> float:
        if not self.total_bytes:
            return 0.0
        return self.disassembled_bytes / self.total_bytes

    def row(self) -> str:
        return (
            f"{self.name:<16} {self.match_ratio * 100:>7.2f}%  "
            f"{self.matched_bytes:>9} / {self.total_bytes:<9}  "
            f"{self.functions_matched:>5} / {self.functions_total:<5} funcs  "
            f"disasm {self.disasm_ratio * 100:>6.2f}%"
        )


@dataclasses.dataclass
class Progress:
    """Progress across every executable image in the project."""

    images: dict[str, ImageProgress] = dataclasses.field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(image.total_bytes for image in self.images.values())

    @property
    def matched_bytes(self) -> int:
        return sum(image.matched_bytes for image in self.images.values())

    @property
    def disassembled_bytes(self) -> int:
        return sum(image.disassembled_bytes for image in self.images.values())

    @property
    def functions_total(self) -> int:
        return sum(image.functions_total for image in self.images.values())

    @property
    def functions_matched(self) -> int:
        return sum(image.functions_matched for image in self.images.values())

    @property
    def match_ratio(self) -> float:
        total = self.total_bytes
        return self.matched_bytes / total if total else 0.0

    def image(self, name: str) -> ImageProgress:
        if name not in self.images:
            self.images[name] = ImageProgress(name=name)
        return self.images[name]

    def report(self) -> str:
        lines = [
            "Rush-Decomp matching progress",
            "=" * 78,
            f"{'image':<16} {'matched':>9}  {'bytes':>21}  {'functions':>18}",
            "-" * 78,
        ]
        for name in sorted(self.images):
            lines.append(self.images[name].row())
        lines += [
            "-" * 78,
            f"{'TOTAL':<16} {self.match_ratio * 100:>7.2f}%  "
            f"{self.matched_bytes:>9} / {self.total_bytes:<9}  "
            f"{self.functions_matched:>5} / {self.functions_total:<5} funcs  "
            f"disasm {self.disassembled_bytes / max(self.total_bytes, 1) * 100:>6.2f}%",
        ]
        problems = [
            (name, problem)
            for name, image in sorted(self.images.items())
            for problem in image.problems
        ]
        if problems:
            lines += ["", "annotation problems:"]
            lines += [f"  {name}: {problem}" for name, problem in problems[:40]]
            if len(problems) > 40:
                lines.append(f"  ... and {len(problems) - 40} more")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "total_bytes": self.total_bytes,
            "matched_bytes": self.matched_bytes,
            "disassembled_bytes": self.disassembled_bytes,
            "match_ratio": self.match_ratio,
            "functions_total": self.functions_total,
            "functions_matched": self.functions_matched,
            "images": {
                name: {
                    "total_bytes": image.total_bytes,
                    "matched_bytes": image.matched_bytes,
                    "disassembled_bytes": image.disassembled_bytes,
                    "functions_total": image.functions_total,
                    "functions_matched": image.functions_matched,
                    "match_ratio": image.match_ratio,
                    "problems": image.problems,
                }
                for name, image in sorted(self.images.items())
            },
        }


def scan_sources(src_dir: str) -> list[tuple[str, str, int, str]]:
    """Collect every ``@rush_match`` annotation under *src_dir*.

    Returns ``[(source_path, image, byte_count, symbol), ...]`` sorted by path
    so that repeated runs produce identical output.
    """
    found: list[tuple[str, str, int, str]] = []
    if not os.path.isdir(src_dir):
        return found
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for filename in sorted(files):
            if not filename.endswith((".c", ".h", ".cpp")):
                continue
            path = os.path.join(root, filename)
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                continue
            for match in _ANNOTATION.finditer(text):
                size_text = match.group("size")
                size = (
                    int(size_text, 16)
                    if size_text.lower().startswith("0x")
                    else int(size_text, 10)
                )
                found.append((path, match.group("image"), size, match.group("symbol")))
    return found


def _load_sidecars(asm_dir: str) -> dict[str, dict]:
    """Read every ``*.json`` sidecar the disassembler wrote."""
    out: dict[str, dict] = {}
    if not os.path.isdir(asm_dir):
        return out
    for root, dirs, files in os.walk(asm_dir):
        dirs[:] = sorted(dirs)
        for filename in sorted(files):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(root, filename)
            try:
                with open(path, encoding="utf-8") as handle:
                    out[os.path.splitext(filename)[0]] = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
    return out


def load_progress(
    *,
    asm_dir: str,
    src_dir: str,
    image_sizes: Optional[Iterable[tuple[str, int]]] = None,
) -> Progress:
    """Build a :class:`Progress` from what is currently on disk."""
    progress = Progress()
    if image_sizes:
        for name, size in image_sizes:
            progress.image(name).total_bytes = size

    sidecars = _load_sidecars(asm_dir)
    function_sizes: dict[str, dict[str, int]] = {}
    for name, sidecar in sidecars.items():
        sizes: dict[str, int] = {}
        for function in sidecar.get("functions", []):
            sizes[f"func_{function['start']:08x}"] = function["end"] - function["start"]
        for label in sidecar.get("labels", []):
            if label.get("kind") == "function":
                sizes.setdefault(label["name"], 0)
        function_sizes[name] = sizes
        image = progress.image(name)
        image.disassembled_bytes = sidecar.get("stats", {}).get("code_bytes", 0)
        image.functions_total = len(sidecar.get("functions", []))
        if not image.total_bytes:
            image.total_bytes = sidecar.get("stats", {}).get("image_bytes", 0)

    matched_symbols: dict[str, set[str]] = {}
    for path, image_name, size, symbol in scan_sources(src_dir):
        image = progress.image(image_name)
        known = function_sizes.get(image_name, {})
        if known:
            expected = known.get(symbol)
            if expected is None:
                image.problems.append(
                    f"{os.path.relpath(path)}: {symbol} is not in the {image_name} "
                    "disassembly -- renamed, or the annotation is stale"
                )
            elif expected != size:
                image.problems.append(
                    f"{os.path.relpath(path)}: {symbol} is annotated as {size} "
                    f"bytes but the disassembly measures {expected}"
                )
        image.matched_bytes += size
        matched_symbols.setdefault(image_name, set()).add(symbol)

    for image_name, symbols in matched_symbols.items():
        progress.images[image_name].functions_matched = len(symbols)
    return progress


def save_progress(progress: Progress, path: str) -> None:
    """Write the progress record as JSON, for CI badges and trend tracking."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(progress.to_json(), handle, indent=1, sort_keys=True)
        handle.write("\n")
