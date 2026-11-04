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
  submit/pearl_sell.py      run --keep-coins K --max-sell-coins M, only after the run ended normally
For tests and the fork rehearsal, PEARL_SUBMIT_SCRIPT, PEARL_SELL_SCRIPT and PEARL_EXPORT_SCRIPT replace those paths.

Exit codes: 0 done; 1 refused before anything was sent; 3 the run did not end cleanly (no SUMMARY line, or an
error after it); 4 the run ended but the sale was skipped or stopped. Every non-zero exit prints a one-line reason.
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
import urllib.request
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
# the run's stop reasons after which selling is safe: the day, the budget or the proofs simply ran out
# ("None": no stop was recorded, the proofs ran out). Anything else (a crash, a risk warning, an approval the
# Binance App asks for, failed executes, a signal, STOP, UNSAFE_ARG, ...) blocks the automatic sale.
CLEAN_STOP_RES = [re.compile(p) for p in (
    r"day \d+ budget full \(", r"tx reverted and the day budget is full$", r"gas budget [0-9.]+ BNB reached\b",
    r"no budget left; only tracking earlier transactions$", r"BNB [0-9.]+ minus in-flight fees would drop under keep ",
    r"deadline$", r"chain moved past day \d+$", r"None$")]
SELLER_TRAILER = "stopped before the sale was done"  # pearl_sell.py's general last line; its reason is above it
NOTHING_SOLD = "before any sale"  # pearl_sell.py stopped before its start record: nothing was sold
CRASH_RE = re.compile(r"\bcrash in \w+: ")  # pearl_submit.py's line when a worker crashed, even after a stop
LOCKED_TEXT = "another run already holds"  # pearl_submit.py's refusal when the journal is locked
DECIMALS = 8
TAIL_S = 0.2          # how often the log is read while a child runs
BALANCE_WAIT = 5.0    # seconds between token balance reads after the run
BEFORE_WAIT = 1.0     # ... and before it (the opening may be close)
STATE = {"phase": "before"}  # before | run | after: what a Ctrl-C that reaches main() interrupted
read_answer = input   # replaced in tests
sleep = time.sleep    # replaced in tests


def stdin_is_tty():
    """Replaced in tests."""
    return bool(sys.stdin) and sys.stdin.isatty()


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


# ---------------------------------------------------------------- chain reads
def rpc_balance(url):
    """Raw token balance of PEARL_WALLET from one RPC; RuntimeError if it does not answer. Replaced in tests."""
    sell = mods()[1]
    data = "0x70a08231" + sell.WALLET[2:].lower().rjust(64, "0")
    req = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [
        {"to": sell.TOKEN, "data": data}, "latest"]}).encode(), {"Content-Type": "application/json",
                                                                "User-Agent": "pearl-auto"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=8)).get("result")
    except Exception as e:  # noqa: BLE001 - any failure of this RPC
        raise RuntimeError(f"{url}: {e!r}")
    if isinstance(r, str) and r.startswith("0x") and len(r) == 66:
        return int(r, 16)
    raise RuntimeError(f"{url}: bad answer {str(r)[:80]!r}")


def token_balance():
    """Raw (8-decimal) token balance: the highest answer of all RPCs (one that lags shows an older, lower balance);
    RuntimeError if none answers. Replaced in tests."""
    vals, last = [], None
    for url in mods()[1].RPCS:
        try:
            vals.append(rpc_balance(url))
        except RuntimeError as e:
            last = e
    if not vals:
        raise RuntimeError(f"token balance unreadable: {last}")
    return max(vals)


def stable_balance(wait, above=None, tries=6):
    """The token balance once two reads in a row agree (and, with `above`, are higher than it); the highest read
    counts. None if that does not happen within `tries` reads. Ctrl-C passes through."""
    seen, prev = [], None
    for i in range(tries):
        try:
            b = token_balance()
        except RuntimeError as e:
            out(f"token balance unreadable ({e})")
            b = None
        if b is not None:
            seen.append(b)
            if b == prev and (above is None or b > above):
                return max(seen)
            prev = b
        if i < tries - 1:
            sleep(wait)
    return None


