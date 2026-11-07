# SPDX-License-Identifier: MIT
# Mocked-bus unit tests for the easyECG driver (which depends on protocentral_tla20xx).
# Runs on desktop CPython: `python tests/test_easyecg.py`.
# Needs protocentral_tla20xx: a sibling protocentral-micropython-tla20xx checkout, or
# `pip install protocentral-tla20xx`.

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "..", "protocentral-micropython-tla20xx"))

from protocentral_tla20xx import _REG_CONFIG, _REG_CONVERSION
from protocentral_easyecg import (  # noqa: E402
    EasyECG, EASYECG_I2C_ADDR_DEFAULT, EASYECG_DEFAULT_ADC_RATE, EASYECG_GAIN,
    EASYECG_SATURATION_COUNTS, EASYECG_CONV_REG, EASYECG_CONF_REG,
    DR_128SPS, DR_250SPS, DR_490SPS, DR_920SPS, DR_1600SPS, DR_2400SPS, DR_3300SPS,
    MUX_AIN0_AIN1, MUX_AIN0_AIN3, MUX_AIN1_AIN3, MUX_AIN2_AIN3,
    MUX_AIN0_GND, MUX_AIN1_GND, MUX_AIN2_GND, MUX_AIN3_GND,
    MODE_CONTINUOUS, MODE_SINGLE_SHOT, FSR_6_144V, FSR_4_096V, FSR_2_048V, FSR_0_256V,
)


class FakeI2C:
    def __init__(self, addresses=(0x49,)):
        self.regs = {}
        self.writes = []
        self.addresses = list(addresses)

    def store(self, reg, value):
        self.regs[reg] = bytes([(value >> 8) & 0xFF, value & 0xFF])

    def _ack(self, addr):
        # machine.I2C and smbus2 both raise OSError when nothing answers.
        if addr not in self.addresses:
            raise OSError(19, "ENODEV")

    def readfrom_mem(self, addr, reg, n):
        self._ack(addr)
        return self.regs.get(reg, b"\x00\x00")[:n]

    def writeto_mem(self, addr, reg, buf):
        self._ack(addr)
        self.regs[reg] = bytes(buf)
        self.writes.append((reg, (buf[0] << 8) | buf[1]))

    def scan(self):
        return self.addresses


def test_default_address_is_stock_board():
    assert EASYECG_I2C_ADDR_DEFAULT == 0x49


def test_begin_writes_arduino_config_word():
    bus = FakeI2C()
    assert EasyECG(bus).begin() is True
    assert bus.writes[-1] == (_REG_CONFIG, 0x04C3), hex(bus.writes[-1][1])


def test_begin_false_when_absent():
    bus = FakeI2C(addresses=())
    assert EasyECG(bus).begin() is False
    assert bus.writes == []


def test_begin_false_at_wrong_address():
    # A board re-strapped to 0x48 with the driver left at the default.
    assert EasyECG(FakeI2C(addresses=(0x48,))).begin() is False


def test_begin_true_at_alt_address():
    assert EasyECG(FakeI2C(addresses=(0x48,)), address=0x48).begin() is True


def test_read_config_returns_written_word():
    bus = FakeI2C()
    ecg = EasyECG(bus)
    ecg.begin()
    assert ecg.read_config() == 0x04C3


def test_microvolts_use_x535():
    bus = FakeI2C()
    ecg = EasyECG(bus)
    ecg.begin()
    bus.store(_REG_CONVERSION, 100 << 4)
    assert math.isclose(ecg.read_microvolts(), 100 * 1000.0 / 535.0, rel_tol=1e-9)


def test_negative_count_sign_extends():
    bus = FakeI2C()
    ecg = EasyECG(bus)
    ecg.begin()
    bus.store(_REG_CONVERSION, (-5 << 4) & 0xFFFF)
    assert ecg.read_raw() == -5


def test_baseline_corrects_raw():
    bus = FakeI2C()
    ecg = EasyECG(bus)
    ecg.begin()
    ecg.set_baseline(10)
    bus.store(_REG_CONVERSION, 100 << 4)
    assert ecg.read_raw() == 90
    assert ecg.read_raw_uncorrected() == 100


