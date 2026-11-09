# semmeter-nem — net-energy-metering fix for the SEM Meter

Making a **SEM Meter W338 / Fusion Energy** power monitor report *signed* power,
so it can be used for net metering.

Stock, the meter reports power as an unsigned magnitude. A house with solar or
any other generation therefore cannot tell import from export: 2 kW flowing in
and 2 kW flowing out look identical. That makes the device useless for net
metering, which is the entire reason it was bought.

**Root cause: the metering firmware computes signed power correctly and then
calls `abs()` on it when packing the value for transmission.** The sign is not
missing from the hardware, and it is not lost to a protocol limitation. It is
thrown away by three instructions. This repo documents how that was found and
contains a two-byte patch that stops it.

---

## 1. Hardware

| Part | Role |
| --- | --- |
| **ESP32-S3-WROOM-1U** | Wi-Fi host; talks to Home Assistant |
| **AT32F421** (Artery, Cortex-M4 r0p1, 96 MHz, 64 KB flash, 16 KB SRAM) | metering MCU — the subject of this repo |
| **TI ADS131M04** | 4-channel, 24-bit simultaneous-sampling delta-sigma ADC |

The meter is an Emporia Vue 2 clone: **16 branch circuits + 3 mains phases = 19
channels**.

The ADS131M04 is the important one. It is a **two's-complement, signed** 24-bit
converter, so direction information is present in the raw samples. Confirmed
from the firmware itself: `adc_read_reg` at `0x08004E9C` builds its SPI command
as

```
mov.w r0, #0xA000
orr.w r0, r0, r4, lsl #7     ; command = 0xA000 | (reg << 7)
```

which is exactly the ADS131M04 `RREG` opcode (`101` in bits 15:13, register
address in bits 12:7). The firmware keeps a shadow copy of every register it
reads at `0x20000180`, one `u16` per register number.

### Connections used for this work

- **SWD** on `PA13`/`PA14` (still muxed to AF, so attaching a debugger does not
  disturb the application).
- **Host UART** is `USART2` on **`PA2` (TX) / `PA3` (RX)**, 115200 8N1,
  `BAUDR = 833` at a 96 MHz clock. `CTRL3 = 0x80` (`DMATEN`) — it transmits by
  DMA, which is why poking `USART2_DT` by hand over SWD does nothing.
- `USART1` is enabled in the peripheral but **its pins `PA9`/`PA10` are left as
  plain inputs**, so the firmware's considerable `printf` debug output goes
  nowhere on this hardware variant. Muxing those pins to AF1 does not recover it.

---

## 2. Getting the firmware out

The chip is **not read-protected**: option byte `0x1FFFF800 = 0xFFFF5AA5`, and the
FAP byte `0xA5` means access protection disabled. The full 64 KB read back
cleanly and two independent dumps were byte-identical.

| What | Address | Value |
| --- | --- | --- |
| CPUID | `0xE000ED00` | `0x410FC241` (Cortex-M4 r0p1) |
| Debug IDCODE | `0xE0042000` | `0x50020100` |
| Flash size | `0x1FFFF7E0` | `0x40` = 64 KB |
| UID (96-bit) | `0x1FFFF7E8` | `0b6173f5 5d614000 09403322` |
| Option bytes | `0x1FFFF800` | `0xFFFF5AA5` -> FAP `0xA5`, **unprotected** |

Image facts: 60.0 KB of 64 KB used (last non-`0xFF` byte at `0xF004`), Shannon
entropy 4.19 bits/byte (plain ARM code, not encrypted or compressed), coherent
vector table (SP `0x20003558`, reset `0x080000E1`, all handlers in flash with the
Thumb bit set), single image with no separate bootloader.

`firmware/at32f421_flash.orig.bin` — sha256 `a0efb555...`. **This is the recovery
image.** Keep it.

### Tooling notes (Windows)

- **pyOCD works; OpenOCD does not.** OpenOCD 0.12 connects and identifies the
  core correctly, but `mdw` silently produces no output and no error. pyOCD 0.46
  with a generic `-t cortex_m` target reads memory fine and needs no CMSIS pack.
