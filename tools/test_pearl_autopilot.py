#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Offline checks for pearl.py (the one-command autopilot). No network, no baw, no wallet: the child scripts are
tiny fakes in a temp dir (PEARL_SUBMIT_SCRIPT / PEARL_SELL_SCRIPT / PEARL_EXPORT_SCRIPT) and the token balance
is a file. All values are made up.

  python3 tools/test_pearl_autopilot.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import pearl  # noqa: E402

FAILED = []
WALLET = "0x00000000000000000000000000000000DeaDBeef"  # dummy, as in submit/test_sell.py
COIN = 10 ** 8


def expect(name, cond, got=None):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond or got is None else f"   got: {got!r}"[:600]))
    if not cond:
        FAILED.append(name)


# ---- fake child scripts: they log their argv, follow FAKE_* settings, never touch a network or baw
FAKE_SUBMIT = r'''
import json, os, sys, time
d = os.environ["FAKE_DIR"]
cmd = sys.argv[1]
def arg(n):
    return sys.argv[sys.argv.index(n) + 1] if n in sys.argv else None
rec = {"script": "submit", "argv": sys.argv[1:], "pid": os.getpid()}
if cmd == "check":
    rec["proofs_seen"] = len(json.load(open(arg("--proofs")))["proofs"])
    with open(os.path.join(d, "calls.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
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
pid_seen = None
for _ in range(200):  # the parent writes run.pid right after starting us
    try:
        pid_seen = int(open(os.path.join(os.path.dirname(arg("--journal")), "run.pid")).read())
        break
    except (OSError, ValueError):
        time.sleep(0.01)
rec["run_pid_file"] = pid_seen
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps(rec) + "\n")
mode = os.environ.get("FAKE_RUN", "ok")
print("12:00:01.000 day 9 opens at unix 1800000000; min reward per proof: 80.0000", flush=True)
if mode == "crash":
    raise RuntimeError("boom")
bal_f = os.path.join(d, "balance")
mined = 0 if mode == "nothing" else int(os.environ.get("FAKE_MINED", "100"))
if mined:
    b = int(open(bal_f).read())
    open(bal_f, "w").write(str(b + mined * 100 * 10 ** 8))  # 100 coins per proof
stop = "day 9 budget full (remaining 1.00 < reward 80.00)"
if mode == "stop":
    open(arg("--stop-file"), "w").close()
    stop = "STOP file"
print(f"12:00:30.000 SUMMARY txs={(mined + 15) // 16} proofs_mined={mined} reverted=0 fees=0.002300 BNB stop={stop}")
'''