def _sine(ecg, hz, amplitude, seconds):
    fs = ecg.adc_rate_hz()
    for i in range(int(fs * seconds)):
        ecg._process_adc_sample(int(amplitude * math.sin(2 * math.pi * hz * i / fs)))


def test_notch_removes_mains_and_measures_it():
    ecg = EasyECG(FakeI2C())
    _sine(ecg, 50.0, 500, 3.0)
    assert abs(ecg.ecg_filtered()) < 25, ecg.ecg_filtered()
    # 500-count peak is 354 counts RMS, about 661 uV RMS at the electrodes.
    assert 450.0 < ecg.mains_microvolts() < 800.0, ecg.mains_microvolts()
    assert ecg.is_lead_off()


def test_saturation_flag():
    ecg = EasyECG(FakeI2C())
    for _ in range(250):
        ecg._process_adc_sample(1700)
    assert ecg.is_saturated()
    ecg.reset_filters()
    assert not ecg.is_saturated()


def _beats(ecg, bpm, seconds, amplitude=800):
    """QRS-like triangle pulse, 40 ms wide, at a fixed rate."""
    fs = ecg.adc_rate_hz()
    period = int(fs * 60.0 / bpm)
    half = max(1, int(0.02 * fs))
    for i in range(int(fs * seconds)):
        t = i % period
        v = amplitude * (1.0 - abs(t - half) / half) if t <= 2 * half else 0.0
        ecg._process_adc_sample(int(v))


def test_heart_rate_detected():
    for bpm in (60, 72, 120):
        ecg = EasyECG(FakeI2C())
        _beats(ecg, bpm, 20.0)
        assert abs(ecg.heart_rate() - bpm) < 3.0, (bpm, ecg.heart_rate())
        assert ecg.beat_detected() is True
        assert ecg.beat_detected() is False


def test_heart_rate_clears_when_signal_stops():
    ecg = EasyECG(FakeI2C())
    _beats(ecg, 72, 15.0)
    assert ecg.heart_rate() > 0.0
    for _ in range(ecg.adc_rate_hz() * 8):
        ecg._process_adc_sample(0)
    assert ecg.heart_rate() == 0.0


def test_set_sampling_validates():
    ecg = EasyECG(FakeI2C())
    assert ecg.set_sampling(500, 125) is True
    assert ecg.adc_rate_hz() == 500
    assert ecg.set_sampling(500, 120) is False
    assert ecg.set_sampling(4000, 125) is False
    assert ecg.adc_rate_hz() == 500


def test_update_paces_and_decimates():
    bus = FakeI2C()
    ecg = EasyECG(bus)
    ecg.begin()
    bus.store(_REG_CONVERSION, 0)
    import time
    outs = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.2:
        if ecg.update():
            outs += 1
    # 125 Hz output for 200 ms.
    assert 20 <= outs <= 30, outs



# ----------------------------------------------------------------- Arduino 1.0.0 parity

def test_arduino_defaults():
    ecg = EasyECG(FakeI2C())
    assert ecg.adc_rate_hz() == EASYECG_DEFAULT_ADC_RATE == 250   # Arduino 1000; see README
    assert ecg.output_rate_hz() == 125
    assert ecg.notch_enabled() is True
    assert ecg._notch_hz == 50.0 and ecg._notch_q == 4.0
    assert ecg._lpf_hz == 40.0
    assert ecg._hpf_hz == 0.0
    assert ecg.get_lead_off_threshold() == 200.0
    assert ecg.get_baseline() == 0
    assert ecg.get_gain() == EASYECG_GAIN == 535.0
    assert EASYECG_SATURATION_COUNTS == 1500
    assert ecg._mwi_len == 19                   # 150 ms at 125 Hz, rounded


