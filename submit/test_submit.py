# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Offline tests for pearl_submit.Runner with a fake baw and a fake chain.

  python3 test_submit.py
"""
import collections
import re
import shutil
import signal
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pearl_submit as ps  # noqa: E402

ps.safe_stdio()
REAL_BAW = ps.baw  # run_case swaps in the fake chain's baw; platform_checks() starts a real process
ps.configure("0x00000000000000000000000000000000DeaDBeef", "demoagent")  # dummy identity: the fake chain does not check it
REAL_CHAIN_STATE = ps.chain_state  # run_case replaces ps.chain_state with the fake chain's

REW = 95 * 10**8
LEN_AT = 2 + 8 + 64 * 3
OK_RISKS = {"riskDetails": [], "addresses": {}, "riskBehaviors": []}


class FakeChain:
    def __init__(self, open_in, cap_proofs, risk_after=None, timeout_on=None, baw_lag=0.0,
                 bad_revert=None, transient_previews=0, busy_window=0.1, used_nonces=(), stale_preview=False):
        self.open_at = time.time() + open_in
        self.cap = cap_proofs
        self.mined = 0
        self.lock = threading.Lock()
        self.reqs = {}
        self.rid = 0
        self.txs = {}
        self.n_exec = 0
        self.risk_after = risk_after
        self.timeout_on = timeout_on
        self.baw_lag = baw_lag
        self.bad_revert = bad_revert
        self.transient_left = transient_previews
        self.used_nonces = set(used_nonces)  # already used on chain before the run
        self.stale_preview = stale_preview   # backend approves batches that no longer fit the day cap
        self.busy_window = busy_window
        self.busy_until = 0.0
        self.n_busy = 0
        self.sent_proofs = collections.Counter()
        self.done = set()      # nonces mined on the fake chain
        self.flying = set()    # nonces in a tx whose receipt is not decided yet
        self.violations = []   # nonce sent again while mined or still in flight

    def state(self):
        with self.lock:
            day = 1 if time.time() >= self.open_at else 0
            rem = (self.cap - self.mined) * REW if day == 1 else 43 * 10**8
            return day, rem, REW

    def batch_len(self, cd):
        return int(cd[LEN_AT:LEN_AT + 64], 16)

    def nonces_of(self, cd):
        n = self.batch_len(cd)
        return [int(cd[LEN_AT + 64 * (i + 1):LEN_AT + 64 * (i + 2)], 16) for i in range(n)]

    def simulate(self, cd):
        if self.bad_revert:
            return "revert", self.bad_revert
        if set(self.nonces_of(cd)) & (self.used_nonces | self.done):
            return "revert", ps.PROOF_USED
        day, rem, rew = FakeChain.state(self)  # internal: never the per-test override of our reads
        if day == 0 or rem < rew * self.batch_len(cd):
            return "revert", ps.DAILY_LIMIT
        return "ok", None

    def baw(self, args, journal=None, timeout=60, launch_lock=None, gate=None):
        if launch_lock is not None:
            with launch_lock:
                if gate is not None and not gate():
                    j = {"success": False, "error": {"code": ps.NOT_LAUNCHED, "message": "stopped"}}
                    if journal:
                        journal.write(ev="baw", args=args, resp=j)
                    return j, 0.0
        time.sleep(0.05)
        if args[:2] == ["contract-call", "preview"]:
            cd = args[args.index("--inputData") + 1]
            n = self.batch_len(cd)
            nonces = [int(cd[LEN_AT + 64 * (i + 1):LEN_AT + 64 * (i + 2)], 16) for i in range(n)]
            day, rem, rew = FakeChain.state(self)  # internal: never the per-test override of our reads
            lagging = time.time() < self.open_at + self.baw_lag
            if self.transient_left > 0 and day == 1:
                self.transient_left -= 1
                j = {"success": False, "error": {"code": "TIMEOUT", "message": "baw preview > 60s"}}
            elif (self.bad_revert or day == 0 or lagging or (rem < rew * n and not self.stale_preview)
                  or set(nonces) & (self.used_nonces | self.done)):
                j = {"success": False, "error": {"code": 351805, "name": "SERVICE_ERROR",
                                                 "message": "Transaction simulation failed: execution reverted"}}
            else:
                self.rid += 1
                rid = f"r{self.rid}"
                self.reqs[rid] = nonces
                risks = json.loads(json.dumps(OK_RISKS))
                if self.risk_after is not None and self.n_exec >= self.risk_after:
                    risks["riskDetails"] = [{"code": "RISK_X", "title": "t", "riskType": "RISK"}]
                j = {"success": True, "data": {"requestId": rid, "requireConfirmation": False, "gasLimit": 620000,
                                               "simulationResult": {"simulationCode": "000000000"},
                                               "risks": risks, "expiresAt": int((time.time() + 300) * 1000)}}
        elif args[:2] == ["contract-call", "execute"]:
            rid = args[args.index("--requestId") + 1]
            with self.lock:
                busy = time.time() < self.busy_until
                if busy:
                    self.n_busy += 1
            if busy:  # like the real backend: previous tx of this address still in progress
                j = {"success": False, "error": {"code": 351716, "name": "SERVICE_ERROR",
                                                 "message": "A transaction to the same address on this network is already in progress."}}
                if journal:
                    journal.write(ev="baw", args=args, resp=j)
                return j, 0.05
            nonces = self.reqs.pop(rid)
            with self.lock:
                self.n_exec += 1
                k = self.n_exec
            if self.timeout_on == k:
                j = {"success": False, "error": {"code": "TIMEOUT", "message": "x"}}
            else:
                txh = "0x%064x" % (k + 10**6 + int(self.open_at) * 1000)
                for nn in nonces:
                    self.sent_proofs[nn] += 1
                    if nn in self.done or nn in self.flying:
                        self.violations.append(nn)
                    self.flying.add(nn)
                self.txs[txh] = (nonces, time.time() + 0.4)
                with self.lock:
                    self.busy_until = time.time() + 0.4 + self.busy_window
                j = {"success": True, "data": {"status": "BROADCASTED", "txHash": txh}}
        elif args[:2] == ["wallet", "status"]:
            j = {"success": True, "data": {"status": getattr(self, "wallet_status", "CONNECTED")}}
        else:
            j = {"success": True, "data": {}}
        if journal:
            journal.write(ev="baw", args=args, resp=j)
        return j, 0.05

    def receipt(self, txh):
        if txh not in self.txs:
            return None
        nonces, t_ready = self.txs[txh]
        if time.time() < t_ready:
            return None
        with self.lock:
            if "_r" + txh not in self.txs:
                ok = self.mined + len(nonces) <= self.cap
                if ok:
                    self.mined += len(nonces)
                    self.done.update(nonces)
                self.flying.difference_update(nonces)
                self.txs["_r" + txh] = ok
            ok = self.txs["_r" + txh]
        logs = [{"address": ps.CONTRACT, "topics": [ps.MINED_TOPIC]}] * (len(nonces) if ok else 0)
        return {"status": "0x1" if ok else "0x0", "gasUsed": hex(570000), "effectiveGasPrice": hex(80000000),
                "logs": logs, "blockNumber": "0x10"}


def make_proofs(path, n):
    proofs = [["0x%064x" % (i + 1), "0x%064x" % (i + 7)] for i in range(n)]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"wallet": ps.WALLET, "proofs": proofs}, f)


def run_case(name, chain, n_proofs, journal=None, **kw):
    tmp = tempfile.mkdtemp()
    pf = os.path.join(tmp, "proofs.json")
    make_proofs(pf, n_proofs)
    ps.baw = chain.baw
    ps.chain_state = chain.state
    ps.receipt = chain.receipt
    ps.simulate = chain.simulate
    ps.owner_burned = getattr(chain, "owner_burned", lambda: True)  # mainnet: owner() is 0x...dEaD
    ps.proof_used = lambda p: (int(p, 16) - 6) in (chain.used_nonces | chain.done)
    ps.bnb_balance = lambda: 49_889_000_000_000_000
    ps.START = chain.open_at - 86400
    a = types.SimpleNamespace(proofs=pf, journal=journal or os.path.join(tmp, "j.jsonl"),
                              day=1, budget_bnb=0.045, keep_bnb=0.005, minutes=0.5, previewers=2, executors=1,
                              max_inflight=30, lead=0.6, stagger=0.3, settle=1.5, stop_file=os.path.join(tmp, "STOP"))
    for k, v in kw.items():
        setattr(a, k, v)
    r = ps.Runner(a)
    t0 = time.time()
    try:
        r.run()
    finally:
        r.sender_lock.release()
    dup = list(chain.violations)
    print(f"[{name}] {time.time() - t0:.1f}s sent={r.sent} mined={r.mined} reverted={r.reverted} "
          f"stop={r.stop_reason!r} unsafe_resends={len(dup)} resends_after_revert={sum(1 for c_ in chain.sent_proofs.values() if c_ > 1) - len(set(dup))}", flush=True)
    return r, a, dup


FAKE_CLI = r'''
import json, os, subprocess, sys, time
sys.stdout.reconfigure(encoding="utf-8")
if os.environ.get("FAKE_CLI_PIDFILE"):
    with open(os.environ["FAKE_CLI_PIDFILE"], "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
if os.environ.get("FAKE_CLI_GRANDCHILD"):  # a child of its own that inherits stdout, like node's helpers would
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    with open(os.environ["FAKE_CLI_GRANDCHILD"], "w", encoding="utf-8") as f:
        f.write(str(g.pid))
if os.environ.get("FAKE_CLI_SLEEP"):
    time.sleep(float(os.environ["FAKE_CLI_SLEEP"]))
print(json.dumps({"success": True, "data": {"args": sys.argv[1:], "symbol": "\u73cd\u73e0\u5e01"}}, ensure_ascii=False))
'''


def make_fake_cli(d):
    """A fake baw in d, started the way the real one is: an executable on POSIX, a .cmd shim (cmd.exe) on Windows."""
    script = os.path.join(d, "fake_cli.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(FAKE_CLI)
    if ps.WINDOWS:
        path = os.path.join(d, "baw.cmd")
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write(f'@"{sys.executable}" "%~dp0fake_cli.py" %*\r\n')
    else:
        path = os.path.join(d, "baw")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        os.chmod(path, 0o755)
    return path


def gone_soon(pidfile):
    """True once the process in pidfile has ended (pearl_sell's own check), within 5 s."""
    import pearl_sell
    for _ in range(50):
        if not pearl_sell.mining_run_alive(pidfile):
            return True
        time.sleep(0.1)
    return False


def platform_checks(expect):
    """The pieces that differ between macOS, Linux and Windows, with real files and processes."""
    def check(name, cond, got=None):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond or got is None else f"   got: {got!r}"))
        expect(name, cond)

    print(f"[platform] {sys.platform}, Python {sys.version.split()[0]}, stdout encoding {sys.stdout.encoding}")
    here = os.path.dirname(os.path.abspath(__file__))
    tmp = tempfile.mkdtemp(prefix="xplat-")

    # journal lock: refused in this process and in another one while held, also when the other process names the
    # same file differently (relative path); free again after release()
    jp = os.path.join(tmp, "j.jsonl")
    j1 = ps.Journal(jp)
    lk = ps.SenderLock(j1)
    try:
        ps.SenderLock(ps.Journal(jp))
        same = "taken twice"
    except OSError:
        same = "refused"
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); import pearl_submit as ps\n"
             "try:\n    ps.SenderLock(ps.Journal(sys.argv[2])).release()\nexcept OSError:\n    sys.exit(3)\n")
    other = subprocess.run([sys.executable, "-c", probe, here, jp], timeout=60).returncode
    os.makedirs(os.path.join(tmp, "sub"))
    rel = os.path.join("sub", os.pardir, "j.jsonl")  # same file, other spelling, from another working directory
    other_rel = subprocess.run([sys.executable, "-c", probe, here, rel], cwd=tmp, timeout=60).returncode
    j1.write(ev="note", text="\u73cd\u73e0\u5e01")  # the journal stays writable and readable while locked
    back = [r.get("text") for r in ps.Journal(jp).read()]
    lk.release()
    other_after = subprocess.run([sys.executable, "-c", probe, here, rel], cwd=tmp, timeout=60).returncode
    check("lock: a second holder in this process is refused", same == "refused", same)
    check("lock: another process is refused while held", other == 3, other)
    check("lock: another process using a relative path to the same file is refused", other_rel == 3, other_rel)
    check("lock: the journal is read back while locked (UTF-8)", back == ["\u73cd\u73e0\u5e01"], back)
    check("lock: another process gets it after release()", other_after == 0, other_after)
    check("lock: no side file next to the journal", sorted(os.listdir(tmp)) == ["j.jsonl", "sub"], os.listdir(tmp))

    # cmd.exe guard (checked on every OS by pretending to be on Windows)
    saved = ps.WINDOWS, ps.BAW
    ps.WINDOWS, ps.BAW = True, r"C:\npm\baw.cmd"
    bads = ("a&b", "100%", "x y", 'q"', "a|b", "x^", "!x", "<x", "x>", "a\nb", "a\tb", "a\rb", "a(b", "a)b",
            "0xab\n", "\u73cd", "")
    try:
        ok_cmd = ps.baw_cmd(["contract-call", "preview", "--inputData", "0xab12", "--slippage", "1.5"])
        refused = []
        for bad in bads:
            try:
                ps.baw_cmd(["market-order", bad])
            except ps.UnsafeArg:
                refused.append(bad)
        path_refused = []
        for path in (r"C:\Users\R&D\AppData\Roaming\npm\baw.cmd", r"C:\Users\a%b\npm\baw.cmd",
                     r"C:\Users\x!y z\npm\baw.cmd", r"C:\a(b)\baw.cmd"):
            ps.BAW = path
            try:
                ps.baw_cmd(["wallet", "status"])
            except ps.UnsafeArg:
                path_refused.append(path)
        path_ok = [p_ for p_ in (r"C:\Users\Jo Smith\AppData\Roaming\npm\baw.cmd", r"C:\Program Files (x86)\npm\baw.cmd",
                                 "C:\\Users\\\u738b\\AppData\\Roaming\\npm\\baw.cmd")
                   if ps.cmd_path_problem(p_) is None]
        ps.BAW = r"C:\Program Files\baw\baw.exe"  # an .exe gets its arguments without cmd.exe: no guard
        exe_cmd = ps.baw_cmd(["x y"])
    finally:
        ps.WINDOWS, ps.BAW = saved
    check("cmd.exe guard: plain arguments pass", ok_cmd[-1] == "--json" and ok_cmd[0].endswith("baw.cmd"))
    check("cmd.exe guard: & % space quote | ^ ! < > newline tab CR ( ) trailing newline, non-ASCII and empty refused",
          refused == list(bads), [b for b in bads if b not in refused])
    check("cmd.exe guard: a baw path cmd.exe would split or expand is refused", len(path_refused) == 4, path_refused)
    check("cmd.exe guard: spaces, (x86) inside quotes and non-ASCII user names are fine", len(path_ok) == 3, path_ok)
    check("cmd.exe guard: not applied to an .exe", exe_cmd[1:] == ["x y", "--json"])
    spaced = (r"C:\Users\R&D Team\npm\baw.cmd", r"C:\a b\x^y\baw.cmd", r"C:\a b\x|y\baw.cmd", r"C:\a b\x>y\baw.cmd")
    check("N2: & | < > ^ in a path with spaces are refused too (npm's baw.cmd runs SET dp0=%~dp0 unquoted)",
          all(ps.cmd_path_problem(p_) for p_ in spaced), [p_ for p_ in spaced if not ps.cmd_path_problem(p_)])
    if not ps.WINDOWS:  # macOS: an exited, unreaped session leader has no pgid; its group must still be killed

        class FakeLeader:
            pid, returncode, pearl_own_group = 4206649, None, True

            def kill(self):
                pass
        killed, saved_gp, saved_kp = [], os.getpgid, os.killpg
        os.getpgid = lambda pid: (_ for _ in ()).throw(ProcessLookupError())
        os.killpg = lambda pg, sig: killed.append(pg)
        try:
            ps.kill_tree(FakeLeader())
        finally:
            os.getpgid, os.killpg = saved_gp, saved_kp
        check("N3: getpgid fails with ESRCH (macOS zombie leader): killpg(p.pid) all the same", killed == [4206649],
              killed)
    kind, _ = ps.judge_preview({"success": False, "error": {"code": ps.UNSAFE_ARG,
                                                           "message": "argument not safe: 'timeout 429'"}})
    check("UNSAFE_ARG from a preview stops the run (refuse, even with transient words)", kind == "refuse", kind)
    good = {"success": True, "data": {"requestId": "0f3a9c2e" * 4, "requireConfirmation": False, "gasLimit": 620000,
                                      "simulationResult": {"simulationCode": "000000000"}, "risks": OK_RISKS,
                                      "expiresAt": int((time.time() + 300) * 1000)}}
    kinds = {}
    for rid in ("0f3a9c2e" * 4, "abc&def", "0xab\n", "id with space", "\u73cd"):
        j = json.loads(json.dumps(good))
        j["data"]["requestId"] = rid
        kinds[rid] = ps.judge_preview(j)[0]
    check("requestId: a plain token is accepted, anything cmd.exe would read is refused before an attempt",
          kinds == {"0f3a9c2e" * 4: "ok", "abc&def": "refuse", "0xab\n": "refuse", "id with space": "refuse",
                    "\u73cd": "refuse"}, kinds)

    # a real baw process found on PATH: arguments, UTF-8 output, and a timeout that kills the whole process tree
    bindir = tempfile.mkdtemp(prefix="xplat-bin-")
    fake = make_fake_cli(bindir)
    saved_path, saved_baw = os.environ.get("PATH", ""), ps.BAW
    os.environ["PATH"] = bindir + os.pathsep + saved_path
    try:
        found = ps.find_baw()
        check("find_baw: the fake on PATH" + (" (baw.cmd)" if ps.WINDOWS else ""),
              os.path.normcase(found or "") == os.path.normcase(fake), found)
        ps.BAW = found
        j, dt = REAL_BAW(["wallet", "status"], timeout=30)
        d = j.get("data") or {}
        check("baw: arguments arrive unchanged", d.get("args") == ["wallet", "status", "--json"], j)
        check("baw: UTF-8 output decoded", d.get("symbol") == "\u73cd\u73e0\u5e01", d.get("symbol"))
        pidfile, gpidfile = os.path.join(tmp, "cli.pid"), os.path.join(tmp, "grandchild.pid")
        os.environ.update(FAKE_CLI_SLEEP="30", FAKE_CLI_PIDFILE=pidfile, FAKE_CLI_GRANDCHILD=gpidfile)
        t0 = time.time()
        j, _ = REAL_BAW(["wallet", "status"], timeout=3)
        took = time.time() - t0
        for k in ("FAKE_CLI_SLEEP", "FAKE_CLI_PIDFILE", "FAKE_CLI_GRANDCHILD"):
            os.environ.pop(k)
        check("baw timeout: TIMEOUT within a few seconds, though a grandchild holds the output pipe",
              (j.get("error") or {}).get("code") == "TIMEOUT" and took < 9, (j, round(took, 1)))
        check("baw timeout: the fake process is gone", gone_soon(pidfile))
        check("baw timeout: its own child is gone too (whole tree killed)", gone_soon(gpidfile))
        if ps.WINDOWS:  # a baw.cmd in a folder with spaces and (x86): subprocess quotes it, cmd.exe must run it
            qdir = os.path.join(tempfile.mkdtemp(prefix="xplat-q-"), "npm dir (x86)")
            os.makedirs(qdir)
            ps.BAW = make_fake_cli(qdir)
            j, _ = REAL_BAW(["wallet", "status"], timeout=30)
            check("baw.cmd in a folder with spaces and (x86) runs", (j.get("data") or {}).get("args")
                  == ["wallet", "status", "--json"], j)
    finally:
        os.environ["PATH"] = saved_path
        ps.BAW = saved_baw


