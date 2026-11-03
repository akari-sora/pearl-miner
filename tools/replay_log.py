#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Replay a pearl_submit.py run log in (scaled) real time, with identifying values redacted.

The pause before each line is the original gap between the two lines, divided by --speed. Gaps longer than
--max-gap seconds of log time are cut short and marked, so a long wait before the opening does not stall the replay.

Redaction (always on; idempotent, so an already-redacted log replays unchanged). Examples with made-up values:
  64-hex values (tx hashes)              0xdeadbeef00...cafe  ->  0xdead…████   (--no-prefix: 0x████…████)
  other hex values of 8+ digits          0x0123456789ab       ->  0x████…████
  40-hex addresses (wallets, contracts)  0x0000...beef        ->  0x████…████
  block numbers                          block 100000000      ->  block B+0     (relative to the first one seen)
"0x" may also be "0X" and may follow any non-hex character (tx_0x…, "0x…").

--anonymize goes further, for logs you publish:
  - every hex value of 4+ digits after 0x is masked completely, also cut-off ones and the 4-digit prefixes left by
    a plain redaction; bare hex or digit runs of 40+ characters are masked too;
  - clock times become the time since the opening (`T+3.47s`; the opening is the first "chain is on day N: open"
    line); block numbers are removed;
  - BNB amounts (any 0.xxxx value, and larger values on lines about BNB, fees, balance, budget or spending) are
    rounded to one significant digit (`~0.00005`, `~2`);
  - the handle in `keccak256('…')` and the local date and time printed by `check` (which show the time zone) are
    masked, and so are other block numbers (`(block N)`, `fork_block=N`, any 9-digit number), `check`'s block
    timestamp (`ts N`) and e-mail addresses.
This hides the values in the log, not the run itself: the opening time is public (startsAt + N*86400), so the
T+ times, the number of transactions and their order can still be matched against the public chain by someone
who looks. Do not publish a log if your wallet must stay unlinkable.

  python3 tools/replay_log.py demo/day1-mainnet.log --speed 1.0
  python3 tools/replay_log.py run.log --anonymize --redact-only > run.public.log
