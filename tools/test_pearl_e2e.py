#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""pearl.py end to end with real processes: `pearl.py auto` starts the real pearl_submit.py (check, then run) and
the real pearl_sell.py as child processes, a real STOP file stops a run, and a real Ctrl-C reaches pearl.py the way
a terminal sends it (Windows: CTRL_C_EVENT to the console, which pearl.py passes on as CTRL_BREAK_EVENT). Only the
edges are fake: the chain (FakeChain from submit/test_submit.py), the market, and the token balance, which is a
file. No network, no baw, no wallet; every value is made up.

Each scenario runs pearl.py in a process of its own (this file with --one), so only that process gets the Ctrl-C.

  python3 tools/test_pearl_e2e.py
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WALLET = "0x00000000000000000000000000000000DeaDBeef"  # dummy, as in the other suites
COIN = 10 ** 8
CREDIT = 1000 * COIN  # coins each mined proof adds to the balance file, and pearl.py's bound for one proof's
                      # reward: big enough that the real seller splits the sale (its smallest chunk is 20,000)
BEFORE = 1000    # coins in the wallet before the run
CAP = 48         # proofs the fake day takes: three transactions of 16
OPEN_IN = 5.0    # seconds from the start of a scenario to the fake opening

# ---- the submitter: the real pearl_submit.py; FakeChain answers baw, receipts and simulations. Every mined proof
# adds E2E_CREDIT raw units to the balance file (with E2E_LATE=n, only the first n transactions' coins arrive before
# the run ends; the rest land when the seller starts).
SUBMIT_SHIM = r'''
import json, os, signal, sys, time
d, repo = os.environ["E2E_DIR"], os.environ["E2E_REPO"]
sys.path.insert(0, os.path.join(repo, "submit"))
import pearl_submit as ps
import test_submit as ts  # its FakeChain


def record(**rec):
    with open(os.path.join(d, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def log_signals(tag):  # each handler the real code installs also notes which signal arrived
    real = signal.signal

    def wrapped(sig, handler):
        if callable(handler):
            def h(s, frame, _h=handler):
                with open(os.path.join(d, "signals.txt"), "a") as f:
                    f.write(f"{tag} {s}\n")
                return _h(s, frame)
            return real(sig, h)
        return real(sig, handler)
    signal.signal = wrapped


def read_int(path):
    for _ in range(200):  # Windows: a file being replaced can be unreadable for a moment
        try:
            with open(path) as f:
                return int(f.read())
        except (OSError, ValueError):
            time.sleep(0.01)
    raise RuntimeError(f"cannot read {path}")


def write_int(path, v):
    with open(path + f".{os.getpid()}", "w") as f:
        f.write(str(v))
    for _ in range(200):  # a reader sees the old or the new value, never half a file; Windows may refuse a moment
        try:
            os.replace(path + f".{os.getpid()}", path)
            return
        except PermissionError:
            time.sleep(0.01)
    raise RuntimeError(f"cannot write {path}")


def add_coins(raw):
    p = os.path.join(d, "balance")
    write_int(p, read_int(p) + raw)


record(script="submit", cmd=sys.argv[1], pid=os.getpid(), ppid=os.getppid())
open_at = float(os.environ["E2E_OPEN_AT"])
chain = ts.FakeChain(open_in=0, cap_proofs=int(os.environ["E2E_CAP"]),
                     risk_after=int(os.environ["E2E_RISK_AFTER"]) if os.environ.get("E2E_RISK_AFTER") else None)
chain.open_at = open_at
credited = []
fake_receipt = chain.receipt


def receipt(txh):
    r = fake_receipt(txh)
    if r and r["status"] == "0x1" and txh not in credited:
        credited.append(txh)
        late = bool(os.environ.get("E2E_LATE")) and len(credited) > int(os.environ["E2E_LATE"])
        raw = len(r["logs"]) * int(os.environ["E2E_CREDIT"])
        record(script="credit", raw=raw, late=late)
        if late:  # these coins reach the balance only when the seller starts
            with open(os.path.join(d, "late"), "a") as f:
                f.write(f"{raw}\n")
        else:
            add_coins(raw)
    return r


ps.START = open_at - 86400
ps.chain_state, ps.receipt, ps.simulate = chain.state, receipt, chain.simulate
ps.baw = chain.baw if sys.argv[1] == "run" else (
    lambda args, *k, **kw: ({"success": True, "data": {"status": "CONNECTED"}}, 0.01))
ps.owner_burned = lambda: True
ps.proof_used = lambda p: (int(p, 16) - 6) in (chain.used_nonces | chain.done)
ps.bnb_balance = lambda: 49_889_000_000_000_000
ps.latest_block = lambda: (1, int(time.time()))
ps.call = lambda data: "0x" + "0" * 64
ps.registry_owner = lambda: ps.WALLET.lower()
log_signals("submit")
ps.main()
'''

