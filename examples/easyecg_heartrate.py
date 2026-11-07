# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Ashwin Whitchurch, Protocentral Electronics
#
# easyecg_heartrate.py - Arduino counterpart: 03.HeartRate
#
# Prints the heart rate on every detected beat, blinks an LED if the board has one, and
# says so plainly when the signal can't be trusted. Runs on MicroPython and on a Raspberry
# Pi (no LED there).
#
# The detector is a simplified Pan-Tompkins: it needs about 2 s to learn the signal level,
# then averages the last 8 R-R intervals. Expect a number after 3-5 beats.
#
# Status messages, once a second:
#   SATURATED - the amplifier is pinned at a rail. Usually a mains-powered host (switch
#               to battery, or use an easyISO), or an electrode that has come off.
#   LEAD OFF? - mains pickup is far above normal, which is what a loose or missing
#               electrode looks like. Provisional heuristic - see the README.
#
# For research and education only - not a medical device. With electrodes on a person,
# power the host from a battery or connect through an easyISO isolated Qwiic adapter.

try:
    from machine import Pin, I2C
    # Defaults for the Adafruit QT Py ESP32-C3 (SCL=6, SDA=5). Change for your board.
    i2c = I2C(0, scl=Pin(6), sda=Pin(5), freq=400000)
    try:
        led = Pin("LED", Pin.OUT)       # Pico / Pico W; boards without a named LED skip it
    except Exception:                   # noqa: BLE001 - ports raise different errors
        led = None
except ImportError:                     # Raspberry Pi
    from protocentral_tla20xx_linux import I2C
    i2c = I2C(bus=1)
    led = None

from protocentral_easyecg import EasyECG, ticks_us, ticks_diff

MAINS_HZ = 50.0                         # 50 or 60

ecg = EasyECG(i2c)                      # ships at 0x49
if not ecg.begin():
    raise OSError("easyECG not found - check wiring and I2C address (0x49)")

ecg.set_notch(MAINS_HZ)
ecg.calibrate_baseline(1000)
print("Sit still - learning the signal for 2 s...")

led_on_at = None
last_status = ticks_us()

while True:
    now = ticks_us()

    if led_on_at is not None and ticks_diff(now, led_on_at) >= 60000:
        led.value(0)
        led_on_at = None

    if not ecg.update():
        continue

    if ecg.beat_detected():
        if led is not None:
            led.value(1)
            led_on_at = now
        hr = ecg.heart_rate()
        print("Beat  RR {} ms  HR {}".format(
            ecg.last_rr_millis(), "{:.0f} bpm".format(hr) if hr > 0.0 else "--"))

    # Signal-quality status once a second.
    if ticks_diff(now, last_status) >= 1000000:
        last_status = now
        if ecg.is_saturated():
            print("SATURATED - battery host or isolated adapter? electrode off?")
        elif ecg.is_lead_off():
            print("LEAD OFF? mains pickup {:.0f} uV RMS".format(ecg.mains_microvolts()))
