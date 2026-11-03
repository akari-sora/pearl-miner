#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""One command for a mining day: check, show the plan, submit, then sell part of what this run mined.

  python3 pearl.py auto      check -> plan -> you type yes -> submit -> sell (see --help)
  python3 pearl.py auto --plan    check and show the plan only
  python3 pearl.py status    is a run going? the end of run.log and sell.log

Needs PEARL_WALLET and PEARL_HANDLE in the environment and `baw` signed in (docs/QUICKSTART.md).
Standard library only. The scripts in submit/ run as child processes:
  submit/export_proofs.py   only when submit/proofs.json is missing and miner/proofs.csv exists
  submit/pearl_submit.py    check, then run (output in submit/run.log, its process id in submit/run.pid)
  submit/pearl_sell.py      run --keep-coins K, after the submitter has exited (output in submit/sell.log)
For tests and the fork rehearsal, PEARL_SUBMIT_SCRIPT, PEARL_SELL_SCRIPT and PEARL_EXPORT_SCRIPT replace those paths.

Exit codes: 0 done; 1 refused before anything was sent; 3 the run did not end with a SUMMARY line;
4 the run ended but the sale was skipped or failed. Every non-zero exit prints a one-line reason.
"""
import argparse
import codecs
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time
from decimal import Decimal

ROOT = os.path.dirname(os.path.abspath(__file__))
SUBMIT = os.path.join(ROOT, "submit")
if SUBMIT not in sys.path:
    sys.path.insert(0, SUBMIT)  # pearl_submit / pearl_sell are imported when first needed

EXIT_OK, EXIT_REFUSED, EXIT_RUN, EXIT_SALE = 0, 1, 3, 4
SCRIPTS = {"submit": ("PEARL_SUBMIT_SCRIPT", "pearl_submit.py"),
           "sell": ("PEARL_SELL_SCRIPT", "pearl_sell.py"),
           "export": ("PEARL_EXPORT_SCRIPT", "export_proofs.py")}
SUMMARY_RE = re.compile(r"SUMMARY txs=(\d+) proofs_mined=(\d+) reverted=(\d+) fees=([0-9.]+) BNB stop=(.*)$")
SOLD_RE = re.compile(r"end balance ([0-9,.]+) \S+ \(sold (-?[0-9,.]+)\); BNB from finished orders ≈ (\S+)")
CHECK_OK_RE = re.compile(r"\bCHECK OK\s*$")
PROBLEM_RES = [re.compile(p) for p in (
    r"matches the wallet: False", r"wallet session: (?!CONNECTED\b)", r'"success": false',
    r"owner burned \(nobody can pause/resume\): False", r"paused now: True", r"no proof is left",
    r"used on chain: copy", r"from wallet now: (?!ok\b|revert DailyLimitReached\b)",
    r"Traceback|Error\b|not found|refus|^set the ")]
DECIMALS = 8
TAIL_S = 0.2          # how often the log is read while a child runs
BALANCE_WAIT = 5.0    # seconds between token balance reads after the run
read_answer = input   # replaced in tests
sleep = time.sleep    # replaced in tests


class Refusal(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


def out(*a):
    print(*a, flush=True)


def mods():
    import pearl_submit
    import pearl_sell
    return pearl_submit, pearl_sell


def token_balance():
    """Raw (8-decimal) token balance of PEARL_WALLET; RuntimeError if no RPC answers. Replaced in tests."""
    return mods()[1].token_balance()


# ---------------------------------------------------------------- files and processes
class Paths:
    def __init__(self, d, csv):
        j = os.path.join
        self.dir, self.csv = d, csv
        self.proofs = j(d, "proofs.json")
        self.journal, self.stop = j(d, "journal.jsonl"), j(d, "STOP")
        self.run_log, self.run_pid = j(d, "run.log"), j(d, "run.pid")
        self.sell_log, self.sell_journal = j(d, "sell.log"), j(d, "sell.jsonl")


def short(path):
    try:
        rel = os.path.relpath(path, ROOT)
    except ValueError:  # another drive on Windows
        return path
    return path if rel.startswith("..") else rel


def child_cmd(name, *args):
    env_var, default = SCRIPTS[name]
    return [sys.executable, "-u", os.environ.get(env_var) or os.path.join(SUBMIT, default), *map(str, args)]


def child_env():
    return dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")


def read_pid(path):
    """The process id in a pid file, for display; None if missing or unreadable."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return int(f.read().strip())
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def run_alive(pid_path):
    """True while the process in pid_path may still run. This is pearl_sell.mining_run_alive, the same check the
    seller makes (safe on Windows; a missing or unreadable file counts as alive)."""
    return mods()[1].mining_run_alive(pid_path)


