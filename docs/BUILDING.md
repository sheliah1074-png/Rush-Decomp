# Building

## What you can build today

The toolchain: a Python package that parses DS cartridge images, disassembles
their ARM and Thumb code, reassembles it, and verifies the result byte for byte.
That is complete, tested, and needs nothing but Python 3.11+ and two pip
packages.

```sh
make setup
```

That creates `.venv/`, installs the dependencies, builds the synthetic test
image from `tests/make_test_rom.py`, runs the test suite, and then drives the
whole pipeline against that image — extract, disassemble, verify every
component, rebuild, and `cmp` the result against the base. If `make setup`
passes, the toolchain works on your machine.

No ROM, no compiler and no network access beyond PyPI are needed for that.

## Dependencies

| Dependency | Needed for | Install |
|---|---|---|
| Python 3.11+ | everything | usually present |
| `capstone` | disassembly | `make deps` |
| `keystone-engine` | reassembly, byte-exactness | `make deps` |
| `arm-none-eabi-gcc` | compiling C in `src/` | your package manager |
| Codewarrior for NDS | a genuinely matching binary | see below |

Both Python packages ship native libraries in their wheels, so there is no
compilation step and no system package requirement. On Debian/Ubuntu:

```sh
make deps
```

or by hand:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Working on the real image

Put your own dump at `baserom/sonic_rush.nds` first — see `docs/ROM_SETUP.md`.

```sh
make report        # what is this cartridge?
make extract       # split into data/
make dumpfs        # unpack the Nitro file system into fsdump/
make disasm-all    # every component -> asm/<name>.s + asm/<name>.json
make verify-all    # round trip each one, byte for byte
make rebuild       # rebuild a full image and compare to the base ROM
make progress      # how much is expressed as C
```

Any command takes `ROM=path` to override the default location:

```sh
make extract ROM=/mnt/nas/roms/sonic_rush.nds
```

Or call the CLI directly:

```sh
.venv/bin/python tools/sr report --rom baserom/sonic_rush.nds
.venv/bin/python tools/sr extract --rom baserom/sonic_rush.nds --banner --secure
.venv/bin/python tools/sr disasm arm9
.venv/bin/python tools/sr verify arm9 --strict
```

`tools/sr --help` lists everything.

### Expect the first `verify-all` on a real image to be imperfect

The synthetic image round-trips to 100% because every byte of it is understood.
A retail ARM9 will not, on the first run. The usual causes, in order of
frequency:

1. **Capstone/Keystone spelling disagreements.** Reported as `failures` in the
   verify output. Fix by adding a rule to `assembler.normalise()` — document
   what the disagreement was, because the next person will hit a sibling of it.
2. **Data decoded as code.** A false positive from the window classifier.
   Re-run that component with `--no-windows` and compare; if the mismatch
   disappears, tighten the threshold rather than disabling the scan globally.
3. **Mode confusion at an indirect branch.** Look at `unresolved_tails` in
   `asm/<name>.json`. These are real work items, not toolchain bugs.

Record what you find in the issue tracker. A decompilation project's value is
as much in its notes about what *doesn't* work as in its output.

## Compiling C

`src/` is currently empty apart from a worked example. Once you write real C you
need a cross-compiler:

```sh
sudo apt install gcc-arm-none-eabi binutils-arm-none-eabi
```

Compile a single unit to check it builds and to compare its shape against the
original:

```sh
arm-none-eabi-gcc -c -mcpu=arm946e-s -mthumb-interwork \
    -O2 -fno-strict-aliasing -Wall -Wextra \
    -Iinclude -Isrc \
    src/example/boot.c -o build/boot.o
arm-none-eabi-objdump -d build/boot.o
```

`-mcpu=arm946e-s` is the ARM9's core. The ARM7 is an ARM7TDMI:

```sh
-mcpu=arm7tdmi
```

`-mthumb-interwork` matters: DS binaries mix ARM and Thumb freely and call
between them, so a build without it generates wrong veneers.

## A genuinely matching binary

Getting byte-identical output from C source requires the compiler that produced
the original. Sonic Rush was built with Metrowerks Codewarrior for NDS, and no
open-source compiler reproduces its code generation, register allocation or
instruction scheduling. That is the hard ceiling on every commercial-game
decompilation, and it is why projects of this kind are honest about being
*structurally* complete long before they are *byte* complete.

The realistic ladder:

1. **Disassembly that round-trips** — done, and machine-checked.
2. **Named, documented disassembly** — manual, incremental, the bulk of the work.
3. **C that compiles and behaves identically** — verifiable in an emulator.
4. **C that compiles byte-identically with the original toolchain** — needs
   Codewarrior, which is abandonware you must source yourself. Not something
   this repository can or will provide.

Most contributors will work at levels 2 and 3, where the project is genuinely
useful: understanding how the game works, porting its logic, or building mods.

## Tests

```sh
make test          # 44 tests, runs standalone or under pytest
make selfcheck     # the full pipeline against the synthetic image
make check         # both
```

The suite runs against the synthetic image built by `tests/make_test_rom.py`,
never against a retail cartridge, so it works in CI and on a fresh clone.

`RepoHygieneTest` is the one to know about: it fails if `.gitignore` stops
covering the dangerous paths, if any tracked file has a ROM-like extension, or
if any tracked file exceeds 1 MiB. It is what keeps "no ROM in the repository"
true automatically rather than by good intentions.
