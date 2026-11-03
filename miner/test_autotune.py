#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Offline checks for miner/autotune.py (no GPU, standard library only).

  python3 miner/test_autotune.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from autotune import MAX_GROWTH, MAX_ITERS, next_launch  # noqa: E402

FAILED = []
TARGET, UNIT, GSZ = 0.25, 256, 1 << 22


def expect(name, cond, got=None):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond or got is None else f"   got: {got!r}"))
    if not cond:
        FAILED.append(name)


def run(rate, steps=30, clock=None):
    """Simulate a device doing `rate` hashes/s; clock(true_dt, step) is what the timer reports. Returns the launches."""
    gsz, iters, out = GSZ, 1, []
    for k in range(steps):
        true_dt = gsz * iters / rate
        out.append((gsz, iters, true_dt))
        dt = clock(true_dt, k) if clock else true_dt
        gsz, iters = next_launch(gsz, iters, dt, TARGET, UNIT)
    return out


# a timer that reports 0 or a negative time (coarse clock, clock stepped back): the work never grows
for name, dt in (("0", 0.0), ("negative", -0.5), ("1 microsecond", 1e-6)):
    g, i = GSZ, 1
    for _ in range(50):
        g, i = next_launch(g, i, dt, TARGET, UNIT)
    expect(f"dt {name} fifty times: iters stays 1", (g, i) == (GSZ, 1), (g, i))

# the review's case: first launch measured as 0 on a 2 GH/s GPU, then measured right: no launch near the 2 s TDR
launches = run(2.1e9, clock=lambda t, k: 0.0 if k == 0 else t)
worst = max(t for _, _, t in launches)
expect("RTX-class GPU, first launch measured 0: every launch < 0.5 s", worst < 0.5, round(worst, 3))

for rate, label in ((2.1e9, "2.1 GH/s"), (31e6, "31 MH/s iGPU"), (10e9, "10 GH/s"), (680e6, "680 MH/s")):
    launches = run(rate)
    worst = max(t for _, _, t in launches)
    last = launches[-1][2]
    grow = max(b[1] / a[1] for a, b in zip(launches, launches[1:]))
    expect(f"{label}: never above 1 s, settles at 100-350 ms, grows at most x{MAX_GROWTH} a step",
           worst < 1.0 and 0.1 < last < 0.35 and grow <= MAX_GROWTH, (round(worst, 3), round(last, 3), grow))

# a slow device: one hash per work-item is already too slow, so the global size shrinks (in whole work-groups)
launches = run(2e6)  # 4M items at 2 MH/s = 2.1 s
expect("2 MH/s: global size halves until a launch fits, stays a multiple of the work-group size",
       launches[-1][2] < 0.4 and all(g % UNIT == 0 for g, _, _ in launches), launches[-1])

# a launch that took far too long (first-launch compile, a busy GPU) shrinks the work at once
g, i = next_launch(GSZ, 64, 3.0, TARGET, UNIT)
expect("a 3 s launch at iters 64: next launch about 250 ms", (g, i) == (GSZ, 5), (g, i))
expect("never above MAX_ITERS", next_launch(GSZ, MAX_ITERS, 0.001, TARGET, UNIT)[1] == MAX_ITERS)

print("FAILED:", FAILED or "none")
sys.exit(1 if FAILED else 0)
