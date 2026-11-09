# semmeter-nem
 
Firmware patch for the SEM Meter W338 / Fusion Energy home energy monitor
so it reports signed power and can be used for net metering.
 
## The problem
 
The meter is sold as supporting "Solar/Net Metering". It doesn't. Power is
reported as a magnitude with no sign, so 2 kW of import and 2 kW of export
look exactly the same. If you have solar, the numbers are meaningless.
 
The hardware is fine. The ADC (TI ADS131M04) is a signed 24-bit converter and
the metering MCU (Artery AT32F421) computes signed power from it correctly.
Then, right before writing the value into the record the Wi-Fi chip reads, the
firmware does this:
 
```
bl   __aeabi_f2iz      ; float -> signed int
cmp  r0, #0
bge  skip
negs r0, r0            ; abs()
skip:
str  r0, [r4, #12]
```
 
That `negs` is the whole story. It happens at two places (the two 32-bit power
fields in each channel record). Replace it with `tst r0, r0`, which sets the
flags but leaves `r0` alone, and the sign survives. Two bytes changed.
 
## The patch
 
| Address      | Stock          | Patched         |
| ------------ | -------------- | --------------- |
| `0x08006336` | `4240` (negs)  | `4200` (tst)    |
| `0x0800634A` | `4240` (negs)  | `4200` (tst)    |
 
`0x4200` was chosen over a `nop` because it only clears a bit relative to
`0x4240`, so it can be programmed without erasing the page.
 
There is a third `abs()` at `0x0800631A` on a 16-bit field. It is left alone
on purpose: a negative value would wrap in 16 bits and corrupt something the
host reads as unsigned.
 
Files:
 
```
firmware/at32f421_flash.orig.bin      stock dump, keep this for recovery
firmware/at32f421_flash.patched.bin   stock + the two bytes above
firmware/SHA256SUMS
```
 
## Does it work
 
Yes. Flashed 2026-11-08, reinstalled in the panel. The MCU boots, the ADC
answers, MQTT keeps publishing, and the ESP32 side already decodes the field
as `int32`, so nothing needed changing there.
 
With one mains CT deliberately clipped on backwards:
 
| channel | A     | W      |
| ------- | ----- | ------ |
| phase A | 2.27  | +209   |
| phase B | 16.57 | −1951  |
 
Flip the CT the right way round and phase B reads +1845 W. On stock firmware
both would have read +1951 W and you would never know. Idle channels now
wander a few tenths of a watt either side of zero, which is normal.
 
## The energy counters

The two-byte patch breaks the energy counters. Any channel that reads below
zero, including an idle channel drifting a few tenths of a watt negative,
gains about 2.98e8 accumulator units every 2.5 s.

The accumulator at `0x0800634E` converts the `+16` power with
`__aeabi_ui2d` (`0x080006E6`), not `__aeabi_i2d` (`0x080006C4`). A power of
-189 becomes 4294967107 before it is multiplied by time:

```
acc(+20, float) += (double)(uint32)P16 * 100.0 * dt_ms / 1000 / 3600
```

The frame builder at `0x08004422` sends `(uint32)roundf(acc)`, through
`0x08005124` and `0x08000732`. Observed on a live panel: Solar 1, Solar 2 and
circuit 6 at -189, -120 and -191 each gained exactly 29824 in the MQTT
energy field per update. The formula above gives 29825 at dt = 2.5 s.

`at32f421_flash.energyfix.bin` keeps the signed `+16` but gives the
accumulator `max(P, 0)`. The energy field then counts import only:

