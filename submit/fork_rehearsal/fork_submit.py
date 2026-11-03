#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""pearl_submit.py as a child process of pearl.py during `run_fork.py --autopilot`.

The same pearl_submit, with the two changes run_fork.py makes in-process: its RPC list is the local fork only, and
its day clock is moved so the target day opens a minute after the rehearsal starts. run_fork.py sets
PEARL_SUBMIT_SCRIPT to this file and passes the values in the environment:
  PEARL_FORK_RPC    http://127.0.0.1:PORT (anything else is refused)
  PEARL_FORK_START  the moved startsAt (unix seconds)
  PEARL_FORK_T0     optional: print times as T±s from this unix time (--anonymize)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pearl_submit as ps  # noqa: E402

rpc = os.environ.get("PEARL_FORK_RPC", "")
if not rpc.startswith("http://127.0.0.1:"):
    sys.exit(f"fork_submit.py: PEARL_FORK_RPC must be the local fork (http://127.0.0.1:PORT), got {rpc!r}")
ps.RPCS[:] = [rpc]
ps.START = float(os.environ["PEARL_FORK_START"])
if os.environ.get("PEARL_FORK_T0"):
    t0 = float(os.environ["PEARL_FORK_T0"])
    ps.now = lambda: f"T{time.time() - t0:+.2f}s"
ps.main()
