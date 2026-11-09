"""Sample all of SRAM repeatedly and report which words change.

Use this to find live measurement fields.  With no mains connected only the
tick counters and the stack move, which is how you can tell the metering
pipeline never ran (it is gated on mains zero-cross detection).
"""
import struct, sys, time
from swd import connect, SRAM, SRAM_SIZE

N = int(sys.argv[1]) if len(sys.argv) > 1 else 8
STACK = 0x3400   # anything at/above this in SRAM is stack noise

s = connect(); t = s.target
print(f"core: {'halted' if t.is_halted() else 'RUNNING'}")
snaps = []
for _ in range(N):
    snaps.append(bytes(t.read_memory_block8(SRAM, SRAM_SIZE)))
    time.sleep(0.5)
s.close()

changed = []
for off in range(0, SRAM_SIZE, 4):
    vals = [struct.unpack_from('<I', sn, off)[0] for sn in snaps]
    if len(set(vals)) > 1:
        changed.append((off, vals))
below = [c for c in changed if c[0] < STACK]
print(f"{len(changed)} words changed ({len(below)} below the stack)\n")
for off, vals in below:
    f = struct.unpack('<f', struct.pack('<I', vals[-1]))[0]
    fs = f"{f:13.4f}" if (f == 0 or 1e-6 < abs(f) < 1e9) else " " * 13
    print(f"0x{SRAM+off:08X} {vals[-1]:11d} {fs}  " +
          " ".join(f"{v:08x}" for v in vals[:5]))
