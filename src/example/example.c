/**
 * @file example.c
 * @brief Worked example of a converted function.
 *
 * This is original code written for this repository, not Sonic Rush code.  Its
 * annotation refers to the synthetic image built by tests/make_test_rom.py, so
 * the example can actually be checked rather than merely believed.
 *
 * What it demonstrates:
 *
 *   1. The `@rush_match` annotation format, and that the byte count is
 *      verified against the disassembly rather than trusted.
 *   2. Naming: the disassembly calls this `func_02000000`; here it has a name
 *      that says what it does.
 *   3. Types: the disassembly has `r0` and `r1`. Here they are arguments of a
 *      declared type, which is the reconstruction step a disassembler can never
 *      do for you.
 *   4. Structure over cleverness. The C below follows the original's control
 *      flow even where a modern compiler would write it differently, because
 *      matching the binary matters more than matching good taste.
 *
 * See the README in this directory for how to run the progress check.
 */

#include "nitro/types.h"

/*
 * @rush_match arm9 0x4c func_02000000
 *
 * 0x4c is what the disassembly actually measured, not what the function's own
 * instructions add up to.  The sweep's function bounds over-extend across a
 * call: `func_02000000` runs 0x02000000..0x0200004c, which swallows the Thumb
 * helper it `blx`es into at 0x02000040.  Separating those two is manual work --
 * see the README -- and until it is done, the annotation reports the sweep's
 * bound so that `sr progress` and the disassembly agree.
 *
 * Change this number to anything else and `sr progress` will refuse to count it
 * and say why.  That is the point of the format.
 */

/**
 * The synthetic ARM9's entry point.
 *
 * Its disassembly is an ARM prologue that saves two registers and the link
 * register, adds its arguments, subtracts one, compares against zero and
 * branches to a stub if the result is zero -- otherwise it tail-calls into a
 * Thumb helper and returns whatever that produced.  Deliberately simple, and
 * deliberately crossing the ARM/Thumb boundary, because that crossing is the
 * thing most likely to be gotten wrong in a real conversion.
 *
 * @param a  first operand
 * @param b  second operand
 * @return   0 when @p a + @p b - 1 is zero, otherwise the Thumb helper's result
 */
s32 Example_AddAndDispatch(s32 a, s32 b)
{
    /* The original computes this in a saved register, not on the stack. */
    s32 sum = a + b;
    sum -= 1;

    if (sum == 0) {
        /*
         * Branches to a stub that returns zero.  Written as an explicit return
         * here because the stub's only purpose is to be a branch target; a real
         * conversion would keep the stub if other callers shared it.
         */
        return 0;
    }

    /*
     * The original uses `blx` into Thumb code, which scales the value by four.
     * That call is spelled out rather than inlined so the ARM/Thumb transition
     * is visible in the source -- a reader comparing this against the
     * disassembly needs to see where the mode changed.
     */
    return Example_ThumbScale(sum);
}

/**
 * The Thumb helper the entry point calls into.
 *
 * Corresponds to the function at the tail of the synthetic ARM9's Thumb region.
 * It is declared here rather than defined, because a real project would put
 * Thumb-resident code in its own translation unit and link it with
 * `-mthumb-interwork`.
 */
s32 Example_ThumbScale(s32 value);
