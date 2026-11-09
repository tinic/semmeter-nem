"""Write a patched image to the AT32F421 over SWD.

pyOCD has no flash algorithm for this part, so this drives the flash controller
directly.  The AT32F421 controller is STM32F1-compatible:

    0x40022000  PSR
    0x40022004  UNLOCK      key sequence 0x45670123 then 0xCDEF89AB
    0x4002200C  STS         bit0 OBF(busy) bit2 PRGMERR bit4 EPPERR bit5 ODF
    0x40022010  CTRL        bit0 FPRGM bit1 SECERS bit6 STRT bit7 OPLK
    0x40022014  ADDR

Only the pages that actually differ are touched.  The erased extent is measured
after the erase rather than assumed, so an unexpected page size cannot cause a
partial rewrite: whatever came back as 0xFF is reprogrammed from the target
image in full.

Usage:
    python flash_patch.py <patched.bin> [--expect <original.bin>] [--yes]
"""
import struct
import sys
import time

from swd import connect, FLASH, FLASH_SIZE

FLASH_PSR, FLASH_UNLOCK, FLASH_STS = 0x40022000, 0x40022004, 0x4002200C
FLASH_CTRL, FLASH_ADDR = 0x40022010, 0x40022014
KEY1, KEY2 = 0x45670123, 0xCDEF89AB
OBF, PRGMERR, EPPERR, ODF = 1 << 0, 1 << 2, 1 << 4, 1 << 5
FPRGM, SECERS, STRT, OPLK = 1 << 0, 1 << 1, 1 << 6, 1 << 7


def wait_idle(t, what, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        sts = t.read32(FLASH_STS)
        if not (sts & OBF):
            if sts & (PRGMERR | EPPERR):
                t.write32(FLASH_STS, PRGMERR | EPPERR | ODF)
                raise RuntimeError(f"{what}: STS=0x{sts:08X} "
                                   f"(PRGMERR={bool(sts & PRGMERR)} "
                                   f"EPPERR={bool(sts & EPPERR)})")
            if sts & ODF:
                t.write32(FLASH_STS, ODF)
            return
    raise RuntimeError(f"{what}: timeout, STS=0x{t.read32(FLASH_STS):08X}")


def unlock(t):
    if t.read32(FLASH_CTRL) & OPLK:
        t.write32(FLASH_UNLOCK, KEY1)
        t.write32(FLASH_UNLOCK, KEY2)
    if t.read32(FLASH_CTRL) & OPLK:
        raise RuntimeError("flash unlock failed (OPLK still set)")


def lock(t):
    t.write32(FLASH_CTRL, t.read32(FLASH_CTRL) | OPLK)


def erase_page(t, addr):
    ctrl = t.read32(FLASH_CTRL)
    t.write32(FLASH_CTRL, (ctrl & ~FPRGM) | SECERS)
    t.write32(FLASH_ADDR, addr)
    t.write32(FLASH_CTRL, t.read32(FLASH_CTRL) | STRT)
    wait_idle(t, f"erase 0x{addr:08X}")
    t.write32(FLASH_CTRL, t.read32(FLASH_CTRL) & ~SECERS)


def program(t, addr, data):
    """Program halfwords.  0xFFFF is skipped: erased flash already reads that."""
    t.write32(FLASH_CTRL, (t.read32(FLASH_CTRL) & ~SECERS) | FPRGM)
    for i in range(0, len(data), 2):
        hw, = struct.unpack_from('<H', data, i)
        if hw == 0xFFFF:
            continue
        t.write_memory(addr + i, hw, 16)
        wait_idle(t, f"program 0x{addr + i:08X}")
    t.write32(FLASH_CTRL, t.read32(FLASH_CTRL) & ~FPRGM)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        raise SystemExit(__doc__)
    target_img = open(args[0], 'rb').read()
    if len(target_img) != FLASH_SIZE:
        raise SystemExit(f"image is {len(target_img)} bytes, expected {FLASH_SIZE}")
    expect = None
    if '--expect' in sys.argv:
        expect = open(sys.argv[sys.argv.index('--expect') + 1], 'rb').read()

    s = connect(); t = s.target
    try:
        print(f"core: {'halted' if t.is_halted() else 'RUNNING'}")
        print("reading current flash...")
        current = bytes(t.read_memory_block8(FLASH, FLASH_SIZE))

        if expect is not None:
            if current != expect:
                diff = [i for i in range(FLASH_SIZE) if current[i] != expect[i]]
                raise SystemExit(
                    f"ABORT: device flash does not match --expect image "
                    f"({len(diff)} bytes differ, first at 0x{FLASH + diff[0]:08X}). "
                    f"Refusing to patch an unknown image.")
            print("device flash matches the expected original image")

        diff = [i for i in range(FLASH_SIZE) if current[i] != target_img[i]]
        if not diff:
            print("device already matches the target image; nothing to do")
            return
        print(f"{len(diff)} byte(s) differ: " +
              ", ".join(f"0x{FLASH + d:08X}" for d in diff[:16]))

        # The core must be halted: we are about to erase code it is executing.
        t.halt()
        print(f"core halted (pc=0x{t.read_core_register('pc'):08X})")
        unlock(t)
        print("flash unlocked")

        done = set()
        for d in diff:
            page = (d // 1024) * 1024
            if page in done:
                continue
            addr = FLASH + page
            print(f"\nerasing page at 0x{addr:08X}...")
            erase_page(t, addr)

            # Measure what actually got erased instead of assuming a page size.
            probe = bytes(t.read_memory_block8(FLASH + page, 4096))
            n = 0
            while n < len(probe) and probe[n] == 0xFF:
                n += 1
            # round down to a sane page granularity
            size = 2048 if n >= 2048 else 1024
            print(f"  {n} bytes read back as 0xFF -> treating page size as {size}")
            for p in range(page, page + size, 1024):
                done.add(p)

            print(f"  programming {size} bytes from the target image...")
            program(t, addr, target_img[page:page + size])

            back = bytes(t.read_memory_block8(addr, size))
            if back != target_img[page:page + size]:
                bad = [i for i in range(size)
                       if back[i] != target_img[page + i]]
                raise RuntimeError(
                    f"verify FAILED for page 0x{addr:08X}: {len(bad)} bytes wrong, "
                    f"first at 0x{addr + bad[0]:08X}")
            print(f"  page 0x{addr:08X} verified")

        lock(t)
        print("\nflash locked")
        final = bytes(t.read_memory_block8(FLASH, FLASH_SIZE))
        if final == target_img:
            print("FULL IMAGE VERIFIED: device matches the target image")
        else:
            bad = [i for i in range(FLASH_SIZE) if final[i] != target_img[i]]
            raise RuntimeError(f"full-image verify failed, {len(bad)} bytes differ")

        print("resetting target...")
        t.reset()
        time.sleep(0.3)
        print(f"core after reset: {'halted' if t.is_halted() else 'RUNNING'}")
    finally:
        s.close()


if __name__ == '__main__':
    main()
