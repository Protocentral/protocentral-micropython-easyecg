# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Ashwin Whitchurch, Protocentral Electronics
#
# easyecg_plotter.py - Arduino counterpart: 02.SerialPlotter
#
# The ECG for a plotter, such as Thonny's View -> Plotter. Runs on MicroPython and on a
# Raspberry Pi.
#
# Two traces, in uV at the electrodes:
#   Filtered   - 50 Hz notch + 40 Hz low-pass: what you normally want
#   Unfiltered - the plain ADC average, offset below so the two don't overlap
#
# Output is 250 Hz, as in the Arduino sketch, so the QRS keeps its shape. If
# missed_samples() grows on a slow board, change OUTPUT_HZ to 125.
#
# For research and education only - not a medical device. With electrodes on a person,
# power the host from a battery or connect through an easyISO isolated Qwiic adapter.

try:
    from machine import Pin, I2C
    # Defaults for the Adafruit QT Py ESP32-C3 (SCL=6, SDA=5). Change for your board.
    i2c = I2C(0, scl=Pin(6), sda=Pin(5), freq=400000)
except ImportError:                     # Raspberry Pi
    from protocentral_tla20xx_linux import I2C
    i2c = I2C(bus=1)

from protocentral_easyecg import EasyECG, EASYECG_DEFAULT_ADC_RATE

MAINS_HZ = 50.0                         # 50 or 60
OUTPUT_HZ = 250
UNFILTERED_OFFSET = 1500.0              # uV, vertical spacing between the traces

ecg = EasyECG(i2c)                      # ships at 0x49
if not ecg.begin():
    raise OSError("easyECG not found - check wiring and I2C address (0x49)")

ecg.set_sampling(EASYECG_DEFAULT_ADC_RATE, OUTPUT_HZ)
ecg.set_notch(MAINS_HZ)
ecg.calibrate_baseline(1000)            # one second, sitting still

while True:
    if ecg.update():
        print("Filtered:{:.0f},Unfiltered:{:.0f}".format(
            ecg.ecg_microvolts(), ecg.ecg_unfiltered_microvolts() - UNFILTERED_OFFSET))
