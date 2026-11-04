#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Offline checks for miner/autotune.py (no GPU, standard library only).

  python3 miner/test_autotune.py
"""
import os
import subprocess
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

# miner.py on a fresh clone without its packages: one line saying what to install, no traceback. Each case runs
# miner.py in a Python of its own where one package is missing or fails to load (an import hook says so whether
# or not it is installed here); the packages imported before it are stand-ins.
MINER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miner.py")
CASE = r"""
import runpy, sys, types
fake, block, kind = sys.argv[1].split(","), sys.argv[2], sys.argv[3]
broken = kind == "broken"
for m in filter(None, fake):
    sys.modules[m] = types.ModuleType(m)
class Hook:
    def find_spec(self, name, path=None, target=None):
        if name == block and broken:
            raise ImportError("libOpenCL.so.1: cannot open shared object file", name=block + "._cl")
        if name == block and kind.startswith("sub:"):  # the package is there, a module inside it is not
            raise ModuleNotFoundError(f"No module named {kind[4:]!r}", name=kind[4:])
        if name == block:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
for m in list(sys.modules):
    if m.split(".")[0] == block:
        del sys.modules[m]
sys.meta_path.insert(0, Hook())
sys.argv = [sys.argv[4], "--help"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
for fake, block, kind, want in ((",", "numpy", "missing", "numpy is not installed"),
                                ("numpy", "pyopencl", "missing", "pyopencl is not installed"),
                                ("numpy,pyopencl", "Crypto", "missing", "pycryptodome is not installed"),
                                ("numpy", "pyopencl", "broken", "pyopencl could not be loaded"),
                                ("numpy", "pyopencl", "sub:pyopencl._cl", "pyopencl is not installed"),
                                ("numpy,pyopencl", "Crypto", "sub:Crypto.Hash", "pycryptodome is not installed")):
    p = subprocess.run([sys.executable, "-I", "-c", CASE, fake, block, kind, MINER], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    err = p.stderr.decode("utf-8", "replace").strip()
    expect(f"miner.py without {block} ({kind}): exit 1, one line naming the package and the pip command, no traceback",
           p.returncode == 1 and want in err and "-m pip install -r " in err and "requirements.txt" in err
           and "\n" not in err and "Traceback" not in err, (p.returncode, err[-300:]))

print("FAILED:", FAILED or "none")
sys.exit(1 if FAILED else 0)
