# `asm/` — committed disassembly

This directory is **tracked**. It is where the disassembly of a real Sonic Rush
cartridge lands, and unlike the raw extraction output it is meant to be
committed.

It is currently empty apart from this file. No retail cartridge has been
analysed in this repository, so there is nothing here yet — the toolchain has
been verified end to end against the synthetic image built by
`tests/make_test_rom.py`, not against the game.

## Layout

One pair of files per component, named after it:

```
asm/arm9.s                 disassembly, GAS syntax
asm/arm9.json              sidecar: functions, extents, modes, byte coverage
asm/arm9_overlay_003.s
asm/arm9_overlay_003.json
asm/arm7.s
asm/arm7.json
```

Regenerate any of them with `sr disasm <component>` after `sr extract`.

## Line format

Instructions carry their address and raw encoding in a comment, delimited so the
reassembler can strip it:

```
/* 02001A80 | 10402DE9 */    push    {r4, lr}
/* 02001A84 |          */    .word   0x02004c1d  # -> 0x02004c1c [thumb]
```

Code and data are emitted in **strict address order**. Interleaving them is not
a stylistic choice: grouping all the data after all the code reads better but
reassembles into a completely different binary.

## Why this is committed and `data/` is not

`data/arm9.bin` is a byte-identical chunk of the cartridge. `asm/arm9.s` is a
transformation of one, carrying symbol names, function extents and structure
that do not exist anywhere in the original binary — and it cannot be
reconstituted into something a flash cart would boot, because the padding,
secure area, header CRCs and original compression are all gone.

`make clean` honours the difference. It removes only untracked `*.s` and `*.json`
from this directory, so generated disassembly can be swept without destroying
work someone has committed or has not committed yet.