def main():
    """Every temp dir of this run goes under one dir of its own, removed at the end, also after a failure."""
    own, saved = tempfile.mkdtemp(prefix="pearl-test-submit-"), tempfile.tempdir
    tempfile.tempdir = own
    try:
        return _main()
    finally:
        tempfile.tempdir = saved
        shutil.rmtree(own, ignore_errors=True)


def _main():
    fails = []

    checks = [0]

    def expect(name, cond):
        checks[0] += 1
        if not cond:
            fails.append(name)

    c = FakeChain(open_in=2, cap_proofs=16 * 10)
    r, a, dup = run_case("cap fills", c, 16 * 40)
    expect("cap fills", not dup and r.mined == 160 and re.search(r"budget (is )?full", r.stop_reason or ""))

    c = FakeChain(open_in=1, cap_proofs=10**6, risk_after=3)
    r, a, dup = run_case("risk item", c, 16 * 40)
    expect("risk item", not dup and "risk" in (r.stop_reason or "") and r.sent <= 3 + 2 + 2)

    c = FakeChain(open_in=1, cap_proofs=16 * 6, timeout_on=2)
    r, a, dup = run_case("execute timeout", c, 16 * 40)
    j = ps.Journal(a.journal).read()
    unknown = {p for rec in j if rec.get("ev") == "exec_unknown" for p in rec["proofs"]}
    broadcast = {p for rec in j if rec.get("ev") == "broadcast" for p in rec["proofs"]}
    expect("execute timeout", not dup and len(unknown) == 16 and not (unknown & broadcast))

    blocked, _, reserved, _, _ = ps.blocked_from_journal(j)
    print(f"   reserved for the unknown execute: {reserved / 1e18:.6f} BNB")
    succeeded = set()
    tx_proofs = {rec["tx"]: rec["proofs"] for rec in j if rec.get("ev") == "broadcast"}
    for rec in j:
        if rec.get("ev") == "receipt" and rec.get("status") == 1:
            succeeded.update(tx_proofs[rec["tx"]])
    c2 = FakeChain(open_in=1, cap_proofs=16 * 4)
    r2, a2, _ = run_case("restart", c2, 16 * 40, journal=a.journal)
    sent2 = {"0x%064x" % (n + 6) for n in c2.sent_proofs}  # proof hex = nonce + 6 in make_proofs
    expect("restart", (succeeded | unknown) <= blocked and not (sent2 & (succeeded | unknown)))

    c = FakeChain(open_in=1, cap_proofs=16 * 3 + 5)
    r, a, dup = run_case("partial last batch", c, 16 * 10)
    expect("partial last batch", not dup and r.mined >= 52 and re.search(r"budget (is )?full", r.stop_reason or ""))

    c = FakeChain(open_in=1, cap_proofs=16 * 5, baw_lag=3.0)
    r, a, dup = run_case("baw simulation lags 3s", c, 16 * 20)
    expect("baw lag", not dup and r.mined == 80)

    c = FakeChain(open_in=1, cap_proofs=16 * 5, bad_revert="0xd9c1988f")
    r, a, dup = run_case("InvalidSolution", c, 16 * 20)
    expect("InvalidSolution", r.sent == 0 and "InvalidSolution" in (r.stop_reason or ""))

    c = FakeChain(open_in=1, cap_proofs=16 * 5, transient_previews=3)
    r, a, dup = run_case("transient preview errors", c, 16 * 20)
    expect("transient", not dup and r.mined == 80)

    c = FakeChain(open_in=1, cap_proofs=10**6)
    r, a, dup = run_case("budget", c, 16 * 400, budget_bnb=0.0005)
    expect("budget", not dup and r.fees <= 0.0005e18 and "budget" in (r.stop_reason or ""))

    c = FakeChain(open_in=1, cap_proofs=16 * 6, busy_window=2.5)
    r, a, dup = run_case("backend busy 2.5s after each tx (351716)", c, 16 * 20)
    j = ps.Journal(a.journal).read()
    rejected = sum(1 for rec in j if rec.get("ev") == "exec_rejected")
    print(f"   351716 rejections: {rejected}")
    expect("busy", not dup and r.mined == 96 and rejected > 0)

    c = FakeChain(open_in=1, cap_proofs=16 * 6, used_nonces={3, 20})
    r, a, dup = run_case("2 proofs already used on chain", c, 16 * 20)
    j = ps.Journal(a.journal).read()
    dropped = {p for rec in j if rec.get("ev") == "used_elsewhere" for p in rec["proofs"]}
    print(f"   dropped as used: {len(dropped)}")
    expect("used proofs", not dup and r.mined == 96 and len(dropped) == 2)

    # STOP file mid-run: after the stop record, no execute may be launched
    c = FakeChain(open_in=1, cap_proofs=10**6)
    tmp2 = tempfile.mkdtemp()
    threading.Timer(3.0, lambda: open(os.path.join(tmp2, "STOP"), "w", encoding="utf-8").close()).start()
    r, a, dup = run_case("STOP file mid-run", c, 16 * 200, stop_file=os.path.join(tmp2, "STOP"))
    j = ps.Journal(a.journal).read()
    t_stop = min(rec["t"] for rec in j if rec.get("ev") == "stop")
    late = [rec for rec in j if rec.get("ev") == "baw" and rec["args"][:2] == ["contract-call", "execute"]
            and rec["t"] > t_stop + 0.2 and (rec.get("resp", {}).get("error") or {}).get("code") != ps.NOT_LAUNCHED]
    print(f"   executes launched after stop: {len(late)}")
    expect("STOP file", not dup and "STOP" in (r.stop_reason or "") and not late and r.sent > 0)

    # used proofs stay blocked across restarts even if a later receipt for their tx reverted
    recs = [{"ev": "attempt", "requestId": "x", "proofs": ["0x1"], "est": 1},
            {"ev": "broadcast", "tx": "0xt", "requestId": "x", "proofs": ["0x1"], "nonces": ["0x1"]},
            {"ev": "used_elsewhere", "proofs": ["0x" + "0" * 63 + "1"]},
            {"ev": "receipt", "tx": "0xt", "status": 0, "fee": 5}]
    blocked, fees, reserved, open_, used = ps.blocked_from_journal(recs)
    print(f"   replay: blocked={sorted(blocked)} used={sorted(used)} fees={fees} reserved={reserved} open={list(open_)}")
    expect("used replay", ps.canon("0x1") in blocked and ps.canon("0x1") in used and fees == 5
           and reserved == 0 and not open_)

    # restart: a used proof sits in a broadcast whose receipt turns out reverted at start -> still never reused
    c = FakeChain(open_in=1, cap_proofs=16 * 4, used_nonces={1})
    p1 = "0x%064x" % 7  # make_proofs: nonce 1 -> proof 7
    txh = "0x" + "ab" * 32
    c.txs[txh] = ([1], 0.0)
    c.txs["_r" + txh] = False
    tmp3 = tempfile.mkdtemp()
    jp3 = os.path.join(tmp3, "j.jsonl")
    with open(jp3, "w", encoding="utf-8") as f:
        for rec in [{"ev": "attempt", "requestId": "x", "proofs": [p1], "est": 1, "t": 1},
                    {"ev": "broadcast", "tx": txh, "requestId": "x", "proofs": [p1], "nonces": ["0x1"], "est": 1, "t": 2},
                    {"ev": "used_elsewhere", "proofs": [p1], "t": 3}]:
            f.write(json.dumps(rec) + "\n")
    r, a, dup = run_case("restart: used proof in a reverted broadcast", c, 16 * 4, journal=jp3)
    print(f"   usable at start: {r.n_usable} (of {16 * 4})")
    expect("used stays out after reverted receipt", r.n_usable == 16 * 4 - 1 and c.sent_proofs[1] == 0 and not dup)

    # an unexplained simulation failure appears while the executor is settling: nothing may launch
    # from the moment the diagnosis starts until the run stops
    c = FakeChain(open_in=1, cap_proofs=10**6, busy_window=0.0)
    diag = {}
    real_sim = c.simulate

    def slow_sim(cd):
        if c.bad_revert:
            diag.setdefault("t", time.time())
            time.sleep(1.5)
        return real_sim(cd)
    c.simulate = slow_sim
    threading.Timer(3.0, lambda: setattr(c, "bad_revert", "0xd9c1988f")).start()
    r, a, dup = run_case("unexplained failure during settle", c, 16 * 200)
    j = ps.Journal(a.journal).read()
    t_d = diag.get("t", float("inf"))
    late = [rec for rec in j if rec.get("ev") == "baw" and rec["args"][:2] == ["contract-call", "execute"]
            and rec["t"] > t_d + 0.2 and (rec.get("resp", {}).get("error") or {}).get("code") != ps.NOT_LAUNCHED]
    print(f"   executes launched after diagnosis began: {len(late)}")
    expect("no launch during diagnosis", not dup and "unexpected simulation failure" in (r.stop_reason or "")
           and not late and r.sent > 0)

    # near the cap, a backend that keeps approving stale 16-proof previews must not livelock the executor:
    # it splits the batch to the room left and fills the cap exactly
    c = FakeChain(open_in=1, cap_proofs=16 * 3 + 5, stale_preview=True)
    r, a, dup = run_case("stale previews near the cap", c, 16 * 10)
    print(f"   mined {r.mined} of cap {16 * 3 + 5}")
    expect("near-cap split", not dup and r.mined == 16 * 3 + 5 and "budget full" in (r.stop_reason or ""))

    # older journals: a reverted receipt written before its broadcast record must still unblock the proofs
    recs = [{"ev": "attempt", "requestId": "y", "proofs": ["0x2"], "est": 1},
            {"ev": "receipt", "tx": "0xu", "status": 0, "fee": 7},
            {"ev": "broadcast", "tx": "0xu", "requestId": "y", "proofs": ["0x2"], "nonces": ["0x2"]}]
    blocked, fees, reserved, open_, used = ps.blocked_from_journal(recs)
    print(f"   receipt-before-broadcast replay: blocked={sorted(blocked)} fees={fees} reserved={reserved} open={list(open_)}")
    expect("receipt before broadcast", ps.canon("0x2") not in blocked and fees == 7 and reserved == 0 and not open_)

    # Multicall3 aggregate3 encode/decode round trip (one consistent snapshot of day/remaining/reward)
    cd = ps._aggregate3_calldata([ps.SEL_MINING_DAY, ps.SEL_DAILY_REMAINING, ps.SEL_CURRENT_REWARD])
    fake = "0x" + "".join(format(x, "064x") for x in (
        0x20, 3, 0x60, 0xe0, 0x160,
        1, 0x40, 32, 1,
        1, 0x40, 32, 4345679227,
        1, 0x40, 32, 9560493732))
    dec = ps._aggregate3_decode(fake, 3)
    print(f"   multicall: calldata {len(cd) // 2 - 1} bytes, decoded {dec}")
    expect("multicall", cd.startswith("0x82ad56cb") and (len(cd) - 10) // 64 == 20 and dec == [1, 4345679227, 9560493732])

    # a malformed multicall answer is an RPC failure (RuntimeError), never a crash of the watcher
    real_rpc = ps.rpc
    bad = 0
    zero_rew = fake[:-64] + "0" * 64
    for junk in ("0xzz", "0x" + "00" * 10, {"x": 1}, None, fake[:-62], zero_rew, fake + "0"):
        ps.rpc = lambda m, p, junk=junk: junk
        try:
            REAL_CHAIN_STATE()
        except RuntimeError:
            bad += 1
        except Exception as e:  # noqa: BLE001
            print(f"   multicall junk {junk!r} raised {e!r}")
    ps.rpc = real_rpc
    expect("multicall junk -> RuntimeError", bad == 7)

    # after the opening grace, a baw failure that our own call cannot reproduce is unexplained: stop
    old_grace = ps.LAG_GRACE
    ps.LAG_GRACE = 0.5
    try:
        c = FakeChain(open_in=1, cap_proofs=10**6, baw_lag=6.0)
        r, a, dup = run_case("baw fails long after the opening", c, 16 * 20)
    finally:
        ps.LAG_GRACE = old_grace
    expect("unexplained after grace", not dup and r.sent == 0 and "unexplained" in (r.stop_reason or ""))

    # inside the opening grace, one baw failure our call cannot reproduce, then baw approves the same batch on
    # the re-preview: explained (a batch cannot go from failure back to success within a day), no stop
    c = FakeChain(open_in=1, cap_proofs=16 * 40)  # far from the cap, so only the re-preview can explain it
    flip = {"n": 0}
    real_baw = c.baw

    def baw_once(args, journal=None, **kw):
        if args[:2] == ["contract-call", "preview"] and time.time() > c.open_at + 1.0 and flip["n"] == 0:
            flip["n"] = 1
            return {"success": False, "error": {"code": 351805, "message": "Transaction simulation failed: execution reverted"}}, 0.05
        return real_baw(args, journal, **kw)
    c.baw = baw_once
    r, a, dup = run_case("one baw failure in the grace, re-preview approves", c, 16 * 20)
    expect("re-preview explains", not dup and flip["n"] == 1 and r.mined == 16 * 20 and r.stop_reason is None)

    # baw keeps failing ONE batch our own eth_call accepts: nothing may launch while that is unresolved,
    # and the run stops when the opening grace ends without baw approving it
    ps.LAG_GRACE = 3.0
    try:
        c = FakeChain(open_in=1, cap_proofs=10**6)
        real_baw2 = c.baw
        mark = {}
        word40 = format(40, "064x")

        def baw_one(args, journal=None, **kw):
            if (args[:2] == ["contract-call", "preview"] and word40 in args[args.index("--inputData") + 1]
                    and time.time() > c.open_at):
                mark.setdefault("t", time.time())
                return {"success": False, "error": {"code": 351805, "message": "Transaction simulation failed: execution reverted"}}, 0.05
            return real_baw2(args, journal, **kw)
        c.baw = baw_one
        r, a, dup = run_case("one batch keeps failing on baw only", c, 16 * 30)
    finally:
        ps.LAG_GRACE = old_grace
    j = ps.Journal(a.journal).read()
    t_m = mark.get("t", float("inf"))
    late = [x for x in j if x.get("ev") == "baw" and x["args"][:2] == ["contract-call", "execute"] and x["t"] > t_m + 0.2
            and (x.get("resp", {}).get("error") or {}).get("code") != ps.NOT_LAUNCHED]
    print(f"   executes launched while the discrepancy was unresolved: {len(late)}")
    expect("hold while unexplained", not dup and "t" in mark and not late and "unexplained" in (r.stop_reason or ""))

    def fail_first_preview_of_each_batch(c, slow_repreview=0.0):
        """baw fails the first post-opening preview of every batch; later previews of it are approved."""
        seen = set()
        inner = c.baw

        def baw_x(args, journal=None, **kw):
            if args[:2] == ["contract-call", "preview"] and time.time() > c.open_at:
                cd = args[args.index("--inputData") + 1]
                if cd not in seen:
                    seen.add(cd)
                    return {"success": False, "error": {"code": 351805, "message": "Transaction simulation failed: execution reverted"}}, 0.05
                time.sleep(slow_repreview)
            return inner(args, journal, **kw)
        c.baw = baw_x
        return seen

    # explained failures must not pile up toward the unexplained-failure limit (20 in the opening minute)
    ps.LAG_GRACE = 60.0
    try:
        c = FakeChain(open_in=1, cap_proofs=16 * 40)
        seen = fail_first_preview_of_each_batch(c)
        r, a, dup = run_case("every batch fails once then baw approves (25 batches)", c, 16 * 25)
    finally:
        ps.LAG_GRACE = old_grace
    expect("explained lags do not accumulate", not dup and len(seen) >= 21 and r.mined == 16 * 25 and r.stop_reason is None)

    # an approval that comes back after the opening grace does not count
    ps.LAG_GRACE = 1.0
    try:
        c = FakeChain(open_in=1, cap_proofs=16 * 40)
        fail_first_preview_of_each_batch(c, slow_repreview=1.5)
        r, a, dup = run_case("approval returns after the grace", c, 16 * 10)
    finally:
        ps.LAG_GRACE = old_grace
    expect("late approval stops", not dup and "after the opening grace" in (r.stop_reason or ""))

    # if the owner could pause/resume, a re-preview approval proves nothing: stop
    c = FakeChain(open_in=1, cap_proofs=16 * 40)
    c.owner_burned = lambda: False
    fail_first_preview_of_each_batch(c)
    r, a, dup = run_case("owner not burned", c, 16 * 10)
    expect("live owner stops", not dup and "owner could pause" in (r.stop_reason or ""))

    # near the cap baw's node (a block ahead) rejects batches our node still accepts: expected skew, no stop
    c = FakeChain(open_in=1, cap_proofs=16 * 4 + 8)
    inner_b = c.baw
    skew = {"n": 0}

    def baw_ahead(args, journal=None, **kw):
        if args[:2] == ["contract-call", "preview"] and time.time() > c.open_at + 0.5:
            day, rem, rew = FakeChain.state(c)
            if day == 1 and rem < rew * 2 * 16 and skew["n"] < 8:
                skew["n"] += 1
                return {"success": False, "error": {"code": 351805, "message": "Transaction simulation failed: execution reverted"}}, 0.05
        return inner_b(args, journal, **kw)
    c.baw = baw_ahead
    ps.LAG_GRACE = 0.5  # the skew happens after the opening grace
    try:
        r, a, dup = run_case("near-cap node skew after the grace", c, 16 * 10)
    finally:
        ps.LAG_GRACE = old_grace
    expect("near-cap skew", not dup and skew["n"] >= 3 and "budget full" in (r.stop_reason or "") and r.mined == 16 * 4 + 8)

    # a re-preview that fails only after the grace, while our own call now reads the cap as reached, must not
    # release the executor through reclassification: the grace deadline is checked after every blocking call
    ps.LAG_GRACE = 1.0
    try:
        c = FakeChain(open_in=1, cap_proofs=16 * 40)
        inner3, sim3 = c.baw, c.simulate
        st3 = {"n": 0, "late": False}
        w40 = format(40, "064x")

        def baw_late(args, journal=None, **kw):
            if (args[:2] == ["contract-call", "preview"] and time.time() > c.open_at
                    and w40 in args[args.index("--inputData") + 1]):
                st3["n"] += 1
                if st3["n"] > 1:
                    time.sleep(1.5)
                    st3["late"] = True
                return {"success": False, "error": {"code": 351805, "message": "Transaction simulation failed: execution reverted"}}, 0.05
            return inner3(args, journal, **kw)

        def sim_late(cd):
            return ("revert", ps.DAILY_LIMIT) if st3["late"] and w40 in cd else sim3(cd)
        c.baw, c.simulate = baw_late, sim_late
        r, a, dup = run_case("re-preview fails after the grace, cap then looks reached", c, 16 * 10)
    finally:
        ps.LAG_GRACE = old_grace
    expect("late sim stops", not dup and st3["late"] and "past the opening grace" in (r.stop_reason or ""))

    # confirm's re-simulation cannot read (unknown) inside the grace, and the nested re-check returns only after
    # the grace with DailyLimitReached while our state shows room: the late nested result must not release the
    # executor (round 12: the deadline was not carried into after_sim_failure)
    ps.LAG_GRACE = 1.0
    try:
        c = FakeChain(open_in=1, cap_proofs=16 * 40)
        inner4, sim4 = c.baw, c.simulate
        st4 = {"n": 0, "late": False}
        w40b = format(40, "064x")

        def baw_fail40(args, journal=None, **kw):
            if (args[:2] == ["contract-call", "preview"] and time.time() > c.open_at
                    and w40b in args[args.index("--inputData") + 1]):
                return {"success": False, "error": {"code": 351805, "message": "Transaction simulation failed: execution reverted"}}, 0.05
            return inner4(args, journal, **kw)

        def sim_nested(cd):
            if w40b in cd and time.time() > c.open_at:
                st4["n"] += 1
                if st4["n"] == 2:
                    return ("unknown", "rpc down")
                if st4["n"] >= 3:
                    time.sleep(max(0.0, c.open_at + 2.2 - time.time()))
                    st4["late"] = True
                    return ("revert", ps.DAILY_LIMIT)
            return sim4(cd)
        c.baw, c.simulate = baw_fail40, sim_nested
        r, a, dup = run_case("nested re-check returns after the grace", c, 16 * 10)
    finally:
        ps.LAG_GRACE = old_grace
    expect("late nested stops", not dup and st4["late"] and "past the opening grace" in (r.stop_reason or ""))

    # wallet status fails twice (baw hiccup), then CONNECTED: the run starts
    c = FakeChain(open_in=1, cap_proofs=16 * 5)
    inner5 = c.baw
    st5 = {"n": 0}

    def baw_status_flaky(args, journal=None, **kw):
        if args[:2] == ["wallet", "status"]:
            st5["n"] += 1
            if st5["n"] <= 2:
                return {"success": False, "error": {"code": "TIMEOUT", "message": "x"}}, 0.05
        return inner5(args, journal, **kw)
    c.baw = baw_status_flaky
    r, a, dup = run_case("wallet status flaky then connected", c, 16 * 5)
    expect("flaky status starts", not dup and r.mined > 0 and st5["n"] == 3)

    # at the opening all RPCs fail for 3 s (our state reads and re-simulations): no stop, mining starts after
    c = FakeChain(open_in=1, cap_proofs=16 * 5, baw_lag=2.0)
    real_state, real_sim2 = c.state, c.simulate

    def down():
        return c.open_at - 0.3 <= time.time() < c.open_at + 3.0

    def state_down():
        if down():
            raise RuntimeError("all RPCs failed for eth_call: 429")
        return real_state()

    def sim_down(cd):
        return ("unknown", "429") if down() else real_sim2(cd)
    c.state, c.simulate = state_down, sim_down
    r, a, dup = run_case("RPCs down at the opening", c, 16 * 20)
    expect("RPCs down at the opening", not dup and r.mined == 80 and "budget full" in (r.stop_reason or ""))

    # a disconnected wallet session must stop the start, not fail at the opening
    c = FakeChain(open_in=1, cap_proofs=16)
    c.wallet_status = "UNCONNECTED"
    try:
        run_case("wallet session not connected", c, 16)
        expect("refuse when UNCONNECTED", False)
    except SystemExit as e:
        print(f"[wallet session not connected] refused: {e}")
        expect("refuse when UNCONNECTED", "not CONNECTED" in str(e))

    # a second sender on the same journal must refuse to start
    tmp = tempfile.mkdtemp()
    jp = os.path.join(tmp, "j.jsonl")
    holder = ps.Journal(jp)
    held = ps.SenderLock(holder)
    try:
        run_case("second sender", FakeChain(open_in=1, cap_proofs=16), 16, journal=jp)
        expect("journal lock", False)
    except SystemExit as e:
        print(f"[second sender] refused: {str(e).replace(tmp, '<tmp>')}")
    held.release()

    # an execute refused before any process started (cmd.exe guard): provably not sent, so exec_rejected + stop,
    # nothing blocked or reserved (exec_unknown would block these proofs for good)
    c = FakeChain(open_in=1, cap_proofs=10**6)
    inner_u = c.baw

    def baw_unsafe(args, journal=None, **kw):
        if args[:2] == ["contract-call", "execute"]:
            j = {"success": False, "error": {"code": ps.UNSAFE_ARG,
                                             "message": "argument not safe to pass through cmd.exe: 'r&1'"}}
            if journal:
                journal.write(ev="baw", args=args, resp=j)
            return j, 0.0
        return inner_u(args, journal, **kw)
    c.baw = baw_unsafe
    r, a, dup = run_case("execute refused before launch (UNSAFE_ARG)", c, 16 * 4)
    j = ps.Journal(a.journal).read()
    rej = [x for x in j if x.get("ev") == "exec_rejected" and x.get("code") == ps.UNSAFE_ARG]
    unk = [x for x in j if x.get("ev") == "exec_unknown"]
    blocked, _, reserved, open_, _ = ps.blocked_from_journal(j)
    expect("UNSAFE_ARG execute: exec_rejected and stop, nothing blocked or reserved",
           len(rej) == 1 and not unk and r.sent == 0 and "refused before launch" in (r.stop_reason or "")
           and not blocked and reserved == 0 and not open_)

    # a preview whose requestId cmd.exe would read: refused before any attempt is written
    c = FakeChain(open_in=1, cap_proofs=10**6)
    inner_r = c.baw

    def baw_bad_rid(args, journal=None, **kw):
        j, dt_ = inner_r(args, journal, **kw)
        if args[:2] == ["contract-call", "preview"] and j.get("success") is True:
            j = json.loads(json.dumps(j))
            j["data"]["requestId"] = "r&1"
        return j, dt_
    c.baw = baw_bad_rid
    r, a, dup = run_case("preview with a requestId cmd.exe would read", c, 16 * 4)
    j = ps.Journal(a.journal).read()
    expect("unsafe requestId: refused before any attempt",
           r.sent == 0 and not [x for x in j if x.get("ev") == "attempt"]
           and "not a plain token" in (r.stop_reason or ""))

    platform_checks(expect)

    # per-day reward floor: same numbers as the contract's rewardForClaim on mainnet, and it falls every day
    def reward_for_claim(n):
        return ps.cumulative_mining(n + 1) - ps.cumulative_mining(n)
    expect("rewardForClaim(418605) == 9130862551 (mainnet)", reward_for_claim(418605) == 9130862551)
    expect("rewardForClaim(642858) == 8711108660 (mainnet)", reward_for_claim(642858) == 8711108660)
    floors = [ps.min_reward_for_day(d) for d in range(0, 30)]
    print(f"[reward floor] day 1..3: {[round(f / 1e8, 2) for f in floors[1:4]]}")
    expect("reward floor day 1/2/3 = 90.40 / 86.24 / 82.18",
           [round(f / 1e8, 2) for f in floors[1:4]] == [90.40, 86.24, 82.18])
    expect("reward floor falls every day", all(x > y > 0 for x, y in zip(floors, floors[1:])))
    expect("reward floor day 43 > 0, day 44 and later 0 (no bound past the supply)",
           ps.min_reward_for_day(43) > 0 and ps.min_reward_for_day(44) == 0 and ps.min_reward_for_day(50) == 0)
    try:
        ps.min_reward_for_day(-1)
        expect("reward floor refuses day -1", False)
    except SystemExit:
        expect("reward floor refuses day -1", True)

    # --handle: the registry's isValidHandle (a-z 0-9, 3..20), nothing corrected silently
    good_h = ["abc", "yourhandle", "a1b2c3", "x" * 20]
    bad_h = ["", "ab", "x" * 21, "Abc", "ABC", " abc", "abc ", "abc\n", "a bc", "abc.ai", "ABC.AI", "a_b", "a-b", "a.b"]
    expect("valid handles accepted", all(ps.handle_problem(h) is None for h in good_h))
    expect("invalid handles refused", all(ps.handle_problem(h) for h in bad_h))
    print(f"[handle] refused: {[(h, ps.handle_problem(h)) for h in bad_h[3:7]]}")

    # --wallet: 0x + 40 hex; a mixed-case address must carry a valid EIP-55 checksum
    good_w = ["0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
              "0x" + "ab" * 20, "0x" + "AB" * 20]
    bad_w = ["0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD", "0x5aaeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
             "0X" + "ab" * 20, "0x" + "ab" * 19, "0x" + "ab" * 21, "ab" * 20, "", None]
    expect("valid wallets accepted (EIP-55 vectors, all lower, all upper)", all(ps.wallet_problem(w) is None for w in good_w))
    expect("bad wallets refused (wrong checksum, 0X, 38/42 hex, no 0x)", all(ps.wallet_problem(w) for w in bad_w))

    # --min-reward must be at least 1 coin (a tiny value would disable the floor)
    keep = ps.MIN_REWARD_PER_PROOF
    for v, ok in ((0.5, False), (1e-9, False), (0, False), (1, True), (85, True), (1000, False)):
        try:
            ps.configure(ps.WALLET, ps.HANDLE, v)
            expect(f"--min-reward {v} accepted", ok)
        except SystemExit:
            expect(f"--min-reward {v} refused", not ok)
    ps.MIN_REWARD_PER_PROOF = keep

    # run --day: default is the next opening; a past day or a negative day is refused
    a = types.SimpleNamespace(day=None, minutes=90)
    ps.resolve_day(a)
    nxt = int((time.time() - ps.START) // 86400) + 1
    expect("run --day defaults to the next opening", a.day == nxt and ps.START + 86400 * a.day > time.time())
    for d in (0, -1):
        try:
            ps.resolve_day(types.SimpleNamespace(day=d, minutes=90))
            expect(f"run --day {d} refused", False)
        except SystemExit as e:
            expect(f"run --day {d} refused", True)
            print(f"[run --day {d}] refused: {str(e)[:90]}")

    # check on day 43 (floor for day 44 is 0) without a proofs file: no crash, identity still checked
    saved = {k: getattr(ps, k) for k in ("baw", "latest_block", "chain_state", "owner_burned", "call",
                                         "bnb_balance", "registry_owner", "say")}
    out = []
    ps.baw = lambda args, *k, **kw: ({"success": True, "data": {"status": "CONNECTED"}}, 0.01)
    ps.latest_block = lambda: (1, int(time.time()))
    ps.chain_state = lambda: (43, 0, 10 ** 6)
    ps.owner_burned = lambda: True
    ps.call = lambda data: "0x" + "0" * 64
    ps.bnb_balance = lambda: 10 ** 16
    ps.registry_owner = lambda: ps.WALLET.lower()
    ps.say = lambda *x: out.append(" ".join(str(v) for v in x))
    try:
        ps.cmd_check(types.SimpleNamespace(proofs=os.path.join(tempfile.mkdtemp(), "none.json"), min_reward=None))
        crashed = None
    except Exception as e:  # noqa: BLE001 - any crash fails the check
        crashed = repr(e)
    finally:
        for k, v in saved.items():
            setattr(ps, k, v)
    expect("check on day 43 without a proofs file: no crash, CHECK OK", crashed is None and out and out[-1] == "CHECK OK")
    expect("check on day 43: says there is no floor from day 44", any("no reward floor" in x for x in out))
    ps.MIN_REWARD_PER_PROOF = keep

    # check with a proofs file: proofs sent before (journal) and proofs used elsewhere (not in the journal)
    def check_case(name, n, used_nonces, journal_nonces=(), sim_error=None):
        tmp = tempfile.mkdtemp()
        pf, jf = os.path.join(tmp, "proofs.json"), os.path.join(tmp, "journal.jsonl")
        make_proofs(pf, n)  # nonce i+1, proof i+7
        if journal_nonces:
            prs = ["0x%064x" % (i + 6) for i in journal_nonces]
            with open(jf, "w", encoding="utf-8") as f:
                for rec in ({"ev": "attempt", "requestId": "r1", "proofs": prs},
                            {"ev": "broadcast", "requestId": "r1", "tx": "0x" + "ab" * 32, "proofs": prs},
                            {"ev": "receipt", "tx": "0x" + "ab" * 32, "status": 1, "fee": 10 ** 13, "mined": len(prs)}):
                    f.write(json.dumps(rec) + "\n")
        used, sims = set(used_nonces) | set(journal_nonces), []

        def simulate(cd):
            nonces = {int(cd[2 + 8 + 64 * 4 + 64 * k:2 + 8 + 64 * 5 + 64 * k], 16)
                      for k in range(int(cd[2 + 8 + 64 * 3:2 + 8 + 64 * 4], 16))}
            sims.append(nonces)
            if sim_error:
                return "revert", sim_error
            return ("revert", ps.PROOF_USED) if nonces & used else ("revert", ps.DAILY_LIMIT)
        saved = {k: getattr(ps, k) for k in ("baw", "latest_block", "chain_state", "owner_burned", "call", "simulate",
                                             "proof_used", "bnb_balance", "registry_owner", "say")}
        out = []
        ps.baw = lambda args, *k, **kw: ({"success": True, "data": {"status": "CONNECTED"}}, 0.01)
        ps.latest_block = lambda: (1, int(time.time()))
        ps.chain_state = lambda: (1, 0, 91 * 10 ** 8)
        ps.owner_burned = lambda: True
        ps.call = lambda data: "0x" + "0" * 64
        ps.simulate = simulate
        ps.proof_used = lambda p: (int(p, 16) - 6) in used
        ps.bnb_balance = lambda: 10 ** 16
        ps.registry_owner = lambda: ps.WALLET.lower()
        ps.say = lambda *x: out.append(" ".join(str(v) for v in x))
        try:
            ps.cmd_check(types.SimpleNamespace(proofs=pf, journal=jf, min_reward=None))
        finally:
            for k, v in saved.items():
                setattr(ps, k, v)
        ps.MIN_REWARD_PER_PROOF = keep
        print(f"[check: {name}] " + " | ".join(x for x in out if "proof" in x or "WARNING" in x or "CHECK" in x)[:400])
        made = os.path.exists(jf)
        for f in (pf, jf):  # leave no temp dir behind
            if os.path.exists(f):
                os.remove(f)
        os.rmdir(tmp)
        return out, sims, made

    out, sims, _ = check_case("journal covers the used front", 200, (), journal_nonces=range(1, 33))
    expect("check: proofs sent before (journal, front of the file) are skipped: CHECK OK",
           out[-1] == "CHECK OK" and not any("WARNING" in x for x in out) and any("32 already sent" in x for x in out))
    expect("check: the simulated batch is run's first batch after them", sims == [set(range(33, 49))])
    out, sims, made = check_case("used elsewhere, no journal", 200, range(1, 21))
    expect("check: proofs used on chain but not in the journal: WARNING, still CHECK OK",
           out[-1] == "CHECK OK" and any(x.startswith("WARNING: ") and "will be skipped by run" in x for x in out)
           and not any("CHECK HAS PROBLEMS" in x for x in out))
    expect("check: simulates the first batch past the used proofs", sims[-1] == set(range(33, 49)))
    expect("check: never creates a journal", not made)
    out, sims, _ = check_case("day-1 proofs used, journal lost", 300, range(1, 193))
    expect("check: 192 used proofs at the front and no journal: WARNING, CHECK OK",
           out[-1] == "CHECK OK" and any("first 192 proofs" in x for x in out) and sims[-1] == set(range(193, 209)))
    out, sims, _ = check_case("one used proof the samples miss", 300, (5,))
    expect("check: a used proof outside the samples still gets a WARNING, CHECK OK",
           out[-1] == "CHECK OK" and any(x.startswith("WARNING: the first 16 proofs") for x in out))
    out, _, _ = check_case("every batch in reach holds a used proof", 200, range(1, 201))
    expect("check: no unused proof in reach: HAS PROBLEMS", out[-1] == "CHECK HAS PROBLEMS")
    out, _, _ = check_case("all sent", 40, (), journal_nonces=range(1, 41))
    expect("check: every proof sent before: HAS PROBLEMS", out[-1] == "CHECK HAS PROBLEMS")
    out, _, _ = check_case("other simulation failure", 200, (), sim_error="0xd9c1988f")
    expect("check: InvalidSolution is not relaxed: HAS PROBLEMS", out[-1] == "CHECK HAS PROBLEMS")

    # D5: a Ctrl-C while the run is still starting up (reading the chain, starting baw) is a clean stop, no crash
    c = FakeChain(open_in=30, cap_proofs=10 ** 6)
    tmp5 = tempfile.mkdtemp(prefix="sig-")
    pf5 = os.path.join(tmp5, "proofs.json")
    make_proofs(pf5, 64)
    ps.baw, ps.chain_state, ps.receipt, ps.simulate = c.baw, c.state, c.receipt, c.simulate
    ps.owner_burned = lambda: True
    ps.proof_used = lambda p: (int(p, 16) - 6) in (c.used_nonces | c.done)
    fired = []

    def balance_then_ctrl_c():
        if not fired:
            fired.append(1)
            signal.raise_signal(signal.SIGINT)  # as if Ctrl-C arrived while Runner reads the start balance
        return 49_889_000_000_000_000
    ps.bnb_balance = balance_then_ctrl_c
    ps.START = c.open_at - 86400
    a5 = types.SimpleNamespace(proofs=pf5, journal=os.path.join(tmp5, "j.jsonl"), day=1, budget_bnb=0.045,
                               keep_bnb=0.005, minutes=0.5, previewers=2, executors=1, max_inflight=30, lead=0.6,
                               stagger=0.3, settle=1.5, stop_file=os.path.join(tmp5, "STOP"), min_reward=None)
    crashed = None
    sigint_before = signal.signal(signal.SIGINT, signal.default_int_handler)  # a background shell may start us ignoring it
    try:
        ps.cmd_run(a5)
    except KeyboardInterrupt:
        crashed = "KeyboardInterrupt"
    sigint_after = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, sigint_before)
    stops5 = [r.get("reason") for r in ps.Journal(a5.journal).read() if r.get("ev") == "stop"]
    print(f"[D5 early signal] crashed={crashed} stops={stops5}")
    expect("D5: a Ctrl-C while the run starts up: a clean stop 'signal', no crash",
           crashed is None and fired and stops5 == ["signal (Ctrl-C / SIGTERM)"])
    expect("D5: the earlier SIGINT handler is back afterwards", sigint_after is signal.default_int_handler)
    ps.MIN_REWARD_PER_PROOF = keep

    print(f"[summary] {checks[0]} checks")
    print("FAILED:", fails if fails else "none")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
