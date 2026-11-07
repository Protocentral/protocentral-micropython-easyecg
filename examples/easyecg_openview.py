# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Ashwin Whitchurch, Protocentral Electronics
#
# easyecg_openview.py - Arduino counterpart: 04.OpenView
#
# Streams the ECG into the Protocentral OpenView desktop app over USB serial. MicroPython
# only (it needs sys.stdout.buffer).
#
#   In OpenView 3.4 or later:  Connect -> USB -> select "easyECG Breakout"
#
# On earlier versions the "ADS1292R Breakout" profile also reads this stream, with the
# labels wrong: its ECG is the filtered trace, its Respiration the unfiltered trace, its
# HR the heart rate and its RR the signal status.
#
# Same packets as the Arduino sketch, 125 per second, written as raw bytes to the USB
# serial port (the baud rate setting doesn't matter over USB CDC):
#
#   0x0A 0xFA 0x08 0x00 0x02 | 8-byte payload | 0x00 0x0B
#   payload, int16 little-endian:
#     ECG (filtered)   <- notch + 40 Hz low-pass, counts
#     ECG (unfiltered) <- plain average of the ADC samples (0 if SECOND_CHANNEL_UNFILTERED
#                         is False). Hidden by default in OpenView.
#     Heart rate       <- bpm, rounded
#     Signal           <- 0 = OK, 1 = lead-off / mains pickup, 2 = amplifier saturated
#
# The driver samples at 250 SPS (Arduino: 1000) and decimates to 125 Hz, so OpenView sees
# the same rate and scale. No digital high-pass by default: the board's 0.5 Hz analog
# corner is already there, and a second pole distorts the ST segment.
#
# HOW TO RUN: save this file to the board as main.py and reset it, then close Thonny or
# mpremote so OpenView can open the port. The REPL shares that port, so `mpremote run`
# can't be used here: it would read the packets itself. Don't print() anything else -
# OpenView expects binary packets only. To stop it, connect Thonny and press Ctrl-C.
#
# Needs a port whose sys.stdout has a .buffer (ESP32, RP2040/RP2350 and most others).
#
# Download OpenView: https://github.com/Protocentral/protocentral_openview
#
# On a mains-powered host the driven-right-leg amplifier saturates and there is no trace.
# For research and education only - not a medical device. With electrodes on a person,
# power the host from a battery or connect through an easyISO isolated Qwiic adapter.

import struct
import sys

from machine import Pin, I2C
from protocentral_easyecg import EasyECG, EASYECG_DEFAULT_ADC_RATE, sleep_ms

# ---- user settings ------------------------------------------------------------------
MAINS_HZ = 50.0                         # 50 or 60
LOW_PASS_HZ = 40.0                      # 0 disables the digital low-pass
HIGH_PASS_HZ = 0.0                      # 0 = off (keeps ST shape); 0.5 for wander
SECOND_CHANNEL_UNFILTERED = True

# Defaults for the Adafruit QT Py ESP32-C3 (SCL=6, SDA=5). Change for your board.
i2c = I2C(0, scl=Pin(6), sda=Pin(5), freq=400000)

# ---- Protocentral OpenView packet format -------------------------------------------
CES_CMDIF_PKT_START_1 = 0x0A
CES_CMDIF_PKT_START_2 = 0xFA
CES_CMDIF_TYPE_DATA = 0x02
CES_CMDIF_PKT_STOP = 0x0B
DATA_LEN = 0x08

# One preallocated packet: header (5) + payload (8) + footer (2). Only the payload changes.
packet = bytearray(5 + DATA_LEN + 2)
packet[0] = CES_CMDIF_PKT_START_1
packet[1] = CES_CMDIF_PKT_START_2
packet[2] = DATA_LEN
packet[3] = 0
packet[4] = CES_CMDIF_TYPE_DATA
packet[13] = 0
packet[14] = CES_CMDIF_PKT_STOP

out = getattr(sys.stdout, "buffer", sys.stdout)


def send_data_packet(ecg_ch, second_ch, hr, status):
    struct.pack_into("<hhhh", packet, 5, ecg_ch, second_ch, hr, status)
    out.write(packet)


ecg = EasyECG(i2c)                      # ships at 0x49
if not ecg.begin():
    # No text output - the host is expecting binary packets.
    while True:
        sleep_ms(500)

ecg.set_sampling(EASYECG_DEFAULT_ADC_RATE, 125)
ecg.set_notch(MAINS_HZ)
ecg.set_low_pass(LOW_PASS_HZ)
ecg.set_high_pass(HIGH_PASS_HZ)

# Zero the amplifier offset over one second. Sit still.
ecg.calibrate_baseline(1000)

while True:
    if not ecg.update():
        continue

    status = 0
    if ecg.is_saturated():
        status = 2
    elif ecg.is_lead_off():
        status = 1

    second = ecg.ecg_unfiltered() if SECOND_CHANNEL_UNFILTERED else 0
    send_data_packet(ecg.ecg_filtered(), second, int(ecg.heart_rate() + 0.5), status)
