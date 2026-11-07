# Changelog

All notable changes to this driver are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-07

First public release. A port of the
[Protocentral easyECG Arduino library](https://github.com/Protocentral/protocentral_easyecg_arduino)
1.0.0 for MicroPython and Raspberry Pi.

### Added
- The Arduino API in snake_case, with the same defaults and validation rules: paced
  sampling with missed-sample counting, 50/60 Hz notch, 40 Hz low-pass, optional
  high-pass, heart rate from a simplified Pan-Tompkins detector, saturation, mains
  pickup and a provisional lead-off heuristic, readings in µV at the electrodes.
- Low-level ADC passthroughs: `set_range()`, `set_data_rate()`, `set_mode()`,
  `set_mux()` and `read_single_shot()`.
- Python-only extras: `set_gain()` / `get_gain()` and `.adc`.
- An example for every Arduino example: `easyecg_simpletest.py` and
  `easyecg_raspberrypi.py` (`01`), `easyecg_plotter.py` (`02`), `easyecg_heartrate.py`
  (`03`), `easyecg_openview.py` (`04`, the same packets for OpenView's easyECG Breakout
  profile) and `easyecg_diagnostics.py` (`05`).
- Mocked-bus tests and an `mpy-cross` compile in CI, on every push and before every
  PyPI publish.

### Differences from the Arduino library
- The default ADC rate is 250 SPS rather than 1000, because interpreted Python can't
  hold 1 kSPS through the filters. Output rates (125 / 250 Hz) are the same.
- `begin()` returns `False` on a missing board instead of writing to it.

Validated with the mocked-bus tests. Bench results on MicroPython and Raspberry Pi
hosts will be added to the README as they're measured.
