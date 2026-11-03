#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Offline checks for replay_log.py redaction. All values below are made up.

  python3 tools/test_replay_log.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from replay_log import Redactor, clock_seconds, safe_stdio  # noqa: E402

safe_stdio()

FAILED = []
H64 = "deadbeef" * 8                      # a made-up 64-hex tx hash
A40 = "00000000000000000000000000000000DeaDBeef"  # a made-up address
LEAK = re.compile(r"0[xX][0-9a-fA-F]{8,}|[0-9a-fA-F]{40,}")


def expect(name, cond, got=None):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond or got is None else f"   got: {got!r}"))
    if not cond:
        FAILED.append(name)


# ---- default redaction (replay): prefixes kept, blocks relative
red = Redactor()
out = red(f"12:00:01.000 tx#1 broadcast 0x{H64} (16 proofs) from 0x{A40} block 100000123")
expect("default: hash keeps a 4-digit prefix", "0xdead…████" in out, out)
expect("default: address fully masked", "0x████…████ block" in out and "eef" not in out.split("from")[1], out)
expect("default: block made relative", out.endswith("block B+0"), out)

# ---- --anonymize
zero = clock_seconds("12:00:00.000")
an = Redactor(anonymize=True, zero=zero)
cases = {
    "tx_0x prefix": f"12:00:01.000 receipt tx_0x{H64} status 1",
    "0X upper case": f"12:00:01.000 from 0X{A40}",
    "letter before 0x": f"12:00:01.000 X0x{H64}",
    "quoted JSON": f'12:00:01.000 baw preview: {{"to": "0x{A40}", "data": "0x{H64}"}}',
    "cut-off value (7 hex)": '12:00:01.000 baw execute: {"txHash": "0x1234567',
    "already prefix-redacted": "12:00:01.000 tx#1 broadcast 0xbeef…████ (16 proofs)",
    "bare 64 hex": f"12:00:01.000 data {H64} end",
    "bare 77-digit agentId": "12:00:01.000 id " + "12345678901" * 7,
}
for name, line in cases.items():
    o = an(line)
    expect(f"anonymize masks: {name}", not LEAK.search(o) and "1234567" not in o and "beef" not in o, o)

o = an("12:00:00.500 wallet 0x" + A40 + "; agentId = keccak256('somebody') = 0x" + H64)
expect("anonymize masks the handle in keccak256('…')", "somebody" not in o and "keccak256('████')" in o, o)
o = an("12:00:00.500 next day opens at unix 1900000000 = 2030-03-17 09:46:40 local, in 80000s")
expect("anonymize masks the local date and time (time zone)", "09:46:40" not in o and "2030-03-17" not in o and "████ local" in o, o)
o = an("12:00:02.250 receipt 0x" + H64 + " block 100000124 status 1 mined 16 fee 0.0000456")
expect("anonymize: clock -> T+, block removed, fee rounded",
       o.startswith("T+2.25s") and "block" not in o and "fee ~0.00005" in o, o)
o = an("12:00:00.100 run: 400 usable proofs; balance 1.234567 BNB, fees so far 12.345678, effective budget 0.044889")
expect("anonymize rounds BNB amounts >= 1", "balance ~1 BNB" in o and "fees so far ~10" in o and "~0.04" in o, o)
o = an("12:00:00.150 run: 400 usable proofs; balance 0.049889 BNB; day 2 opens unix 1900000123.456789 (29s from now)")
expect("anonymize keeps the (public) opening time on a BNB line", "unix 1900000123.456789" in o and "~0.05 BNB" in o, o)
o = an("12:00:00.200 BNB balance 3.141593")
expect("anonymize rounds the check's BNB balance", o.endswith("BNB balance ~3"), o)
for keep in ("min reward per proof for day 2: 86.2400", "miningDay=1 dailyRemaining=9120.00 currentReward=99.5345",
             "chain is on day 1: open (remaining 9120, reward 95.0000)"):
    o = an("12:00:00.000 " + keep)
    expect(f"anonymize keeps public reward numbers: {keep[:28]}…", o.endswith(keep), o)

for name, line, gone in (
        ("(block N)", "12:00:01.000 receipt seen (block 123456789) status 1", "123456789"),
        ("fork_block=N", "12:00:01.000 exit=0 wall=99s fork_block=123456789 journal=j", "123456789"),
        ("chain head …: N", "12:00:01.000 execute 1.9s, chain head right after it returned: 123456789, x", "123456789"),
        ("a bare 9-digit number", "12:00:01.000 nonce probe at 123456789", "123456789"),
        ("check's block timestamp", "12:00:01.000 chain block 123456789 ts 1900000000; local clock - block ts = +0.5s",
         "1900000000"),
        ("an e-mail address", '12:00:01.000 baw wallet status: {"email": "some.body+x@example.com"}', "some.body")):
    o = an(line)
    expect(f"anonymize masks: {name}", gone not in o and "████" in o, o)

# default redaction: only a 64-digit value (a tx hash) keeps its prefix; a cut-off address does not
o = red('12:00:01.000 baw execute: {"from": "0x' + A40[:30])
expect("default: a cut-off address keeps no prefix", "0x████…████" in o and "0000" not in o, o)
o = red("12:00:01.000 proof 0x" + "ab" * 40)
expect("default: an 80-hex value keeps no prefix", "0x████…████" in o and "abab" not in o, o)

o = an("11:45:48.480 day 1 opens unix 1900000851 (851s from now)")
expect("anonymize: time before the opening is negative", o.startswith("T-851.52s"), o)

# idempotent: an anonymized log anonymized again is unchanged
for name, line in list(cases.items()) + [("receipt with fee", "12:00:02.250 receipt 0x" + H64 + " fee 0.0000456")]:
    once = an(line)
    expect(f"idempotent: {name}", an(once) == once, (once, an(once)))

print("FAILED:", FAILED or "none")
sys.exit(1 if FAILED else 0)
