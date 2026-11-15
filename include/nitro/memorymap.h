/**
 * @file memorymap.h
 * @brief The DS memory map, as the ARM9 and ARM7 each see it.
 *
 * Hand-written for this project from the published hardware layout.
 *
 * The single most confusing thing about DS memory is that the two processors do
 * not see the same map.  Main RAM is shared but mirrored at different addresses
 * depending on which CPU asks; some regions are ARM9-only, some ARM7-only, and
 * the VRAM banks can be redirected between them at runtime.  A disassembly that
 * does not keep this straight will attribute ARM7 audio code to the ARM9 and
 * wonder why the addresses make no sense.
 *
 * Which mirror a given binary uses is decided by the `WRAMCNT` register at
 * 0x04000247, so a reconstruction should never hard-code a main-RAM address
 * without saying which mapping it assumes.
 */

#ifndef RUSHDECOMP_NITRO_MEMORYMAP_H
#define RUSHDECOMP_NITRO_MEMORYMAP_H

/* ------------------------------------------------------------------------ */
/* Main RAM (4 MiB), shared, mirrored per processor                          */
/* ------------------------------------------------------------------------ */

/**
 * The ARM9 sees main RAM at 0x02000000 when `WRAMCNT` selects the full 4 MiB
 * for the ARM9 side.  This is the mapping a retail ARM9 binary is linked
 * against, and it is what the cartridge header's `arm9_ram_address` reports.
 */
#define ARM9_MAIN_RAM_BASE      0x02000000u
#define ARM9_MAIN_RAM_SIZE      0x00400000u   /* 4 MiB */
#define ARM9_MAIN_RAM_END       (ARM9_MAIN_RAM_BASE + ARM9_MAIN_RAM_SIZE)

/**
 * The ARM7 sees main RAM at 0x02000000 too, but only the window `WRAMCNT`
 * leaves it.  Retail ARM7 binaries are usually linked for 0x02380000 -- a
 * 512 KiB slice at the top of main RAM -- because the ARM9 owns the rest.
 */
#define ARM7_MAIN_RAM_BASE      0x02000000u
#define ARM7_DEFAULT_RAM_BASE   0x02380000u   /* common retail ARM7 link base */
#define ARM7_DEFAULT_RAM_SIZE   0x00080000u   /* 512 KiB */

/* ------------------------------------------------------------------------ */
/* On-chip memory                                                            */
/* ------------------------------------------------------------------------ */

/**
 * ARM9 Instruction TCM: 32 KiB, zero-wait-state, position configurable in
 * 32 KiB steps.  Latency-critical code -- interrupt handlers, the inner loops
 * of a platformer's physics -- lives here.
 */
#define ARM9_ITCM_BASE          0x00000000u
#define ARM9_ITCM_SIZE          0x00008000u   /* 32 KiB */

/**
 * ARM9 Data TCM: 16 KiB, base configurable in 16 KiB steps, usually at
 * 0x027C0000.  Holds stacks and hot data.
 */
#define ARM9_DTCM_DEFAULT_BASE  0x027C0000u
#define ARM9_DTCM_SIZE          0x00004000u   /* 16 KiB */

/** ARM9 shared WRAM, when not allocated to the ARM7. */
#define ARM9_WRAM_BASE          0x03000000u
#define ARM9_WRAM_SIZE          0x00008000u   /* 32 KiB */

/**
 * ARM7 WRAM: 64 KiB private to the ARM7.  Its stacks and IPC buffers live
 * here, not in main RAM.
 */
#define ARM7_WRAM_BASE          0x037F8000u
#define ARM7_WRAM_SIZE          0x00010000u   /* 64 KiB */

/* ------------------------------------------------------------------------ */
/* I/O registers                                                             */
/* ------------------------------------------------------------------------ */
#define ARM9_IO_BASE            0x04000000u
#define ARM7_IO_BASE            0x04000000u   /* same base, different decode */
#define ARM9_IO_SIZE            0x00001000u

/* ------------------------------------------------------------------------ */
/* VRAM                                                                      */
/* ------------------------------------------------------------------------ */

/**
 * VRAM is nine banks (A-I) of differing size, each individually steerable to
 * the ARM9's LCD controller, the ARM7's, the texture cache, or plain CPU-access
 * RAM.  The base below is the linear view; which banks are actually visible
 * there depends on `VRAMCNT_*` at the time.
 */
