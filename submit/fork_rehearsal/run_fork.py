# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Dress rehearsal of pearl_submit.py `run` against the REAL PearlMiningV2 on a local anvil fork of BSC.

Nothing touches the real chain or the real wallet:
- anvil forks BSC into memory (--no-storage-caching) and impersonates your wallet (no key exists or is needed);
- a fake `baw` (fork_rehearsal/bin/baw; baw.cmd on Windows) turns preview/execute into eth_call/eth_sendTransaction
  on the fork;
- pearl_submit.RPCS is pointed at the fork only; journal and proofs are copies in a temp dir.
The fork's clock is moved to 30 s before the next day's boundary and the submitter's boundary is moved to
"now + 30 s", so the opening happens live. A competitor thread then eats the day cap through storage writes so
the cap-full end path runs against the real contract.

The contract checks that the sender owns the Agent ID (registry ownerOf + handleOf) and that every proof was mined
for the sender, so this needs YOUR wallet address, raw handle and proofs.json (same as `run`; no key, nothing sent).

  PEARL_WALLET=0x... PEARL_HANDLE=yourhandle \
  python3 fork_rehearsal/run_fork.py [--cap-proofs 400] [--competitor-rate 4] [--minutes 3] [--proofs proofs.json]

--anonymize (for recordings and logs you share): times as T±s from the simulated opening instead of the clock,
every hex value of 4+ digits (tx hashes, your address) masked, block numbers and unix times removed, BNB amounts
rounded and the temp work dir hidden; the rules of tools/replay_log.py --anonymize. The day before the target is set
full on the fork before the opening, so nothing can be mined early.

