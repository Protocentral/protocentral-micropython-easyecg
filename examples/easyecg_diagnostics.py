# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Ashwin Whitchurch, Protocentral Electronics
#
# easyecg_diagnostics.py - Arduino counterpart: 05.Diagnostics
#
# Run this when the trace looks wrong and you want numbers instead of guesses. Runs on
# MicroPython and on a Raspberry Pi.
#
#   1. Is the ADC on the bus, and at which address?
#   2. Did the configuration take? (CONFIG read back, expect 0x04C3)
#   3. Is the conversion register changing at all?
#   4. Every 2 s: noise and mains figures for the live signal.
#
# Step 4 prints, per 2 s window, in uV at the electrodes:
#
#   filt_rms   RMS of the filtered output
#   raw_rms    RMS of the unfiltered output
#   mains      mains pickup the notch is removing
#   pp         peak-to-peak of the filtered output (about the R-wave height with a heart)
#   hr         heart rate, bpm
#   rate       output samples per second actually delivered (should read 250; Python only)
#   sat, lead  amplifier saturated, lead-off heuristic
#   missed     samples the loop couldn't take on time (should stay 0)
#
# Two runs separate board noise from body noise:
#
#   a) Short LA, RA and RL together at the jack (or clip all three leads to one
#      electrode). filt_rms is now the board's own noise floor - expect a few uV.
#   b) Electrodes on, sitting still. If filt_rms is far above (a) and mains is small, the
#      extra noise is muscle or electrode contact. Filtering won't remove it.
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

import math

from protocentral_easyecg import (
    EasyECG, EASYECG_I2C_ADDR_DEFAULT, EASYECG_I2C_ADDR_ALT, EASYECG_SATURATION_COUNTS,
    EASYECG_DEFAULT_ADC_RATE, ticks_us, ticks_diff, sleep_ms,
)

MAINS_HZ = 50.0                         # 50 or 60
EXPECTED_CONFIG = 0x04C3


def yes_no(flag):
    return "Y" if flag else "-"


print()
print("=== Protocentral easyECG diagnostics ===")

print("--- 1. I2C scan")
found = None
for addr in i2c.scan():
    label = ""
    if addr in (EASYECG_I2C_ADDR_DEFAULT, EASYECG_I2C_ADDR_ALT):
        label = "  <- easyECG"
        if found is None:
            found = addr
    print("   device at 0x{:02X}{}".format(addr, label))
if found is None:
    raise OSError("easyECG not found at 0x49/0x48 - check wiring and power")

ecg = EasyECG(i2c, found)
ecg.begin()
ecg.set_sampling(EASYECG_DEFAULT_ADC_RATE, 250)     # 250 Hz out, as in the Arduino sketch
ecg.set_notch(MAINS_HZ)

# Bit 15 reads 0 while a conversion is in progress, so it is masked off.
print("--- 2. CONFIG read-back")
conf = ecg.read_config()
print("   read 0x{:04X}, expected 0x{:04X} (+/-2.048 V, continuous, 3300 SPS)  {}".format(
    conf, EXPECTED_CONFIG, "OK" if (conf & 0x7FFF) == EXPECTED_CONFIG else "MISMATCH"))

print("--- 3. Conversion register activity")
lo = 32767
hi = -32768
total = 0
for _ in range(500):
    v = ecg.read_raw_uncorrected()
    lo = min(lo, v)
    hi = max(hi, v)
    total += v
    sleep_ms(1)
print("   500 samples: min {}  max {}  mean {}".format(lo, hi, int(total / 500)))
if lo == hi:
    print("   STUCK: the ADC returns a constant. Check power and the CONFIG word.")
elif hi >= EASYECG_SATURATION_COUNTS or lo <= -EASYECG_SATURATION_COUNTS:
    print("   Near a rail: electrode off, or mains-powered host without isolation.")
else:
    print("   OK - converting.")

ecg.calibrate_baseline(1000)
print("   baseline {} counts".format(ecg.get_baseline()))

print("--- 4. Live figures, uV at the electrodes, every 2 s")
print("   ADC {} SPS, output {} Hz".format(ecg.adc_rate_hz(), ecg.output_rate_hz()))
print("   filt_rms  raw_rms    mains       pp      hr    rate  sat lead missed")

window = 2 * ecg.output_rate_hz()
skip = 1                                # let the filters settle first
sum_f = sum_f2 = sum_r = sum_r2 = 0.0
min_f = 1e9
max_f = -1e9
n = 0
t0 = ticks_us()

while True:
    if not ecg.update():
        continue

    f = ecg.ecg_microvolts()
    r = ecg.ecg_unfiltered_microvolts()
    sum_f += f
    sum_f2 += f * f
    sum_r += r
    sum_r2 += r * r
    min_f = min(min_f, f)
    max_f = max(max_f, f)
    n += 1
    if n < window:
        continue

    t1 = ticks_us()
    if skip:
        skip -= 1
    else:
        rms_f = math.sqrt(max(0.0, sum_f2 / n - (sum_f / n) ** 2))
        rms_r = math.sqrt(max(0.0, sum_r2 / n - (sum_r / n) ** 2))
        rate = n * 1000000.0 / ticks_diff(t1, t0)
        print("   {:8.1f} {:8.1f} {:8.1f} {:8.1f} {:7.1f} {:7.1f}   {}    {}   {}".format(
            rms_f, rms_r, ecg.mains_microvolts(), max_f - min_f, ecg.heart_rate(), rate,
            yes_no(ecg.is_saturated()), yes_no(ecg.is_lead_off()), ecg.missed_samples()))

    sum_f = sum_f2 = sum_r = sum_r2 = 0.0
    min_f = 1e9
    max_f = -1e9
    n = 0
    t0 = ticks_us()