#define VRAM_BASE               0x06000000u
#define VRAM_SIZE               0x00100000u   /* 1 MiB across all banks */

#define VRAM_A_BASE             0x06000000u   /* 128 KiB */
#define VRAM_B_BASE             0x06020000u   /* 128 KiB */
#define VRAM_C_BASE             0x06840000u   /* 128 KiB */
#define VRAM_D_BASE             0x06860000u   /* 128 KiB */
#define VRAM_E_BASE             0x06200000u   /*  64 KiB */
#define VRAM_F_BASE             0x06890000u   /*  16 KiB */
#define VRAM_G_BASE             0x06894000u   /*  16 KiB */
#define VRAM_H_BASE             0x06898000u   /*  32 KiB */
#define VRAM_I_BASE             0x068A0000u   /*  16 KiB */

/** Engine A (main engine) and Engine B (sub engine) backgrounds and objects. */
#define BG_VRAM_A               0x06000000u
#define BG_VRAM_B               0x06020000u
#define OBJ_VRAM_A              0x06400000u
#define OBJ_VRAM_B              0x06600000u

/**
 * Texture VRAM.  Only banks A, B, C and D can serve textures, and only in
 * 128 KiB slots; this is why a DS game's texture budget is 512 KiB no matter
 * how the rest of VRAM is allocated.
 */
#define TEX_VRAM_SLOT_0         0x06000000u
#define TEX_VRAM_SLOT_1         0x06020000u
#define TEX_VRAM_SLOT_2         0x06840000u
#define TEX_VRAM_SLOT_3         0x06860000u

/** Palette RAM: 512 KiB, split between the two engines. */
#define PALETTE_VRAM_BASE       0x05000000u
#define PALETTE_VRAM_SIZE       0x00080000u

/** Object attribute memory, one per engine. */
#define OAM_A_BASE              0x07000000u
#define OAM_B_BASE              0x07000400u
#define OAM_ENTRY_COUNT         128

/* ------------------------------------------------------------------------ */
/* Other regions                                                             */
/* ------------------------------------------------------------------------ */

/** ARM7-exclusive I/O and the Wi-Fi hardware, invisible to the ARM9. */
#define ARM7_WIFI_BASE          0x04800000u
#define ARM7_WIFI_SIZE          0x00008000u

/** GBA slot, accessible from the ARM7 for slot-2 peripherals. */
#define GBA_SLOT_BASE           0x08000000u
#define GBA_SLOT_SIZE           0x02000000u

/** Cartridge ROM, memory-mapped while a transfer is in flight. */
#define CARD_ROM_BASE           0x08000000u

/**
 * BIOS.  Not readable as data by design: the ARM9 BIOS at 0xFFFF0000 and the
 * ARM7 BIOS at 0x00000000/0x3800000 are execute-only mirrors, and the
 * decompression and SWI entry points live in them.
 */
#define ARM9_BIOS_BASE          0xFFFF0000u
#define ARM7_BIOS_BASE          0x00000000u

/* ------------------------------------------------------------------------ */
/* Helpers                                                                   */
/* ------------------------------------------------------------------------ */

/** Which main-RAM mirror an address belongs to, or 0 if neither. */
#define IN_ARM9_MAIN_RAM(addr) \
    ((u32)(addr) >= ARM9_MAIN_RAM_BASE && (u32)(addr) < ARM9_MAIN_RAM_END)

/** True for an address in the ARM9's instruction TCM. */
#define IN_ARM9_ITCM(addr) \
    ((u32)(addr) < ARM9_ITCM_SIZE)

/** True for an address in the ARM9's data TCM at its default base. */
#define IN_ARM9_DTCM(addr) \
    ((u32)(addr) >= ARM9_DTCM_DEFAULT_BASE && \
     (u32)(addr) <  ARM9_DTCM_DEFAULT_BASE + ARM9_DTCM_SIZE)

/** True for an address in VRAM, whichever bank it lands in. */
#define IN_VRAM(addr) \
    ((u32)(addr) >= VRAM_BASE && (u32)(addr) < VRAM_BASE + VRAM_SIZE)

#endif /* RUSHDECOMP_NITRO_MEMORYMAP_H */
