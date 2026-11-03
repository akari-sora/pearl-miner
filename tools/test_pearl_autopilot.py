#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Offline checks for pearl.py (the one-command autopilot). No network, no baw, no wallet: the child scripts are
tiny fakes in a temp dir (PEARL_SUBMIT_SCRIPT / PEARL_SELL_SCRIPT / PEARL_EXPORT_SCRIPT) and the token balance
is a file. All values are made up. The last section starts pearl.py in a process of its own and sends it a real
Ctrl-C (Windows: CTRL_C_EVENT to the console; elsewhere SIGINT).

  python3 tools/test_pearl_autopilot.py
"""
import contextlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import pearl  # noqa: E402

FAILED = []
WALLET = "0x00000000000000000000000000000000DeaDBeef"  # dummy, as in submit/test_sell.py
COIN = 10 ** 8
REAL = {k: getattr(pearl, k) for k in ("token_balance", "rpc_balance", "max_reward_per_proof", "stdin_is_tty",
                                       "coins_arg", "sleep", "read_answer")}


def expect(name, cond, got=None):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond or got is None else f"   got: {got!r}"[:700]))
    if not cond:
        FAILED.append(name)


# ---- fake child scripts: they log their argv, follow FAKE_* settings, never touch a network or baw
FAKE_SUBMIT = r'''
import json, os, signal, sys, time
d = os.environ["FAKE_DIR"]
cmd = sys.argv[1]
def arg(n):
    return sys.argv[sys.argv.index(n) + 1] if n in sys.argv else None
def record(rec):
    with open(os.path.join(d, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
rec = {"script": "submit", "argv": sys.argv[1:], "pid": os.getpid(), "ppid": os.getppid()}
if cmd == "check":
    rec["proofs_seen"] = len(json.load(open(arg("--proofs")))["proofs"])
    record(rec)
    print("12:00:00.000 wallet 0x...; agentId = keccak256('x')")
    if os.environ.get("FAKE_CHECK") == "bad":
        print("12:00:00.100 registry ownerOf(agentId) = none (handle not registered); matches the wallet: False")
        print("12:00:00.200 proof 0x0000000000000001… used=False")
        print("12:00:00.300 CHECK HAS PROBLEMS")
    else:
        print("12:00:00.100 registry ownerOf(agentId) = 0x...; matches the wallet: True")
        if os.environ.get("FAKE_CHECK") == "warn":
            print("12:00:00.200 WARNING: 3 sampled proofs are already used on chain and will be skipped by run")
        print("12:00:00.300 CHECK OK")
    sys.exit(0)
mode = os.environ.get("FAKE_RUN", "ok")
if mode == "locked":
    record(rec)
    sys.exit(f"another run already holds {arg('--journal')}; refusing to start a second sender")
got = []
for name in ("SIGINT", "SIGBREAK"):  # like pearl_submit: a signal becomes a clean stop with a SUMMARY
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), lambda s, f: got.append(s))
pid_seen = None
for _ in range(200):  # the parent writes run.pid right after starting us
    try:
        pid_seen = int(open(os.path.join(os.path.dirname(arg("--journal")), "run.pid")).read())
        break
    except (OSError, ValueError):
        time.sleep(0.01)
rec["run_pid_file"] = pid_seen
record(rec)
print("12:00:01.000 day 9 opens; 珍珠币 run started", flush=True)
open(os.path.join(d, "started"), "w").close()
if mode == "crash":
    raise RuntimeError("boom")
mined = 0 if mode == "nothing" else int(os.environ.get("FAKE_MINED", "100"))
stop = os.environ.get("FAKE_STOP", "day 9 budget full (remaining 1.00 < reward 80.00)")
if mode == "wait":  # until STOP or a signal, like the real run
    mined = 0
    for _ in range(600):
        if os.path.exists(arg("--stop-file")):
            stop = "STOP file"
            break
        if got:
            stop = "signal (Ctrl-C / SIGTERM)"
            break
        time.sleep(0.1)
if mined and mode != "nogrow":
    b = int(open(os.path.join(d, "balance")).read())
    extra = int(os.environ.get("FAKE_EXTRA", "0"))  # coins that arrive from elsewhere meanwhile
    open(os.path.join(d, "balance"), "w").write(str(b + (mined * 100 + extra) * 10 ** 8))  # 100 coins per proof
if mode == "stop":
    open(arg("--stop-file"), "w").close()
    stop = "STOP file"
print(f"12:00:30.000 SUMMARY txs={(mined + 15) // 16} proofs_mined={mined} reverted=0 fees=0.002300 BNB stop={stop}",
      flush=True)
'''

FAKE_SELL = r'''
import json, os, signal, sys, time
from decimal import Decimal
d = os.environ["FAKE_DIR"]
with open(os.path.join(d, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"script": "sell", "argv": sys.argv[1:], "pid": os.getpid()}) + "\n")
mode = os.environ.get("FAKE_SELL", "ok")
if mode == "refuse":
    sys.exit("--wallet 0x... is not the wallet baw is signed in to: refusing to sell")
if mode == "slow":  # like pearl_sell: a signal only sets a flag; this one never stops on it
    for name in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda s, f: None)
    open(os.path.join(d, "sell_started"), "w").close()
    time.sleep(20)
keep = int(Decimal(sys.argv[sys.argv.index("--keep-coins") + 1]) * 10 ** 8)
cap = int(Decimal(sys.argv[sys.argv.index("--max-sell-coins") + 1]) * 10 ** 8)
b = int(open(os.path.join(d, "balance")).read())
sold = max(0, min(b - keep, cap))
open(os.path.join(d, "balance"), "w").write(str(b - sold))
print(f"12:01:00 end balance {(b - sold) / 1e8:,.2f} 珍珠币 (sold {sold / 1e8:,.2f}); BNB from finished orders ≈ 0.0123")
'''

FAKE_EXPORT = r'''
import json, os, shutil, sys
d = os.environ["FAKE_DIR"]
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"script": "export", "argv": sys.argv[1:]}) + "\n")
shutil.copy(os.path.join(d, "proofs.template.json"), sys.argv[sys.argv.index("--out") + 1])
print("40 verified unique proofs -> proofs.json; rejected 0")
'''


def proofs_doc(n=40):
    return {"wallet": WALLET, "seed": "0x" + "00" * 32, "count": n,
            "proofs": [["0x%064x" % (1000 + i), "0x%064x" % (i + 1)] for i in range(n)]}


def fake_baw_dir(d):
    """A `baw` that always fails, first on PATH (baw.cmd too, for Windows): a test that reached a real script could
    never reach the wallet."""
    p = os.path.join(d, "nobaw")
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, "baw"), "w") as f:
        f.write("#!/bin/sh\necho '{\"success\": false, \"error\": {\"code\": \"TEST\"}}'\nexit 1\n")
    os.chmod(os.path.join(p, "baw"), 0o755)
    with open(os.path.join(p, "baw.cmd"), "w") as f:
        f.write('@echo {"success": false, "error": {"code": "TEST"}}\r\n@exit /b 1\r\n')


class Env:
    """A temp dir with fakes, proofs.json, a balance file; run() calls pearl.main in-process."""

    dirs = []

    def __init__(self, balance=1000, journal=None, proofs=True, csv=False):
        self.d = tempfile.mkdtemp(prefix="pearl-auto-")
        Env.dirs.append(self.d)
        self.data = os.path.join(self.d, "data")
        os.mkdir(self.data)
        for name, src in (("submit", FAKE_SUBMIT), ("sell", FAKE_SELL), ("export", FAKE_EXPORT)):
            with open(os.path.join(self.d, f"fake_{name}.py"), "w", encoding="utf-8") as f:
                f.write(src)
        with open(os.path.join(self.d, "proofs.template.json"), "w") as f:
            json.dump(proofs_doc(), f)
        if proofs:
            shutil.copy(os.path.join(self.d, "proofs.template.json"), os.path.join(self.data, "proofs.json"))
        self.csv = os.path.join(self.d, "proofs.csv")
        if csv:
            open(self.csv, "w").write("0x01,0x02\n")
        if journal:
            with open(os.path.join(self.data, "journal.jsonl"), "w") as f:
                for r in journal:
                    f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        self.set_balance(balance * COIN)
        self.balance_reads = 0
        fake_baw_dir(self.d)

    def set_balance(self, raw):
        with open(os.path.join(self.d, "balance"), "w") as f:
            f.write(str(raw))

    def balance(self):
        with open(os.path.join(self.d, "balance")) as f:
            return int(f.read())

    def calls(self, script=None):
        p = os.path.join(self.d, "calls.jsonl")
        recs = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()] if os.path.exists(p) else []
        return [r for r in recs if script is None or r["script"] == script]

    def env(self, extra=None):
        e = {"PEARL_WALLET": WALLET, "PEARL_HANDLE": "testhandle1", "FAKE_DIR": self.d,
             "PEARL_SUBMIT_SCRIPT": os.path.join(self.d, "fake_submit.py"),
             "PEARL_SELL_SCRIPT": os.path.join(self.d, "fake_sell.py"),
             "PEARL_EXPORT_SCRIPT": os.path.join(self.d, "fake_export.py"),
             "PATH": os.path.join(self.d, "nobaw") + os.pathsep + os.environ.get("PATH", ""),
             "FAKE_CHECK": None, "FAKE_RUN": None, "FAKE_SELL": None, "FAKE_MINED": None, "FAKE_STOP": None,
             "FAKE_EXTRA": None}
        e.update(extra or {})
        return e

    def run(self, args, answer="yes", env=None, balance_fails=False, cmd="auto", tty=True, max_reward=100 * COIN,
            balances=None, sleep_hook=None):
        self.answers = []

        def ask(prompt):
            self.answers.append(prompt)
            if isinstance(answer, BaseException):
                raise answer
            return answer() if callable(answer) else answer

        seq = list(balances or [])

        def bal():
            self.balance_reads += 1
            if balance_fails:
                raise RuntimeError("token balance unreadable: test")
            if seq:
                v = seq.pop(0)
                if isinstance(v, BaseException):
                    raise v
                return v
            return self.balance()

        def nap(s):
            if sleep_hook:
                sleep_hook()
            time.sleep(min(s, 0.02))
        pearl.read_answer, pearl.token_balance, pearl.sleep = ask, bal, nap
        pearl.stdin_is_tty = lambda: tty
        pearl.max_reward_per_proof = lambda day: max_reward
        pearl.TAIL_S, pearl.BALANCE_WAIT, pearl.BEFORE_WAIT = 0.02, 0, 0
        e = self.env(env)
        saved = {k: os.environ.get(k) for k in e}
        for k, v in e.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        o, er = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(o), contextlib.redirect_stderr(er):
                rc = pearl.main([cmd, *args, "--dir", self.data] + (["--csv", self.csv] if cmd == "auto" else []))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.out, self.err = o.getvalue(), er.getvalue()
        return rc


def argval(call, name):
    return call["argv"][call["argv"].index(name) + 1] if name in call["argv"] else None


# ---- real signals: one scenario per process (this file with --one), so only that process gets the Ctrl-C
def one(scenario, workdir, data):
    """In a process of its own: pearl.main with the fakes; a thread creates STOP or sends a real Ctrl-C."""
    signal.signal(signal.SIGINT, signal.default_int_handler)  # POSIX children inherit the outer run's SIG_IGN
    pearl.token_balance = lambda: int(open(os.path.join(workdir, "balance")).read())
    pearl.max_reward_per_proof = lambda day: 100 * COIN
    pearl.BALANCE_WAIT = pearl.BEFORE_WAIT = 0

    def act():
        for _ in range(300):
            if os.path.exists(os.path.join(workdir, "started")) and os.path.exists(os.path.join(data, "run.pid")):
                break
            time.sleep(0.05)
        time.sleep(0.5)
        with open(os.path.join(workdir, "alive_during.txt"), "w") as f:
            f.write(str(pearl.run_alive(os.path.join(data, "run.pid"))))
        if scenario == "stop":
            open(os.path.join(data, "STOP"), "w").close()
        elif scenario == "ctrlc":
            try:
                if os.name == "nt":  # what the user's Ctrl-C does: every process on this console gets it
                    os.kill(0, signal.CTRL_C_EVENT)
                else:  # the terminal signals pearl.py; its child is in a session of its own
                    os.kill(os.getpid(), signal.SIGINT)
            except OSError as e:  # no console to send it on
                with open(os.path.join(workdir, "skip.txt"), "w") as f:
                    f.write(repr(e))
    if scenario in ("stop", "ctrlc"):
        threading.Thread(target=act, daemon=True).start()
    rc = pearl.main(["auto", "--yes", "--dir", data, "--csv", os.path.join(workdir, "none.csv")])
    with open(os.path.join(workdir, "rc.txt"), "w") as f:
        f.write(str(rc))
    with open(os.path.join(workdir, "alive_after.txt"), "w") as f:
        f.write(str(pearl.run_alive(os.path.join(data, "run.pid"))))
    return rc


if "--one" in sys.argv:
    i = sys.argv.index("--one")
    sys.exit(one(*sys.argv[i + 1:i + 4]))


print("---- A: how much may be sold")
k, ms = pearl.sale_plan(1000 * COIN, 11000 * COIN, 100, 100 * COIN, 0.8)
expect("A3: 1,000 before, 11,000 after, 100 proofs at <= 100: keep 3,000, sell at most 8,000",
       (k, ms) == (3000 * COIN, 8000 * COIN), (k, ms))
k, ms = pearl.sale_plan(1000 * COIN, 61000 * COIN, 100, 100 * COIN, 0.8)
expect("A3: 50,000 that came in from elsewhere are never sold: cap 80% of 100 x 100",
       ms == 8000 * COIN and k == 53000 * COIN, (k, ms))
k, ms = pearl.sale_plan(1, 1 + COIN + 7, 10 ** 6, 10 ** 9, 0.8)  # 80% of 100000007 raw = 80000005.6
expect("A3: rounds so that never more than the fraction is sold", ms == 80000005 and k == 1 + COIN + 7 - ms, (k, ms))
expect("A3: fraction 1 sells all new coins and keeps the old",
       pearl.sale_plan(5 * COIN, 9 * COIN, 1, 10 * COIN, 1.0) == (5 * COIN, 4 * COIN))
expect("A4: the --keep-coins / --max-sell-coins argument is exact", pearl.coins_arg(3000 * COIN + 12345) == "3000.00012345")
expect("A3: the emission formula's reward at the start of day 2 is 91.31 (CONTRACT.md)",
       abs(pearl.day_start_reward(2) / COIN - 91.30862551) < 1e-6, pearl.day_start_reward(2))
expect("A3: ... and just under 100 on day 0", 99.999 < pearl.day_start_reward(0) / COIN <= 100)
ps = pearl.mods()[0]
saved_cs = ps.chain_state
ps.chain_state = lambda: (1, 0, 91 * COIN)
expect("A3: the reward bound is the chain's currentReward when it can be read", REAL["max_reward_per_proof"](2) == 91 * COIN)
ps.chain_state = lambda: (_ for _ in ()).throw(RuntimeError("all RPCs failed"))
with contextlib.redirect_stdout(io.StringIO()):
    v = REAL["max_reward_per_proof"](2)
expect("A3: ... and the emission formula (which errs low) when not", v == pearl.day_start_reward(2), v)
ps.chain_state = saved_cs
sell_mod = pearl.mods()[1]
saved_rpcs, sell_mod.RPCS = list(sell_mod.RPCS), ["a", "b", "c"]
answers = {"a": 5 * COIN, "b": 7 * COIN, "c": RuntimeError("down")}


def fake_rpc(url):
    v = answers[url]
    if isinstance(v, BaseException):
        raise v
    return v


pearl.rpc_balance = fake_rpc
expect("A1: the balance is the highest answer of all RPCs", REAL["token_balance"]() == 7 * COIN)
answers = {"a": RuntimeError("x"), "b": RuntimeError("y"), "c": RuntimeError("z")}
try:
    REAL["token_balance"]()
    expect("A1: no RPC answers: RuntimeError", False)
except RuntimeError:
    expect("A1: no RPC answers: RuntimeError", True)
sell_mod.RPCS[:], pearl.rpc_balance = saved_rpcs, REAL["rpc_balance"]
reads = iter([100, 120, 120])
pearl.token_balance, pearl.sleep = (lambda: next(reads)), (lambda s: None)
expect("A1: a balance counts once two reads agree; the highest read wins", pearl.stable_balance(0) == 120)
reads = iter([100, 100, 100, 100, 100, 100])
expect("A2: after mining, reads that do not grow are not a balance", pearl.stable_balance(0, above=100) is None)
reads = iter([100, 100, 130, 130])
expect("A2: ... until two reads agree above the old balance", pearl.stable_balance(0, above=100) == 130)

print("---- B: which stops allow the sale")
clean = ["day 9 budget full (remaining 1.00 < reward 80.00)", "tx reverted and the day budget is full",
         "gas budget 0.045000 BNB reached (committed 0.045100)", "gas budget 0.045000 BNB reached at launch",
         "no budget left; only tracking earlier transactions",
         "BNB 0.004900 minus in-flight fees would drop under keep 0.005", "deadline", "chain moved past day 9", "None"]
blocked = ["crash in tracker: KeyError('x')", "preview refused: risk [{'x': 1}]",
           "execute needs Binance App approval (order 7)", "3 execute failures in a row", "signal (Ctrl-C / SIGTERM)",
           "STOP file", "execute refused before launch: UNSAFE_ARG argument not safe", "wallet backend busy (351716) for 181s",
           "3 reverted txs while the day still had room", "unexpected simulation failure: InvalidSolution",
           "chain still on day 8 60s after the boundary", "4 unexplained simulation failures: x", "none", ""]
expect("B: clean stops allow the sale", all(pearl.clean_stop(x) for x in clean), [x for x in clean if not pearl.clean_stop(x)])
expect("B: every other stop blocks it", not any(pearl.clean_stop(x) for x in blocked),
       [x for x in blocked if pearl.clean_stop(x)])
src = open(os.path.join(ROOT, "submit", "pearl_submit.py"), encoding="utf-8").read()
reasons = re.findall(r'(?:self\.stop|self\._stop_locked)\((f?"[^"]*")', src)
known = {  # every stop reason pearl_submit.py can give, and whether a sale may follow it
    "wallet backend busy (351716)": False, "chain moved past day": True, "budget full (remaining": True,
    "minus in-flight fees would drop under keep": True, "signal (Ctrl-C / SIGTERM)": False, "STOP file": False,
    "deadline": True, "preview refused:": False, "unexplained simulation failures": False,
    "re-checking a batch baw keeps failing": False, "ProofAlreadyUsed but no proof": False,
    "unexpected simulation failure": False, "could not re-simulate": False, "near-cap simulation mismatches": False,
    "baw's simulation failed while ours succeeds": False, "60s after the boundary": False,
    "baw's approval of the failed batch": False, "baw keeps failing a batch": False, "baw unavailable (": False,
    "BNB reached": True, "execute refused before launch": False, "execute needs Binance App approval": False,
    "3 execute failures in a row": False, "tx reverted and the day budget is full": True,
    "3 reverted txs while the day still had room": False, "crash in ": False,
    "no budget left; only tracking earlier transactions": True}
unknown = [r for r in reasons if not any(k in r for k in known)]
expect("B: every stop reason in pearl_submit.py is classified here (a new one must be decided on)", not unknown, unknown)

print("---- confirmation")
for what, ans in (("no", "no"), ("empty", ""), ("y", "y"), ("end of input", EOFError())):
    e = Env()
    rc = e.run([], answer=ans)
    expect(f"answer {what}: refused, exit 1, nothing started", rc == 1 and not e.calls("submit")[1:]
           and not os.path.exists(os.path.join(e.data, "run.pid")) and "not confirmed" in e.err, (rc, e.err))
e = Env()
rc = e.run([], answer="no")
expect("A1: the balance is read only after the yes", e.balance_reads == 0, e.balance_reads)
e = Env()
rc = e.run([], answer=" YES ")
expect("answer yes: runs and sells", rc == 0 and len(e.calls("sell")) == 1 and e.answers, (rc, e.err))
expect("plan: shows day, UTC time, budget, keep reserve, usable proofs, sell plan",
       all(s in e.out for s in ("opens", " UTC", "budget 0.045 BNB", "Keep reserve 0.005", "40 usable",
                                "Sells at most 80% of the coins THIS run mines", "approves this plan in advance")), e.out)
expect("wording: advance approval of the plan, not the Binance App's confirmation of each transaction",
       "This is your advance approval of this plan, not the Binance App's confirmation of each transaction" in e.out
       and "whether Binance accepts it is up to Binance" in e.out and "this is that confirmation" not in e.out, e.out)
expect("wording: never claims Binance approves unattended use",
       "does not mean Binance approves unattended use" in e.out and "endorse" not in e.out)
expect("B: the plan says the sale follows only a normal end", "Only after a normal end" in e.out)
e = Env()
rc = e.run(["--yes"], answer=AssertionError("asked"))
expect("--yes: no question asked", rc == 0 and e.answers == [], (rc, e.err))
e = Env()
rc = e.run(["--plan"], answer=AssertionError("asked"), tty=False)
expect("--plan: check and plan only, exit 0, nothing started (allowed without a terminal and --day)",
       rc == 0 and [c["argv"][0] for c in e.calls()] == ["check"] and "==== the plan ====" in e.out
       and e.answers == [] and e.balance_reads == 0 and not os.path.exists(os.path.join(e.data, "run.pid")), (rc, e.err))
expect("E1: --plan prints the --day to start this plan with", re.search(r"with --day \d+", e.out) is not None)
e = Env()
rc = e.run([], tty=False)
expect("E2: no terminal, no --day, no --yes: refused before check, nothing started",
       rc == 1 and "pass the --day shown in the plan" in e.err and not e.calls() and e.answers == [], (rc, e.err))
e = Env()
rc = e.run(["--day", "40"], tty=False)
expect("E2: no terminal but --day given: the piped yes is accepted", rc == 0 and e.answers and len(e.calls("sell")) == 1,
       (rc, e.err))
e = Env()
rc = e.run(["--yes"], env={"FAKE_CHECK": "warn"})
expect("E3: a check WARNING with --yes: refused, nothing started",
       rc == 1 and "--allow-used-elsewhere" in e.err and len(e.calls("submit")) == 1, (rc, e.err))
e = Env()
rc = e.run(["--yes", "--allow-used-elsewhere", "--sell", "off"], env={"FAKE_CHECK": "warn"})
expect("E3: ... allowed with --allow-used-elsewhere, and the warning is in the plan",
       rc == 0 and "Check   WARNING: 3 sampled proofs are already used on chain" in e.out
       and len(e.calls("submit")) == 2, (rc, e.err))
e = Env()
rc = e.run(["--sell", "off"], env={"FAKE_CHECK": "warn"})
expect("E3: ... and with a typed yes (the user saw the warning)", rc == 0 and len(e.calls("submit")) == 2, (rc, e.err))

print("---- preflight")
e = Env()
rc = e.run([], env={"FAKE_CHECK": "bad"})
expect("failed check: exit 1, run never started", rc == 1 and [c["argv"][0] for c in e.calls("submit")] == ["check"]
       and not os.path.exists(os.path.join(e.data, "run.pid")), (rc, e.err))
expect("failed check: the failing line is shown", "check found problems:" in e.out
       and "  12:00:00.100 registry ownerOf(agentId) = none (handle not registered); matches the wallet: False"
       in e.out and "used=False" not in e.out.split("check found problems:")[1], e.out)
expect("failed check: one-line reason", e.err.strip().count("\n") == 0 and "CHECK OK" in e.err, e.err)
e = Env()
rc = e.run([], env={"PEARL_WALLET": None})
expect("no PEARL_WALLET: refused", rc == 1 and "PEARL_WALLET is not set" in e.err and not e.calls(), e.err)
e = Env()
rc = e.run([], env={"PEARL_HANDLE": "Name.ai"})
expect("bad PEARL_HANDLE: refused", rc == 1 and "PEARL_HANDLE" in e.err and not e.calls(), e.err)
e = Env()
rc = e.run([], env={"PEARL_WALLET": "0x1234"})
expect("bad PEARL_WALLET: refused", rc == 1 and "PEARL_WALLET" in e.err and not e.calls(), e.err)
e = Env(proofs=False, csv=True)
rc = e.run(["--sell", "off"])
expect("proofs.json missing, proofs.csv present: export runs first",
       rc == 0 and [c["script"] for c in e.calls()][:2] == ["export", "submit"]
       and e.calls("export")[0]["argv"][0] == e.csv, (rc, e.err, e.calls()))
e = Env(proofs=False)
rc = e.run([])
expect("no proofs at all: refused", rc == 1 and "no proofs" in e.err and not e.calls(), e.err)
e = Env()
rc = e.run(["--day", "0"])
expect("a day whose window is over: refused", rc == 1 and "window" in e.err and not e.calls(), e.err)
e = Env()
with open(os.path.join(e.data, "run.pid"), "w") as f:
    f.write(str(os.getpid()))  # a live process
rc = e.run([])
expect("a run already going: refused", rc == 1 and "already going" in e.err and not e.calls(), e.err)
e = Env(journal=['{"ev": "broadcast", "proofs": []}'])
rc = e.run([])
expect("journal record this version cannot read: one-line reason, no traceback, exit 1",
       rc == 1 and "cannot read" in e.err and "Traceback" not in e.err + e.out and e.err.strip().count("\n") == 0
       and not e.calls(), (rc, e.err))

print("---- journal: proofs sent before and the budget")
att = {"ev": "attempt", "requestId": "r1", "proofs": ["0x%064x" % (i + 1) for i in range(16)]}
bro = {"ev": "broadcast", "requestId": "r1", "tx": "0x" + "ab" * 32, "proofs": att["proofs"]}
rcp = {"ev": "receipt", "tx": "0x" + "ab" * 32, "status": 1, "fee": 10 ** 15, "mined": 16}
e = Env(journal=[att, bro, rcp])
rc = e.run(["--sell", "off"])
cargv = e.calls("submit")[0]["argv"]
expect("check gets proofs.json and the journal (it skips sent proofs itself, like run)",
       rc == 0 and cargv[cargv.index("--proofs") + 1] == os.path.join(e.data, "proofs.json")
       and cargv[cargv.index("--journal") + 1] == os.path.join(e.data, "journal.jsonl"), cargv)
expect("plan counts them", "24 usable (40 in" in e.out and "16 already sent" in e.out, e.out)
expect("no temporary proofs copy is left", sorted(os.listdir(e.data)) == ["journal.jsonl", "proofs.json", "run.log",
                                                                           "run.pid"], os.listdir(e.data))
expect("the run still gets the full proofs.json (its journal blocks what was sent)",
       argval(e.calls("submit")[1], "--proofs") == os.path.join(e.data, "proofs.json"))
expect("fees from earlier runs are shown", "earlier runs used 0.001000" in e.out, e.out)
expect("the plan says the budget counts every run of the journal", "in total for" in e.out
       and "not per" in e.out, e.out)
expect("summary: this run's fees only", "gas about 0.001300 BNB" in e.out, e.out)
e = Env(journal=[att, bro, dict(rcp, fee=5 * 10 ** 16)])
rc = e.run([])
expect("budget already used by earlier runs: refused", rc == 1 and "already used" in e.err and not e.calls(), e.err)
e = Env(journal=[dict(att, proofs=["0x%064x" % (i + 1) for i in range(40)])])
rc = e.run([])
expect("every proof sent before: refused", rc == 1 and "already sent" in e.err, e.err)

print("---- run")
e = Env()
rc = e.run(["--yes", "--sell", "off", "--budget-bnb", "0.03", "--keep-bnb", "0.004", "--day", "40"])
run_call = e.calls("submit")[1]
# on Windows a venv's python.exe is a launcher: run.pid then names the launcher, the parent of the interpreter
expect("F5: run.pid holds the child's process id (or its venv launcher's) while it runs",
       run_call["run_pid_file"] == pearl.read_pid(os.path.join(e.data, "run.pid"))
       and run_call["run_pid_file"] in ((run_call["pid"], run_call["ppid"]) if os.name == "nt" else (run_call["pid"],)),
       run_call)
argv = run_call["argv"]
expect("run gets --day, --budget-bnb, --keep-bnb, --stop-file",
       argv[0] == "run" and argval(run_call, "--day") == "40" and argval(run_call, "--budget-bnb") == "0.03"
       and argval(run_call, "--keep-bnb") == "0.004" and argval(run_call, "--stop-file") == os.path.join(e.data, "STOP"),
       argv)
log = open(os.path.join(e.data, "run.log"), encoding="utf-8").read()
expect("the child's output goes to run.log and to the console", "SUMMARY txs=" in log and "SUMMARY txs=" in e.out)
rc2 = e.run(["--yes", "--sell", "off", "--day", "40"])
expect("a second run appends to run.log", rc2 == 0 and open(os.path.join(e.data, "run.log")).read().count("SUMMARY") == 2)
expect("F5: the no-wallet baw exists as baw.cmd too (found first on Windows)",
       os.path.exists(os.path.join(e.d, "nobaw", "baw.cmd")))

print("---- C: run.pid and the journal lock, right before the start")
e = Env()
j = ps.Journal(os.path.join(e.data, "journal.jsonl"))
lock = ps.SenderLock(j)  # a submitter started by hand: it holds the lock and writes no run.pid
try:
    rc = e.run(["--yes"])
finally:
    lock.release()
    j.f.close()
expect("C: the journal lock is held: refused before the start, nothing started",
       rc == 1 and "another run holds" in e.err and len(e.calls("submit")) == 1, (rc, e.err))
e = Env()
rc = e.run([], answer=lambda: open(os.path.join(e.data, "STOP"), "w").close() or "yes")
expect("C: STOP created while the question waited: refused before the start",
       rc == 1 and "STOP" in e.err and len(e.calls("submit")) == 1, (rc, e.err))
e = Env()


def start_other():
    with open(os.path.join(e.data, "run.pid"), "w") as f:
        f.write(str(os.getpid()))  # a live process
    return "yes"


rc = e.run([], answer=start_other)
expect("C: a run started meanwhile (live run.pid): refused before the start",
       rc == 1 and "started meanwhile" in e.err and len(e.calls("submit")) == 1, (rc, e.err))
e = Env()
with open(os.path.join(e.data, "run.pid"), "w") as f:
    f.write("4206649\n")  # an earlier run's pid, long finished
rc = e.run(["--yes"], env={"FAKE_RUN": "locked"})
expect("C: the child found the journal locked: exit 3 and run.pid holds what it held before",
       rc == 3 and open(os.path.join(e.data, "run.pid")).read() == "4206649\n", (rc, e.err))
e = Env()
rc = e.run(["--yes"], env={"FAKE_RUN": "locked"})
expect("C: ... and with no run.pid before, none after", rc == 3 and not os.path.exists(os.path.join(e.data, "run.pid")))

print("---- sell")
e = Env(balance=1000)
rc = e.run(["--yes"])
s = e.calls("sell")
expect("A3/A4: keep = before + 20% of the new coins, at most 80% of 100 x 100 sold, mode now",
       rc == 0 and len(s) == 1 and argval(s[0], "--keep-coins") == "3000.00000000"
       and argval(s[0], "--max-sell-coins") == "8000.00000000" and argval(s[0], "--mode") == "now", s)
expect("sale: the seller gets run.pid and sell.jsonl",
       argval(s[0], "--run-pid") == os.path.join(e.data, "run.pid")
       and argval(s[0], "--journal") == os.path.join(e.data, "sell.jsonl"))
expect("sale: summary shows the BNB from the seller", "sold 8,000.00 coins for about 0.0123 BNB" in e.out, e.out)
expect("sale: seller output in sell.log", "BNB from finished orders" in open(os.path.join(e.data, "sell.log"),
                                                                              encoding="utf-8").read())
e = Env()
rc = e.run(["--yes", "--sell", "trail", "--sell-fraction", "0.5"])
s = e.calls("sell")
expect("--sell trail --sell-fraction 0.5", rc == 0 and argval(s[0], "--mode") == "trail"
       and argval(s[0], "--keep-coins") == "6000.00000000" and argval(s[0], "--max-sell-coins") == "5000.00000000", s)
e = Env(balance=1000)
rc = e.run(["--yes"], env={"FAKE_EXTRA": "50000"})
s = e.calls("sell")
expect("A3: 50,000 coins that arrived from elsewhere during the run are not sold",
       rc == 0 and argval(s[0], "--max-sell-coins") == "8000.00000000" and e.balance() == 53000 * COIN,
       (s, e.balance()))
e = Env(balance=1000)
rc = e.run(["--yes"], balances=[1000 * COIN, 900 * COIN, 1000 * COIN, 1000 * COIN])
s = e.calls("sell")
expect("A1: a lagging read before the run does not lower 'before': keep 3,000",
       rc == 0 and argval(s[0], "--keep-coins") == "3000.00000000", (s, e.err))
e = Env(balance=1000)
rc = e.run(["--yes"], balances=[1000 * COIN, 1001 * COIN, 1002 * COIN, 1003 * COIN, 1004 * COIN, 1005 * COIN])
expect("A1: a balance before the run that never reads the same twice: the run goes ahead, no sale, exit 4",
       rc == 4 and len(e.calls("submit")) == 2 and not e.calls("sell") and "did not read the same twice" in e.err,
       (rc, e.err))
e = Env()
rc = e.run(["--yes"], env={"FAKE_RUN": "nogrow"})
expect("A2: proofs mined but the balance never grows: no sale, exit 4 (not 0)",
       rc == 4 and not e.calls("sell") and "did not settle above" in e.err, (rc, e.err))
e = Env()
rc = e.run(["--yes"], env={"FAKE_RUN": "nothing"})
expect("nothing new mined: no sale, exit 0", rc == 0 and not e.calls("sell") and "nothing to sell" in e.out,
       (rc, e.err))
e = Env()
rc = e.run(["--yes", "--sell", "off"])
expect("--sell off: no sale and no balance read", rc == 0 and not e.calls("sell") and e.balance_reads == 0,
       (rc, e.balance_reads))
e = Env()
open(os.path.join(e.data, "STOP"), "w").close()
rc = e.run(["--yes"])
expect("STOP file before the start: refused, nothing started", rc == 1 and "STOP" in e.err and not e.calls(), e.err)
e = Env(balance=1000)
rc = e.run(["--yes"], env={"FAKE_RUN": "stop"})
expect("STOP file during the run: no sale, exit 4", rc == 4 and not e.calls("sell") and "STOP" in e.err, (rc, e.err))
expect("exit 4 prints the seller command with --keep-coins (the old balance) and --max-sell-coins",
       "--keep-coins 1000.00000000 --max-sell-coins 8000.00000000" in e.err, e.err)
for stop in blocked[:6]:
    e = Env()
    rc = e.run(["--yes"], env={"FAKE_STOP": stop})
    expect(f"B: stop '{stop[:40]}' after mining: no sale, exit 4",
           rc == 4 and not e.calls("sell") and "did not end normally" in e.err, (rc, e.err))
for stop in ("deadline", "None", "gas budget 0.045000 BNB reached (committed 0.045100)"):
    e = Env()
    rc = e.run(["--yes"], env={"FAKE_STOP": stop})
    expect(f"B: stop '{stop[:40]}': sold", rc == 0 and len(e.calls("sell")) == 1, (rc, e.err))
e = Env()
rc = e.run(["--yes"], env={"FAKE_RUN": "crash"})
expect("child crash (no SUMMARY): no sale, exit 3", rc == 3 and not e.calls("sell") and "without a SUMMARY" in e.err,
       (rc, e.err))
expect("child crash: the traceback reached the console", "RuntimeError: boom" in e.out)
e = Env()
rc = e.run(["--yes"], balance_fails=True)
expect("balance unreadable: the run goes ahead, the sale is skipped, exit 4",
       rc == 4 and len(e.calls("submit")) == 2 and not e.calls("sell") and "did not read the same twice" in e.err,
       (rc, e.err))
e = Env()
rc = e.run(["--yes"], env={"FAKE_SELL": "refuse"})
expect("seller refuses: exit 4 with its reason", rc == 4 and "not the wallet baw is signed in to" in e.err, e.err)

print("---- D: Ctrl-C at each phase")
e = Env(balance=1000)
rc = e.run(["--yes"], balances=[1000 * COIN, 1000 * COIN, KeyboardInterrupt()])
expect("D1: Ctrl-C while reading the balance after the run: summary printed, exit 4, not 'nothing was sent'",
       rc == 4 and "==== summary ====" in e.out and "you pressed Ctrl-C" in e.err and "nothing was sent" not in e.err
       and not e.calls("sell"), (rc, e.err))
e = Env(balance=1000)
pearl.coins_arg = lambda raw: (_ for _ in ()).throw(KeyboardInterrupt())
try:
    rc = e.run(["--yes"])
finally:
    pearl.coins_arg = REAL["coins_arg"]
expect("D2: a Ctrl-C after the run started never says 'nothing was sent'",
       rc == 4 and "after the run started" in e.err and "nothing was sent" not in e.err, (rc, e.err))
e = Env(balance=1000)
hits = [0]


def ctrl_c_in_sale():
    if os.path.exists(os.path.join(e.d, "sell_started")):
        hits[0] += 1
        if hits[0] in (1, 3):
            raise KeyboardInterrupt


rc = e.run(["--yes"], env={"FAKE_SELL": "slow"}, sleep_hook=ctrl_c_in_sale)
for c in e.calls("sell"):  # the fake seller ignores Ctrl-C: end it here
    try:
        os.kill(c["pid"], signal.SIGTERM)
    except OSError:
        pass
expect("D3: a second Ctrl-C during the sale: exit 4 (not 3), the seller left running",
       rc == 4 and "left fake_sell.py" in e.err, (rc, e.err))
e = Env()
real_replace, fired = os.replace, [0]


def replace_once(src_, dst):
    if dst.endswith("run.pid") and not fired[0]:
        fired[0] = 1
        raise KeyboardInterrupt
    return real_replace(src_, dst)


os.replace = replace_once
try:
    rc = e.run(["--yes", "--sell", "off"])
finally:
    os.replace = real_replace
# the child may get the passed-on Ctrl-C before Python is up and end at once (exit 3, nothing sent): either way the
# Ctrl-C is handled, never reported as "nothing was sent", and run.pid still names the child
expect("D3: a Ctrl-C right after the start is handled: run.pid is still written",
       fired[0] and rc in (0, 3) and pearl.read_pid(os.path.join(e.data, "run.pid")) is not None
       and "nothing was sent" not in e.err and "before the run started" not in e.err, (rc, e.err))

print("---- F6: run_fork --autopilot when pearl.py refused before the run")
sys.path.insert(0, os.path.join(ROOT, "submit", "fork_rehearsal"))
import run_fork  # noqa: E402
empty = tempfile.mkdtemp(prefix="pearl-auto-")
Env.dirs.append(empty)
try:
    res = run_fork.autopilot_checks(empty, 1)
    expect("F6: no run.log and no run.pid: three FAILs in the table, no FileNotFoundError",
           len(res) == 3 and not any(res.values()), res)
except Exception as ex:  # noqa: BLE001
    expect("F6: no run.log and no run.pid: three FAILs in the table, no FileNotFoundError", False, repr(ex))

print("---- status")
e = Env()
e.run(["--yes", "--sell", "off"])
rc = e.run(["--lines", "5"], cmd="status")
expect("status: run.pid finished, STOP absent, log tails", rc == 0 and "finished" in e.out and "absent" in e.out
       and "SUMMARY txs=" in e.out and "sell.log: no file yet" in e.out, e.out)
e = Env()
with open(os.path.join(e.data, "run.pid"), "w") as f:
    f.write(str(os.getpid()))
open(os.path.join(e.data, "STOP"), "w").close()
rc = e.run([], cmd="status")
expect("status: a live pid shows RUNNING, STOP present", rc == 0 and "RUNNING" in e.out and "present" in e.out, e.out)
e = Env()
dead = os.path.join(e.data, "dead.pid")
with open(dead, "w") as f:
    f.write(str(2 ** 22 + 12345))  # above every pid_max: no such process
expect("run_alive: a pid that does not exist is finished", not pearl.run_alive(dead))
expect("run_alive: a missing pid file counts as alive (fail closed)", pearl.run_alive(os.path.join(e.data, "none.pid")))
with open(os.path.join(e.data, "run.pid"), "w") as f:
    f.write("garbage")
rc = e.run([])
expect("an unreadable run.pid refuses the start", rc == 1 and "already going" in e.err and not e.calls(), e.err)

print("---- real processes: STOP and a real Ctrl-C (D6: Windows forwards it as CTRL_BREAK_EVENT)")
if os.name == "nt":  # a session started without Ctrl-C handling would ignore CTRL_C_EVENT: switch it on
    import ctypes
    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
for scenario in ("full", "stop", "ctrlc"):
    e = Env()
    env = dict(os.environ)
    for k, v in e.env({"FAKE_RUN": "ok" if scenario == "full" else "wait"}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)  # the Windows CTRL_C_EVENT reaches this process too
    try:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--one", scenario, e.d, e.data], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=120)
    finally:
        signal.signal(signal.SIGINT, old)
    rd = (lambda n: open(os.path.join(e.d, n)).read() if os.path.exists(os.path.join(e.d, n)) else None)
    out_, err_ = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    if rd("skip.txt"):
        print(f"  [SKIP] {scenario}: no console to send Ctrl-C on ({rd('skip.txt')})")
        continue
    try:
        p.stdout.decode("utf-8")
        utf8 = True
    except UnicodeDecodeError:
        utf8 = False
    runc = next((c for c in e.calls("submit") if c["argv"][0] == "run"), None)
    expect(f"{scenario}: stdout is UTF-8 and carries 珍珠币 from the child", utf8 and "珍珠币 run started" in out_)
    expect(f"{scenario}: run.pid names the run child; it has exited afterwards",
           runc is not None and pearl.read_pid(os.path.join(e.data, "run.pid")) == runc["run_pid_file"]
           and rd("alive_after.txt") == "False", (runc, rd("alive_after.txt"), err_[-300:]))
    if scenario == "full":
        expect("full: exit 0, sold with keep 3000 and at most 8000", rd("rc.txt") == "0"
               and argval(e.calls("sell")[0], "--max-sell-coins") == "8000.00000000", (out_[-500:], err_))
    else:
        why = "STOP file" if scenario == "stop" else "signal (Ctrl-C / SIGTERM)"
        expect(f"{scenario}: run.pid was alive while the run waited", rd("alive_during.txt") == "True")
        expect(f"{scenario}: the child stopped for '{why}' with a SUMMARY; no sale, exit 4",
               f"stop={why}" in out_ and rd("rc.txt") == "4" and not e.calls("sell"), (rd("rc.txt"), err_[-300:]))

for d in Env.dirs:
    shutil.rmtree(d, ignore_errors=True)
# ---- the v0.1.2 cases (keep_raw(mined) and CLEAN_STOP there), on sale_plan / clean_stop that replaced them
k, ms = pearl.sale_plan(1000 * COIN, 11000 * COIN, 16, 100 * COIN, 0.8)
expect("v0.1.2: coins beyond mined x 100 (from elsewhere / stale before) are never sold",
       ms == int(16 * 100 * COIN * 0.8) and k == 11000 * COIN - ms, (k, ms))
expect("v0.1.2: the cap does not bind when the growth fits",
       pearl.sale_plan(1000 * COIN, 2000 * COIN, 16, 100 * COIN, 0.8)[0] == 1200 * COIN)
for reason in ("day 2 budget full (remaining 0.71 < reward 91.30)", "tx reverted and the day budget is full",
               "deadline", "gas budget 0.045000 BNB reached at launch",
               "BNB 0.004000 minus in-flight fees would drop under keep 0.005", "chain moved past day 2"):
    expect(f"v0.1.2: clean stop sells: {reason[:30]}", pearl.clean_stop(reason))
for reason in ("crash in tracker: KeyError('x')", "3 execute failures in a row", "signal (Ctrl-C / SIGTERM)",
               "execute needs Binance App approval (order 1)", "STOP file", "unexpected simulation failure: X",
               "execute refused before launch: UNSAFE_ARG"):
    expect(f"v0.1.2: abnormal stop does not sell: {reason[:30]}", not pearl.clean_stop(reason))

print("FAILED:", FAILED or "none")
sys.exit(1 if FAILED else 0)