"""
import argparse
import math
import re
import sys
import time

CLOCK = re.compile(r"^(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?\b")
REL = re.compile(r"^T([+-]\d+(?:\.\d+)?)s\b")
ADDR = re.compile(r"(?<![0-9a-fA-F])0[xX][0-9a-fA-F]{40}(?![0-9a-fA-F])")
HEX = re.compile(r"(?<![0-9a-fA-F])0[xX]([0-9a-fA-F]{8,})")
ANY_HEX = re.compile(r"(?<![0-9a-fA-F])0[xX][0-9a-fA-F]{4,}")  # --anonymize: cut-off values and 4-digit prefixes
BARE_HEX = re.compile(r"(?<![0-9a-zA-Z])[0-9a-fA-F]{40,}(?![0-9a-zA-Z])")
HANDLE = re.compile(r"keccak256\((['\"]).*?\1\)")
LOCAL_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)? local\b")
BLOCK = re.compile(r"\bblock (\d{5,})\b")
BLOCK_ANY = re.compile(r" block (?:\d{5,}|B[+-]\d+)\b")
BLOCK_WORD = re.compile(r"\b(block|fork_block|blockNumber|chain head[^:\n]{0,40}:)([ =:]*)\d{5,}\b")  # (block N), x=N
NINE_DIGITS = re.compile(r"(?<![\d.])\d{9}(?![\d.])")  # BSC block numbers (unix times have 10 digits)
TS = re.compile(r"\bts \d{10}\b")  # `check`: block timestamp = when it ran
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
AMOUNT = re.compile(r"(?<![\d.~])0\.\d{4,}\b")  # BNB amounts in run logs: 0.0000452, 0.049881, ...
BIG_AMOUNT = re.compile(r"(?<![\d.~])(?<!unix )[1-9]\d{0,5}\.\d{4,}\b")  # 1.234567 BNB; BNB lines only, not times
BNB_LINE = re.compile(r"BNB|\bfees?\b|\bbalance\b|\bbudget\b|\bspent\b", re.I)
OPEN = re.compile(r"chain is on day \d+: open")
MASK = "████"


def clock_seconds(line):
    m = CLOCK.match(line)
    if not m:
        return None
    h, mi, s, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + (int(frac) / 10 ** len(frac) if frac else 0.0)


def seconds(line):
    """Log time of a line (clock or T±s form), or None."""
    m = REL.match(line)
    if m:
        return float(m.group(1))
    return clock_seconds(line)


def one_sig(m):
    v = float(m.group(0))
    if v == 0:
        return "~0"
    digits = -int(math.floor(math.log10(v)))  # negative for v >= 10: 12.3 -> 10
    return "~" + f"{round(v, digits):.{max(digits, 0)}f}"


class Redactor:
    def __init__(self, keep_prefix=True, anonymize=False, zero=None):
        self.keep_prefix = keep_prefix and not anonymize
        self.anonymize = anonymize
        self.zero = zero  # clock seconds of the opening line (anonymize)
        self.first_block = None

    def _block(self, m):
        n = int(m.group(1))
        if self.first_block is None:
            self.first_block = n
        return f"block B{n - self.first_block:+d}"

    def __call__(self, line):
        line = ADDR.sub("0x" + MASK + "…" + MASK, line)
        # only a 64-digit value (a tx hash) keeps a prefix: anything else may be a cut-off address
        line = HEX.sub(lambda m: "0x" + (m.group(1)[:4] if self.keep_prefix and len(m.group(1)) == 64 else MASK)
                       + "…" + MASK, line)
        if not self.anonymize:
            return BLOCK.sub(self._block, line)
        line = ANY_HEX.sub("0x" + MASK, line)
        line = BARE_HEX.sub(MASK + "…" + MASK, line)
        line = HANDLE.sub("keccak256('" + MASK + "')", line)
        line = LOCAL_TIME.sub(MASK + " local", line)
        line = BLOCK_ANY.sub("", line)
        line = BLOCK_WORD.sub(lambda m: m.group(1) + (m.group(2) or " ") + MASK, line)
        line = NINE_DIGITS.sub(MASK, line)
        line = TS.sub("ts " + MASK, line)
        line = EMAIL.sub(MASK + "@" + MASK, line)
        line = AMOUNT.sub(one_sig, line)
        if BNB_LINE.search(line):
            line = BIG_AMOUNT.sub(one_sig, line)
        t = clock_seconds(line)
        if t is not None and self.zero is not None:
            rel = (t - self.zero + 43200) % 86400 - 43200  # nearest, across midnight
            line = f"T{rel:+.2f}s" + line[CLOCK.match(line).end():]
        return line


STYLES = [  # first match wins
    (re.compile(r"\bSTOP\b|FAIL|reverted [1-9]|status 0\b"), "\033[1;31m"),
    (re.compile(r"\bSUMMARY\b|\bPASS\b"), "\033[1;32m"),
    (re.compile(r"\bbroadcast\b"), "\033[36m"),
    (re.compile(r"\breceipt\b"), "\033[32m"),
    (re.compile(r"\bopen\b"), "\033[1;33m"),
    (re.compile(r"\bstatus:"), "\033[2m"),
]


def paint(line, color):
    if not color:
        return line
    for rx, code in STYLES:
        if rx.search(line):
            return code + line + "\033[0m"
    return line


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("log")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed (2.0 = twice as fast)")
    ap.add_argument("--max-gap", type=float, default=5.0, help="cap any gap to this many seconds of log time")
    ap.add_argument("--no-prefix", action="store_true", help="redact hashes completely")
    ap.add_argument("--anonymize", action="store_true",
                    help="no hash prefix, time since the opening instead of clock time, no block numbers, "
                         "rounded BNB amounts")
    ap.add_argument("--redact-only", action="store_true", help="print the redacted log at once, no timing")
    ap.add_argument("--title", default=None, help="print this banner first")
    ap.add_argument("--note", default=None, help="a dim second banner line")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    a = ap.parse_args()
    if a.speed <= 0 or a.max_gap < 0:
        raise SystemExit("--speed must be > 0 and --max-gap >= 0")
    color = a.color == "always" or (a.color == "auto" and sys.stdout.isatty())
    with open(a.log, encoding="utf-8", errors="replace") as f:
        lines = [ln.rstrip("\n") for ln in f]
    zero = None
    if a.anonymize:
        zero = next((clock_seconds(ln) for ln in lines if OPEN.search(ln) and clock_seconds(ln) is not None), None)
        if zero is None and any(clock_seconds(ln) is not None for ln in lines):
            raise SystemExit("--anonymize: no 'chain is on day N: open' line to measure time from")
    red = Redactor(keep_prefix=not a.no_prefix, anonymize=a.anonymize, zero=zero)
    if a.redact_only:
        for ln in lines:
            print(red(ln))
        return
    if a.title:
        print(("\033[1m" + a.title + "\033[0m") if color else a.title)
        if a.note:
            print(("\033[2m" + a.note + "\033[0m") if color else a.note)
        print()
        sys.stdout.flush()
    prev = None
    for ln in lines:
        t = seconds(ln)
        if t is not None and prev is not None:
            gap = (t - prev) % 86400  # a clock-time log may run past midnight
            if gap > a.max_gap:
                skipped = f"   ⋯ {gap:,.0f} s of waiting skipped ⋯"
                print(("\033[2m" + skipped + "\033[0m") if color else skipped, flush=True)
                gap = a.max_gap
            time.sleep(gap / a.speed)
        if t is not None:
            prev = t
        print(paint(red(ln), color), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
