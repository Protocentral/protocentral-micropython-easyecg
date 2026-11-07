# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Ashwin Whitchurch, Protocentral Electronics
#
# MicroPython driver for the Protocentral easyECG single-lead ECG breakout. An INA333
# and two OPA2333 stages (total gain x535, roughly 0.5-31 Hz) feed an on-board TI
# TLA2024 12-bit ADC, read differentially against the buffered 1.65 V mid-rail. This
# library DEPENDS ON the TLA20xx driver and adds the easyECG configuration, the paced
# filter chain and the heart-rate detector.
#
# Ported from the Protocentral easyECG Arduino library v1.0.0 (single source of truth):
#   https://github.com/Protocentral/protocentral_easyecg_arduino
# Same public surface, in snake_case: begin() ... read_single_shot(). Python-only extras
# are set_gain() / get_gain() and the .adc attribute.
#
# Same chain as the Arduino library:
#
#   ADC -> 50/60 Hz notch -> 4th-order Butterworth low-pass (40 Hz)
#       -> decimate to the output rate -> optional high-pass (OFF by default)
#       -> R-peak detector -> heart rate
#
# One deliberate difference: the default ADC rate is 250 SPS, not 1000. Interpreted
# Python can't run these filters at 1 kSPS on most boards. The filters are built for an
# exact rate, so a clock the host can actually hold beats a faster one it can't. At
# 250 SPS the 50/60 Hz notch and the 40 Hz low-pass are still well inside Nyquist, and
# both output rates the Arduino examples use (125 and 250 Hz) are reachable.
#
# Requires: protocentral_tla20xx  (mip/pip dependency)

import math

__version__ = "0.2.0"

try:
    from time import ticks_us, ticks_diff, ticks_add, sleep_ms, sleep_us
except ImportError:                     # CPython (Raspberry Pi)
    import time as _time

    def ticks_us():
        return int(_time.monotonic() * 1000000)

    def ticks_diff(a, b):
        return a - b

    def ticks_add(a, b):
        return a + b

    def sleep_ms(ms):
        _time.sleep(ms / 1000.0)

    def sleep_us(us):
        _time.sleep(us / 1000000.0)

from protocentral_tla20xx import (
    TLA20XX, FSR_6_144V, FSR_4_096V, FSR_2_048V, FSR_1_024V, FSR_0_512V, FSR_0_256V,
    DR_128SPS, DR_250SPS, DR_490SPS, DR_920SPS, DR_1600SPS, DR_2400SPS, DR_3300SPS,
    MUX_AIN0_AIN1, MUX_AIN0_AIN3, MUX_AIN1_AIN3, MUX_AIN2_AIN3,
    MUX_AIN0_GND, MUX_AIN1_GND, MUX_AIN2_GND, MUX_AIN3_GND,
    OP_CONTINUOUS, OP_SINGLE, _REG_CONFIG,
)

# Arduino's easyECG::Mode names for the TLA20xx OP_* values.
MODE_CONTINUOUS = OP_CONTINUOUS
MODE_SINGLE_SHOT = OP_SINGLE

# JP1 bridged 1-2 straps ADDR to 3.3 V -> 0x49. Cut and bridge 2-3 for 0x48. An easyEMG
# ships at 0x48, so the two share a bus without touching the jumper.
EASYECG_I2C_ADDR_DEFAULT = 0x49
EASYECG_I2C_ADDR_ALT = 0x48

# INA333 x11 (R10 = 10k) times 1 + R14/R19 = 1 + 100k/2.1k = x48.6. Design value; pass
# your measured gain to set_gain(). Only absolute microvolts depend on it.
EASYECG_DEFAULT_GAIN = 535.0
EASYECG_GAIN = EASYECG_DEFAULT_GAIN     # Arduino name

EASYECG_HPF_HZ = 0.49                   # C6 1 uF / R13 324k, 1-pole
EASYECG_LPF_HZ = 31.0                   # Sallen-Key, 33k / 100n, Q = 0.5

# The OPA2333 swings to about +/-1.6 V around mid-rail; 1500 counts leaves some margin.
EASYECG_SATURATION_COUNTS = 1500