def day_start_reward(day):
    """The emission formula's reward for the claim that filled the day before `day`, if every earlier day filled its
    cap (as in pearl_submit.min_reward_for_day, without the 1% margin). The first claim of `day` pays this or less;
    an earlier day that did not fill means fewer claims and a higher real reward, so this can only be too low."""
    ps = mods()[0]
    cap_end = ps.DAILY_CAP * day
    if cap_end >= ps.MINING_SUPPLY:
        return 0
    n = ps.EMISSION_K * cap_end // (ps.MINING_SUPPLY - cap_end) + 1
    return ps.cumulative_mining(n + 1) - ps.cumulative_mining(n)


def max_reward_per_proof(day):
    """Raw coins one proof of this run can earn at most. The reward only falls as claims add up and every claim of
    this run comes after this read, so the chain's currentReward now is an upper bound. If the chain cannot be read,
    day_start_reward(day), which errs low: the sale can then only be smaller. Replaced in tests."""
    try:
        return mods()[0].chain_state()[2]
    except RuntimeError as e:
        out(f"currentReward unreadable ({e}); using the emission formula for day {day}")
        return day_start_reward(day)


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


def stream_child(cmd, log_path, pid_path=None, second_code=EXIT_RUN):
    """Start `cmd` with its output appended to log_path, write its process id to pid_path, and copy the new part
    of the log to the console until it exits. Ctrl-C is passed on to the child, which finishes what is in flight;
    a second Ctrl-C leaves it running in the background and ends pearl.py with `second_code`.
    The child gets its own process group: on macOS and Linux a new session, so closing the terminal does not end
    it (like nohup); on Windows CREATE_NEW_PROCESS_GROUP, so the console's Ctrl-C reaches it only through us (as
    CTRL_BREAK_EVENT). Closing the console window on Windows ends it. Returns (exit code, lines, Ctrl-C count)."""
    if pid_path and os.path.exists(pid_path):
        os.remove(pid_path)  # missing counts as "alive" for the seller until the new id is written
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "ab") as lf:
        lf.write(f"==== pearl.py {stamp}: {os.path.basename(cmd[2])} {' '.join(cmd[3:4])} ====\n".encode())
        lf.flush()
        start = lf.tell()
        kw = ({"start_new_session": True} if os.name == "posix"
              else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP})
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=child_env(), **kw)
    name = os.path.basename(cmd[2])
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    buf, lines, interrupts, pid_written, f = "", [], 0, not pid_path, None

    def take(text):
        nonlocal buf
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
            buf += text
            *done, buf = buf.split("\n")
            lines.extend(x.rstrip("\r") for x in done)
    try:  # from here on a Ctrl-C is handled: the child is running
        while True:
            try:
                if not pid_written:
                    tmp = pid_path + ".tmp"
                    with open(tmp, "w") as pf:
                        pf.write(f"{p.pid}\n")
                    os.replace(tmp, pid_path)
                    pid_written = True
                if f is None:
                    f = open(log_path, "rb")
                    f.seek(start)
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
                if interrupts > 1:
                    raise Refusal(second_code, f"left {name} (pid {p.pid}) running in the background; nothing more "
                                               "is done. See: python3 pearl.py status")
                try:
                    p.send_signal(signal.SIGINT if os.name == "posix" else signal.CTRL_BREAK_EVENT)
                except OSError:
                    pass
                out(f"\nCtrl-C: asking {name} to stop; it finishes what is in flight. "
                    "Press Ctrl-C again to leave it running in the background.")
    finally:
        if f is not None:
            f.close()
    if buf:
        lines.append(buf)
        out()
    return p.returncode, lines, interrupts


# ---------------------------------------------------------------- pieces of the plan
def sale_plan(before, after, mined, max_reward, fraction):
    """(keep, max_sell) in raw units: sell at most `fraction` of min(after - before, mined x max_reward); int()
    truncates, never more. max_reward is an upper bound read before the start, so of the coins held before or sent
    from elsewhere at most `fraction` x mined x (max_reward - the real reward) can be sold: a few percent of what the
    run earned, at most."""
    new = max(0, min(after - before, mined * max_reward))
    max_sell = int(Decimal(new) * Decimal(str(fraction)))
    return after - max_sell, max_sell


def coins_arg(raw):
    return f"{raw // 10 ** DECIMALS}.{raw % 10 ** DECIMALS:0{DECIMALS}d}"