# ---- the seller: the real pearl_sell.py on a fake market. A sale of q coins gets RATE * q / (1 + q / DEPTH) BNB:
# 38,400 coins at once is over the 2% impact cap, its 20,000-coin minimum chunk is not. Time is virtual.
SELL_SHIM = r'''
import json, os, signal, sys, time
d, repo = os.environ["E2E_DIR"], os.environ["E2E_REPO"]
sys.path.insert(0, os.path.join(repo, "submit"))
import pearl_sell as ps
COIN, RATE, DEPTH = 10 ** 8, 1 / 431426, 1_300_000
orders, clock, after_fill = {}, [1_800_000_000.0], [0]


def record(**rec):
    with open(os.path.join(d, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def log_signals(tag):
    real = signal.signal

    def wrapped(sig, handler):
        if callable(handler):
            def h(s, frame, _h=handler):
                with open(os.path.join(d, "signals.txt"), "a") as f:
                    f.write(f"{tag} {s}\n")
                return _h(s, frame)
            return real(sig, h)
        return real(sig, handler)
    signal.signal = wrapped


def read_int(path):
    for _ in range(200):  # Windows: a file being replaced can be unreadable for a moment
        try:
            with open(path) as f:
                return int(f.read())
        except (OSError, ValueError):
            time.sleep(0.01)
    raise RuntimeError(f"cannot read {path}")


def write_int(path, v):
    with open(path + f".{os.getpid()}", "w") as f:
        f.write(str(v))
    for _ in range(200):  # a reader sees the old or the new value, never half a file; Windows may refuse a moment
        try:
            os.replace(path + f".{os.getpid()}", path)
            return
        except PermissionError:
            time.sleep(0.01)
    raise RuntimeError(f"cannot write {path}")


def get():
    return read_int(os.path.join(d, "balance"))


def put(v):
    write_int(os.path.join(d, "balance"), v)


def token_balance():
    late = os.path.join(d, "late")
    if os.path.exists(late):  # the run's last coins land now, after pearl.py read the balance
        with open(late) as f:
            put(get() + sum(int(x) for x in f.read().split()))
        os.remove(late)
    if os.environ.get("E2E_STRANGER") and any("bnb" in o for o in orders.values()):
        after_fill[0] += 1
        if after_fill[0] == 2:  # between the first and the second order: coins from elsewhere land
            put(get() + int(os.environ["E2E_STRANGER"]) * COIN)
    return get()


def bnb_out(q):
    return RATE * q / (1 + q / DEPTH)


def signalled():
    p = os.path.join(d, "signals.txt")
    return os.path.exists(p) and "sell " in open(p).read()


def baw(args, timeout=60):
    cmd = args[:2]
    if cmd == ["wallet", "address"]:
        return {"success": True, "data": {"addresses": [{"binanceChainId": "56", "address": ps.WALLET}]}}
    if cmd == ["market-order", "quote"]:
        q = int(args[args.index("--fromTokenQty") + 1])
        return {"success": True, "data": {"toCoinAmount": f"{bnb_out(q):.18f}"}}
    if cmd == ["market-order", "swap"]:
        q = int(args[args.index("--fromTokenQty") + 1])
        oid = str(1000 + len(orders))
        orders[oid] = {"qty": q, "polls": 0}
        record(script="swap", qty=q, order=oid)
        if os.environ.get("E2E_SELL_HOLD"):  # the order is in flight: the test presses Ctrl-C or creates STOP now
            open(os.path.join(d, "order_in_flight"), "w").close()
            for _ in range(500):
                if signalled() or os.path.exists(os.path.join(os.environ["E2E_DATA"], "STOP")):
                    break
                time.sleep(0.02)
        return {"success": True, "data": {"orderId": oid}}
    if cmd == ["market-order", "list"] and "--status" in args:
        return {"success": True, "data": {"total": 0, "list": []}}
    if cmd == ["market-order", "list"]:
        oid = args[args.index("--orderId") + 1]
        o = orders[oid]
        o["polls"] += 1
        if o["polls"] < 2:
            return {"success": True, "data": {"list": [{"orderId": oid, "status": "PENDING"}]}}
        if "bnb" not in o:
            o["bnb"] = bnb_out(o["qty"])
            put(get() - o["qty"] * COIN)
        return {"success": True, "data": {"list": [{"orderId": oid, "status": "FINISHED", "txHash": "0x" + oid.rjust(64, "0"),
                                                    "toTokenActualQty": f"{o['bnb']:.18f}", "refund": None}]}}
    return {"success": False, "error": {"code": "E2E", "message": f"unexpected baw call {args[:2]}"}}


def sleep(sec):
    clock[0] += max(sec, 0.01)
    time.sleep(0.002)


record(script="sell", argv=sys.argv[1:], pid=os.getpid())
ps.baw, ps.token_balance, ps.native_balance = baw, token_balance, lambda: 40 * 10 ** 15
ps.now, ps.sleep = (lambda: clock[0]), sleep
ps.HERE = os.environ["E2E_DATA"]  # its STOP file next to pearl.py's
log_signals("sell")
ps.main()
'''

