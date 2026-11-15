# `assets/` — converted art, audio, and factual maps

This directory is **tracked**. It holds work derived from a Sonic Rush cartridge
that is a *transformation* of the data rather than a copy of it — the same
distinction `pret/pokered` draws when it commits `gfx/intro/gengar.png` but
gitignores the compiled `*.2bpp` intermediates.

It is currently empty apart from this file: no retail cartridge has been
analysed in this repository yet.

## How these are generated

```sh
make assets        # extract -> disassemble every component -> write assets/
```

or directly, once `data/` and `asm/` are populated:

```sh
sr assets --rom baserom/sonic_rush.nds
```

`layout.md`, `filesystem.txt` and every `symbols/*.txt` are written by
`tools/rushdecomp/artifacts.py`. Symbol maps need the disassembly sidecars in
`asm/`, so run `sr disasm` per component first — without them the other two are
still written and the command says why the maps are missing.

**Generation refuses to write a leak.** Before any file reaches the disk,
`find_verbatim_run()` scans it for 16 or more contiguous bytes that also occur
in the cartridge image, and raises if it finds one. Offsets and sizes are hex
too, but `0x` prefixes and column separators break candidate runs, so address
tables pass while a pasted hexdump does not. Both directions are tested.

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