def coins(raw):
    return f"{raw / 10 ** DECIMALS:,.2f}"


def clean_stop(reason):
    return any(r.match(reason.strip()) for r in CLEAN_STOP_RES)


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
    return [how, f"Sells at most {pct} of what THIS run's proofs can have earned (proofs mined x the reward read "
                 f"before the start, which only falls);",
            f"coins you held before stay, apart from at most {pct} of the gap between that bound and what the proofs "
            f"really earned.",
            "Only after a normal end (cap full, budget spent, proofs used up, deadline): never after STOP, Ctrl-C,",
            "a crash, a risk warning or an error."]


def sell_later_cmd(P, before, mined, max_reward, fraction, after=None):
    """The seller command that sells what this run's sale would have sold, at most."""
    paths = "" if P.dir == SUBMIT else f" --run-pid {P.run_pid} --journal {P.sell_journal}"
    if before is None:  # the cap still holds: never more than the fraction of what the proofs can have earned
        cap = int(Decimal(mined * max_reward) * Decimal(str(fraction)))
        return (f"python3 submit/pearl_sell.py run --mode now --keep-coins N --max-sell-coins {coins_arg(cap)}{paths}, "
                "with N at least the coins you held before this run")
    keep, max_sell = (sale_plan(before, after, mined, max_reward, fraction) if after is not None else
                      (before, int(Decimal(mined * max_reward) * Decimal(str(fraction)))))
    return (f"python3 submit/pearl_sell.py run --mode now --keep-coins {coins_arg(keep)} "
            f"--max-sell-coins {coins_arg(max_sell)}{paths}")


def ready_to_start(P, ps):
    """Right before the run starts: no STOP file, no live run.pid, and the journal lock is free (a submitter started
    by hand writes no run.pid but holds the lock). The lock is taken and released again at once."""
    if os.path.exists(P.stop):
        raise Refusal(EXIT_REFUSED, f"{short(P.stop)} appeared: nothing was sent; delete it, then start again")
    if os.path.exists(P.run_pid) and run_alive(P.run_pid):
        raise Refusal(EXIT_REFUSED, f"a run started meanwhile (pid {read_pid(P.run_pid)} in {short(P.run_pid)}); "
                                    "nothing was sent. See: python3 pearl.py status")
    j = ps.Journal(P.journal)
    try:
        try:
            lock = ps.SenderLock(j)
        except OSError:
            raise Refusal(EXIT_REFUSED, f"another run holds {short(P.journal)} (a submitter started by hand?); "
                                        "nothing was sent")
        lock.release()
    finally:
        j.f.close()


