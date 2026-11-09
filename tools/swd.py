"""Shared SWD helper for the SEM Meter AT32F421.

pyOCD has no CMSIS pack for the AT32F421, but a generic ``cortex_m`` target
reads and writes memory fine, which is all we need.  Always connect in
``attach`` mode: pyOCD's default halts the core, which stops the metering
firmware and makes a live device look dead.
"""
import logging

RECORDS      = 0x20002428   # 19 channel records, stride 28
RECORD_STRIDE = 28
RECORD_COUNT  = 19
ADC_SHADOW   = 0x20000180   # ADS131M04 register shadow, u16 per register
SRAM         = 0x20000000
SRAM_SIZE    = 0x4000
FLASH        = 0x08000000
FLASH_SIZE   = 0x10000


def connect(quiet=True, attach=True, frequency=1_800_000):
    """Return an open pyOCD session.  Caller is responsible for ``close()``."""
    if quiet:
        logging.disable(logging.WARNING)
    from pyocd.core.helpers import ConnectHelper
    session = ConnectHelper.session_with_chosen_probe(
        target_override="cortex_m",
        connect_mode="attach" if attach else "halt",
        frequency=frequency, blocking=False, return_first=True,
        options={"warning.cortex_m_default": False})
    if session is None:
        raise SystemExit("no debug probe found (is WinUSB bound to the ST-Link?)")
    session.open()
    return session
