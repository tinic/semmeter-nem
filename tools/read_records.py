"""Decode the 19 live channel records at 0x20002428.

Field layout (stride 28), recovered from the record builder at 0x080062C0:

    +0   u8   record ID (0..18: 0-15 branch circuits, 16/17/18 = phase A/B/C)
    +1   u8   status flags (built with `orr #6` over a base of 0x01)
    +2   u8
    +4   u16  voltage      (unsigned)
    +6   u16
    +8   u16  abs() applied in stock firmware
    +12  i32  abs() applied in stock firmware  <- power candidate
    +16  i32  abs() applied in stock firmware  <- power candidate (feeds +20)
    +20  u32  energy accumulator
    +24  u8   frequency

On stock firmware +12/+16 are always non-negative.  After the abs() patch they
are true two's-complement int32, so a negative value here is export.
"""
import struct, sys, time
from swd import connect, RECORDS, RECORD_STRIDE, RECORD_COUNT

LOOP = "-w" in sys.argv

def show(blob):
    print(f"{'ID':>3} {'stat':>5} {'+2':>4} {'volt':>7} {'+6':>7} {'+8':>8} "
          f"{'+12 (i32)':>13} {'+16 (i32)':>13} {'energy':>11} {'Hz':>4}")
    for i in range(RECORD_COUNT):
        r = blob[i*RECORD_STRIDE:(i+1)*RECORD_STRIDE]
        rid, stat, b2 = r[0], r[1], r[2]
        volt, f6, f8 = struct.unpack_from('<3H', r, 4)[0], 0, 0
        volt, = struct.unpack_from('<H', r, 4)
        f6,   = struct.unpack_from('<H', r, 6)
        f8,   = struct.unpack_from('<h', r, 8)
        p12,  = struct.unpack_from('<i', r, 12)
        p16,  = struct.unpack_from('<i', r, 16)
        energy, = struct.unpack_from('<I', r, 20)
        hz = r[24]
        name = f"ch{rid+1}" if rid < 16 else f"ph{'ABC'[rid-16]}"
        print(f"{rid:>3} {stat:#05x} {b2:>4} {volt:>7} {f6:>7} {f8:>8} "
              f"{p12:>13} {p16:>13} {energy:>11} {hz:>4}   {name}")

s = connect(); t = s.target
try:
    while True:
        blob = bytes(t.read_memory_block8(RECORDS, RECORD_STRIDE*RECORD_COUNT))
        show(blob)
        if not LOOP:
            break
        print()
        time.sleep(1.0)
finally:
    s.close()
