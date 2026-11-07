# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Ashwin Whitchurch, Protocentral Electronics
#
# easyecg_simpletest.py - Arduino counterpart: 01.BasicReading
#
# The "hello world": prints the filtered ECG in ADC counts and in uV at the electrodes,
# 125 times a second. Runs on MicroPython; easyecg_raspberrypi.py is the Raspberry Pi
# version. Requires protocentral_tla20xx on the board (installed automatically as a mip
# dependency).
#
# Electrode placement (chest, Lead I):
#   LA (sleeve) - below the LEFT collarbone
#   RA (ring)   - below the RIGHT collarbone
#   RL (tip)    - lower right abdomen, or over the hip bone
#
# Press Ctrl-C to stop; it then prints how many samples the loop missed (should be 0).
#
# For research and education only - not a medical device. With electrodes on a person,
# power the host from a battery or connect through an easyISO isolated Qwiic adapter.

from machine import Pin, I2C
from protocentral_easyecg import EasyECG

# Defaults for the Adafruit QT Py ESP32-C3 (SCL=6, SDA=5). Change for your board.
i2c = I2C(0, scl=Pin(6), sda=Pin(5), freq=400000)

ecg = EasyECG(i2c)              # ships at 0x49
if not ecg.begin():
    raise OSError("easyECG not found - check wiring and I2C address (0x49)")

# ecg.set_notch(60.0)           # where mains is 60 Hz

print("Protocentral easyECG ready. Sit still...")
ecg.calibrate_baseline(1000)
print("ECG(counts)\tECG(uV)")

try:
    while True:
        # update() samples the ADC on a fixed clock (250 SPS) and returns True each time
        # a filtered output sample is ready (125 per second by default).
        if ecg.update():
            print("{}\t{:.1f}".format(ecg.ecg_filtered(), ecg.ecg_microvolts()))
except KeyboardInterrupt:
    print("missed samples:", ecg.missed_samples())