SCENARIOS = {  # name: extra environment
    "full": {},
    "stop": {},
    "ctrlc-run": {},
    "ctrlc-balance": {},
    "unclean": {"E2E_RISK_AFTER": "1"},
    "late": {"E2E_CAP": "64", "E2E_LATE": "3", "E2E_STRANGER": "5000"},  # 4 txs; the 4th's coins land late
    "ctrlc-sale": {"E2E_SELL_HOLD": "1"},
    "stop-sale": {"E2E_SELL_HOLD": "1"},
}


def read_int(path):
    for _ in range(200):  # Windows: a file being replaced can be unreadable for a moment
        try:
            with open(path) as f:
                return int(f.read())
        except (OSError, ValueError):
            time.sleep(0.01)
    raise RuntimeError(f"cannot read {path}")


def write_int(path, v):
    with open(path + f".{os.getpid()}", "w") as f:
        f.write(str(v))
    for _ in range(200):  # a reader sees the old or the new value, never half a file; Windows may refuse a moment
        try:
            os.replace(path + f".{os.getpid()}", path)
            return
        except PermissionError:
            time.sleep(0.01)
    raise RuntimeError(f"cannot write {path}")


def ctrl_c(work):
    """What the user's Ctrl-C does. Windows: CTRL_C_EVENT to every process on this console (pearl.py's children
    are in groups of their own and do not get it). Elsewhere the terminal signals pearl.py; its children are in
    sessions of their own."""
    try:
        if os.name == "nt":
            os.kill(0, signal.CTRL_C_EVENT)
        else:
            os.kill(os.getpid(), signal.SIGINT)
    except OSError as e:  # no console to send it on
        with open(os.path.join(work, "skip.txt"), "w") as f:
            f.write(repr(e))


