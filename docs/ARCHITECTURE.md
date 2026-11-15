# Architecture

Two things are described here: what a DS cartridge image actually contains, and
how this repository is organised around it.

## The cartridge image

A `.nds` file is a flat byte array with a header at the front naming the offsets
and sizes of everything else. There is no container format, no directory of
sections, no index beyond what the header fields say.

```
0x000000  +----------------------------------+
          | Header (0x4000 bytes)            |  names every other region
0x004000  +----------------------------------+
          | Secure area (0x2000 bytes)       |  the only encrypted part
0x006000  +----------------------------------+
          | ARM9 binary                      |  may be statically compressed
          +----------------------------------+
          | ARM7 binary                      |  always raw
          +----------------------------------+
          | ARM9 overlay table (Y9)          |  32 bytes per overlay
          +----------------------------------+
          | ARM7 overlay table (Y7)          |
          +----------------------------------+
          | FNT — file names, hierarchical   |
          +----------------------------------+
          | FAT — file extents, flat         |
          +----------------------------------+
          | File payloads                    |  overlays live here too
          +----------------------------------+
          | Banner — icon, palette, titles   |
          +----------------------------------+
```

### Two processors, one card

The DS has an ARM9 (the main CPU, running the game) and an ARM7 (handling
sound, Wi-Fi and the touch controller). Each gets its own binary, entry
address, load address and overlay table. They are separate programs sharing a
cartridge, and the ARM9/ARM7 split is the first thing to understand when
reading a DS disassembly: code that touches audio or wireless lives on the ARM7
and has no business appearing in ARM9 analysis.

### Overlays

The DS has about 4 MB of main RAM. A game with dozens of zones cannot keep all
its code resident, so it ships *overlays*: separately-compiled code images that
are loaded into a fixed RAM window on demand and unloaded when no longer needed.

Each overlay is described by a 32-byte entry in the Y9 or Y7 table giving its
RAM address, its size, its BSS size, the address range of its static
initialisers, and the file ID of the payload in the Nitro file system. For a
zone-based platformer this is how per-level code, boss routines and
multiplayer-only logic share one small window.

The practical consequence for decompilation: **overlay code cannot be analysed
in isolation**. Two overlays may reuse the same RAM address for entirely
different functions, so symbol names have to be scoped per overlay or they will
collide. This toolchain names dumps `arm9_overlay_NNN` for exactly that reason.

### The Nitro file system

Two tables, joined by file ID:

- **FAT** — a flat array of 8-byte `(start, end)` pairs. No names.
- **FNT** — a hierarchical name tree whose leaf records end in a 16-bit index
  into the FAT.

Overlays have no FNT entry; their file IDs come from the overlay tables and are
allocated outside the file-system range. This is why `sr dumpfs` and
`sr extract` are separate operations that both read the FAT.

### Static ARM9 compression

The ARM9 is loaded and started before the SDK's own decompression code exists,
so a packed ARM9 has to be unpacked by the BIOS. The convention:

1. The first byte is `0x30` rather than the LZ77 marker `0x10`. `0x30` is not a
   valid LZ77 marker — that is the point, so generic decompressors leave it
   alone.
2. Twelve bytes are appended after the payload: the decompressed size, the
   compressed size, and a verification word `0x5F030000 - (size >> 9)` that lets
   the BIOS tell a real statically-compressed ARM9 from a first byte that
   happens to be `0x30`.
3. The BIOS expands it in place, backwards from the top, because the
   decompressed image is larger than the compressed one.

The verification word has two spellings in the wild — an arithmetic
(sign-extending) shift and a logical one — from different SDK revisions. The
toolchain records which a given image used and writes back the same spelling, so
a rebuild that changed one instruction does not incidentally change the footer.

### Why compression is never guessed

`push {r4, lr}` assembles to `10 40 2D E9`. Its first byte is `0x10` — the LZ10
marker. `and` starts with `0x11`, the LZ11 marker. Any function prologue can
look exactly like a compressed stream to a byte-sniffing loader, and
"decompressing" it produces plausible-looking garbage rather than an error.

So this toolchain takes compression **only** from an authoritative source:

| Component | Source of truth |
|---|---|
| ARM9 | the `0x30` marker *and* a self-consistent 12-byte footer |
| ARM7 | never compressed — the BIOS loads it raw |
| Overlays | the compress flag in the overlay table entry |
| File-system files | never auto-detected; the game decides at runtime |

This is not a theoretical concern. It is the bug that the synthetic test image
was built to catch: its ARM7 begins with `0x10`, and the first version of the
extractor happily "decompressed" it into nonsense.