- The ST-Link's debug interface needed **WinUSB bound with Zadig** (it was stuck
  on Code 28, "no driver"); its VCP worked all along because Windows has an inbox
  CDC driver, which makes the probe look functional when it is not.
- Set `PYTHONIOENCODING=utf-8` or pyOCD's table output throws a cp1252 error.
- Always connect with `connect_mode="attach"`. pyOCD's default halts the core,
  which stops the metering firmware and makes a live device look dead.
- The ST-Link reports `Target voltage: ~0.005` even when the target is powered
  and reads work perfectly — the `VDD_TARGET` pin is only a **sense** input and
  was simply unwired. Harmless; don't chase it.

---

## 3. The record builder, and where the sign dies

The firmware maintains **19 channel records at `0x20002428`, stride 28 bytes**
(from the literal pool: `0x080063A0` = `0x20002428`, `0x08006394` =
`0x2000263C` = base + 19*28). The initialisation loop at `0x080061F0` makes the
shape unambiguous:

```
80061f4:  rsb  r3, r1, r1, lsl #3     ; i*7
80061f8:  add  r0, r5, r3, lsl #2     ; base + i*28       -> stride 28
8006204:  strb r1, [r5, r3, lsl #2]   ; [+0] = record ID = i
8006218:  cmp  r1, #19                ; 19 records
```

### Record layout

| Offset | Type | Meaning |
| --- | --- | --- |
| `+0` | u8 | record ID (0-15 branch circuits, 16/17/18 = phase A/B/C) |
| `+1` | u8 | status flags — built with `orr #6` over a base of `0x01` |
| `+2` | u8 | copied from another struct; `0xFF` is mapped to `0` |
| `+4` | u16 | voltage (**unsigned** conversion) |
| `+6` | u16 | x10.0, unsigned |
| `+8` | u16 | x10.0, **signed then abs()** |
| `+12` | u32 | **signed then abs()** — power candidate |
| `+16` | u32 | **signed then abs()** — power candidate, feeds the accumulator |
| `+20` | u32 | energy accumulator (built via a `double` x100.0 path) |
| `+24` | u8 | frequency |

### The three `abs()` sites

```
        bl   0x8000700        ; float -> SIGNED int   (__aeabi_f2iz)
        cmp  r0, #0
        bge  .skip
        negs r0, r0           ; <-- the sign dies here
.skip:  str  r0, [r4, #off]
```

| # | `abs()` at | store | field |
| --- | --- | --- | --- |
| 1 | `0x0800631A` | `strh r0,[r4,#8]` | `+8` u16 |
| 2 | **`0x08006336`** | `str r0,[r4,#12]` | **`+12` u32** |
| 3 | **`0x0800634A`** | `str r0,[r4,#16]` | **`+16` u32** |

**Why these are certainly signed quantities:** fields that are legitimately
unsigned are converted with `bl 0x8000732` (float->**unsigned**), while all three
abs'd fields are converted with `bl 0x8000700` (float->**signed**). The firmware
distinguishes the two cases deliberately — it computes signed values and then
flattens them.

### Hypotheses that were ruled out

- **"Status bit 2 is a direction flag."** The observed wire status values `0x03`
  and `0x07` differ only in bit 2, and a third-party protocol doc calls both
  merely "active", so this looked promising. It is wrong: the firmware builds the
  byte with `ldrb / orr r1,r1,#6 / strb`, unconditionally, over a base of `0x01`.
  Nothing direction-dependent goes into it.
- **"The ADC is configured to deliver magnitudes."** No — the ADS131M04 is signed
  and the firmware reads it as signed.
- **"There is no `abs()` in the firmware."** This was an early wrong conclusion
  here, worth recording as a trap: searching for `vabs`/`vneg` or the float
  bitmask idiom (`bic #0x80000000`) finds nothing, because this firmware is
  **soft-float** and converts to **integer** first, then uses `cmp`/`bge`/`negs`.
  Grep for `negs`, not for `fabs`.

---

## 4. The patch

Replace `negs r0, r0` (`0x4240`) with **`tst r0, r0` (`0x4200`)**. `tst` only sets
flags and leaves `r0` untouched, so the signed value survives to the store.