EASYECG_CONV_REG = 0x00
EASYECG_CONF_REG = 0x01

# Arduino defaults to 1000 SPS (500 on AVR). See the note at the top of this file.
EASYECG_DEFAULT_ADC_RATE = 250
EASYECG_DEFAULT_OUTPUT_RATE = 125
EASYECG_MAX_DECIMATION = 16
EASYECG_MWI_MAX = 40
EASYECG_RR_HISTORY = 8

# PROVISIONAL: not yet validated on the bench per electrode.
_DEFAULT_LEADOFF_UV = 200.0

_BW4_Q1 = 0.54119610
_BW4_Q2 = 1.30656296

# TLA2024 conversion time per DR code, microseconds (datasheet minimum rates).
_CONV_US = (7813, 4000, 2041, 1087, 625, 417, 303)

_FSR_VOLTS = {
    FSR_6_144V: 6.144, FSR_4_096V: 4.096, FSR_2_048V: 2.048,
    FSR_1_024V: 1.024, FSR_0_512V: 0.512, FSR_0_256V: 0.256,
}


class _Biquad:
    """RBJ cookbook biquad, direct form II transposed."""

    def __init__(self, b0=1.0, b1=0.0, b2=0.0, a1=0.0, a2=0.0):
        self.b0, self.b1, self.b2, self.a1, self.a2 = b0, b1, b2, a1, a2
        self.z1 = 0.0
        self.z2 = 0.0

    def clear(self):
        self.z1 = 0.0
        self.z2 = 0.0

    def run(self, x):
        y = (self.b0 * x) + self.z1
        self.z1 = (self.b1 * x) - (self.a1 * y) + self.z2
        self.z2 = (self.b2 * x) - (self.a2 * y)
        return y


def _low_pass(fc, fs, q):
    w0 = 2.0 * math.pi * fc / fs
    cosw0 = math.cos(w0)
    alpha = math.sin(w0) / (2.0 * q)
    a0 = 1.0 + alpha
    b0 = ((1.0 - cosw0) * 0.5) / a0
    return _Biquad(b0, (1.0 - cosw0) / a0, b0, (-2.0 * cosw0) / a0, (1.0 - alpha) / a0)


def _notch(f0, fs, q):
    w0 = 2.0 * math.pi * f0 / fs
    cosw0 = math.cos(w0)
    alpha = math.sin(w0) / (2.0 * q)
    a0 = 1.0 + alpha
    b0 = 1.0 / a0
    b1 = (-2.0 * cosw0) / a0
    return _Biquad(b0, b1, b0, b1, (1.0 - alpha) / a0)


def _clamp_counts(v):
    # Arduino clampCounts(): saturate to int16, round half away from zero.
    if v > 32767.0:
        v = 32767.0
    elif v < -32768.0:
        v = -32768.0
    return int(v + 0.5) if v >= 0.0 else int(v - 0.5)