--autopilot runs the one-command path instead, as you would on the day: `pearl.py auto --yes --sell off` with
check and run as child processes (fork_submit.py points them at the fork). --journal copies an earlier
journal.jsonl into the work dir, so proofs sent on earlier days are skipped as in a real later-day run.
"""
import argparse
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SUBMIT = os.path.dirname(HERE)
sys.path.insert(0, SUBMIT)
import pearl_submit as ps  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(SUBMIT), "tools"))
from replay_log import Redactor  # noqa: E402

PORT = os.environ.get("PEARL_FORK_PORT", "8547")  # checked in main()
FORK_RPC = None
CAP = 20_000_000 * 10 ** 8
LEAD_IN = 30


def frpc(method, params):
    req = urllib.request.Request(FORK_RPC, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                 {"content-type": "application/json"})
    j = json.load(urllib.request.urlopen(req, timeout=30))
    if "error" in j:
        raise RuntimeError(f"{method}: {j['error']}")
    return j["result"]


def slot_hex(i):
    return "0x" + format(i, "064x")


def find_slots():
    """startsAt, taskSeed, successfulProofs, budgetDay, mintedInBudgetDay are consecutive."""
    vals = [int(frpc("eth_getStorageAt", [ps.CONTRACT, slot_hex(i), "latest"]), 16) for i in range(40)]
    for i, v in enumerate(vals):
        if v == ps.START and i + 4 < len(vals):
            return i + 3, i + 4, vals[i + 3], vals[i + 4]
    raise SystemExit("startsAt slot not found")


class RedactedStream(io.TextIOBase):
    """Line-buffered stream that rewrites every complete line with `fn` before passing it on."""

    def __init__(self, inner, fn):
        self.inner, self.fn, self.buf, self.lock = inner, fn, "", threading.Lock()

    def write(self, text):
        with self.lock:
            self.buf += text
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                self.inner.write(self.fn(line) + "\n")
        return len(text)

    def flush(self):
        with self.lock:
            self.inner.flush()

    def close_line(self):
        with self.lock:
            if self.buf:
                self.inner.write(self.fn(self.buf))
                self.buf = ""
            self.inner.flush()


def main():
    ps.safe_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap-proofs", type=int, default=400, help="day-2 room left at the opening, in proofs")
    ap.add_argument("--competitor-rate", type=float, default=4.0, help="proofs per second eaten by others")
    ap.add_argument("--minutes", type=float, default=3.0)
    ap.add_argument("--fork-url", default="https://bsc-rpc.publicnode.com")
    ap.add_argument("--wallet", default=os.environ.get("PEARL_WALLET"), help="default: env PEARL_WALLET")
    ap.add_argument("--handle", default=os.environ.get("PEARL_HANDLE"), help="default: env PEARL_HANDLE")
    ap.add_argument("--proofs", default=os.path.join(SUBMIT, "proofs.json"), help="your proofs.json (copied)")
    ap.add_argument("--day", type=int, default=None, help="day to open on the fork (default: the next one)")
    ap.add_argument("--port", default=PORT, help="local anvil port, must be free (env PEARL_FORK_PORT; default 8547)")
    ap.add_argument("--autopilot", action="store_true",
                    help="run `pearl.py auto --yes --sell off` (check + run as child processes) instead of the submitter")
    ap.add_argument("--journal", default=None, help="an earlier journal.jsonl to copy in, so proofs sent before are "
                    "skipped (with --autopilot: submit/journal.jsonl if it exists)")
    ap.add_argument("--anonymize", action="store_true",
                    help="for recordings: T± times, hashes and addresses masked, no block numbers or temp paths")
    a = ap.parse_args()
    if not str(a.port).isdigit() or not 1024 <= int(a.port) <= 65535:
        raise SystemExit(f"--port / PEARL_FORK_PORT must be a port number from 1024 to 65535 (got {a.port!r})")
    a.port = int(a.port)
    global FORK_RPC
    FORK_RPC = f"http://127.0.0.1:{a.port}"
    try:  # a node already on this port would receive our time and storage writes: refuse
        socket.create_connection(("127.0.0.1", a.port), timeout=1).close()
        raise SystemExit(f"port {a.port} is already in use (another anvil?): free it or pass --port")
    except OSError:
        pass
    ps.configure(a.wallet, a.handle)
    if a.autopilot and a.journal is None and os.path.exists(os.path.join(SUBMIT, "journal.jsonl")):
        a.journal = os.path.join(SUBMIT, "journal.jsonl")  # as on the day: proofs sent before are skipped
    if not os.path.exists(a.proofs):
        raise SystemExit(f"no proofs file at {a.proofs}: mine and export first, or pass --proofs")

    work = tempfile.mkdtemp(prefix="pearl-fork-")
    streams = []
    if a.anonymize:
        red = Redactor(anonymize=True)

        repo = os.path.dirname(SUBMIT)  # check prints "baw at <path>": the path can hold your user name

        def scrub(line):
            line = line.replace(work, "<work dir>").replace(os.path.realpath(work), "<work dir>")
            line = line.replace(repo, "<repo>").replace(os.path.realpath(repo), "<repo>")
            line = re.sub(r"\bfork_block=\d+", "fork_block=…", line)
            return red(re.sub(r"\bunix \d{10}(?:\.\d+)?", "unix …", line))  # on the fork that is the clock now
        streams = [RedactedStream(sys.stdout, scrub), RedactedStream(sys.stderr, scrub)]
        sys.stdout, sys.stderr = streams
    log = open(os.path.join(work, "anvil.log"), "w", encoding="utf-8")
    anvil = subprocess.Popen(["anvil", "--fork-url", a.fork_url, "--port", str(a.port), "--no-storage-caching",
                              "--block-time", "1", "--auto-impersonate", "--chain-id", "56"], stdout=log, stderr=log)
    with open(os.path.join(work, "anvil.pid"), "w", encoding="utf-8") as f:
        f.write(str(anvil.pid))
    try:
        for _ in range(60):
            if anvil.poll() is not None:  # bad --fork-url, port taken meanwhile, ...: stop at once
                break
            try:
                frpc("eth_blockNumber", [])
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise SystemExit(f"anvil did not answer in 30 s; see {os.path.join(work, 'anvil.log')}")
        time.sleep(0.3)  # an anvil that lost the port to another node exits right away: do not talk to that node
        if anvil.poll() is not None:
            raise SystemExit(f"anvil exited (code {anvil.returncode}); see {os.path.join(work, 'anvil.log')}")
        fork_block = int(frpc("eth_blockNumber", []), 16)
        s_day, s_minted, budget_day, minted = find_slots()
        fork_day = int(frpc("eth_call", [{"to": ps.CONTRACT, "data": "0x" + ps.SEL_MINING_DAY}, "latest"]), 16)
        target = a.day if a.day is not None else fork_day + 1
        if target <= fork_day:
            raise SystemExit(f"--day {target}: the fork is already on day {fork_day}; pick a later day")
        print(f"forked BSC{'' if a.anonymize else f' at block {fork_block}'} (day {fork_day}); slots budgetDay={s_day} ({budget_day}) "
              f"mintedInBudgetDay={s_minted} (day-{budget_day} minted {minted / 1e8:,.0f} of {CAP / 1e8:,.0f}); "
              f"opening day {target} on the fork", flush=True)

        # the day before the target is full during the lead-in, as on mainnet (its cap fills within minutes), so
        # nothing can be mined before the opening even with --day > fork day + 1 or a fork taken before the fill.
        # minted first, day second (a minted value for another day is ignored, so there is no half-written state)
        frpc("anvil_setStorageAt", [ps.CONTRACT, slot_hex(s_minted), slot_hex(CAP)])
        frpc("anvil_setStorageAt", [ps.CONTRACT, slot_hex(s_day), slot_hex(target - 1)])

        # clocks: chain boundary B_c happens LEAD_IN seconds from now; the submitter is told the same instant
        b_chain = ps.START + 86400 * target
        lead = LEAD_IN + 30 if a.autopilot else LEAD_IN  # the autopilot runs check before the run starts
        frpc("anvil_setTime", [b_chain - lead])
        b_wall = time.time() + lead
        ps.START = b_wall - 86400 * target
        if a.anonymize:  # the submitter's log lines start with T±s from the opening instead of the clock
            ps.now = lambda: f"T{time.time() - b_wall:+.2f}s"
        ps.RPCS[:] = [FORK_RPC]
        frpc("anvil_setBalance", [ps.WALLET, hex(int(0.0499 * 1e18))])

        # run dir: copies only
        proofs = os.path.join(work, "proofs.json")
        shutil.copy(a.proofs, proofs)
        journal = os.path.join(work, "journal.jsonl")
        if a.journal:
            shutil.copy(a.journal, journal)
        j0 = ps.Journal(journal)  # creates it when no --journal was given
        recs0 = j0.read()
        j0.f.close()  # no open handle of ours while the submitter holds its SenderLock
        blocked0, fees0 = ps.blocked_from_journal(recs0)[:2]
        bin_dir = os.path.join(HERE, "bin")
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ["FAKEBAW_STATE"] = os.path.join(work, "fakebaw.json")
        os.environ["FAKEBAW_RPC"] = FORK_RPC
        os.environ["FAKEBAW_PYTHON"] = sys.executable  # bin/baw.cmd (Windows) runs bin/baw with this interpreter
        fake = os.path.join(bin_dir, "baw.cmd" if ps.WINDOWS else "baw")
        if os.path.normcase(ps.find_baw() or "") != os.path.normcase(fake):
            raise SystemExit(f"fake baw is not first on PATH (found {ps.find_baw()!r})")

        reward = int(frpc("eth_call", [{"to": ps.CONTRACT, "data": "0x" + ps.SEL_CURRENT_REWARD}, "latest"]), 16)
        stop_comp = threading.Event()
        comp = {"eaten": 0, "set_at": None}

        def competitor():
            while not stop_comp.is_set():  # wait for the day flip on the fork
                blk = frpc("eth_getBlockByNumber", ["latest", False])
                if int(blk["timestamp"], 16) >= b_chain:
                    break
                time.sleep(0.2)
            day = target
            cur = int(frpc("eth_getStorageAt", [ps.CONTRACT, slot_hex(s_minted), "latest"]), 16)
            cur_day = int(frpc("eth_getStorageAt", [ps.CONTRACT, slot_hex(s_day), "latest"]), 16)
            ours = cur if cur_day == day else 0
            start = CAP - a.cap_proofs * reward + ours
            # minted first, day second: while budgetDay is still the old day the new minted value is ignored
            # (miningToday() == 0), so the switch from "full room" to "cap_proofs room" is one atomic step
            if cur_day != day:
                frpc("anvil_setStorageAt", [ps.CONTRACT, slot_hex(s_minted), slot_hex(start)])
                frpc("anvil_setStorageAt", [ps.CONTRACT, slot_hex(s_day), slot_hex(day)])
            else:
                frpc("anvil_setStorageAt", [ps.CONTRACT, slot_hex(s_minted), slot_hex(start)])
            comp["set_at"] = time.time()
            print(f"[competitor] day {day} open: room set to {a.cap_proofs} proofs; eating {a.competitor_rate}/s", flush=True)
            acc = 0.0
            while not stop_comp.is_set():
                time.sleep(1.0)
                acc += a.competitor_rate
                k = int(acc)
                acc -= k
                cur = int(frpc("eth_getStorageAt", [ps.CONTRACT, slot_hex(s_minted), "latest"]), 16)
                new = min(CAP, cur + k * reward)
                if new != cur:
                    frpc("anvil_setStorageAt", [ps.CONTRACT, slot_hex(s_minted), slot_hex(new)])
                    comp["eaten"] += (new - cur) // reward
        th = threading.Thread(target=competitor, daemon=True)
        th.start()

        t0 = time.time()
        rc = 0
        if a.autopilot:  # the one-command path: pearl.py here, check and run as child processes on the fork
            os.environ.update(PEARL_WALLET=ps.WALLET, PEARL_HANDLE=ps.HANDLE, PEARL_FORK_RPC=FORK_RPC,
                              PEARL_FORK_START=repr(ps.START),
                              PEARL_SUBMIT_SCRIPT=os.path.join(HERE, "fork_submit.py"))
            if a.anonymize:
                os.environ["PEARL_FORK_T0"] = repr(b_wall)
            sys.path.insert(0, os.path.dirname(SUBMIT))
            import pearl
            rc = pearl.main(["auto", "--yes", "--sell", "off", "--day", str(target), "--minutes", str(a.minutes),
                             "--dir", work])
        else:
            sys.argv = ["pearl_submit.py", "run", "--day", str(target), "--proofs", proofs, "--journal", journal,
                        "--minutes", str(a.minutes), "--stop-file", os.path.join(work, "STOP"),
                        "--wallet", ps.WALLET, "--handle", ps.HANDLE]
            try:
                ps.main()
            except SystemExit as e:
                rc = e.code or 0
        stop_comp.set()
        dt = time.time() - t0

        # verify against the fork
        recs = ps.Journal(journal).read()
        b, fees, reserved, open_, used = ps.blocked_from_journal(recs)
        recs = recs[len(recs0):]  # this run's records only (the rest came with --journal)
        txs = [r for r in recs if r.get("ev") == "broadcast"]
        rcpts = {r["tx"]: r for r in recs if r.get("ev") == "receipt"}
        ok_txs = [t for t in txs if rcpts.get(t["tx"], {}).get("status") == 1]
        sent_proofs = [p for t in ok_txs for p in t["proofs"]]
        onchain_used = sum(1 for p in sent_proofs if ps.hexint(frpc("eth_call", [{"to": ps.CONTRACT, "data": "0x" + ps.SEL_USED_PROOFS + p[2:].rjust(64, "0")}, "latest"])) == 1)
        bal_after = int(frpc("eth_getBalance", [ps.WALLET, "latest"]), 16)
        day, rem, rew = ps.chain_state()
        print("\n==== FORK REHEARSAL RESULT ====")
        print(f"exit={rc} wall={dt:.0f}s fork_block={fork_block} journal={journal}")
        print(f"txs broadcast={len(txs)} ok={len(ok_txs)} reverted={sum(1 for t in txs if rcpts.get(t['tx'], {}).get('status') == 0)} "
              f"no_receipt={sum(1 for t in txs if t['tx'] not in rcpts)}")
        print(f"proofs in ok txs={len(sent_proofs)}; marked used on chain={onchain_used}")
        print(f"competitor ate {comp['eaten']} proofs; chain now day={day} remaining={rem / 1e8:.2f} reward={rew / 1e8:.2f}")
        print(f"journal replay: blocked={len(b)} fees={fees / 1e18:.6f} BNB (earlier runs {fees0 / 1e18:.6f}) "
              f"reserved={reserved} open={list(open_)} used={len(used)}")
        print(f"balance on fork: {0.0499:.6f} -> {bal_after / 1e18:.6f} BNB (spent {0.0499 - bal_after / 1e18:.6f})")
        stops = [r.get("reason") for r in recs if r.get("ev") == "stop"]
        print(f"stop reason: {stops}")
        checks = {
            "all ok-tx proofs used on chain": onchain_used == len(sent_proofs),
            "replay: nothing reserved or open": reserved == 0 and not open_,
            "fees match spend": abs((fees - fees0) / 1e18 - (0.0499 - bal_after / 1e18)) < 1e-9,
            "stopped for cap full": any("budget full" in (s or "") for s in stops),
            "sent something": len(ok_txs) > 0,
        }
        if a.journal:
            checks["no proof from the earlier journal sent again"] = not blocked0 & set(sent_proofs)
        if a.autopilot:  # exit 0 also means check printed CHECK OK: pearl.py refuses to start the run otherwise
            log = open(os.path.join(work, "run.log"), encoding="utf-8", errors="replace").read()
            pid_file = os.path.join(work, "run.pid")
            checks["autopilot: pearl.py exit 0 (check OK, run ended with SUMMARY)"] = rc == 0
            checks["autopilot: run.log has the run's SUMMARY"] = "SUMMARY txs=" in log
            checks["autopilot: run.pid names the run, now finished"] = (pearl.read_pid(pid_file) is not None
                                                                         and not pearl.run_alive(pid_file))
        for k, v in checks.items():
            print(f"  [{'PASS' if v else 'FAIL'}] {k}")
        print("FORK REHEARSAL:", "PASS" if all(checks.values()) else "FAIL")
    finally:
        anvil.terminate()
        try:
            anvil.wait(10)
        except subprocess.TimeoutExpired:
            anvil.kill()
        print(f"anvil stopped (pid {anvil.pid}); work dir {work}")
        for st in streams:
            st.close_line()


if __name__ == "__main__":
    main()
