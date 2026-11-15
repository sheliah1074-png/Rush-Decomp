# Rush-Decomp

A decompilation toolkit for **Sonic Rush** (Nintendo DS, 2005).

It parses a DS cartridge image, splits it into its ARM9, ARM7 and overlay
components, disassembles their ARM and Thumb code into GAS-syntax assembly,
reassembles that assembly, and verifies the result is **byte-identical to the
original**. That round trip is what makes the project a decompilation rather
than a pile of hex dumps: every claim about the code is machine-checkable
against the binary it came from.

## What is and is not in this repository

The line this project draws is between **the cartridge** and **work derived from
it**.

Never committed:

- `baserom/` — the cartridge image, the copyrighted work itself
- `build/` — build products, including `rebuilt.nds`, because a rebuilt image is
  still an image
- `data/`, `fsdump/` — raw extraction output: byte-identical chunks of the
  cartridge which, concatenated, reconstitute most of it
- `tools/keys/` — the Nintendo KEY1 blob is copyrighted firmware data

Committed, because it is the work product:

- `asm/` — the disassembly, carrying the symbol names and structure given to it
  here rather than anything present in the original binary
- `assets/` — art and audio *converted* out of the cartridge's own formats, plus
  symbol maps and filesystem listings
- `src/`, `include/`, `docs/`, `tools/` — original reconstruction and tooling
- a **synthetic test cartridge** built from code written for this repository,
  which is how the whole pipeline is verified in CI without touching a retail
  dump

