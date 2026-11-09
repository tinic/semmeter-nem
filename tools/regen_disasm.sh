#!/bin/sh
# Regenerate the full disassembly of the dumped image.
OD=${OBJDUMP:-arm-none-eabi-objdump}
"$OD" -D -b binary -m arm -M force-thumb --adjust-vma=0x08000000 \
  --start-address=0x08000000 --stop-address=0x0800f010 \
  "$(dirname "$0")/../firmware/at32f421_flash.orig.bin" > full_disasm.txt
echo "wrote full_disasm.txt"
