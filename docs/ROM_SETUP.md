# ROM setup

This repository contains **no cartridge image and no verbatim copy of one**.
`.gitignore` excludes `baserom/`, `data/`, `fsdump/` and `build/`;
`RepoHygieneTest` reads the first 0x4000 bytes of every tracked file and fails
the build if any of them opens with a plausible cartridge header, so renaming an
image does not get it through; and CI runs that check over every blob in git
history on every push.

Derived *analysis* is a different matter and is committed on purpose — see
[What is and is not in this repository](../README.md#what-is-and-is-not-in-this-repository).

So the first thing you have to do is supply your own image.

## You need a dump of a cartridge you own

Extracting a ROM you did not buy is piracy, and distributing one is worse. This
project is set up so that nobody has to: the tools run against whatever image
you point them at, locally, and the generated output never leaves your machine.

Dump your own cartridge with hardware made for it:

| Method | Notes |
|---|---|
| [GodMode9](https://github.com/d0k3/GodMode9) on a 3DS | Reads a DS card inserted in the 3DS's slot. The usual route today. |
| `nds-bootstrap` + a flash cart | Works if you already have the hardware. |
| A DS Lite with a slot-2 dumper | Older, slower, still fine. |

Copy the resulting `.nds` file to:

```
baserom/sonic_rush.nds
```

`baserom/` is gitignored. You can also point any command at a different path
with `--rom`:

```sh
make report ROM=/mnt/nas/roms/sonic_rush.nds
```

## Confirming what you have

Sonic Rush shipped as several distinct revisions. They are **not**
interchangeable: code addresses differ between them, so a disassembly built
from one revision will not line up with another. Before you do anything else,
run:

```sh
make report
```

and check the game code in the output.

| Region | Game code | Maker code | Notes |
|---|---|---|---|
| North America | `SRSE` | `8P` | The revision this project's `config/` targets by default. |
| Europe | `SRSP` | `8P` | Multi-language banner; different code layout. |
| Japan | `SRJJ` | `8P` | Original release. |

The report also prints the image's SHA-256 and MD5. Record them somewhere — if a
rebuild ever stops matching, the first question is whether the base image
changed, and a checksum answers it instantly.

`config/srse.json` holds the expected identity for the North American revision.
`sr report` flags an image that does not match it rather than failing, so a
European dump still works; it just tells you which config applies.

## What "no ROM in the repository" covers

Everything below is generated from your dump, and all of it is gitignored:

| Path | Contents |
|---|---|
| `baserom/` | your cartridge image |
| `data/` | extracted ARM9/ARM7/overlay payloads, banner, secure area, manifest |
| `asm/` | generated `.s` disassembly and its JSON sidecars |
| `fsdump/` | the unpacked Nitro file system |
| `build/` | rebuilt images, progress records, the synthetic test ROM |
| `tools/keys/` | any Nintendo KEY1 blob you supply for secure-area decryption |

If you find yourself about to `git add -f` one of those, stop. The one exception
in the whole repository is `tests/data/testrom.nds`, which is the *synthetic*
image built by `tests/make_test_rom.py` from code in this repository. It is not
a dump of anything, and it is safe to commit — but it is also regenerated on
demand, so it is not committed either.

## Optional: secure-area decryption

The secure area at cartridge offset `0x4000` is the only encrypted part of a DS
image. Everything a decompilation cares about — ARM9, ARM7, overlays, the file
system — is plaintext, so you do not need this.

If you do want it decoded, the toolchain needs Nintendo's KEY1 table, a
`0x1048`-byte block of Blowfish P-array and S-box values from the DS BIOS. That
is Nintendo firmware data and it is **not** in this repository, and it never
will be. Dump it from hardware you own and place it at:

```
tools/keys/nds_key1.bin
```

`tools/keys/` is gitignored. Extraction validates its work: a correct key
yields the ASCII signature `encryObj` at the start of the decrypted block, and
if that signature is absent the tool reports failure and leaves the bytes raw
rather than emitting garbage.

## Troubleshooting

**`error: no cartridge image at 'baserom/sonic_rush.nds'`**
Put your dump there, or pass `ROM=...`.

**`only N bytes; a DS image needs at least 0x160`**
You are pointing at a save file, a patch, or an archive. Extract the archive
first — `.zip` and `.7z` are not cartridge images.

**`ARM9 extent ... does not fit inside a 0x...-byte file`**
The image is truncated. Re-dump it; a partial copy will not work and cannot be
repaired from here.

**`unit code 0xNN is not a DS/DSi value`**
Probably not a DS image at all — this happens when a GBA or SNES ROM is
renamed to `.nds`.

**Disassembly shows a wall of `andeq r0, r0, r0`**
That is zero padding being read as code. It should not happen for any region
reached by the sweep, because the padding guard refuses `0x00`/`0xFF` runs as
instruction starts. If you see it anyway, the base or entry address is wrong;
check `data/<component>.json`.

**`LZ10 reference ... reaches back past the start of the output`**
A blob was treated as compressed when it is not. Compression is taken only from
authoritative sources — the ARM9 footer or an overlay table flag — never from
sniffing the first byte. If you hit this, something upstream mis-set that flag;
open an issue with the component name and the report output.