def one(scenario, work):
    """pearl.main in this process, with a thread that creates STOP or presses Ctrl-C at the scenario's moment."""
    signal.signal(signal.SIGINT, signal.default_int_handler)  # POSIX children inherit the outer run's SIG_IGN
    sys.path.insert(0, ROOT)
    import pearl
    data = os.path.join(work, "data")
    open_at = time.time() + OPEN_IN
    os.environ["E2E_OPEN_AT"] = repr(open_at)  # the children read it
    ps = pearl.mods()[0]
    ps.START = open_at - 86400
    ps.chain_state = lambda: (0, 43 * COIN, CREDIT)  # pearl.py's bound for the reward of one proof
    reads = [0]

    def balance():
        v = read_int(os.path.join(work, "balance"))
        if scenario == "ctrlc-balance" and pearl.STATE["phase"] == "after":
            open(os.path.join(work, "reading_after"), "w").close()
            reads[0] += 1
            return v + reads[0]  # never the same twice: pearl.py keeps reading
        return v
    pearl.token_balance = balance
    pearl.BEFORE_WAIT, pearl.BALANCE_WAIT = 0.2, 0.4
    pearl.read_answer = lambda prompt: "yes"  # what `echo yes | pearl.py auto --day 1` gives it

    def wait_for(cond, limit=60):
        t0 = time.time()
        while time.time() - t0 < limit:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def act():
        if scenario == "stop":
            if wait_for(lambda: os.path.exists(os.path.join(data, "run.pid"))):
                time.sleep(1.0)
                open(os.path.join(data, "STOP"), "w").close()
        elif scenario == "ctrlc-run":  # once the first transaction's coins arrived: transactions were sent
            if wait_for(lambda: read_int(os.path.join(work, "balance")) > BEFORE * COIN):
                ctrl_c(work)
        elif scenario == "ctrlc-balance":
            if wait_for(lambda: os.path.exists(os.path.join(work, "reading_after"))):
                ctrl_c(work)
        elif scenario == "ctrlc-sale":
            if wait_for(lambda: os.path.exists(os.path.join(work, "order_in_flight"))):
                ctrl_c(work)
        elif scenario == "stop-sale":
            if wait_for(lambda: os.path.exists(os.path.join(work, "order_in_flight"))):
                open(os.path.join(data, "STOP"), "w").close()
    threading.Thread(target=act, daemon=True).start()
    rc = pearl.main(["auto", "--day", "1", "--minutes", "1", "--dir", data, "--csv", os.path.join(work, "none.csv")])
    with open(os.path.join(work, "rc.txt"), "w") as f:
        f.write(str(rc))
    with open(os.path.join(work, "alive_after.txt"), "w") as f:
        f.write(str(pearl.run_alive(os.path.join(data, "run.pid"))))
    return rc


def setup(base):
    work = tempfile.mkdtemp(prefix="e2e-", dir=base)
    data = os.path.join(work, "data")
    os.mkdir(data)
    proofs = [["0x%064x" % (i + 1), "0x%064x" % (i + 7)] for i in range(160)]  # FakeChain: proof = nonce + 6
    with open(os.path.join(data, "proofs.json"), "w") as f:
        json.dump({"wallet": WALLET, "count": len(proofs), "proofs": proofs}, f)
    for name, src in (("e2e_submit.py", SUBMIT_SHIM), ("e2e_sell.py", SELL_SHIM)):
        with open(os.path.join(work, name), "w", encoding="utf-8") as f:
            f.write(src)
    with open(os.path.join(work, "balance"), "w") as f:
        f.write(str(BEFORE * COIN))
    nobaw = os.path.join(work, "nobaw")  # a baw that always fails, first on PATH: nothing here can reach a wallet
    os.mkdir(nobaw)
    with open(os.path.join(nobaw, "baw"), "w") as f:
        f.write("#!/bin/sh\necho '{\"success\": false, \"error\": {\"code\": \"TEST\"}}'\nexit 1\n")
    os.chmod(os.path.join(nobaw, "baw"), 0o755)
    with open(os.path.join(nobaw, "baw.cmd"), "w") as f:
        f.write('@echo {"success": false, "error": {"code": "TEST"}}\r\n@exit /b 1\r\n')
    return work