| Address      | patched.bin            | energyfix.bin          |
| ------------ | ---------------------- | ---------------------- |
| `0x08006346` | `2800` cmp r0, #0      | `6120` str r0, [r4,#16] |
| `0x08006348` | `da00` bge 0x0800634C  | `2800` cmp r0, #0      |
| `0x0800634A` | `4200` tst r0, r0      | `da00` bge 0x0800634E  |
| `0x0800634C` | `6120` str r0, [r4,#16] | `2000` movs r0, #0     |

Some bits go from 0 to 1, so the page needs an erase. `flash_patch.py`
erases and verifies:

```
python tools/flash_patch.py firmware/at32f421_flash.energyfix.bin \
    --expect firmware/at32f421_flash.patched.bin
```

The records are not saved to flash (`my_para_save` writes `0x20000200`), so
the AT32 counters restart from zero at reset. The ESP32 side was not
analysed.

## Flashing it
 
You need an SWD probe (ST-Link, CMSIS-DAP, Pico probe) and pyOCD. OpenOCD
connects but can't read memory on this part, don't bother.
 
### The headers
 
The board has two SWD headers, one for the ESP32 and one for the AT32F421.
You want the AT32 one. Its pins are SWDIO, SWCLK, GND and RST (NRST).
 
Flash on the bench, not in the panel. Disconnect the unit from mains and
power it from the pad labeled 5V next to the 3.3 V regulator.
 
The chip is not read-protected. Connect in attach mode; halting the core
stops the metering loop and the device looks dead until reset.
 
```
pip install pyocd
python tools/flash_patch.py
```
 
`flash_patch.py` checks the chip matches the stock dump byte for byte before
writing, then verifies the whole image after. If the check fails, stop; your
unit has different firmware and the offsets above don't apply.
 
Other tools:
 
```
tools/read_records.py      show the 19 live channel records over SWD
tools/ram_watch.py         diff SRAM to find live fields
tools/adc_status.py        ADS131M04 register shadow and SPI state
tools/swd.py               shared pyOCD connection helper
tools/regen_disasm.sh      regenerate the full disassembly
```
 
You won't see live readings on the bench: the metering pipeline waits for
mains zero-crossings, so every record stays zero until the unit is back in
the panel. `flash_patch.py`'s verify step is the bench check.
 
## Record format
 
The AT32F421 keeps 19 channel records (16 branch circuits, 3 mains phases) at
`0x20002428`, 28 bytes each:
 
| offset | type | meaning                                   |
| ------ | ---- | ----------------------------------------- |
| +0     | u8   | channel id, 0–15 branch, 16/17/18 phase A/B/C |
| +1     | u8   | status flags                              |
| +4     | u16  | voltage                                   |
| +6     | u16  | ×10, unsigned                             |
| +8     | u16  | ×10, abs()'d (not patched)                |
| +12    | i32  | power, patched to signed                  |
| +16    | i32  | power, patched to signed, feeds the accumulator |
| +20    | u32  | energy accumulator                        |
| +24    | u8   | frequency                                 |
 
The UART framing to the ESP32 is documented in
[quky/sem-meter-esphome](https://github.com/quky/sem-meter-esphome). Only
four of the 22 payload bytes are decoded there. The firmware's debug strings
mention `angle`, `rp[m]`, `rp_adj[m]`, `rp_wh` and others, so the frame
carries more than that, including a phase angle.
 
## Hardware notes
 
| part            | role                                              |
| --------------- | ------------------------------------------------- |
| ESP32-S3-WROOM-1U | Wi-Fi, MQTT to Home Assistant                   |
| AT32F421        | metering MCU, Cortex-M4, 96 MHz, 64 KB flash      |
| ADS131M04       | 4-channel 24-bit simultaneous-sampling ADC        |
 
Host UART is USART2 on PA2/PA3, 115200 8N1, DMA driven. USART1 is configured
for printf output but its pins are left as plain inputs on this board, so the
debug output goes nowhere.
 
If you're hunting for the abs() yourself: the firmware is soft-float and
converts to integer before taking the absolute value, so searching for
`fabs`, `vabs` or the `bic #0x80000000` idiom finds nothing. Grep for `negs`.
 
## Credits
 
UART protocol from [quky/sem-meter-esphome](https://github.com/quky/sem-meter-esphome).
HA MQTT setup from [eyal0/sem-meter-home-assistant](https://github.com/eyal0/sem-meter-home-assistant).