This follows the convention the public decompilation projects actually use:
[`pret/pokered`](https://github.com/pret/pokered) commits its full disassembly
and converted `gfx/*.png` while gitignoring the compiled `*.gb`;
[`zeldaret/ph`](https://github.com/zeldaret/ph) and
[`zeldaret/st`](https://github.com/zeldaret/st) gitignore `*.nds` and their
extracted directories.

Enforced three ways:

- `.gitignore` excludes the cartridge and its verbatim contents, and is tested in
  *both* directions — a rule broad enough to keep a ROM out can quietly keep the
  disassembly out too, leaving a repository that looks clean and contains no work
- `RepoHygieneTest` reads the first 0x4000 bytes of every tracked file and fails
  if any of them opens with a plausible cartridge header, so renaming an image
  does not help it get through
- CI runs that same content check over **every blob in git history**, not just
  the tip

You supply your own dump of a cartridge you own. See
[`docs/ROM_SETUP.md`](docs/ROM_SETUP.md).

## Status

| | |
|---|---|
| Toolchain | **Working.** Verified end to end against a synthetic image. |
| Byte-exactness | **100%** on every component of the synthetic image, including LZ10 recompression of a statically-compressed ARM9. |
| Sonic Rush disassembly | **Not started.** Needs a cartridge image, which cannot be distributed. |
| Matched C | **0%.** `src/` holds a worked example, not real reconstruction. |

The honest summary: the machinery for decompiling Sonic Rush is built and
tested. Running it on the actual game requires a ROM that is yours, on your
machine — which is the whole point of how this is set up.

## Quick start

```sh
git clone <this repository> && cd Rush-Decomp
make setup
```

No ROM needed for that. It creates a venv, installs `capstone` and
`keystone-engine`, builds the synthetic test image, runs 55 tests, then drives
the complete pipeline against it and `cmp`s the rebuilt image against the base.

Then, with your own dump at `baserom/sonic_rush.nds`:

```sh
make report        # what is this cartridge?
make extract       # split it into data/
make dumpfs        # unpack the Nitro file system into fsdump/
make disasm-all    # every component -> asm/<name>.s
make verify-all    # round trip each one, byte for byte
make rebuild       # rebuild a full image, compare to the base ROM
make progress      # how much is expressed as C
```

## What the pipeline does

```
your.nds ──► parse header ──► split ARM9 / ARM7 / overlays / NitroFS
                    │
                    ▼
             disassemble (capstone)          recursive sweep, ARM + Thumb,
                    │                        mode-tracked, padding-aware
                    ▼
              asm/<name>.s  +  asm/<name>.json
                    │
                    ▼
             reassemble (keystone) ──► compare byte for byte ──► MATCH
                    │
                    ▼
             recompress + relayout ──► build/rebuilt.nds ──► cmp your.nds
```

Every step is a pure function from bytes to bytes, so it can be checked rather
than trusted.

## Sonic Rush revisions

Code addresses differ between revisions, so they are **not** interchangeable —
a disassembly built from one will not line up with another. `make report` tells
you which you have.

| Region | Game code | Maker code |
|---|---|---|
| North America | `SRSE` | `8P` |
| Europe | `SRSP` | `8P` |
| Japan | `SRJJ` | `8P` |

`config/srse.json` records the expected identity for the North American
revision. A mismatch is reported, not fatal — a European dump works fine, it
just tells you which config applies.

## Layout

```
tools/sr                  CLI entry point
tools/rushdecomp/         the toolkit
  lz77.py                 Nintendo LZ10/LZ11, encode and decode
  crc.py                  CRC-16 as the DS uses it
  header.py               the 0x4000-byte cartridge header
  banner.py               icon, palette, localised titles
  filesystem.py           FNT + FAT joined into a path index
  overlays.py             Y9/Y7 descriptor tables
  arm9.py                 static ARM9 compression and its footer
  secure.py               Blowfish and the secure area
  rom.py                  NDSRom: one object that knows where everything is
  disasm.py               recursive-descent ARM/Thumb disassembler
  assembler.py            round trip and byte comparison
  progress.py             matching-progress accounting
  cli.py                  the verbs
include/nitro/            hand-written hardware headers
src/                      hand-written C reconstruction
asm/                      committed disassembly          (tracked)
assets/                   converted art/audio, symbol maps (tracked)
config/                   per-revision expected identity
tests/                    end-to-end tests + synthetic image builder
docs/                     documentation
```

`data/`, `fsdump/`, `build/` and `baserom/` are generated and gitignored: they
hold the image itself and verbatim chunks of it. `asm/` and `assets/` are
generated too but **tracked** — see
[What is and is not in this repository](#what-is-and-is-not-in-this-repository).

## Documentation

- [`docs/ROM_SETUP.md`](docs/ROM_SETUP.md) — dumping your own cartridge,
  revision identification, troubleshooting
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the cartridge format, how
  disassembly decides what is code, and the limits of the current pipeline
- [`docs/BUILDING.md`](docs/BUILDING.md) — dependencies, the CLI, compiling C,
  and what "matching" actually requires
- [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) — what needs doing, how to
  convert a function, what may and may not be committed

## Requirements

Python 3.11+ (3.9 and 3.10 are past end of life and absent from current CI
runner images, though the code itself uses no newer syntax), plus:

| Package | For |
|---|---|
| `capstone` | disassembly |
| `keystone-engine` | reassembly and byte-exactness checking |

Both ship native libraries in their wheels — no compiler, no system packages.
`make deps` handles it.

An `arm-none-eabi-gcc` cross-toolchain is only needed once you start compiling
C under `src/`; it is not a Python dependency.

## Known limits

Stated plainly, because they determine what is ready to work on:

- **No symbol recovery.** Labels are addresses (`func_02001A80`), not names.
  Naming is manual reverse-engineering.
- **No type reconstruction.** Instruction-level output; turning it into C
  structs is the human part.
- **Jump tables are not followed.** A compiled `switch` shows up as unresolved
  indirect branches plus data words annotated with their targets — findable,
  not resolved.
- **Recompression is not guaranteed.** The LZ encoders are greedy over a hash
  chain and reproduce the reference output for most inputs, but a different
  vendor encoder may break a tie differently. `recompression_matches()` makes
  the build check before trusting a regenerated blob, and fall back to the
  pristine bytes rather than claim a match it did not earn.
- **No matching C build.** Sonic Rush was compiled with Metrowerks Codewarrior
  for NDS. No open-source compiler reproduces its code generation, so a
  byte-identical C rebuild is out of reach without sourcing that toolchain
  yourself. This is the hard ceiling on every commercial-game decompilation.

## License

Tooling and documentation are MIT — see [`LICENSE.md`](LICENSE.md). That licence
covers this repository's own work only. It says nothing about Sonic Rush, which
is a Sega work, and it grants no rights in any ROM or in anything extracted from
one.

This project exists to study and understand a commercial game, in the tradition
of other decompilation and preservation efforts. It distributes no cartridge
image and no verbatim copy of one. Derived analysis — disassembly, converted
assets, symbol maps — is committed here on the same basis as the public
decompilation projects listed above; whether that is lawful where you are is
your judgement to make, and not something an MIT licence on the tooling settles
for you.

You are responsible for how you use it: dump your own cartridge, and never upload
what `.gitignore` excludes.