def outer():
    if os.name == "nt":  # a session started without Ctrl-C handling would ignore CTRL_C_EVENT: switch it on
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the Windows CTRL_C_EVENT reaches this process too
    base = tempfile.mkdtemp(prefix="pearl-test-e2e-")  # everything of this run; removed at the end
    failed = []

    def expect(name, cond, got=None):
        print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond or got is None else f"   got: {got!r}"[:900]),
              flush=True)
        if not cond:
            failed.append(name)
    print(f"platform {sys.platform}, Python {sys.version.split()[0]}", flush=True)
    want_sig = signal.SIGBREAK if os.name == "nt" else signal.SIGINT
    sig_name = "CTRL_BREAK_EVENT (SIGBREAK)" if os.name == "nt" else "SIGINT"
    try:
        for scenario, extra in SCENARIOS.items():
            work = setup(base)
            data = os.path.join(work, "data")
            env = dict(os.environ, PEARL_WALLET=WALLET, PEARL_HANDLE="testhandle1", E2E_DIR=work, E2E_DATA=data,
                       E2E_REPO=ROOT, E2E_CAP=str(CAP), E2E_CREDIT=str(CREDIT), PEARL_SUBMIT_SCRIPT=os.path.join(work, "e2e_submit.py"),
                       PEARL_SELL_SCRIPT=os.path.join(work, "e2e_sell.py"),
                       PEARL_EXPORT_SCRIPT=os.path.join(work, "no_export.py"),
                       PATH=os.path.join(work, "nobaw") + os.pathsep + os.environ.get("PATH", ""))
            for k in ("E2E_RISK_AFTER", "E2E_LATE", "E2E_STRANGER", "E2E_SELL_HOLD"):
                env.pop(k, None)
            env.update(extra)
            t0 = time.time()
            p = subprocess.run([sys.executable, os.path.abspath(__file__), "--one", scenario, work], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=240)
            dt = time.time() - t0

            def rd(name):
                path = os.path.join(work, name)
                return open(path, encoding="utf-8").read() if os.path.exists(path) else None
            out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
            calls = [json.loads(x) for x in (rd("calls.jsonl") or "").splitlines() if x.strip()]
            runc = next((c for c in calls if c["script"] == "submit" and c["cmd"] == "run"), None)
            sells = [c for c in calls if c["script"] == "sell"]
            swaps = [c["qty"] for c in calls if c["script"] == "swap"]
            credits = [c for c in calls if c["script"] == "credit"]
            sigs = (rd("signals.txt") or "").splitlines()
            summary = next((x for x in reversed(out.splitlines()) if "SUMMARY txs=" in x), "")
            mined = int(summary.split("proofs_mined=")[1].split()[0]) if summary else None
            stop = summary.split(" stop=", 1)[1] if " stop=" in summary else None
            rc = rd("rc.txt")
            bal = int(rd("balance")) if rd("balance") else None
            print(f"-- {scenario}: pearl.py exit {rc}, {dt:.1f}s, mined {mined}, stop {stop!r}, swaps {swaps}, "
                  f"signals {sigs}; stderr: {err.strip()[-240:]!r}", flush=True)
            if rd("skip.txt"):
                print(f"  [SKIP] {scenario}: no console to send Ctrl-C on ({rd('skip.txt')})", flush=True)
                continue
            argv = sells[0]["argv"] if sells else []
            arg = (lambda n: argv[argv.index(n) + 1] if n in argv else None)
            expect(f"{scenario}: run.pid names the real submitter (on Windows maybe its venv launcher); it has exited afterwards",
                   runc is not None and rd(os.path.join("data", "run.pid")) is not None
                   and int(rd(os.path.join("data", "run.pid"))) in ((runc["pid"], runc["ppid"]) if os.name == "nt"
                                                                     else (runc["pid"],))
                   and rd("alive_after.txt") == "False", (runc, rd(os.path.join("data", "run.pid")), rd("alive_after.txt")))
            expect(f"{scenario}: never 'nothing was sent' once the run started", "nothing was sent" not in out + err)
            if scenario == "full":
                new = sum(c["raw"] for c in credits)
                ms = int(0.8 * min(new, mined * CREDIT)) if mined else None
                expect("full: the real run filled the cap and stopped normally", mined == CAP and stop is not None
                       and ("budget full" in stop), (mined, stop))
                expect("full: exit 0; the seller got --keep-coins = before + 20% and --max-sell-coins = 80% of the new",
                       rc == "0" and len(sells) == 1 and ms is not None and arg("--max-sell-coins") == f"{ms / COIN:.8f}"
                       and arg("--keep-coins") == f"{(BEFORE * COIN + new - ms) / COIN:.8f}", (rc, argv, err[-300:]))
                expect("full: the real seller split the sale (2% impact cap) and sold exactly that",
                       len(swaps) >= 2 and sum(swaps) * COIN == ms and bal == BEFORE * COIN + new - ms, (swaps, bal))
                try:
                    utf8 = "珍珠币" in p.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    utf8 = False
                expect("full: pearl.py's output is UTF-8 and carries 珍珠币 from the seller's end line", utf8)
                expect("full: no signal anywhere; the summary shows the seller's BNB", not sigs and "sold " in out
                       and " BNB" in out.split("==== summary ====")[-1], (sigs, out[-400:]))
            elif scenario == "stop":
                expect("stop: a real STOP file ends the run with a SUMMARY (stop=STOP file); no sale, exit 4",
                       stop == "STOP file" and rc == "4" and not sells and "STOP exists" in err, (stop, rc, err[-300:]))
                expect("stop: STOP before the opening, nothing mined: no sell command (never --max-sell-coins 0)",
                       mined == 0 and "mined no proofs" in err and "pearl_sell.py run" not in err, (mined, err[-300:]))
            elif scenario == "ctrlc-run":
                expect(f"ctrlc-run: a real Ctrl-C reaches the submitter as {sig_name}, once",
                       sigs == [f"submit {int(want_sig)}"], sigs)
                expect("ctrlc-run: it stopped with a SUMMARY (stop=signal) after sending; no sale, exit 4",
                       stop == "signal (Ctrl-C / SIGTERM)" and (mined or 0) >= 16 and rc == "4" and not sells
                       and "you pressed Ctrl-C" in err, (stop, mined, rc, err[-300:]))
            elif scenario == "ctrlc-balance":
                expect("ctrlc-balance: Ctrl-C while pearl.py reads the balance after the run: exit 4, summary printed",
                       rc == "4" and "==== summary ====" in out and "you pressed Ctrl-C" in err and not sells,
                       (rc, err[-300:]))
                expect("ctrlc-balance: the run itself had ended normally; no child got a signal",
                       stop is not None and "budget full" in stop and not sigs, (stop, sigs))
            elif scenario == "unclean":
                expect("unclean: a preview risk stops the real run after it mined; the sale is blocked, exit 4",
                       (stop or "").startswith("preview refused") and (mined or 0) >= 16 and rc == "4" and not sells
                       and "did not end normally" in err, (stop, mined, rc, err[-300:]))
            elif scenario == "late":
                early = sum(c["raw"] for c in credits if not c["late"])
                late = sum(c["raw"] for c in credits if c["late"])
                ms = int(0.8 * min(early, (mined or 0) * CREDIT))
                expect("late: pearl.py capped the sale at 80% of what it saw arrive (--max-sell-coins)",
                       rc == "0" and late > 0 and arg("--max-sell-coins") == f"{ms / COIN:.8f}", (rc, argv, early, late))
                expect("late: the run's coins that land after pearl.py read the balance, and 5,000 from elsewhere "
                       "between the two orders, are not sold: two orders, exactly --max-sell-coins, exit 0",
                       len(swaps) == 2 and sum(swaps) * COIN == ms
                       and bal == BEFORE * COIN + early + late + 5_000 * COIN - ms, (swaps, ms, bal))
            elif scenario == "ctrlc-sale":
                evs = [json.loads(x).get("ev") for x in (rd(os.path.join("data", "sell.jsonl")) or "").splitlines()
                       if x.strip()]
                expect(f"ctrlc-sale: a real Ctrl-C during an order reaches the seller as {sig_name}",
                       sigs == [f"sell {int(want_sig)}"], sigs)
                expect("ctrlc-sale: the order in flight is followed to its end and journaled, no second order, an end "
                       "record", len(swaps) == 1 and "filled" in evs and evs[-1:] == ["end"], (swaps, evs))
                expect("ctrlc-sale: exit 4 with the summary and the seller's own stop reason", rc == "4"
                       and "==== summary ====" in out and "seller stopped before it was done" in err
                       and "signal (Ctrl-C / SIGTERM): stopping" in err, (rc, err[-300:]))
            elif scenario == "stop-sale":
                evs = [json.loads(x) for x in (rd(os.path.join("data", "sell.jsonl")) or "").splitlines() if x.strip()]
                expect("stop-sale: STOP while an order is in flight: the order is followed to its end, no second "
                       "order, an end record marked not done", len(swaps) == 1 and any(r.get("ev") == "filled" for r in evs)
                       and evs[-1:] and evs[-1].get("ev") == "end" and evs[-1].get("done") is False and not sigs,
                       (swaps, [r.get("ev") for r in evs], sigs))
                expect("stop-sale: the seller exits non-zero, so pearl.py exits 4 and says coins may be sold already",
                       rc == "4" and "seller stopped before it was done" in err and "Some coins may be sold already" in err
                       and "STOP file: stopping" in err and "stopped before the sale was done" not in err, (rc, err[-400:]))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("FAILED:", failed or "none", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    if "--one" in sys.argv:
        i = sys.argv.index("--one")
        sys.exit(one(sys.argv[i + 1], sys.argv[i + 2]))
    sys.exit(outer())