# ---------------------------------------------------------------- auto
def cmd_auto(a):
    STATE["phase"] = "before"
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
    if not (a.plan or a.yes or a.day is not None or stdin_is_tty()):
        raise Refusal(EXIT_REFUSED, "the answer does not come from a terminal: pass the --day shown in the plan, so "
                                    "the yes cannot apply to a later day; nothing was sent")
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
    try:
        blocked, fees0, reserved0, _, _ = ps.blocked_from_journal(journal_records(P.journal))
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise Refusal(EXIT_REFUSED, f"{short(P.journal)} has a record this version cannot read "
                                    f"({type(e).__name__}: {e}); nothing was sent")
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
    out("This is your advance approval of this plan, not the Binance App's confirmation of each transaction;")
    out("whether Binance accepts it is up to Binance. It does not mean Binance approves unattended use, and")
    out("driving a wallet with scripts may break its terms.")
    if a.plan:
        out(f"\n--plan: nothing was sent. To start this plan, run the same command without --plan and with "
            f"--day {day}.")
        return EXIT_OK
    if warnings and (a.yes or not stdin_is_tty()) and not a.allow_used_elsewhere:
        raise Refusal(EXIT_REFUSED, "check printed a WARNING (proofs already used on chain but not in the journal: "
                                    "a lost journal, or another machine sending): an answer given beforehand (--yes, "
                                    "or a yes from a pipe) needs --allow-used-elsewhere too; nothing was sent"
                                    + ("" if a.yes else ". If you are at a console Python does not see as a terminal "
                                       "(Git Bash/mintty, an IDE console), read the warning, then start again with "
                                       "--allow-used-elsewhere"))
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

    # 3. submit: the balance and the reward bound are read now, after the yes and right before the start
    before, max_reward, sale_block = None, None, None
    if a.sell != "off":
        before = stable_balance(BEFORE_WAIT)
        if before is None:
            sale_block = "the token balance before the run did not read the same twice"
            out(f"WARNING: {sale_block}: the sale will be skipped. Sell later with submit/pearl_sell.py.")
        else:
            out(f"token balance before the run: {coins(before)} coins")
        max_reward = max_reward_per_proof(day)
        out(f"highest reward one proof of this run can earn: {coins(max_reward)} coins")
    ready_to_start(P, ps)
    out(f"\n==== run: output also in {short(P.run_log)}, process id in {short(P.run_pid)}; "
        f"stop any time: create {short(P.stop)} or press Ctrl-C ====")
    STATE["phase"] = "run"
    rc, lines, interrupts = stream_child(
        child_cmd("submit", "run", "--day", day, "--budget-bnb", a.budget_bnb, "--keep-bnb", a.keep_bnb,
                  "--minutes", a.minutes, "--proofs", P.proofs, "--journal", P.journal, "--stop-file", P.stop),
        P.run_log, P.run_pid, EXIT_RUN)
    STATE["phase"] = "after"
    m = next((m for m in map(SUMMARY_RE.search, reversed(lines)) if m), None)
    logs = f"logs: {short(P.run_log)}, {short(P.journal)}"
    if not m and any(LOCKED_TEXT in x for x in lines):  # another submitter has the journal and ours never ran:
        try:                                               # no run.pid at all, so the seller refuses (fail closed)
            os.remove(P.run_pid)
        except OSError:
            pass
    if not m or rc != 0:
        last = next((x for x in reversed(lines) if x.strip()), "no output")
        what = "without a SUMMARY line" if not m else "with an error after its SUMMARY line"
        raise Refusal(EXIT_RUN, f"the run ended {what} (exit {rc}; last line: {last.strip()[:200]}); "
                                f"nothing is sold. {logs}")
    txs, mined, reverted = int(m.group(1)), int(m.group(2)), int(m.group(3))
    stop = m.group(5).strip()
    fees = max(0, int(Decimal(m.group(4)) * 10 ** 18) - fees0)
    run_line = (f"proofs mined {mined} in {txs} txs ({reverted} reverted), gas about {fees / 1e18:.6f} BNB, "
                f"stop reason: {stop}")

    # 4. sell: only after a normal end, and never more than this run's proofs can have earned
    sold_line, code, reason, after = "off", EXIT_OK, None, None
    if a.sell != "off":
        if interrupts:
            sale_block = "you pressed Ctrl-C"
        elif os.path.exists(P.stop):
            sale_block = f"{short(P.stop)} exists"
        elif not clean_stop(stop):
            sale_block = f"the run did not end normally ({stop})"
        elif any(CRASH_RE.search(x) for x in lines):
            sale_block = ("the run reported a crash: " + next(x for x in lines if CRASH_RE.search(x)).strip()[:160])
        if sale_block is None and mined == 0:
            sold_line = "nothing to sell: this run mined no proofs"
        elif sale_block is None:
            try:
                after = stable_balance(BALANCE_WAIT, above=before)
            except KeyboardInterrupt:
                sale_block = "you pressed Ctrl-C"
            if sale_block is None and after is None:
                sale_block = (f"the token balance did not settle above {coins(before)} after mining {mined} proofs "
                              "(an RPC behind, or the balance unreadable)")
            elif sale_block is None:
                keep, max_sell = sale_plan(before, after, mined, max_reward, a.sell_fraction)
                out(f"\n==== sell ({a.sell}): balance {coins(before)} -> {coins(after)}, +{coins(after - before)}; "
                    f"keeping {coins(keep)}, selling at most {coins(max_sell)}. Output also in {short(P.sell_log)} ====")
                src, slines, s_int = stream_child(
                    child_cmd("sell", "run", "--mode", a.sell, "--keep-coins", coins_arg(keep), "--max-sell-coins",
                              coins_arg(max_sell), "--run-pid", P.run_pid, "--journal", P.sell_journal),
                    P.sell_log, None, EXIT_SALE)
                sm = next((x for x in map(SOLD_RE.search, reversed(slines)) if x), None)
                sold_line = (f"sold {sm.group(2)} coins for about {sm.group(3)} BNB; {sm.group(1)} coins left"
                             if sm else "no end line from the seller")
                if src != 0 or s_int:  # the seller's own stop reason, not its general last line or end balance
                    full = next((x for x in reversed(slines) if x.strip() and not SOLD_RE.search(x)
                                 and SELLER_TRAILER not in x), "no output").strip()
                    last = full[:200]
                    if NOTHING_SOLD in full:  # the whole line: a long path can push the marker past 200 chars
                        sold_line = "skipped: the seller stopped before any sale"
                        code, reason = EXIT_SALE, (f"the seller stopped before any sale (exit {src}): {last}. Nothing "
                                                   f"was sold. To sell later: "
                                                   + sell_later_cmd(P, before, mined, max_reward, a.sell_fraction, after))
                    else:
                        code, reason = EXIT_SALE, (f"the seller stopped before it was done (exit {src}): {last}. Some "
                                                   f"coins may be sold already: check python3 pearl.py status and "
                                                   f"{short(P.sell_log)} before selling more")
        if sale_block:
            sold_line = f"skipped: {sale_block}"
            later = ("This run mined no proofs, so there is nothing of it to sell." if mined == 0 else
                     "To sell later: " + sell_later_cmd(P, before, mined, max_reward or day_start_reward(day),
                                                        a.sell_fraction, after))
            code, reason = EXIT_SALE, f"sale skipped: {sale_block}. {later}"
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
                                    "the plan, not the Binance App's confirmation of each transaction; whether "
                                    "Binance accepts it is up to Binance, and it does not mean Binance approves "
                                    "unattended use.",
                        epilog="Exit codes: 0 done, 1 refused before sending, 3 run did not end cleanly, "
                               "4 sale skipped or stopped.")
    au.add_argument("--day", type=int, default=None,
                    help="mining day to wait for (default: the next opening); required when the answer is piped in")
    au.add_argument("--budget-bnb", type=float, default=0.045,
                    help="most gas in BNB for the journal, earlier runs included (default 0.045)")
    au.add_argument("--keep-bnb", type=float, default=0.005, help="BNB that always stays in the wallet (default 0.005)")
    au.add_argument("--sell", choices=("now", "trail", "off"), default="now",
                    help="now: sell right after the run (default); trail: watch the price up to 2 h; off: no sale")
    au.add_argument("--sell-fraction", type=float, default=0.8,
                    help="share of what THIS run's proofs can have earned to sell at most (default 0.8)")
    au.add_argument("--minutes", type=float, default=90, help="stop this many minutes after the opening (default 90)")
    au.add_argument("--yes", action="store_true",
                    help="skip the question; only if you decided this plan yourself beforehand")
    au.add_argument("--allow-used-elsewhere", action="store_true",
                    help="let --yes or a piped yes start although check warned about proofs used on chain but not "
                         "in the journal")
    au.add_argument("--plan", action="store_true", help="run check and show the plan, then exit; nothing is sent")
    st = sub.add_parser("status", help="is a run going? the end of run.log and sell.log")
    st.add_argument("--lines", type=int, default=15, help="lines of each log to show (default 15)")
    for s in (au, st):
        s.add_argument("--dir", default=SUBMIT, help=argparse.SUPPRESS)  # proofs, journal, logs, run.pid, STOP
        s.add_argument("--csv", default=os.path.join(ROOT, "miner", "proofs.csv"), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    STATE["phase"] = "before"
    try:
        return {"auto": cmd_auto, "status": cmd_status}[a.cmd](a)
    except Refusal as e:
        print(f"pearl.py: {e.reason}", file=sys.stderr, flush=True)
        return e.code
    except KeyboardInterrupt:
        if STATE["phase"] == "before":
            print("pearl.py: cancelled (Ctrl-C) before the run started; nothing was sent", file=sys.stderr, flush=True)
            return EXIT_REFUSED
        print("pearl.py: interrupted (Ctrl-C) after the run started: transactions may have been sent and nothing more "
              "is done. See: python3 pearl.py status", file=sys.stderr, flush=True)
        return EXIT_RUN if STATE["phase"] == "run" else EXIT_SALE


if __name__ == "__main__":
    sys.exit(main())
