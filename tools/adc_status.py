"""Dump the ADS131M04 register shadow and the SPI1 state.

A non-zero ID in shadow register 0 means the ADC is answering the MCU.
"""
from swd import connect, ADC_SHADOW

NAMES = {0: "ID", 1: "STATUS", 2: "MODE", 3: "CLOCK", 4: "GAIN1", 5: "GAIN2",
         6: "CFG", 9: "CH0_CFG", 14: "CH1_CFG", 19: "CH2_CFG", 24: "CH3_CFG"}
s = connect(); t = s.target
print("ADS131M04 register shadow (0x20000180, u16 per register):")
for reg in sorted(NAMES):
    v = t.read16(ADC_SHADOW + reg*2)
    print(f"  reg {reg:>2} {NAMES[reg]:<8} = 0x{v:04X}")
print("\nSPI1 @0x40013000:")
for n, o in [("CTRL1",0x00),("CTRL2",0x04),("STS",0x08)]:
    print(f"  {n:<6} 0x{t.read32(0x40013000+o):08X}")
c1 = t.read32(0x40013000)
print(f"  -> SPIEN={(c1>>6)&1} MSTEN={(c1>>2)&1}")
s.close()