def test_constants_mirror_arduino_enums():
    assert (DR_128SPS, DR_250SPS, DR_490SPS, DR_920SPS) == (0, 1, 2, 3)
    assert (DR_1600SPS, DR_2400SPS, DR_3300SPS) == (4, 5, 6)
    assert (MUX_AIN0_AIN1, MUX_AIN0_AIN3, MUX_AIN1_AIN3, MUX_AIN2_AIN3) == (0, 1, 2, 3)
    assert (MUX_AIN0_GND, MUX_AIN1_GND, MUX_AIN2_GND, MUX_AIN3_GND) == (4, 5, 6, 7)
    assert (MODE_CONTINUOUS, MODE_SINGLE_SHOT) == (0, 1)
    assert (FSR_6_144V, FSR_2_048V, FSR_0_256V) == (0, 2, 5)
    assert (EASYECG_CONV_REG, EASYECG_CONF_REG) == (0, 1)


def test_set_notch_validates_and_disables():
    ecg = EasyECG(FakeI2C())
    assert ecg.set_notch(60.0) is True and ecg._notch_hz == 60.0
    assert ecg.set_notch(50.0, q=0.0) is False
    assert ecg.set_notch(120.0) is False        # 250 SPS < 2.5 x 120
    assert ecg._notch_hz == 60.0
    assert ecg.set_notch(0) is True
    assert ecg.notch_enabled() is False
    # Notch off: mains still measured (at 50 Hz) but passed through.
    _sine(ecg, 50.0, 500, 3.0)
    assert ecg.mains_microvolts() > 450.0


def test_set_low_and_high_pass_validate():
    ecg = EasyECG(FakeI2C())
    assert ecg.set_low_pass(-1.0) is False
    assert ecg.set_low_pass(0.45 * 250 + 1) is False
    assert ecg.set_low_pass(25.0) is True and ecg._lpf_hz == 25.0
    assert ecg.set_low_pass(0) is True          # disabled
    _sine(ecg, 10.0, 100, 1.0)                  # runs with no low-pass stage
    ecg.set_high_pass(-3.0)
    assert ecg._hpf_hz == 0.0 and ecg._hpf_a == 1.0
    ecg.set_high_pass(0.5)
    rc = 1.0 / (2.0 * math.pi * 0.5)
    assert math.isclose(ecg._hpf_a, rc / (rc + 1.0 / 125.0))


def test_high_pass_removes_dc_only_when_on():
    ecg = EasyECG(FakeI2C())
    for _ in range(250 * 10):
        ecg._process_adc_sample(200)
    assert abs(ecg.ecg_filtered() - 200) <= 1       # off by default: DC passes
    ecg.set_high_pass(0.5)
    for _ in range(250 * 10):
        ecg._process_adc_sample(200)
    assert abs(ecg.ecg_filtered()) <= 2


def test_set_sampling_rejects_corners_out_of_range():
    ecg = EasyECG(FakeI2C())
    assert ecg.set_sampling(250, 250) is True       # decimation 1, used by the plotter
    assert ecg.set_sampling(80, 10) is False        # 40 Hz low-pass > 0.45 x 80
    ecg.set_low_pass(0)
    assert ecg.set_sampling(100, 10) is False       # 50 Hz notch needs >= 125 SPS
    assert ecg.adc_rate_hz() == 250


def test_decimation_averages_unfiltered():
    ecg = EasyECG(FakeI2C())
    ecg.set_sampling(500, 125)                      # decimate by 4
    outs = []
    for v in (0, 4, 8, 12, 100, 100, 100, 104):
        ecg._out_ready = False
        ecg._process_adc_sample(v)
        if ecg._out_ready:
            outs.append(ecg._out_unfiltered)
    assert outs == [6.0, 101.0], outs


def test_output_counts_clamp_and_round_like_arduino():
    ecg = EasyECG(FakeI2C())
    ecg._out_filtered = 40000.0
    ecg._out_unfiltered = -40000.0
    assert ecg.ecg_filtered() == 32767
    assert ecg.ecg_unfiltered() == -32768
    ecg._out_filtered = -2.5
    ecg._out_unfiltered = 2.5
    assert ecg.ecg_filtered() == -3
    assert ecg.ecg_unfiltered() == 3


def test_full_scale_fallback_for_unknown_code():
    ecg = EasyECG(FakeI2C())
    ecg._fsr = 6
    assert ecg.full_scale_volts() == 2.048


