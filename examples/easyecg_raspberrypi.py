# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Ashwin Whitchurch, Protocentral Electronics
#
# easyecg_raspberrypi.py - Arduino counterpart: 01.BasicReading
#
# The basic reading on a Raspberry Pi, plus heart rate and missed samples. Reuses the
# TLA20xx smbus2 shim.
#
# Setup:  sudo raspi-config (enable I2C); pip install protocentral-easyecg
#         i2cdetect -y 1   ->  should show 0x49
#
# A mains-powered Pi gives a flat line.
# For research and education only - not a medical device. With electrodes on a person,
# power the host from a battery or connect through an easyISO isolated Qwiic adapter.

from protocentral_tla20xx_linux import I2C   # shim ships with protocentral-tla20xx
from protocentral_easyecg import EasyECG

with I2C(bus=1) as i2c:
    ecg = EasyECG(i2c)
    if not ecg.begin():
        raise OSError("easyECG not found - check wiring and I2C address (0x49)")
    # ecg.set_notch(60.0)                   # where mains is 60 Hz
    print("Protocentral easyECG ready. Sit still...")
    ecg.calibrate_baseline(1000)
    print("counts        uV     bpm  missed")
    while True:
        if ecg.update():
            print("{:6d}  {:8.1f}   {:5.1f}  {}".format(
                ecg.ecg_filtered(), ecg.ecg_microvolts(), ecg.heart_rate(),
                ecg.missed_samples()))