class EasyECG:
    """Driver for the Protocentral easyECG breakout.

    Example:
        from machine import Pin, I2C
        from protocentral_easyecg import EasyECG
        i2c = I2C(0, scl=Pin(6), sda=Pin(5), freq=400000)   # QT Py C3: SCL=6, SDA=5
        ecg = EasyECG(i2c)
        ecg.begin()
        while True:
            if ecg.update():
                print(ecg.ecg_microvolts(), ecg.heart_rate())
    """

    def __init__(self, i2c, address=EASYECG_I2C_ADDR_DEFAULT):
        self.i2c = i2c
        self.address = address
        self.adc = TLA20XX(i2c, address)
        self._fsr = FSR_2_048V
        self._gain = EASYECG_DEFAULT_GAIN
        self._baseline = 0

        self._adc_rate = EASYECG_DEFAULT_ADC_RATE
        self._out_rate = EASYECG_DEFAULT_OUTPUT_RATE
        self._decim = EASYECG_DEFAULT_ADC_RATE // EASYECG_DEFAULT_OUTPUT_RATE
        self._period_us = 1000000 // EASYECG_DEFAULT_ADC_RATE
        self._next_us = 0
        self._clock_running = False
        self._missed = 0

        self._notch_on = True
        self._notch_hz = 50.0
        self._notch_q = 4.0
        self._lpf_hz = 40.0
        self._hpf_hz = 0.0
        self._lead_off_uv = _DEFAULT_LEADOFF_UV

        self._rebuild_filters()

    # ------------------------------------------------------------- connection
    def begin(self):
        """AIN0/AIN1, +/-2.048 V, continuous, 3300 SPS -> CONF 0x04C3.

        Returns False without touching the bus further if the ADC doesn't answer. A write
        to an absent device raises OSError on real I2C, so check first.
        """
        if not self.is_connected():
            return False
        self.adc.begin()
        self.adc._mux = MUX_AIN0_AIN1
        self.adc._mode = OP_CONTINUOUS
        self.adc._dr = DR_3300SPS
        self._fsr = FSR_2_048V
        self.adc.set_fsr(FSR_2_048V)
        self.reset_filters()
        return True

    def is_connected(self):
        return self.address in self.i2c.scan()

    def read_config(self):
        """CONFIG register as read back. Bit 15 reads 0 while a conversion is in progress."""
        return self.adc._read16(_REG_CONFIG)

    # ------------------------------------------------------------- single samples
    def read_raw_uncorrected(self):
        """Signed 12-bit counts straight from the ADC."""
        return self.adc.read_adc()

    def read_raw(self):
        """Signed 12-bit counts, baseline-corrected."""
        return self.read_raw_uncorrected() - self._baseline

    def read_voltage(self):
        """Differential ADC voltage, volts (output-referred)."""
        return self.read_raw() * (self.full_scale_volts() / 2048.0)

    def read_microvolts(self):
        """uV at the electrodes, unfiltered."""
        return self._counts_to_uv(self.read_raw())

    def full_scale_volts(self):
        # Codes 6 and 7 aren't in the table; fall back to +/-2.048 V as Arduino does.
        return _FSR_VOLTS.get(self._fsr, 2.048)

    def _counts_to_uv(self, counts):
        if self._gain <= 0.0:
            return 0.0
        return counts * (self.full_scale_volts() / 2048.0) * 1000000.0 / self._gain

    # ------------------------------------------------------------- filter set-up
    def _rebuild_filters(self):
        fs = float(self._adc_rate)

        # Always designed: its residue is the mains measurement.
        nf = self._notch_hz if self._notch_hz > 0.0 else 50.0
        self._notch = _notch(nf, fs, self._notch_q)

        if self._lpf_hz > 0.0:
            self._lpf1 = _low_pass(self._lpf_hz, fs, _BW4_Q1)
            self._lpf2 = _low_pass(self._lpf_hz, fs, _BW4_Q2)

        if self._hpf_hz > 0.0:
            rc = 1.0 / (2.0 * math.pi * self._hpf_hz)
            dt = 1.0 / self._out_rate
            self._hpf_a = rc / (rc + dt)
        else:
            self._hpf_a = 1.0

        self._sat_alpha = 1.0 / (0.25 * fs)
        self._mains_alpha = 1.0 / (0.5 * fs)

        self._mwi_len = max(1, min(EASYECG_MWI_MAX, int(0.15 * self._out_rate + 0.5)))
        self.reset_filters()

    def reset_filters(self):
        """Clears filter, detector and status state, e.g. after re-attaching electrodes."""
        self._notch.clear()
        if self._lpf_hz > 0.0:
            self._lpf1.clear()
            self._lpf2.clear()
        self._hpf_prev_x = 0.0
        self._hpf_prev_y = 0.0

        self._decim_count = 0
        self._raw_accum = 0.0
        self._out_ready = False
        self._out_filtered = 0.0
        self._out_unfiltered = 0.0

        self._sat_level = 0.0
        self._mains_sq = 0.0

        self._hr_x = [0.0] * 5
        self._mwi_buf = [0.0] * EASYECG_MWI_MAX
        self._mwi_idx = 0
        self._mwi_sum = 0.0
        self._sig_level = 0.0
        self._noise_level = 0.0
        self._hr_count = 0
        self._last_beat = 0
        self._have_beat = False
        self._in_peak = False
        self._peak_val = 0.0
        self._rr = [0] * EASYECG_RR_HISTORY
        self._rr_count = 0
        self._rr_idx = 0
        self._last_rr = 0
        self._bpm = 0.0
        self._beat_flag = False

        self._clock_running = False

    def set_sampling(self, adc_rate_hz, output_rate_hz):
        """adc_rate_hz must be a multiple of output_rate_hz, at most 16 times it.

        Returns False and keeps the old setting if the pair is invalid.
        """
        if adc_rate_hz <= 0 or output_rate_hz <= 0 or adc_rate_hz % output_rate_hz:
            return False
        d = adc_rate_hz // output_rate_hz
        if d < 1 or d > EASYECG_MAX_DECIMATION:
            return False
        if self._lpf_hz > 0.0 and self._lpf_hz > 0.45 * adc_rate_hz:
            return False
        if self._notch_hz > 0.0 and adc_rate_hz < self._notch_hz * 2.5:
            return False
        self._adc_rate = adc_rate_hz
        self._out_rate = output_rate_hz
        self._decim = d
        self._period_us = 1000000 // adc_rate_hz
        self._rebuild_filters()
        return True

    def adc_rate_hz(self):
        return self._adc_rate

    def output_rate_hz(self):
        return self._out_rate

    def set_notch(self, mains_hz, q=4.0):
        """Mains notch, 50 or 60 Hz. On by default at 50 Hz. Pass 0 to disable."""
        if mains_hz <= 0.0:
            self._notch_on = False
            self._notch_hz = 0.0
            self._rebuild_filters()
            return True
        if self._adc_rate < mains_hz * 2.5 or q <= 0.0:
            return False
        self._notch_on = True
        self._notch_hz = mains_hz
        self._notch_q = q
        self._rebuild_filters()
        return True

    def notch_enabled(self):
        return self._notch_on

    def set_low_pass(self, corner_hz):
        """Digital low-pass corner, Hz. Default 40. Pass 0 to disable."""
        if corner_hz < 0.0 or corner_hz > 0.45 * self._adc_rate:
            return False
        self._lpf_hz = corner_hz
        self._rebuild_filters()
        return True

    def set_high_pass(self, corner_hz):
        """Digital high-pass corner, Hz. Default 0 = OFF.

        Off on purpose: the board is already AC-coupled at about 0.5 Hz, and every extra
        high-pass pole distorts the ST segment.
        """
        self._hpf_hz = corner_hz if corner_hz > 0.0 else 0.0
        self._rebuild_filters()

    # ------------------------------------------------------------- paced pipeline
    def update(self):
        """Non-blocking. Call as often as you can.

        Takes one ADC sample when the sample clock is due and returns True when a new
        output sample is ready.
        """
        now = ticks_us()
        if not self._clock_running:
            self._clock_running = True
            self._next_us = now

        late = ticks_diff(now, self._next_us)
        if late < 0:
            return False

        # Advance by whole periods so the rate stays exact. Resync only if badly behind.
        if late > 4 * self._period_us:
            self._missed += late // self._period_us
            self._next_us = now
        self._next_us = ticks_add(self._next_us, self._period_us)

        self._out_ready = False
        self._process_adc_sample(self.read_raw_uncorrected())
        if self._out_ready:
            self._out_ready = False
            return True
        return False

    def _process_adc_sample(self, uncorrected):
        ind = 1.0 if abs(uncorrected) >= EASYECG_SATURATION_COUNTS else 0.0
        self._sat_level += self._sat_alpha * (ind - self._sat_level)

        x = float(uncorrected - self._baseline)

        n = self._notch.run(x)
        resid = x - n
        self._mains_sq += self._mains_alpha * ((resid * resid) - self._mains_sq)

        y = n if self._notch_on else x
        if self._lpf_hz > 0.0:
            y = self._lpf2.run(self._lpf1.run(y))

        self._raw_accum += x
        self._decim_count += 1
        if self._decim_count >= self._decim:
            self._out_unfiltered = self._raw_accum / self._decim
            self._raw_accum = 0.0
            self._decim_count = 0
            self._process_output_sample(y)

    def _process_output_sample(self, x):
        if self._hpf_hz > 0.0:
            y = self._hpf_a * (self._hpf_prev_y + x - self._hpf_prev_x)
            self._hpf_prev_x = x
            self._hpf_prev_y = y
            x = y
        self._out_filtered = x
        self._out_ready = True
        self._detect_beat(x)

    def ecg_filtered(self):
        return _clamp_counts(self._out_filtered)

    def ecg_microvolts(self):
        return self._counts_to_uv(self._out_filtered)

    def ecg_unfiltered(self):
        return _clamp_counts(self._out_unfiltered)

    def ecg_unfiltered_microvolts(self):
        return self._counts_to_uv(self._out_unfiltered)

    def missed_samples(self):
        return self._missed

    # ------------------------------------------------------------- heart rate
    def _detect_beat(self, x):
        # Simplified Pan-Tompkins: 5-point derivative -> square -> 150 ms moving-window
        # integral -> adaptive threshold, 200 ms refractory, T-wave rejection inside 360 ms.
        h = self._hr_x
        h[4] = h[3]                     # one at a time: a 5-way swap builds a tuple
        h[3] = h[2]
        h[2] = h[1]
        h[1] = h[0]
        h[0] = x
        d = ((2.0 * h[0]) + h[1] - h[3] - (2.0 * h[4])) * 0.125
        sq = d * d

        self._mwi_sum -= self._mwi_buf[self._mwi_idx]
        self._mwi_buf[self._mwi_idx] = sq
        self._mwi_sum += sq
        self._mwi_idx += 1
        if self._mwi_idx >= self._mwi_len:
            self._mwi_idx = 0
        if self._mwi_sum < 0.0:
            self._mwi_sum = 0.0
        mwi = self._mwi_sum / self._mwi_len

        self._hr_count += 1
        if self._hr_count <= 2 * self._out_rate:        # learning phase
            if mwi > self._sig_level:
                self._sig_level = mwi
            self._noise_level += (mwi - self._noise_level) / self._hr_count
            return

        threshold = self._noise_level + 0.25 * (self._sig_level - self._noise_level)
        since = self._hr_count - self._last_beat
        refractory = int(0.2 * self._out_rate)

        if not self._in_peak:
            if mwi > threshold and (not self._have_beat or since > refractory):
                self._in_peak = True
                self._peak_val = mwi
            else:
                self._noise_level += 0.01 * (mwi - self._noise_level)
        else:
            if mwi > self._peak_val:
                self._peak_val = mwi
            elif mwi < 0.5 * self._peak_val:
                self._in_peak = False
                rr_ms = (since * 1000) // self._out_rate

                # A small peak soon after a beat is a T-wave, not a QRS.
                if self._have_beat and rr_ms < 360 and self._peak_val < 0.5 * self._sig_level:
                    self._noise_level += 0.125 * (self._peak_val - self._noise_level)
                    return

                self._sig_level = 0.125 * self._peak_val + 0.875 * self._sig_level

                if self._have_beat and 270 <= rr_ms <= 2000:    # 30-220 bpm
                    self._last_rr = rr_ms
                    self._rr[self._rr_idx] = rr_ms
                    self._rr_idx = (self._rr_idx + 1) % EASYECG_RR_HISTORY
                    if self._rr_count < EASYECG_RR_HISTORY:
                        self._rr_count += 1
                    total = 0                   # no slice: keep allocations out
                    rr = self._rr
                    for i in range(self._rr_count):
                        total += rr[i]
                    self._bpm = 60000.0 / (total / self._rr_count)

                self._have_beat = True
                self._last_beat = self._hr_count
                self._beat_flag = True

        # No beat for 3 s: let a stale signal level decay. After 5 s, stop reporting a rate.
        if self._have_beat and since > 3 * self._out_rate and not self._in_peak:
            self._sig_level *= 0.98
            if self._sig_level < self._noise_level:
                self._sig_level = self._noise_level
        if self._have_beat and since > 5 * self._out_rate:
            self._bpm = 0.0
            self._rr_count = 0
            self._rr_idx = 0

    def heart_rate(self):
        """Beats per minute, 0 until established (about 2 s to learn, then a few beats)."""
        return self._bpm

    def beat_detected(self):
        """True once per detected beat; clears when read."""
        b = self._beat_flag
        self._beat_flag = False
        return b

    def last_rr_millis(self):
        """Most recent R-R interval, ms (Arduino lastRRMillis())."""
        return self._last_rr

    last_rr_ms = last_rr_millis         # 0.1.0 name, kept for compatibility

    # ------------------------------------------------------------- signal quality
    def is_saturated(self):
        """Pinned near a rail for most of the last ~250 ms.

        On a mains-powered host this is the driven-right-leg failure: use a battery or an
        easyISO. It is also what a floating electrode looks like.
        """
        return self._sat_level > 0.5

    def mains_microvolts(self):
        """Mains pickup, uV RMS at the electrodes: what the notch is removing."""
        return self._counts_to_uv(math.sqrt(self._mains_sq))

    def is_lead_off(self):
        """PROVISIONAL heuristic: saturated, or mains pickup above the threshold."""
        return self.is_saturated() or (
            self._lead_off_uv > 0.0 and self.mains_microvolts() > self._lead_off_uv)

    def set_lead_off_threshold(self, mains_uv_rms):
        self._lead_off_uv = mains_uv_rms

    def get_lead_off_threshold(self):
        return self._lead_off_uv

    # ------------------------------------------------------------- baseline and gain
    def calibrate_baseline(self, duration_ms=1000):
        """Average the uncorrected reading over whole heartbeats. Electrodes on, sit still."""
        if duration_ms <= 0:
            return
        # Average the UNCORRECTED count so repeated calls converge instead of stacking.
        total = 0
        n = 0
        t0 = ticks_us()
        while ticks_diff(ticks_us(), t0) < duration_ms * 1000:
            total += self.read_raw_uncorrected()
            n += 1
            sleep_us(500)
        if n:
            self._baseline = int(total / n)     # truncates toward zero, as in C
        self.reset_filters()

    def set_baseline(self, offset_counts):
        self._baseline = offset_counts
        self.reset_filters()

    def get_baseline(self):
        return self._baseline

    def set_gain(self, gain):
        """Python-only. Arduino fixes the gain at EASYECG_GAIN (set by R10, R14, R19).

        Pass a measured gain to correct absolute microvolts. Ignored unless > 0.
        """
        if gain > 0.0:
            self._gain = gain

    def get_gain(self):
        return self._gain

    # ------------------------------------------------------------- low-level passthroughs
    def set_data_rate(self, rate):
        """ADC conversion rate, a DR_* code. begin() sets DR_3300SPS.

        Leave it there for the paced pipeline: the conversion must outrun the ADC rate
        set with set_sampling(), or update() reads the same conversion twice.
        """
        self.adc.set_dr(rate)

    def set_range(self, fsr):
        """Change the PGA range, an FSR_* code. +/-2.048 V is the ECG range; see the README."""
        self._fsr = fsr
        self.adc.set_fsr(fsr)
        self.reset_filters()

    def set_mode(self, mode):
        """MODE_CONTINUOUS (begin() default) or MODE_SINGLE_SHOT."""
        self.adc.set_mode(mode)

    def set_mux(self, mux):
        """Input mux, a MUX_* code. begin() sets MUX_AIN0_AIN1 (signal vs mid-rail)."""
        self.adc.set_mux(mux)

    def read_single_shot(self):
        """Start one conversion, wait for it, return baseline-corrected counts.

        As in Arduino, this leaves the ADC in single-shot mode: call
        set_mode(MODE_CONTINUOUS) or begin() before using update() again.
        """
        adc = self.adc
        adc._mode = OP_SINGLE
        adc._write16(_REG_CONFIG, adc._conf_word() | 0x8000)
        wait = _CONV_US[adc._dr] if 0 <= adc._dr < len(_CONV_US) else _CONV_US[0]
        sleep_us(2 * wait)
        return self.read_raw()
