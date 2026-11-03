# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Launch sizing for miner.py: each kernel launch aims at a target time, so a GPU that drives a display never runs
one long enough for the Windows (TDR, 2 s) or macOS watchdog. Standard library only: miner/test_autotune.py checks it
without a GPU.

Measure dt with time.perf_counter(). time.time() can be 15.6 ms coarse (Windows before Python 3.13) or step back,
and a launch measured as 0 s would otherwise ask for thousands of times more work at once.
"""
MAX_ITERS = 4096    # hashes per work-item per launch, at most
MAX_GROWTH = 8      # at most this many times more work than the launch just measured
MIN_DT = 1e-4       # s: a launch measured shorter than this (or <= 0) says nothing about speed: never grow on it


def next_launch(gsz, iters, dt, target, unit):
    """(global size, iters) for the next launch, after one of gsz work-items x iters took dt seconds."""
    if dt > 1.5 * target and iters == 1 and gsz > unit:  # even one hash per work-item is too slow: fewer items
        return max(unit, gsz // 2 // unit * unit), iters
    if dt > 1.25 * target:
        return gsz, max(1, int(iters * target / dt))
    if MIN_DT <= dt < 0.8 * target:
        return gsz, max(1, min(MAX_ITERS, iters * MAX_GROWTH, int(iters * target / dt)))
    return gsz, iters