def test_set_range_rescales_and_resets():
    bus = FakeI2C()
    ecg = EasyECG(bus)
    ecg.begin()
    ecg._sat_level = 1.0
    ecg.set_range(FSR_4_096V)
    assert bus.writes[-1] == (_REG_CONFIG, 0x02C3), hex(bus.writes[-1][1])
    assert ecg.full_scale_volts() == 4.096
    assert not ecg.is_saturated()


def test_low_level_passthroughs_write_config():
    bus = FakeI2C()
    ecg = EasyECG(bus)
    ecg.begin()
    ecg.set_mux(MUX_AIN2_GND)
    assert bus.writes[-1] == (_REG_CONFIG, 0x64C3)
    ecg.set_mux(MUX_AIN0_AIN1)
    ecg.set_data_rate(DR_128SPS)
    assert bus.writes[-1] == (_REG_CONFIG, 0x0403)
    ecg.set_data_rate(DR_3300SPS)
    ecg.set_mode(MODE_SINGLE_SHOT)
    assert bus.writes[-1] == (_REG_CONFIG, 0x05C3)
    ecg.set_mode(MODE_CONTINUOUS)
    assert bus.writes[-1] == (_REG_CONFIG, 0x04C3)


def test_read_single_shot():
    bus = FakeI2C()
    ecg = EasyECG(bus)
    ecg.begin()
    ecg.set_baseline(3)
    bus.store(_REG_CONVERSION, 50 << 4)
    assert ecg.read_single_shot() == 47
    # OS bit set, mode bit set; Arduino leaves the ADC in single-shot afterwards.
    assert bus.writes[-1] == (_REG_CONFIG, 0x85C3), hex(bus.writes[-1][1])
    assert ecg.adc._mode == MODE_SINGLE_SHOT


def test_read_voltage_is_output_referred():
    bus = FakeI2C()
    ecg = EasyECG(bus)
    ecg.begin()
    bus.store(_REG_CONVERSION, 1024 << 4)
    assert math.isclose(ecg.read_voltage(), 1.024)


def test_calibrate_baseline_converges():
    bus = FakeI2C()
    ecg = EasyECG(bus)
    ecg.begin()
    bus.store(_REG_CONVERSION, (-37 << 4) & 0xFFFF)
    ecg.calibrate_baseline(20)
    assert ecg.get_baseline() == -37
    ecg.calibrate_baseline(20)                      # averages UNCORRECTED: no stacking
    assert ecg.get_baseline() == -37
    assert ecg.read_raw() == 0
    ecg.calibrate_baseline(0)                       # no-op
    assert ecg.get_baseline() == -37


def test_lead_off_threshold():
    ecg = EasyECG(FakeI2C())
    _sine(ecg, 50.0, 100, 3.0)                      # ~132 uV RMS
    assert 100.0 < ecg.mains_microvolts() < 170.0, ecg.mains_microvolts()
    assert not ecg.is_lead_off()
    ecg.set_lead_off_threshold(50.0)
    assert ecg.is_lead_off()
    ecg.set_lead_off_threshold(0.0)                 # 0 disables the mains test
    assert not ecg.is_lead_off()
    assert ecg.get_lead_off_threshold() == 0.0


def test_last_rr_millis():
    ecg = EasyECG(FakeI2C())
    _beats(ecg, 72, 15.0)
    assert abs(ecg.last_rr_millis() - 833) <= 16, ecg.last_rr_millis()
    assert ecg.last_rr_ms() == ecg.last_rr_millis()


def test_heart_rate_at_250_hz_output():
    ecg = EasyECG(FakeI2C())
    ecg.set_sampling(250, 250)
    assert ecg._mwi_len == 38
    _beats(ecg, 90, 20.0)
    assert abs(ecg.heart_rate() - 90) < 3.0, ecg.heart_rate()


def test_heart_rate_zero_during_learning():
    ecg = EasyECG(FakeI2C())
    _beats(ecg, 72, 1.9)
    assert ecg.heart_rate() == 0.0
    assert ecg.beat_detected() is False


