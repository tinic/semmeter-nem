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
str  r0, [r4, #16]
```

`+16` is the real power in centiwatts. It also feeds the energy accumulator,
a float in Wh, and the frame builder sends the rounded total:

```
acc(+20) += (double)(uint32)P16 / 100 * dt_ms / 1000 / 3600
frame    =  (uint32)roundf(acc)
```

Both conversions are unsigned (`__aeabi_ui2d` at `0x080006E6`,
`__aeabi_f2uiz` at `0x08000732`). That is harmless while `abs()` runs
first. With `abs()` removed alone, -189 W becomes 4294967107 and adds
29826 Wh per 2.5 s cycle.

## The patch

| Address      | Stock                    | Patched                  |
| ------------ | ------------------------ | ------------------------ |
| `0x0800634A` | `4240` negs r0, r0       | `4200` tst r0, r0        |
| `0x0800634E` | `bl 0x080006E6` ui2d     | `bl 0x080006C4` i2d      |
| `0x080044D8` | `bl 0x08000732` f2uiz    | `bl 0x08000700` f2iz     |

Power and energy are both signed. The energy field is the net energy of
the channel in Wh, as an `int32`: it rises on import and falls on export.
Import and export per interval are the positive and negative parts of its
change.

`+12` is Vrms × Irms and is never negative, so its `abs()` at `0x08006336`
is left as is. There is a third `abs()` at `0x0800631A` on a 16-bit field.
It is left alone on purpose: a negative value would wrap in 16 bits and
corrupt something the host reads as unsigned.

Files:

```
firmware/at32f421_flash.orig.bin      stock dump, keep this for recovery
firmware/at32f421_flash.patched.bin   stock + the changes above
firmware/SHA256SUMS
```

## Does it work

Signed power was verified on a live panel on 2026-11-08 with the first
change alone. The MCU boots, the ADC answers, MQTT keeps publishing, and the
ESP32 side already decodes the power field as `int32`.

With one mains CT deliberately clipped on backwards:

| channel | A     | W      |
| ------- | ----- | ------ |
| phase A | 2.27  | +209   |
| phase B | 16.57 | −1951  |

Flip the CT the right way round and phase B reads +1845 W. On stock firmware
both would have read +1951 W and you would never know. Idle channels now
wander a few tenths of a watt either side of zero, which is normal.

With the first change alone, Solar 1, Solar 2 and circuit 6 at -189, -120
and -191 W each gained exactly 29824 Wh in the MQTT energy field per update.

The signed energy path was verified by emulating the patched code in
Unicorn, not yet on a live panel. Over 2.5 s cycles at 1000 W, then -189 W,
then -3000 W, the frame value goes 1, 1, 2, 3, then 3, 3, 2, 2, then 0, -2,
-4, -6. Whether the ESP32 publishes a negative energy value correctly is
not yet known.

The accumulator is a 32-bit float. At 54 kWh it loses loads under about
3 W; at 429 kWh, under about 22 W. This is stock behaviour.

The records are not saved to flash (`my_para_save` writes `0x20000200`), so
the AT32 energy counters restart from zero at reset.

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
python tools/flash_patch.py firmware/at32f421_flash.patched.bin \
    --expect firmware/at32f421_flash.orig.bin
```
 
`flash_patch.py` checks the chip matches the stock dump byte for byte before
writing, erases and reprograms each changed page, then verifies the whole
image after. If the check fails, stop; your
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
| +4     | u16  | Vrms ×10                                  |
| +6     | u16  | Irms ×10                                  |
| +8     | u16  | "Ad" ×10 in the debug output, abs()'d     |
| +12    | i32  | Vrms × Irms ×10, never negative           |
| +16    | i32  | real power in cW, signed, feeds +20       |
| +20    | f32  | net energy in Wh                          |
| +24    | u8   | frequency                                 |
 
The frame builder at `0x08004422` sends each record big-endian as `+0`,
`+1`, `+2`, `+4`, `+6`, `+8`, `+12`, `+16`, `(uint32)roundf(+20)`, `+24`
(22 bytes). The link is two-way: `wifi_uart_RX_handler` accepts commands
from the ESP32, and command 2 zeroes every `+20` (`0x08004F24`). The UART framing to the ESP32 is documented in
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
