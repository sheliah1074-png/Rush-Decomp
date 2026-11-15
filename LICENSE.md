MIT License

Copyright (c) 2026 Rush-Decomp contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---------------------------------------------------------------------------

Scope of this licence
---------------------------------------------------------------------------

This licence covers the tooling, documentation, headers and tests in this
repository -- work written by its contributors.

It does not cover, and grants no rights in:

  * Sonic Rush, which is a work of Sega Corporation and its developers;
  * any Nintendo DS ROM image, or any data extracted, disassembled,
    decompiled or otherwise derived from one;
  * the Nintendo DS BIOS, including the KEY1 table referenced by
    tools/rushdecomp/secure.py, which is Nintendo firmware data and is not
    present in this repository;
  * any Codewarrior for NDS toolchain or its output.

No cartridge image is present in this repository, and no verbatim copy of one.
Raw extraction output -- the image you supply, the binaries split from it, the
file system contents and the rebuilt image -- is written into directories
excluded from version control and remains yours alone.

Derived analysis is present: the disassembly under asm/ and converted art, audio
and symbol maps under assets/. This licence grants no rights in that material
either. It is included on the same basis as other public decompilation projects,
and the judgement about whether that is lawful where you are is yours to make.
See docs/CONTRIBUTING.md and docs/ROM_SETUP.md.