## The repository

```
tools/
  sr                     CLI entry point
  rushdecomp/
    lz77.py              Nintendo LZ10/LZ11, encode and decode
    crc.py               CRC-16 as the DS uses it
    header.py            the 0x4000-byte cartridge header
    banner.py            icon, palette, localised titles
    filesystem.py        FNT + FAT joined into a path index
    overlays.py          Y9/Y7 descriptor tables
    arm9.py              static ARM9 compression and its footer
    secure.py            Blowfish and the secure area
    rom.py               NDSRom: one object that knows where everything is
    disasm.py            capstone-based recursive-descent disassembler
    assembler.py         keystone-based round trip and byte comparison
    progress.py          matching-progress accounting
    cli.py               argument parsing and the verbs
include/nitro/           hardware headers, hand-written
src/                     hand-written C reconstruction
config/                  per-revision expected identity
tests/                   end-to-end tests plus the synthetic image builder
docs/                    this directory
```

`data/`, `asm/`, `fsdump/`, `build/` and `baserom/` are generated and gitignored.

### How disassembly decides what is code

There is no symbol table, so it is heuristic, in this order:

1. **Seeded recursive sweep** from the entry address, following every direct
   branch whose target lands inside the image. Each worklist entry carries the
   mode it must be decoded in, because ARM and Thumb decode differently.
2. **Terminator discipline.** An unconditional `b`, `bx`, `pop {..., pc}` or
   `ldr pc, ...` ends a block; the sweep does *not* fall through into the next
   word. Falling through is what silently walks an epilogue into whatever
   padding follows it.
3. **Padding guard.** A zero word is a legal `andeq r0, r0, r0`. Without
   refusing `0x00`/`0xFF` runs as instruction starts, the sweep eats megabytes
   of filler and then enters the next section from the wrong side, decoding
   Thumb bytes as ARM.
4. **Prologue scan** for unreached `push {..., lr}` and `mov ip, sp`, which
   recovers functions only ever called indirectly.
5. **Window classification** for anything still unexplained, scoring a window
   decoded as ARM against the same window decoded as Thumb. The threshold is
   high and the address must be word-aligned, because a false positive renders
   data as instructions.

Indirect branches (`bx r4`, `blx r0`) cannot be resolved statically. They are
recorded as *unresolved tails* in the JSON sidecar and reported, because they
are exactly where jump tables hide. Expect the tail list to be long on a real
image; it is a work queue, not a failure.

### Why emission is address-ordered

The renderer interleaves code and data in address order rather than printing all
code first and all data second. The tidier output would reassemble into a
completely different binary: the section is a flat byte stream, so position *is*
meaning.

### The byte-exactness contract

```
payload  ->  disasm  ->  .s  ->  assemble  ->  bytes  ->  compare
```

`sr verify` runs that loop and reports the match ratio. An image that survives
it has a disassembly that faithfully represents every instruction the DS would
execute — the precondition for converting any of it to C. `sr rebuild` extends
it to the whole cartridge, recompressing the ARM9 and comparing the resulting
image against the base ROM.

Capstone and Keystone are not perfect mirrors of each other, so
`assembler.normalise` rewrites the handful of operand spellings where they
legitimately disagree. Every rewrite is documented in that function, so a
mismatch can be traced to a normalisation rule rather than to a real difference
in the code.

### Limits of the current pipeline

Stated plainly, because they determine what is and is not ready to convert:

- **No symbol recovery.** Labels are `func_02001A80` — addresses, not names.
  Naming is manual reverse-engineering work, informed by string references and
  Nitro SDK conventions.
- **No type reconstruction.** The disassembly is instruction-level. Turning it
  into C structs is the human part of the project.
- **Jump tables are not followed.** A `switch` compiled to a table of pointers
  shows up as unresolved tails plus a run of data words annotated with their
  targets. The annotation is there to make those findable, not to resolve them.
- **Recompression is not guaranteed.** The LZ10/LZ11 encoders are greedy over a
  hash chain and reproduce the reference output for most inputs, but a different
  vendor encoder may break a tie differently. `recompression_matches()` exists
  so the build checks before trusting a regenerated blob, and falls back to the
  pristine bytes rather than claiming a match it did not earn.
- **No compiler.** Nothing here builds C. Once `src/` has content you will want
  `arm-none-eabi-gcc`, and eventually the original Codewarrior toolchain if the
  goal is a genuinely matching binary. See `docs/BUILDING.md`.
