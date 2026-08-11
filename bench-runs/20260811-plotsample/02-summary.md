# Plot Variable sampling test (test-plan §11 16h, backend contract)

date: 2026-08-11 16:39:50   elf: C:\br1_rear_ecu\build\bin\br1_rear.elf   core: rtu0.0

## Phase A 10 Hz — target 10.0 Hz for 20.0 s (sent 400)
- **watch-TEST_cnt** (uint @ 0x317BA1D0): {"samples": 200, "achieved_hz": 10.0, "min": 66, "max": 967, "rising_fraction": 0.899, "wraps": 20, "saw_period_s": 1.0}
- **globals-TEST_cnt** (uint @ 0x317BABD0): {"samples": 200, "achieved_hz": 10.0, "min": 874, "max": 6048, "rising_fraction": 1.0, "wraps": 0, "saw_period_s": null}

## Phase B 25 Hz — target 25.0 Hz for 10.0 s (sent 500)
- **watch-TEST_cnt** (uint @ 0x317BA1D0): {"samples": 250, "achieved_hz": 24.97, "min": 26, "max": 994, "rising_fraction": 0.96, "wraps": 10, "saw_period_s": 1.0}
- **globals-TEST_cnt** (uint @ 0x317BABD0): {"samples": 250, "achieved_hz": 24.97, "min": 6074, "max": 8674, "rising_fraction": 0.402, "wraps": 0, "saw_period_s": null}

## Phase C recovery — target 10.0 Hz for 5.0 s (sent 100)
- **watch-TEST_cnt** (uint @ 0x317BA1D0): {"samples": 49, "achieved_hz": 10.0, "min": 0, "max": 900, "rising_fraction": 0.896, "wraps": 5, "saw_period_s": 1.0}
- **globals-TEST_cnt** (uint @ 0x317BABD0): {"samples": 49, "achieved_hz": 10.0, "min": 8752, "max": 10000, "rising_fraction": 0.625, "wraps": 0, "saw_period_s": null}

- watch-TEST_cnt: 499 samples total, 0 null ticks, 1 stale-handle recoveries -> samples-watch-TEST_cnt.csv
- globals-TEST_cnt: 499 samples total, 0 null ticks, 1 stale-handle recoveries -> samples-globals-TEST_cnt.csv
- waveform: waveform.svg