`0x4200` was chosen deliberately over a `nop` (`0xBF00`): it is a **bit-subset**
of `0x4240` — one bit cleared, no bits set — so it can be written as a flash
bit-clear, without erasing the page, on a controller that only clears bits.

Applied at `0x08006336` and `0x0800634A`, the two 32-bit fields:
**2 bytes changed** -> `firmware/at32f421_flash.patched.bin`, sha256 `af0750d7...`.

`0x0800631A` is deliberately **left alone**: `+8` is only 16 bits wide, so a
negative value there would wrap and could garble a field the host reads as
unsigned.

### Consequence for the host

`+12`/`+16` become true two's-complement **`int32`** in a field the host
currently reads as `uint32`, so export arrives as a raw value above 2^31. The
host-side fix is one line — reinterpret as `int32_t`.

If a host firmware genuinely cannot be changed, an offset encoding is possible
instead: the `cmp`/`bge`/`negs` sequence is 6 bytes, which is enough room for
`add.w r0, r0, #0x40000000` (4 bytes) plus a `nop` (2 bytes), biasing the field
by 2^30 so it stays positive for any realistic power. Signed `int32` is preferred
where possible — no magic constant and no loss of range.

---

## 5. Host link protocol

The AT32F421 **streams autonomously** (no polling): 447-byte cycles, carried as
three transport chunks of 150/150/147 bytes, with records crossing chunk
boundaries. 19 records at a 22-byte cadence. Reverse engineered by
[quky/sem-meter-esphome](https://github.com/quky/sem-meter-esphome) — see its
`docs/protocol.md`:

- Record: byte 0 marker (`0xFF`, `0x3B` or `0x3C`), byte 1 record ID,
  byte 2 = `r[0]` status.
- `r[4..5]` voltage, big-endian `u16`, /10.30
- `r[12..15]` power, big-endian **`u32`** <- the field this patch makes signed
- `r[20]` frequency in Hz
- Status `0x01` inactive (publish 0 W), `0x03` active, `0x07` active, others ignored.
- Scaling: branch power /95.0, mains power /102.0.

Only 4 of the 22 payload bytes are documented there; **18 bytes per record remain
unknown**. The firmware's own debug format strings reference `angle`, `Am`,
`rawflg`, `flg`, `rp[m]`, `rp_adj[m]` and `rp_wh`, so the frame carries more than
that doc decodes — including a phase angle, which is itself a direction signal.

Note the unit this work was done on does **not** run that ESPHome firmware; it
publishes MQTT `SEMMETER/<MAC>/HA` carrying a 19x5 `sense` array.

---

## 6. Validation status

**The patch is derived from static analysis and has not been validated against
live measurements.** This is the honest state of things and the main caveat.

The metering pipeline is gated on **mains zero-cross detection** (`match_en
changed to %d`, `match_start falling edge detected`). With no mains voltage
connected it never runs at all: the 19-record array stays all zeros, and the
only SRAM words that change are the millisecond tick, a seconds counter and the
stack. This was independently confirmed from the other end — MQTT
`SEMMETER/<MAC>/HA` publishes a `sense` array of all zeros at the same time.

A current-carrying CT with no voltage reference is therefore not a usable test
setup, and sign *is* the V/I phase relationship, so there is nothing to observe
without voltage. The device itself is healthy throughout: core running in its
main loop, no faults (`CFSR`/`HFSR` clear), ADC answering with a valid ID, SPI1
enabled and error-free.

Once the unit can be safely energised, `tools/read_records.py -w` reads the
records live over SWD and will show the sign appear directly — a far better
instrument than tapping the UART.

---

## 7. Layout

```
firmware/at32f421_flash.orig.bin      verified stock dump (recovery image)
firmware/at32f421_flash.patched.bin   abs() neutralised at +12 and +16
firmware/SHA256SUMS
docs/strings.txt                      ASCII strings from the image
docs/xref.txt                         string -> referencing code map
tools/swd.py                          shared pyOCD connection helper
tools/read_records.py                 decode the 19 live channel records
tools/ram_watch.py                    find live fields by diffing SRAM
tools/adc_status.py                   ADS131M04 shadow registers + SPI1 state
tools/flash_patch.py                  write a patched image over SWD
tools/regen_disasm.sh                 regenerate full_disasm.txt
```
