/**
 * @file types.h
 * @brief Fixed-width types and the calling conventions a DS build needs.
 *
 * Hand-written for this project.  The Nitro SDK ships its own equivalents;
 * these are independent, and deliberately minimal, so that `src/` compiles with
 * a stock `arm-none-eabi-gcc` and nothing else.
 *
 * The typedef names match the SDK's on purpose.  A reconstructed function that
 * reads `u32 OSLock_Lock(OSInterruptMask mask)` is far more useful to a reader
 * than one reading `uint32_t f(uint32_t x)`, and the point of a decompilation is
 * to be read.
 */

#ifndef RUSHDECOMP_NITRO_TYPES_H
#define RUSHDECOMP_NITRO_TYPES_H

#include <stddef.h>
#include <stdint.h>

/* ------------------------------------------------------------------------ */
/* Fixed-width types, SDK spelling                                           */
/* ------------------------------------------------------------------------ */
typedef uint8_t  u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;

typedef int8_t   s8;
typedef int16_t  s16;
typedef int32_t  s32;
typedef int64_t  s64;

typedef volatile u8  vu8;
typedef volatile u16 vu16;
typedef volatile u32 vu32;
typedef volatile u64 vu64;

typedef volatile s8  vs8;
typedef volatile s16 vs16;
typedef volatile s32 vs32;
typedef volatile s64 vs64;

typedef float  f32;
typedef double f64;

typedef volatile f32 vf32;
typedef volatile f64 vf64;

#ifndef BOOL
typedef int BOOL;
#endif

#ifndef TRUE
#define TRUE  1
#endif
#ifndef FALSE
#define FALSE 0
#endif

#ifndef NULL
#define NULL ((void *)0)
#endif

/* ------------------------------------------------------------------------ */
/* Word helpers                                                              */
/* ------------------------------------------------------------------------ */

/** Read a little-endian u16 from a byte pointer, unaligned-safe. */
static inline u16 RD16(const void *p)
{
    const u8 *b = (const u8 *)p;
    return (u16)(b[0] | (b[1] << 8));
}

/** Read a little-endian u32 from a byte pointer, unaligned-safe. */
static inline u32 RD32(const void *p)
{
    const u8 *b = (const u8 *)p;
    return (u32)b[0] | ((u32)b[1] << 8) | ((u32)b[2] << 16) | ((u32)b[3] << 24);
}

/** Write a little-endian u16 through a byte pointer, unaligned-safe. */
static inline void WR16(void *p, u16 v)
{
    u8 *b = (u8 *)p;
    b[0] = (u8)(v & 0xFF);
    b[1] = (u8)((v >> 8) & 0xFF);
}

/** Write a little-endian u32 through a byte pointer, unaligned-safe. */
static inline void WR32(void *p, u32 v)
{
    u8 *b = (u8 *)p;
    b[0] = (u8)(v & 0xFF);
    b[1] = (u8)((v >> 8) & 0xFF);
    b[2] = (u8)((v >> 16) & 0xFF);
    b[3] = (u8)((v >> 24) & 0xFF);
}

/* ------------------------------------------------------------------------ */
/* Placement and calling conventions                                         */
/* ------------------------------------------------------------------------ */

/**
 * Put a function or variable in a named section.
 *
 * DS binaries are laid out by section: the ARM9 has its code, its initialised
 * data, its BSS, and -- importantly for a decompilation -- the static
 * initialiser ranges named by each overlay descriptor.  Section placement is
 * how a reconstruction ends up in the right one.
 */
#define SDK_SECTION(name) __attribute__((section(name)))

/** Force into the ARM9's ITCM, which is fast but only 32 KiB. */
#define SDK_ITCM SDK_SECTION(".itcm")

/** Force into an overlay's static-initialiser range. */
#define SDK_STATIC_INIT SDK_SECTION(".static_init")

/**
 * Prevent the compiler from reordering or eliding a call.
 *
 * Necessary for hardware register access and for anything whose observable
 * behaviour is its side effect rather than its return value.  Overuse it and
 * generated code diverges from the original for reasons that have nothing to do
 * with the source.
 */
#define SDK_NOINLINE  __attribute__((noinline))
#define SDK_ALWAYS_INLINE inline __attribute__((always_inline))

/**
 * Tell GCC the function never returns.
 *
 * Getting this wrong on something like a panic handler changes every caller's
 * epilogue, which is a very confusing way to fail a match.
 */
#define SDK_NORETURN __attribute__((noreturn))

/** Mark a struct as tightly packed, for on-media or IPC layouts. */
#define SDK_PACKED __attribute__((packed))

/**
 * Assert an alignment the hardware requires.
 *
 * DMA, the texture cache and the VRAM banks all reject or silently corrupt
 * misaligned accesses.  A compile-time check beats a mystery on real hardware.
 */
#define SDK_ASSERT_ALIGN(type, n) \
    _Static_assert(_Alignof(type) >= (n) || sizeof(type) % (n) == 0, \
                   #type " must be " #n "-byte aligned")

/* ------------------------------------------------------------------------ */
/* Array sizing                                                              */
/* ------------------------------------------------------------------------ */

/** Element count of a statically-declared array. */
#define SDK_ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))

#endif /* RUSHDECOMP_NITRO_TYPES_H */