def test_missed_samples_counted_when_loop_is_late():
    import protocentral_easyecg as mod
    bus = FakeI2C()
    ecg = EasyECG(bus)
    ecg.begin()
    clock = [1000000]
    saved = mod.ticks_us
    mod.ticks_us = lambda: clock[0]
    try:
        assert ecg.update() is False                # first ADC sample (decimation 2)
        clock[0] += 4000                            # exactly on time: one period
        assert ecg.update() is True
        assert ecg.update() is False                # not due yet: no sample taken
        clock[0] += 4000 * 10                       # next was due 1 period on: 9 late
        ecg.update()
        assert ecg.missed_samples() == 9, ecg.missed_samples()
        clock[0] += 4000 * 3                        # 2 late (<= 4): caught up, not counted
        ecg.update()
        assert ecg.missed_samples() == 9
    finally:
        mod.ticks_us = saved


def test_reset_filters_clears_detector():
    ecg = EasyECG(FakeI2C())
    _beats(ecg, 72, 10.0)
    assert ecg.heart_rate() > 0.0
    ecg.reset_filters()
    assert ecg.heart_rate() == 0.0
    assert ecg.last_rr_millis() == 0
    assert ecg.beat_detected() is False
    assert ecg.mains_microvolts() == 0.0


# ----------------------------------------------------------------- OpenView example

class _Stop(Exception):
    pass


def test_openview_example_packets():
    """Runs examples/easyecg_openview.py against a fake board and checks the framing
    against Arduino 04.OpenView: 0A FA 08 00 02 | 4 x int16 LE | 00 0B."""
    import io
    import struct
    import types
    import protocentral_easyecg as mod

    bus = FakeI2C()
    machine = types.ModuleType("machine")
    machine.Pin = lambda *a, **k: None
    machine.I2C = lambda *a, **k: bus
    sink = io.BytesIO()
    fake_stdout = types.SimpleNamespace(buffer=sink)

    frames = [
        # filtered, unfiltered, bpm, sat_level, mains_sq -> expected status
        (-1234.4, 40000.0, 71.6, 0.0, 0.0, 0),
        (12.5, -7.0, 0.0, 0.0, 1e6, 1),
        (0.0, 0.0, 60.4, 1.0, 1e6, 2),
    ]
    calls = [0]
    seen = {}

    def fake_update(self):
        seen["rates"] = (self.adc_rate_hz(), self.output_rate_hz(), self._hpf_hz,
                         self._lpf_hz, self.notch_enabled())
        i = calls[0]
        calls[0] += 1
        if i >= len(frames):
            raise _Stop()
        f, u, bpm, sat, msq, _ = frames[i]
        self._out_filtered, self._out_unfiltered, self._bpm = f, u, bpm
        self._sat_level, self._mains_sq = sat, msq
        return True

    saved = (sys.modules.get("machine"), sys.stdout, mod.EasyECG.update,
             mod.EasyECG.calibrate_baseline)
    sys.modules["machine"] = machine
    mod.EasyECG.update = fake_update
    mod.EasyECG.calibrate_baseline = lambda self, ms=1000: None
    path = os.path.join(ROOT, "examples", "easyecg_openview.py")
    try:
        sys.stdout = fake_stdout
        try:
            exec(compile(open(path).read(), path, "exec"), {"__name__": "__main__"})
        except _Stop:
            pass
    finally:
        sys.stdout = saved[1]
        mod.EasyECG.update = saved[2]
        mod.EasyECG.calibrate_baseline = saved[3]
        if saved[0] is None:
            del sys.modules["machine"]
        else:
            sys.modules["machine"] = saved[0]

    assert seen["rates"] == (250, 125, 0.0, 40.0, True), seen["rates"]
    data = sink.getvalue()
    assert len(data) == 15 * len(frames), len(data)
    expect = [(-1234, 32767, 72, 0), (13, -7, 0, 1), (0, 0, 60, 2)]
    for k, want in enumerate(expect):
        p = data[15 * k:15 * (k + 1)]
        assert p[:5] == bytes([0x0A, 0xFA, 0x08, 0x00, 0x02]), p[:5]
        assert p[13:] == bytes([0x00, 0x0B]), p[13:]
        assert struct.unpack("<hhhh", p[5:13]) == want, (k, struct.unpack("<hhhh", p[5:13]))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
    print("ALL PASSED")
