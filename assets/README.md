# `assets/` — converted art, audio, and factual maps

This directory is **tracked**. It holds work derived from a Sonic Rush cartridge
that is a *transformation* of the data rather than a copy of it — the same
distinction `pret/pokered` draws when it commits `gfx/intro/gengar.png` but
gitignores the compiled `*.2bpp` intermediates.

It is currently empty apart from this file: no retail cartridge has been
analysed in this repository yet.

## What belongs here

| Path | Contents |
|---|---|
| `assets/textures/` | Art converted **out of** the cartridge's 4/8 bpp tiled formats into PNG, with palettes applied |
| `assets/audio/` | Sequences and samples decoded from `SSEQ`/`SBNK`/`SWAR` into a working format |
| `assets/symbols/` | Address → name maps, one per component (`arm9.txt`, `arm9_overlay_003.txt`) |
| `assets/filesystem.txt` | The NitroFS listing: paths, file IDs, sizes |
| `assets/layout.md` | Overlay table, memory map, load addresses — the structural facts |

## What does not

- **Raw extraction output.** `data/*.bin` and `fsdump/**` are byte-identical
  chunks of the cartridge and stay gitignored. Convert first, then commit the
  conversion.
- **The cartridge image**, in whole or in part, under any name or extension.
  `RepoHygieneTest` reads the first 0x4000 bytes of every tracked file and
  fails the build if any of them opens with a plausible cartridge header, so
  renaming an image does not get it through.
- **`build/rebuilt.nds`** — a rebuilt image is still an image.

## Why the distinction is worth keeping

Verbatim blobs concatenated reconstitute the ROM, which is the one thing this
project has agreed never to publish. A disassembly, a converted texture, or a
symbol map cannot be reassembled into a bootable cartridge: the transformation
loses the padding, the secure area, the header CRCs and the original
compression, and adds naming and structure that did not exist in the source.

That is also the practical test. If a file here could be `cat`'d back into
something a flash cart would boot, it does not belong here.

## `make clean` does not touch this directory

Everything in here is meant to be committed, and uncommitted work is exactly
what a blanket `git clean` destroys. Clean the generated directories instead.
