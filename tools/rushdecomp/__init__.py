"""Rush-Decomp: a decompilation toolkit for Nintendo DS cartridge images.

Built to take Sonic Rush apart, and generic enough for any DS image.

The pipeline
------------
::

    .nds  ->  header  ->  ARM9 / ARM7 / overlays / NitroFS
                              |
                              v
                    disassemble (capstone)
                              |
                              v
                       asm/<name>.s + .json
                              |
                              v
                    reassemble (keystone)
                              |
                              v
                     compare byte for byte

That final comparison is the point.  A disassembly that reassembles into the
bytes it came from is a faithful description of every instruction the DS would
execute, which is the precondition for translating any of it into C.  Without
it, a decompilation is just a large amount of plausible-looking text.

Modules
-------
Format parsers, in the order a cartridge image is read:

:mod:`~rushdecomp.header`
    The 0x4000-byte cartridge header naming every other region.
:mod:`~rushdecomp.secure`
    The encrypted secure area, and Blowfish.
:mod:`~rushdecomp.arm9`
    Static ARM9 compression: the ``0x30`` marker and its 12-byte footer.
:mod:`~rushdecomp.lz77`
    Nintendo LZ10 and LZ11, encoding and decoding.
:mod:`~rushdecomp.overlays`
    Y9/Y7 descriptor tables.
:mod:`~rushdecomp.filesystem`
    FNT and FAT, joined into a path index.
:mod:`~rushdecomp.banner`
    Icon, palette and localised titles.
:mod:`~rushdecomp.crc`
    CRC-16 as the DS uses it.
:mod:`~rushdecomp.rom`
    :class:`~rushdecomp.rom.NDSRom`, one object that knows where everything is.

Analysis:

:mod:`~rushdecomp.disasm`
    Recursive-descent ARM/Thumb disassembly to GAS syntax.
:mod:`~rushdecomp.assembler`
    Reassembly and byte-exactness comparison.
:mod:`~rushdecomp.progress`
    How much of the binary is expressed as C.
:mod:`~rushdecomp.cli`
    The ``sr`` command line.

No ROM, and nothing derived from one
------------------------------------
This package reads whatever image you point it at and writes only into the
generated directories the repository's ``.gitignore`` excludes.  It fetches
nothing, embeds nothing, and contains no data from any commercial game.  The
test suite runs against a synthetic image built from code in this repository.

See ``docs/ROM_SETUP.md`` for how to supply your own dump of a cartridge you
own, and ``docs/CONTRIBUTING.md`` for the rules about ROM-derived data.
"""

from __future__ import annotations

from . import lz77
from .arm9 import ARM9Image
from .banner import Banner
from .crc import crc16, crc16_ccitt
from .filesystem import NitroFileSystem
from .header import Header
from .overlays import Overlay, OverlayTable
from .rom import NDSRom, RomError
from .secure import Blowfish, SecureArea

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # Format parsers -- no third-party dependencies.
    "NDSRom",
    "RomError",
    "Header",
    "Banner",
    "NitroFileSystem",
    "Overlay",
    "OverlayTable",
    "ARM9Image",
    "SecureArea",
    "Blowfish",
    "lz77",
    "crc16",
    "crc16_ccitt",
    # Analysis -- import these directly; they need capstone / keystone.
    "Disassembler",
]


def __getattr__(name: str):
    """Resolve the analysis names on demand.

    ``disasm`` and ``assembler`` need capstone and keystone.  Importing them
    eagerly here would make ``import rushdecomp`` fail on a machine that only
    wants to parse a header or read the docs, so those two names are resolved
    lazily while the format parsers above are imported normally.

    Only these names go through ``__getattr__``.  Routing *every* attribute --
    including submodules -- through it recurses infinitely, because
    ``from . import lz77`` inside a submodule re-enters ``__getattr__`` before
    the submodule has been registered on the package.
    """
    if name == "Disassembler":
        from .disasm import Disassembler

        return Disassembler
    if name == "round_trip":
        from .assembler import round_trip

        return round_trip
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
