# Worked example

This directory shows the *shape* of a converted function: what the annotation
looks like, how a disassembly becomes C, and what `sr progress` expects.

It is not Sonic Rush code. The function below is a small original routine
written for this repository, and its `@rush_match` annotation points at
`func_02000000` in the **synthetic test image** built by
`tests/make_test_rom.py`. That keeps the example honest — it can be checked —
and keeps it out of the way of real work.

When you convert something real, mirror the component it came from:

```
src/arm9/game/player.c
src/arm7/sound/driver.c
src/arm9_overlay_012/zone/boss.c
```

and delete the corresponding function from `asm/<image>.s` so the two cannot
drift apart.

## Running it

```sh
make selftest                                  # build the synthetic image
.venv/bin/python tools/sr extract --rom build/selftest.nds
.venv/bin/python tools/sr disasm arm9
.venv/bin/python tools/sr progress
```

`sr progress` reads the annotation in `example.c`, checks the declared byte
count against what the disassembly measured for `func_02000000`, and reports
either the matched bytes or a problem explaining why they do not line up. If you
edit the annotation to claim a wrong size, the tool says so instead of quietly
counting it — that check is the whole reason the format exists.

Try it: change `0x4c` to `0x24` in `example.c` and re-run `sr progress`. You get

```
annotation problems:
  arm9: src/example/example.c: func_02000000 is annotated as 36 bytes but the
        disassembly measures 76
```

## A limitation this example exposes

The annotation has to say `0x4c`, not the sum of the entry function's own
instructions. That is because the recursive sweep's function bounds
**over-extend across a call**: `func_02000000` is reported as
`0x02000000..0x0200004c`, which swallows the Thumb helper it `blx`es into at
`0x02000040`.

This is inherent to sweeping without symbols. A callee reached by a direct call
gets absorbed into the caller's extent unless something else — a second
reference, a prologue hit, a mode change — marks it as its own function first.
On the synthetic image the Thumb helper is only reachable through that one
`blx`, so nothing separates them.

Splitting them is manual work, and it is worth doing before converting either:

1. Find the callee's first instruction. A `push {..., lr}` is the usual sign.
2. Add it to the sidecar as its own function entry, or annotate the boundary in
   the `.s` with a comment a later pass can pick up.
3. Convert each separately, with its own `@rush_match`.

Expect this on every real image. It is one of the main reasons a decompilation
is not finished when the disassembler stops producing output.