def journal_records(path):
    recs = []
    if os.path.exists(path):  # read only: never open the journal for writing here
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    recs.append(r)
    return recs


def run_captured(cmd):
    """Run a short child, print its output as it comes; (exit code, lines)."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=child_env(), text=True, encoding="utf-8", errors="replace")
    lines = []
    for line in p.stdout:
        line = line.rstrip("\r\n")
        lines.append(line)
        out(line)
    return p.wait(), lines


def stream_child(cmd, log_path, pid_path=None):
    """Start `cmd` with its output appended to log_path, write its process id to pid_path, and copy the new part
    of the log to the console until it exits. Ctrl-C asks the child to stop (it finishes what is in flight);
    a second Ctrl-C leaves it running in the background. Returns (exit code, lines of this run)."""
    if pid_path and os.path.exists(pid_path):
        os.remove(pid_path)  # missing counts as "alive" for the seller until the new id is written
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "ab") as lf:
        lf.write(f"==== pearl.py {stamp}: {os.path.basename(cmd[2])} {' '.join(cmd[3:4])} ====\n".encode())
        lf.flush()
        start = lf.tell()
        kw = {"start_new_session": True} if os.name == "posix" else {}  # survives a closed terminal, like nohup
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=child_env(), **kw)
    if pid_path:
        tmp = pid_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"{p.pid}\n")
        os.replace(tmp, pid_path)
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    buf, lines, interrupts = "", [], 0

    def take(text):
        nonlocal buf
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
            buf += text
            *done, buf = buf.split("\n")
            lines.extend(x.rstrip("\r") for x in done)
    with open(log_path, "rb") as f:
        f.seek(start)
        while True:
            try:
                chunk = f.read()
                if chunk:
                    take(dec.decode(chunk))
                    continue
                if p.poll() is not None:
                    take(dec.decode(f.read(), final=True))
                    break
                sleep(TAIL_S)
            except KeyboardInterrupt:
                interrupts += 1
                if interrupts == 1:
                    if os.name == "posix":  # the child is in its own session: pass the Ctrl-C on
                        p.send_signal(signal.SIGINT)
                    out(f"\nCtrl-C: asking {os.path.basename(cmd[2])} to stop; it finishes what is in flight. "
                        "Press Ctrl-C again to leave it running in the background.")
                else:
                    raise Refusal(EXIT_RUN, f"left {os.path.basename(cmd[2])} (pid {p.pid}) running in the "
                                            f"background; nothing more is done. See: python3 pearl.py status")
    if buf:
        lines.append(buf)
        out()
    return p.returncode, lines, interrupts


# ---------------------------------------------------------------- pieces of the plan
def keep_raw(before, after, fraction):
    """Raw units to keep: before + (1 - fraction) * (after - before), rounded so that at most `fraction` of the
    new coins is sold (never more)."""
    new = after - before
    return after - int(Decimal(new) * Decimal(str(fraction)))  # int() truncates: sells at most the fraction


def coins_arg(raw):
    return f"{raw // 10 ** DECIMALS}.{raw % 10 ** DECIMALS:0{DECIMALS}d}"


def coins(raw):
    return f"{raw / 10 ** DECIMALS:,.2f}"


def when(t):
    """UTC and local time; the "... local" form is what tools/replay_log.py --anonymize masks."""
    utc = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    return f"{utc:%Y-%m-%d %H:%M:%S} UTC = {datetime.datetime.fromtimestamp(t):%Y-%m-%d %H:%M:%S} local"


def until(t):
    d = int(t - time.time())
    span = f"{abs(d) // 3600} h {abs(d) % 3600 // 60} min" if abs(d) >= 3600 else f"{abs(d) // 60} min {abs(d) % 60} s"
    return f"in {span}" if d >= 0 else f"opened {span} ago, so the run starts at once"


def problem_lines(lines):
    bad = [x for x in lines if any(r.search(x) for r in PROBLEM_RES)]
    return bad or [x for x in lines if x.strip()][-5:]


def sell_text(a):
    pct = f"{a.sell_fraction:.0%}"
    if a.sell == "off":
        return ["off: nothing is sold; every coin stays in the wallet."]
    how = ("now: right after the run ends, in chunks of up to 100k coins; a chunk over 2% price impact is halved "
           "or waits." if a.sell == "now" else
           "trail: after the run, watch the price for up to 120 minutes and sell on a 7% pullback from its high or "
           "when sales fall behind schedule.")
    return [how, f"Sells {pct} of the coins THIS run mines; the other {1 - a.sell_fraction:.0%} and every coin you "
                 "held before the run stay in the wallet."]


# ---------------------------------------------------------------- auto
def cmd_auto(a):
    P = Paths(a.dir, a.csv)
    ps, sell_mod = mods()
    wallet, handle = os.environ.get("PEARL_WALLET"), os.environ.get("PEARL_HANDLE")
    if not wallet:
        raise Refusal(EXIT_REFUSED, "PEARL_WALLET is not set (your Agentic Wallet address, 0x + 40 hex digits)")
    why = ps.wallet_problem(wallet)
    if why:
        raise Refusal(EXIT_REFUSED, f"PEARL_WALLET: {why}")
    if not handle:
        raise Refusal(EXIT_REFUSED, "PEARL_HANDLE is not set (your raw Agent ID handle, a-z and 0-9, without .ai)")
    why = ps.handle_problem(handle)
    if why:
        raise Refusal(EXIT_REFUSED, f"PEARL_HANDLE: {why}")
    ps.configure(wallet, handle)
    ps.BAW = ps.find_baw()  # as ps.main() does; pearl.py itself calls no baw function, its children do
    sell_mod.WALLET = wallet
    if not (0 < a.sell_fraction <= 1 and a.budget_bnb > 0 and a.keep_bnb >= 0 and a.minutes > 0):
        raise Refusal(EXIT_REFUSED, "bad arguments: --sell-fraction must be in (0, 1], --budget-bnb and --minutes "
                                    "above 0, --keep-bnb at least 0")
    if os.path.exists(P.stop):
        raise Refusal(EXIT_REFUSED, f"{short(P.stop)} exists (it stops the run): delete it, then start again")
    if os.path.exists(P.run_pid) and run_alive(P.run_pid):
        raise Refusal(EXIT_REFUSED, f"a run is already going (pid {read_pid(P.run_pid)} in {short(P.run_pid)}); see: "
                                    "python3 pearl.py status. If none is, delete that file")

    # 1. preflight: proofs, day, budget, check
    if not os.path.exists(P.proofs):
        if not os.path.exists(P.csv):
            raise Refusal(EXIT_REFUSED, f"no proofs: neither {short(P.proofs)} nor {short(P.csv)} exists; "
                                        "mine first: python3 miner/miner.py --out miner/proofs.csv")
        out(f"{short(P.proofs)} is missing: exporting {short(P.csv)} (every proof is re-hashed)")
        rc, _ = run_captured(child_cmd("export", P.csv, "--out", P.proofs))
        if rc != 0 or not os.path.exists(P.proofs):
            raise Refusal(EXIT_REFUSED, f"export_proofs.py failed (exit {rc}); see the lines above")
    elif os.path.exists(P.csv) and os.path.getmtime(P.csv) > os.path.getmtime(P.proofs):
        out(f"note: {short(P.csv)} is newer than {short(P.proofs)}. To use the new proofs, stop the miner and run "
            f"python3 submit/export_proofs.py {short(P.csv)}")
    try:
        proofs = ps.load_proofs(P.proofs)
    except (AssertionError, OSError, ValueError, KeyError, TypeError) as e:
        raise Refusal(EXIT_REFUSED, f"{short(P.proofs)} is unusable: {e or type(e).__name__}")
    blocked, fees0, reserved0, _, _ = ps.blocked_from_journal(journal_records(P.journal))
    usable = [p for _, p in proofs if p not in blocked]
    if not usable:
        raise Refusal(EXIT_REFUSED, f"all {len(proofs)} proofs in {short(P.proofs)} were already sent "
                                    f"(see {short(P.journal)}): mine new ones")
    ns = argparse.Namespace(day=a.day, minutes=a.minutes)
    try:
        ps.resolve_day(ns)
    except SystemExit as e:
        raise Refusal(EXIT_REFUSED, str(e))
    day, opens = ns.day, ps.START + 86400 * ns.day
    left = int(a.budget_bnb * 1e18) - fees0 - reserved0
    if left <= 0:
        raise Refusal(EXIT_REFUSED, f"the gas budget --budget-bnb {a.budget_bnb:g} is already used by earlier runs in "
                                    f"{short(P.journal)} ({(fees0 + reserved0) / 1e18:.6f} BNB): pass a higher one")

    out("==== check (read-only) ====")  # check skips the proofs the journal shows as sent, like run
    rc, lines = run_captured(child_cmd("submit", "check", "--proofs", P.proofs, "--journal", P.journal))
    if not any(CHECK_OK_RE.search(x) for x in lines):
        out("\ncheck found problems:")
        for x in problem_lines(lines):
            out("  " + x)
        raise Refusal(EXIT_REFUSED, f"check did not end with CHECK OK (exit {rc}); fix the lines above and start again")
    warnings = [x[x.index("WARNING:"):] for x in lines if "WARNING:" in x]

    # 2. the plan
    out("\n==== the plan ====")
    out(f"Wallet  {wallet[:10]}…, Agent ID keccak256('{handle}')")  # forms that --anonymize masks
    out(f"Day     {day}: opens {when(opens)}, {until(opens)}.")
    out(f"        The run waits for the opening, then stops when the day's cap is full, the gas budget is spent,")
    out(f"        {a.minutes:g} minutes after the opening, or when {short(P.stop)} exists.")
    out(f"Proofs  {len(usable):,} usable ({len(proofs):,} in {short(P.proofs)}, "
        f"{len(proofs) - len(usable):,} already sent according to {short(P.journal)}).")
    out(f"Gas     budget {a.budget_bnb:g} BNB of fees in total for {short(P.journal)} (every run that used it, not per "
        f"day);")
    out(f"        earlier runs used {(fees0 + reserved0) / 1e18:.6f}, so up to {left / 1e18:.6f} BNB for this run.")
    out(f"        Keep reserve {a.keep_bnb:g} BNB: the run stops before the wallet's BNB would fall below it.")
    for i, x in enumerate(sell_text(a)):
        out(("Sell    " if i == 0 else "        ") + x)
    if a.sell != "off":
        out("        On day 1 the price fell about 51% in the 14 minutes after the opening. Profit is not guaranteed.")
    for i, x in enumerate(warnings):
        out(("Check   " if i == 0 else "        ") + x)
    out("\nTyping yes approves this plan in advance: the mining transactions" +
        (" and the sales" if a.sell != "off" else "") + " go out through baw without asking you again.")
    out("Binance's external-sign flow expects you to confirm what is sent; this is that confirmation.")
    out("This does not mean Binance approves unattended use; driving a wallet with scripts may break its terms.")
    if a.plan:
        out("\n--plan: nothing was sent. Start for real with the same options, without --plan.")
        return EXIT_OK
    if a.yes:
        out("--yes given: the plan was decided beforehand, starting.")
    else:
        try:
            answer = read_answer("Type yes to start, anything else to cancel: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            raise Refusal(EXIT_REFUSED, "not confirmed (type yes, or pass --yes if you decided the plan beforehand); "
                                        "nothing was sent")

    # 3. submit
    before, sale_block = None, None
    if a.sell != "off":
        for i in range(3):
            try:
                before = token_balance()
                break
            except RuntimeError as e:
                out(f"token balance unreadable ({e}); retrying")
                sleep(2)
        if before is None:
            sale_block = "the token balance could not be read before the run"
            out(f"WARNING: {sale_block}: the sale will be skipped. Sell later with submit/pearl_sell.py.")
        else:
            out(f"token balance before the run: {coins(before)} coins (these are kept)")
    out(f"\n==== run: output also in {short(P.run_log)}, process id in {short(P.run_pid)}; "
        f"stop any time: create {short(P.stop)} or press Ctrl-C ====")
    rc, lines, interrupts = stream_child(
        child_cmd("submit", "run", "--day", day, "--budget-bnb", a.budget_bnb, "--keep-bnb", a.keep_bnb,
                  "--minutes", a.minutes, "--proofs", P.proofs, "--journal", P.journal, "--stop-file", P.stop),
        P.run_log, P.run_pid)
    m = next((m for m in map(SUMMARY_RE.search, reversed(lines)) if m), None)
    logs = f"logs: {short(P.run_log)}, {short(P.journal)}"
    if not m or rc != 0:
        last = next((x for x in reversed(lines) if x.strip()), "no output")
        what = "without a SUMMARY line" if not m else "with an error after its SUMMARY line"
        raise Refusal(EXIT_RUN, f"the run ended {what} (exit {rc}; last line: {last.strip()[:200]}); "
                                f"nothing is sold. {logs}")
    txs, mined, reverted = int(m.group(1)), int(m.group(2)), int(m.group(3))
    fees = max(0, int(Decimal(m.group(4)) * 10 ** 18) - fees0)
    run_line = (f"proofs mined {mined} in {txs} txs ({reverted} reverted), gas about {fees / 1e18:.6f} BNB, "
                f"stop reason: {m.group(5).strip()}")

    # 4. sell
    sold_line, code, reason = "off", EXIT_OK, None
    if a.sell != "off":
        if interrupts:
            sale_block = "you pressed Ctrl-C"
        elif os.path.exists(P.stop):
            sale_block = f"{short(P.stop)} exists"
        elif sale_block is None and mined == 0:
            sold_line = "nothing to sell: this run mined no proofs"
        elif sale_block is None:
            seen = []
            for _ in range(6):  # until two reads agree; the highest read counts, so a lagging RPC keeps more
                try:
                    b = token_balance()
                except RuntimeError as e:
                    out(f"token balance unreadable ({e})")
                    b = None
                if b is not None:
                    stable = bool(seen) and b == seen[-1]
                    seen.append(b)
                    if stable:
                        break
                sleep(BALANCE_WAIT)
            after = max(seen) if seen else None
            if after is None:
                sale_block = "the token balance could not be read after the run"
            elif after <= before:
                sold_line = f"nothing to sell: the balance did not grow ({coins(before)} -> {coins(after)})"
            else:
                k = keep_raw(before, after, a.sell_fraction)
                out(f"\n==== sell ({a.sell}): balance {coins(before)} -> {coins(after)}, +{coins(after - before)} "
                    f"this run; keeping {coins(k)}, selling up to {coins(after - k)}. Output also in "
                    f"{short(P.sell_log)} ====")
                src, slines, s_int = stream_child(
                    child_cmd("sell", "run", "--mode", a.sell, "--keep-coins", coins_arg(k), "--run-pid", P.run_pid,
                              "--journal", P.sell_journal), P.sell_log)
                sm = next((x for x in map(SOLD_RE.search, reversed(slines)) if x), None)
                sold_line = (f"sold {sm.group(2)} coins for about {sm.group(3)} BNB; {sm.group(1)} coins left"
                             if sm else "no end line from the seller")
                if src != 0 or s_int:
                    last = next((x for x in reversed(slines) if x.strip()), "no output")
                    code, reason = EXIT_SALE, f"the seller stopped (exit {src}): {last.strip()[:200]}"
        if sale_block:
            sold_line = f"skipped: {sale_block}"
            code, reason = EXIT_SALE, (f"sale skipped: {sale_block}. Sell later with: python3 submit/pearl_sell.py "
                                       "run --mode now --keep-coins N")
        logs += f", {short(P.sell_log)}, {short(P.sell_journal)}"

    # 5. summary
    out("\n==== summary ====")
    out(f"run   {run_line}")
    out(f"sale  {sold_line}")
    out(logs)
    if reason:
        raise Refusal(code, reason)
    return EXIT_OK


# ---------------------------------------------------------------- status
def tail_file(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return None


def cmd_status(a):
    P = Paths(a.dir, a.csv)
    if not os.path.exists(P.run_pid):
        out(f"{short(P.run_pid)}: none (no run started yet)")
    else:
        pid = read_pid(P.run_pid)
        out(f"{short(P.run_pid)}: pid {pid if pid is not None else '(unreadable)'}, "
            + ("RUNNING" if run_alive(P.run_pid) else "finished"))
    out(f"{short(P.stop)}: " + ("present (a new run refuses to start until you delete it)" if os.path.exists(P.stop)
                                else "absent"))
    for path in (P.run_log, P.sell_log):
        t = tail_file(path, a.lines)
        out(f"\n---- {short(path)}: " + ("no file yet" if t is None else f"last {len(t)} lines"))
        for x in t or []:
            out(x)
    return EXIT_OK


# ---------------------------------------------------------------- main
def main(argv=None):
    mods()[0].safe_stdio()  # a console that cannot show 珍珠币 must not crash the run
    ap = argparse.ArgumentParser(prog="pearl.py", description="One command for a mining day. See docs/QUICKSTART.md.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    au = sub.add_parser("auto", help="check, show the plan, wait for your yes, submit, then sell",
                        description="Check, show the plan, wait for you to type yes, submit through baw, then sell "
                                    "part of what this run mined. Typing yes (or --yes) is your advance approval of "
                                    "the plan: Binance's external-sign flow expects you to confirm what is sent. This "
                                    "does not mean Binance approves unattended use.",
                        epilog="Exit codes: 0 done, 1 refused before sending, 3 run ended without SUMMARY, "
                               "4 sale skipped or failed.")
    au.add_argument("--day", type=int, default=None, help="mining day to wait for (default: the next opening)")
    au.add_argument("--budget-bnb", type=float, default=0.045,
                    help="most gas in BNB for the journal, earlier runs included (default 0.045)")
    au.add_argument("--keep-bnb", type=float, default=0.005, help="BNB that always stays in the wallet (default 0.005)")
    au.add_argument("--sell", choices=("now", "trail", "off"), default="now",
                    help="now: sell right after the run (default); trail: watch the price up to 2 h; off: no sale")
    au.add_argument("--sell-fraction", type=float, default=0.8,
                    help="share of the coins THIS run mines to sell (default 0.8); older coins are never sold")
    au.add_argument("--minutes", type=float, default=90, help="stop this many minutes after the opening (default 90)")
    au.add_argument("--yes", action="store_true",
                    help="skip the question; only if you decided this plan yourself beforehand")
    au.add_argument("--plan", action="store_true", help="run check and show the plan, then exit; nothing is sent")
    st = sub.add_parser("status", help="is a run going? the end of run.log and sell.log")
    st.add_argument("--lines", type=int, default=15, help="lines of each log to show (default 15)")
    for s in (au, st):
        s.add_argument("--dir", default=SUBMIT, help=argparse.SUPPRESS)  # proofs, journal, logs, run.pid, STOP
        s.add_argument("--csv", default=os.path.join(ROOT, "miner", "proofs.csv"), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        return {"auto": cmd_auto, "status": cmd_status}[a.cmd](a)
    except Refusal as e:
        print(f"pearl.py: {e.reason}", file=sys.stderr, flush=True)
        return e.code
    except KeyboardInterrupt:
        print("pearl.py: cancelled (Ctrl-C) before the run started; nothing was sent", file=sys.stderr, flush=True)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