FAKE_SELL = r'''
import json, os, sys
d = os.environ["FAKE_DIR"]
rec = {"script": "sell", "argv": sys.argv[1:]}
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps(rec) + "\n")
if os.environ.get("FAKE_SELL") == "refuse":
    sys.exit("--wallet 0x... is not the wallet baw is signed in to: refusing to sell")
keep = round(float(sys.argv[sys.argv.index("--keep-coins") + 1]) * 10 ** 8)
bal_f = os.path.join(d, "balance")
b = int(open(bal_f).read())
open(bal_f, "w").write(str(keep))
print(f"12:01:00 end balance {keep / 1e8:,.2f} 珍珠币 (sold {(b - keep) / 1e8:,.2f}); BNB from finished orders ≈ 0.0123")
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
                    f.write(json.dumps(r) + "\n")
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
        recs = [json.loads(x) for x in open(p) if x.strip()] if os.path.exists(p) else []
        return [r for r in recs if script is None or r["script"] == script]

    def run(self, args, answer="yes", env=None, balance_fails=False, cmd="auto"):
        self.answers = []

        def ask(prompt):
            self.answers.append(prompt)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        def bal():
            self.balance_reads += 1
            if balance_fails:
                raise RuntimeError("token balance unreadable: test")
            return self.balance()
        pearl.read_answer, pearl.token_balance = ask, bal
        pearl.sleep, pearl.TAIL_S, pearl.BALANCE_WAIT = (lambda s: __import__("time").sleep(min(s, 0.02))), 0.02, 0
        e = {"PEARL_WALLET": WALLET, "PEARL_HANDLE": "testhandle1", "FAKE_DIR": self.d,
             "PEARL_SUBMIT_SCRIPT": os.path.join(self.d, "fake_submit.py"),
             "PEARL_SELL_SCRIPT": os.path.join(self.d, "fake_sell.py"),
             "PEARL_EXPORT_SCRIPT": os.path.join(self.d, "fake_export.py"),
             "PATH": os.path.join(self.d, "nobaw") + os.pathsep + os.environ.get("PATH", "")}
        e.update(env or {})
        saved = {k: os.environ.get(k) for k in list(e) + ["FAKE_CHECK", "FAKE_RUN", "FAKE_SELL", "FAKE_MINED"]}
        for k in ("FAKE_CHECK", "FAKE_RUN", "FAKE_SELL", "FAKE_MINED"):
            os.environ.pop(k, None)
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


def fake_baw_dir(d):
    """A `baw` that always fails, first on PATH: a test that reached a real script could never reach the wallet."""
    p = os.path.join(d, "nobaw")
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, "baw"), "w") as f:
        f.write("#!/bin/sh\necho '{\"success\": false, \"error\": {\"code\": \"TEST\"}}'\nexit 1\n")
    os.chmod(os.path.join(p, "baw"), 0o755)


print("---- keep computation")
expect("keep: 1,000 before, 11,000 after, 0.8 -> keep 3,000",
       pearl.keep_raw(1000 * COIN, 11000 * COIN, 0.8) == 3000 * COIN)
k = pearl.keep_raw(1, 1 + COIN + 7, 0.8)  # 80% of 100000007 raw = 80000005.6: at most 80000005 may be sold
expect("keep: rounds so that never more than the fraction is sold", k == 1 + COIN + 7 - 80000005, k)
expect("keep: fraction 1 sells all new coins and keeps the old", pearl.keep_raw(5 * COIN, 9 * COIN, 1.0) == 5 * COIN)
expect("keep: --keep-coins argument is exact", pearl.coins_arg(3000 * COIN + 12345) == "3000.00012345")
expect("keep: the argument survives the seller's float parsing",
       int(__import__("decimal").Decimal(str(float(pearl.coins_arg(123456 * COIN + 99999999)))) * COIN)
       == 123456 * COIN + 99999999)

print("---- confirmation")
for what, ans in (("no", "no"), ("empty", ""), ("y", "y"), ("end of input", EOFError())):
    e = Env()
    rc = e.run([], answer=ans)
    expect(f"answer {what}: refused, exit 1, nothing started", rc == 1 and not e.calls("submit")[1:]
           and not os.path.exists(os.path.join(e.data, "run.pid")) and "not confirmed" in e.err, (rc, e.err))
e = Env()
rc = e.run([], answer=" YES ")
expect("answer yes: runs and sells", rc == 0 and len(e.calls("sell")) == 1 and e.answers, (rc, e.err))
expect("plan: shows day, UTC time, budget, keep reserve, usable proofs, sell plan",
       all(s in e.out for s in ("opens", " UTC", "budget 0.045 BNB", "Keep reserve 0.005", "40 usable",
                                "Sells 80% of the coins THIS run mines", "approves this plan in advance")), e.out)
expect("plan: never claims Binance approves unattended use",
       "This does not mean Binance approves unattended use" in e.out and "endorse" not in e.out)
e = Env()
rc = e.run(["--yes"], answer=AssertionError("asked"))
expect("--yes: no question asked", rc == 0 and e.answers == [], (rc, e.err))
e = Env()
rc = e.run(["--plan"], answer=AssertionError("asked"))
expect("--plan: check and plan only, exit 0, nothing started",
       rc == 0 and [c["argv"][0] for c in e.calls()] == ["check"] and "==== the plan ====" in e.out
       and e.answers == [] and e.balance_reads == 0 and not os.path.exists(os.path.join(e.data, "run.pid")), (rc, e.err))

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

e = Env()
rc = e.run(["--yes", "--sell", "off"], env={"FAKE_CHECK": "warn"})
expect("a WARNING from check is shown in the plan and does not stop the run",
       rc == 0 and "Check   WARNING: 3 sampled proofs are already used on chain" in e.out
       and len(e.calls("submit")) == 2, (rc, e.err))

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
       e.calls("submit")[1]["argv"][e.calls("submit")[1]["argv"].index("--proofs") + 1]
       == os.path.join(e.data, "proofs.json"))
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
expect("run.pid holds the child's process id while it runs",
       run_call["run_pid_file"] == run_call["pid"] and pearl.read_pid(os.path.join(e.data, "run.pid")) == run_call["pid"],
       run_call)
argv = run_call["argv"]
expect("run gets --day, --budget-bnb, --keep-bnb, --stop-file",
       argv[0] == "run" and argv[argv.index("--day") + 1] == "40" and argv[argv.index("--budget-bnb") + 1] == "0.03"
       and argv[argv.index("--keep-bnb") + 1] == "0.004"
       and argv[argv.index("--stop-file") + 1] == os.path.join(e.data, "STOP"), argv)
log = open(os.path.join(e.data, "run.log"), encoding="utf-8").read()
expect("the child's output goes to run.log and to the console", "SUMMARY txs=" in log and "SUMMARY txs=" in e.out)
rc2 = e.run(["--yes", "--sell", "off", "--day", "40"])
expect("a second run appends to run.log", rc2 == 0 and open(os.path.join(e.data, "run.log")).read().count("SUMMARY") == 2)

print("---- sell")
e = Env(balance=1000)
rc = e.run(["--yes"])
s = e.calls("sell")
expect("sale: keep = before + 20% of the new coins, mode now",
       rc == 0 and len(s) == 1 and s[0]["argv"][s[0]["argv"].index("--keep-coins") + 1] == "3000.00000000"
       and s[0]["argv"][s[0]["argv"].index("--mode") + 1] == "now", s)
expect("sale: the seller gets run.pid and sell.jsonl",
       s[0]["argv"][s[0]["argv"].index("--run-pid") + 1] == os.path.join(e.data, "run.pid")
       and s[0]["argv"][s[0]["argv"].index("--journal") + 1] == os.path.join(e.data, "sell.jsonl"))
expect("sale: summary shows the BNB from the seller", "sold 8,000.00 coins for about 0.0123 BNB" in e.out, e.out)
expect("sale: seller output in sell.log", "BNB from finished orders" in open(os.path.join(e.data, "sell.log"),
                                                                              encoding="utf-8").read())
e = Env()
rc = e.run(["--yes", "--sell", "trail", "--sell-fraction", "0.5"])
s = e.calls("sell")
expect("--sell trail --sell-fraction 0.5", rc == 0 and s[0]["argv"][s[0]["argv"].index("--mode") + 1] == "trail"
       and s[0]["argv"][s[0]["argv"].index("--keep-coins") + 1] == "6000.00000000", s)
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
e = Env()
rc = e.run(["--yes"], env={"FAKE_RUN": "stop"})
expect("STOP file during the run: no sale, exit 4", rc == 4 and not e.calls("sell") and "STOP" in e.err, (rc, e.err))
e = Env()
rc = e.run(["--yes"], env={"FAKE_RUN": "crash"})
expect("child crash (no SUMMARY): no sale, exit 3", rc == 3 and not e.calls("sell") and "without a SUMMARY" in e.err,
       (rc, e.err))
expect("child crash: the traceback reached the console", "RuntimeError: boom" in e.out)
e = Env()
rc = e.run(["--yes"], balance_fails=True)
expect("balance unreadable: the run goes ahead, the sale is skipped, exit 4",
       rc == 4 and len(e.calls("submit")) == 2 and not e.calls("sell") and "could not be read" in e.err, (rc, e.err))
e = Env()
rc = e.run(["--yes"], env={"FAKE_SELL": "refuse"})
expect("seller refuses: exit 4 with its reason", rc == 4 and "not the wallet baw is signed in to" in e.err, e.err)

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

for d in Env.dirs:
    shutil.rmtree(d, ignore_errors=True)
# ---- v0.1.2: the sale counts at most mined x 100 coins as new, and only follows a clean stop
expect("keep: coins beyond mined x 100 (from elsewhere / stale before) are never sold",
       pearl.keep_raw(1000 * COIN, 11000 * COIN, 0.8, mined=16) == 11000 * COIN - int(16 * 100 * COIN * 0.8))
expect("keep: the cap does not bind when the growth fits", pearl.keep_raw(1000 * COIN, 2000 * COIN, 0.8, mined=16)
       == 1200 * COIN)
for reason in ("day 2 budget full (remaining 0.71 < reward 91.30)", "tx reverted and the day budget is full",
               "deadline", "gas budget 0.045000 BNB reached at launch", "BNB 0.004 minus in-flight fees would drop",
               "chain moved past day 2"):
    expect(f"clean stop sells: {reason[:30]}", bool(pearl.CLEAN_STOP.match(reason)))
for reason in ("crash in tracker: KeyError('x')", "3 execute failures in a row", "signal (Ctrl-C / SIGTERM)",
               "execute needs Binance App approval (order 1)", "STOP file", "unexpected simulation failure: X",
               "execute refused before launch: UNSAFE_ARG"):
    expect(f"abnormal stop does not sell: {reason[:30]}", not pearl.CLEAN_STOP.match(reason))

print("FAILED:", FAILED or "none")
sys.exit(1 if FAILED else 0)
