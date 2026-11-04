# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Offline simulation of pearl_sell.py: fake baw + a constant-liquidity pool model calibrated on the
PancakeSwap Infinity pool (evening of 2026-11-03: L = 7.06e17, 431,426 coins per BNB, fee 0.3726%)."""
import atexit, json, math, os, shutil, signal, subprocess, sys, tempfile, time
from decimal import Decimal
OWN_TMP = tempfile.mkdtemp(prefix="pearl-test-sell-")  # every temp dir of this run goes here; removed at exit,
tempfile.tempdir = OWN_TMP                              # also after a failure
atexit.register(shutil.rmtree, OWN_TMP, True)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["PEARL_WALLET"] = "0x00000000000000000000000000000000DeaDBeef"  # dummy: balances and quotes are faked
import pearl_sell as ps
ps._submit.safe_stdio()
ps._submit.find_baw = lambda: "baw"  # every case below replaces ps.baw; main() only needs baw to be found
REAL_BAW = ps.baw
INITIAL_SIGINT = signal.getsignal(signal.SIGINT)  # what every run must leave behind (SIG_IGN in a background shell)

FAILED = []

def expect(name, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + name)
    if not cond:
        FAILED.append(name)


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += max(sec, 0.01)


CLOCK = Clock()


class Pool:
    """Constant-liquidity pool; `path(minutes)` multiplies the coin's market price over (virtual) time,
    our own sales move the price on top of that."""
    def __init__(self, L=706415905678329097, tok_per_bnb=431426, fee=0.003726, path=None):
        self.L, self.fee, self.path = L, fee, path or (lambda m: 1.0)
        self.sq0 = math.sqrt(tok_per_bnb * 1e8 / 1e18)
        self.shift = 0.0
        self.t0 = CLOCK.now()

    @property
    def sq(self):
        return self.sq0 / math.sqrt(self.path((CLOCK.now() - self.t0) / 60)) + self.shift

    def out(self, coins):
        sq = self.sq
        dy = coins * 1e8 * (1 - self.fee)
        sq2 = sq + dy / self.L
        return self.L * (1 / sq - 1 / sq2) / 1e18, sq2

    def sell(self, coins):
        bnb, sq2 = self.out(coins)
        self.shift += sq2 - self.sq
        return bnb

    def price(self):
        """BNB per coin now (spot, no fee)."""
        return 1 / (self.sq ** 2 * 1e10)


class Env:
    def __init__(self, coins, pool=None, fail_orders=0, pending_forever=False, swap_errors=0, bad_quotes=0,
                 partial=False, dump_after=None, swap_timeout=False, pending_exists=False, full_refund=False,
                 stale_reads=0, no_actual_qty=False, nan_quotes=0, bnb=0.04, swap_reply=None, alternate_stale=False,
                 outlier_at=None, flaky_every=0, pending_after=None, pending_unreadable=0, signed_in=None,
                 signal_after_swaps=None, signal_on_read=None, stop_on_read=None, stop_after_swaps=None):
        self.signal_after_swaps, self.signal_on_read = signal_after_swaps, signal_on_read  # Ctrl-C at that point
        self.stop_on_read, self.stop_after_swaps = stop_on_read, stop_after_swaps  # the STOP file appears then
        self.fail_reads_if = None  # a callable: while it is true, balance reads fail
        self.signed_in = os.environ["PEARL_WALLET"] if signed_in is None else signed_in  # baw wallet address
        self.pending_unreadable = pending_unreadable
        self.swap_reply, self.alternate_stale, self.outlier_at = swap_reply, alternate_stale, outlier_at
        self.flaky_every, self.pending_after, self.nq, self.flip = flaky_every, pending_after, 0, False
        self.bal_hist = []
        self.swap_timeout, self.pending_exists, self.full_refund = swap_timeout, pending_exists, full_refund
        self.stale_reads, self.no_actual_qty, self.nan_quotes, self.bnb = stale_reads, no_actual_qty, nan_quotes, bnb
        self.stale_left, self.reported_bal = 0, None
        self.bal = int(coins * 10 ** 8)
        self.pool = pool or Pool()
        self.fail_orders, self.pending_forever, self.swap_errors = fail_orders, pending_forever, swap_errors
        self.bad_quotes, self.partial, self.dump_after = bad_quotes, partial, dump_after
        self.orders, self.swaps, self.quotes, self.impacts = {}, [], 0, []
        self.sale_times, self.sale_prices = [], []

    def baw(self, args, timeout=60):
        cmd = args[:2]
        if cmd == ["wallet", "address"]:
            if not self.signed_in:
                return {"success": False, "error": {"code": "X", "message": "not signed in"}}
            return {"success": True, "data": {"addresses": [
                {"binanceChainId": "1", "chainName": "Ethereum", "address": "0x" + "11" * 20},
                {"binanceChainId": "56", "chainName": "BSC", "address": self.signed_in}]}}
        if cmd == ["market-order", "quote"]:
            self.quotes += 1
            assert "--slippage" in args
            self.nq += 1
            if self.flaky_every and self.nq % self.flaky_every == 0:
                return {"success": False, "error": {"code": "X", "message": "sporadic"}}
            if self.outlier_at is not None and self.nq == self.outlier_at:
                qty = int(args[args.index("--fromTokenQty") + 1])
                bnb, _ = self.pool.out(qty)
                return {"success": True, "data": {"toCoinAmount": f"{bnb * 1.5:.18f}"}}
            if self.bad_quotes > 0:
                self.bad_quotes -= 1
                return {"success": False, "error": {"code": "X", "message": "quote unavailable"}}
            if self.nan_quotes > 0:
                self.nan_quotes -= 1
                return {"success": True, "data": {"toCoinAmount": "NaN"}}
            qty = int(args[args.index("--fromTokenQty") + 1])
            bnb, _ = self.pool.out(qty)
            return {"success": True, "data": {"fromCoinSymbol": "珍珠币", "fromCoinAmount": str(qty),
                                              "toCoinSymbol": "BNB", "toCoinAmount": f"{bnb:.18f}", "slippage": 0.02}}
        if cmd == ["market-order", "swap"]:
            qty = int(args[args.index("--fromTokenQty") + 1])
            assert args[args.index("--fromToken") + 1] == ps.TOKEN and args[args.index("--toToken") + 1] == ps.NATIVE
            assert float(args[args.index("--slippage") + 1]) <= 5
            assert "--gasLevel" in args
            if self.swap_reply is not None:  # the backend took the order, the reply says otherwise
                self.swaps.append(qty)
                self.bal -= qty * 10 ** 8
                return self.swap_reply
            if self.swap_timeout:
                self.swaps.append(qty)  # the backend did take it; the CLI just timed out
                self.bal -= qty * 10 ** 8
                return {"success": False, "error": {"code": "TIMEOUT", "message": "baw market-order timed out"}}
            if self.swap_errors > 0:
                self.swap_errors -= 1
                return {"success": False, "error": {"code": "351000", "message": "swap rejected"}}
            ref, _ = self.pool.out(1000)
            got, _ = self.pool.out(qty)
            self.impacts.append(1 - (got / qty) / (ref / 1000))
            self.sale_times.append((CLOCK.now() - self.pool.t0) / 60)
            self.sale_prices.append(self.pool.price())
            assert "--mev" in args and args[args.index("--mev") + 1] == "true"
            oid = str(1000 + len(self.swaps))
            self.swaps.append(qty)
            if self.signal_after_swaps and len(self.swaps) >= self.signal_after_swaps:
                ps.on_signal(2, None)  # Ctrl-C while this order is in flight
            if self.stop_after_swaps and len(self.swaps) >= self.stop_after_swaps:
                open(os.path.join(ps.HERE, "STOP"), "w").close()  # STOP while this order is in flight
            self.orders[oid] = {"qty": qty, "polls": 0}
            return {"success": True, "data": {"orderId": oid}}
        if cmd == ["market-order", "list"] and "--status" in args:
            assert "--fromToken" not in args  # any pending order of the wallet blocks
            self.list_calls = getattr(self, "list_calls", 0) + 1
            if self.pending_unreadable > 0 and self.list_calls >= 2:  # call 1 is the start check
                self.pending_unreadable -= 1
                return {"success": False, "error": {"code": "X", "message": "list unavailable"}}
            late = self.pending_after is not None and len(self.swaps) >= self.pending_after
            pend = [{"orderId": "77", "status": "PENDING"}] if self.pending_exists or late else []
            return {"success": True, "data": {"total": len(pend), "list": pend}}
        if cmd == ["market-order", "list"]:
            oid = args[args.index("--orderId") + 1]
            o = self.orders[oid]
            o["polls"] += 1
            if self.pending_forever or o["polls"] < 2:
                st = "PENDING"
            elif self.fail_orders > 0 and "done" not in o:
                self.fail_orders -= 1
                o["done"] = "FAILED"
                st = "FAILED"
            else:
                st = o.get("done", "FINISHED")
                if "done" not in o:
                    o["done"] = "FINISHED"
                    sold = 0 if self.full_refund else o["qty"] // 2 if self.partial else o["qty"]
                    self.stale_left = self.stale_reads
                    o["bnb"] = self.pool.sell(sold)
                    self.bal -= sold * 10 ** 8
                    o["refund"] = [{"amount": str(o["qty"] - sold)}] if self.partial else None
                    if self.dump_after is not None and len(self.swaps) == self.dump_after:
                        self.pool.sell(3_000_000)  # someone else dumps 3M coins
            rec = {"orderId": oid, "status": st, "txHash": "0x" + oid if st == "FINISHED" else None}
            if st == "FINISHED":
                if not self.no_actual_qty:
                    rec["toTokenActualQty"] = f"{o['bnb']:.18f}"
                rec["refund"] = ([{"amount": str(o["qty"]), "symbol": "珍珠币"}] if self.full_refund
                                 else [dict(x, symbol="珍珠币") for x in o["refund"]] if o["refund"] else None)
            return {"success": True, "data": {"list": [rec]}}
        return {"success": True, "data": {}}


def run(name, env, argv, run_alive=False, stop_file=False, no_pid=False, tmp=None, default_journal=False):
    tmp = tmp or tempfile.mkdtemp(prefix="sell-")
    ps.HERE = tmp
    if stop_file:
        open(os.path.join(tmp, "STOP"), "w", encoding="utf-8").close()
    pid = os.path.join(tmp, "run.pid")
    if not no_pid:
        with open(pid, "w", encoding="utf-8") as f:
            f.write(str(os.getpid() if run_alive else 999999))
    elif os.path.exists(pid):
        os.remove(pid)
    ps.baw = env.baw

    def bal():  # a lagging RPC keeps returning the old balance for a few reads after a fill
        env.bal_hist.append(env.bal)
        if env.signal_on_read and len(env.bal_hist) == env.signal_on_read:
            ps.on_signal(2, None)
        if env.stop_on_read and len(env.bal_hist) == env.stop_on_read:
            open(os.path.join(ps.HERE, "STOP"), "w").close()
        if env.fail_reads_if and env.fail_reads_if():
            raise RuntimeError("token balance unreadable: test")
        if env.alternate_stale:  # two nodes behind a load balancer: every other read is a minute old
            env.flip = not env.flip
            old = env.bal_hist[max(0, len(env.bal_hist) - 40)]
            return old if env.flip else env.bal
        if env.stale_left > 0 and env.reported_bal is not None:
            env.stale_left -= 1
            return env.reported_bal
        env.reported_bal = env.bal
        return env.bal
    ps.token_balance = bal
    ps.native_balance = lambda: int(env.bnb * 1e18)
    ps.now, ps.sleep = CLOCK.now, CLOCK.sleep
    ps.ORDER_TIMEOUT = 150
    sys.argv = ["pearl_sell.py", *argv, "--run-pid", pid] + ([] if default_journal else
                                                             ["--journal", os.path.join(tmp, "sell.jsonl")])
    ps.HERE = tmp
    t0 = time.time()
    err = None
    try:
        ps.main()
    except SystemExit as e:
        err = str(e)
    print(f"[{name}] {time.time() - t0:.1f}s swaps={env.swaps} at min {[round(x) for x in env.sale_times]} "
          f"end={env.bal / 1e8:,.0f} err={err}")
    return err


real_sleep = time.sleep

# 1. normal: 300k coins, sell 80% = 240k in 100k chunks
e = Env(300_000)
run("normal 300k", e, ["run"])
expect("sold exactly 80% in 3 chunks", e.swaps == [100_000, 100_000, 40_000] and e.bal == 60_000 * 10 ** 8)
expect("every executed chunk under 2% impact", all(i <= 0.02 for i in e.impacts))

# 2. thin pool: 100k would exceed 2%, chunks shrink, never below the kept 20%
e = Env(300_000, pool=Pool(L=int(7.06e17 / 20)))
run("thin pool", e, ["run"])
expect("thin pool: kept >= 20%", e.bal >= 60_000 * 10 ** 8)
expect("thin pool: every executed chunk under 2%", e.swaps and all(i <= 0.02 for i in e.impacts))
expect("thin pool: chunks shrank below 100k", max(e.swaps) < 100_000)

# 3. pool too thin even at the minimum chunk: no swap at all
e = Env(300_000, pool=Pool(L=int(7.06e17 / 200)))
run("too thin", e, ["run"])
expect("too thin: nothing sold", e.swaps == [] and e.bal == 300_000 * 10 ** 8)

# 4. two failed orders in a row: stop
e = Env(300_000, fail_orders=2)
run("two failed orders", e, ["run"])
expect("failed orders: stops after 2, nothing sold", len(e.swaps) == 2 and e.bal == 300_000 * 10 ** 8)

# 5. one failed order then success: continues
e = Env(300_000, fail_orders=1)
run("one failed order", e, ["run"])
expect("one failure then continues to 80%", e.bal == 60_000 * 10 ** 8)

# 6. order never reaches a terminal status: stop, never resubmit
e = Env(300_000, pending_forever=True)
run("pending forever", e, ["run"])
expect("pending: exactly one swap, then stop", len(e.swaps) == 1)

# 7. swap rejected twice: stop
e = Env(300_000, swap_errors=2)
run("swap rejected", e, ["run"])
expect("swap rejected twice: stop, nothing sold", e.bal == 300_000 * 10 ** 8)

# 8. mining run alive: refuse
e = Env(300_000)
err = run("mining run alive", e, ["run"], run_alive=True)
expect("refuses while the mining run is alive", err and "may still be alive" in err and e.swaps == [])

# 9. STOP file: nothing sold
e = Env(300_000)
run("STOP file", e, ["run"], stop_file=True)
expect("STOP file: nothing sold", e.swaps == [])

# 10. unusable quotes for a while, then fine
e = Env(300_000, bad_quotes=3)
run("bad quotes then ok", e, ["run"])
expect("recovers after bad quotes", e.bal == 60_000 * 10 ** 8)

# 11. partial fills: progress follows the real balance, kept part respected
e = Env(300_000, partial=True)
run("partial fills", e, ["run"])
expect("partial fills: ends at the kept 20% (within dust)", 60_000 * 10 ** 8 <= e.bal < 61_000 * 10 ** 8)

# 12. dry run: real flow, never a swap, balance untouched
e = Env(0)
run("dry run", e, ["run", "--dry-run", "--assume-balance", "250000"])
expect("dry run: no swap sent", e.swaps == [] and e.quotes > 0 and e.bal == 0)

# 13. someone dumps mid-sale: impact measured against the fresh price, still <= 2%
e = Env(500_000, dump_after=1)
run("dump mid-sale", e, ["run"])
expect("dump mid-sale: every executed chunk under 2%", all(i <= 0.02 for i in e.impacts) and e.bal == 100_000 * 10 ** 8)

# 14. small balance below the minimum chunk: sold in one go, 20% kept
e = Env(15_000)
run("small balance", e, ["run"])
expect("small balance: one swap of 12,000", e.swaps == [12_000] and e.bal == 3_000 * 10 ** 8)

# 16. dust: under 1,000 coins to sell -> nothing
e = Env(1_000)
run("dust", e, ["run"])
expect("dust: no swap", e.swaps == [])

# 17. every swap reports the received BNB from toTokenActualQty (journal end line)
e = Env(300_000)
run("received amount", e, ["run"])
import json as _json
with open(os.path.join(ps.HERE, "sell.jsonl"), encoding="utf-8") as _f:
    recs = [_json.loads(x) for x in _f]
end = [r for r in recs if r.get("ev") == "end"]
expect("received BNB summed from toTokenActualQty", end and float(end[0]["bnb"]) > 0.5)

# 15. bad arguments refused
e = Env(300_000)
err = run("assume-balance without dry run", e, ["run", "--assume-balance", "1000"])
expect("assume-balance needs --dry-run", err and "dry-run" in err and e.swaps == [])

# ---- timing rule (default --mode trail) on price paths ----
def avg_price(env):
    return sum(env.sale_prices) / len(env.sale_prices) if env.sale_prices else 0

# 18. day-0-like rebound: +60% over 30 min, then -1%/min. Sell after the peak, not into the rise.
path = lambda m: 1 + 0.6 * m / 30 if m <= 30 else 1.6 * (0.99 ** (m - 30))
pool = Pool(path=path); p0 = pool.price()
e = Env(200_000, pool=pool)
run("rebound then decline", e, ["run"])
expect("rebound: first sale after the peak (min >= 30)", e.sale_times and min(e.sale_times) >= 30)
expect("rebound: average sale price >= 1.35x the start", avg_price(e) >= 1.35 * p0)
expect("rebound: 80% sold", e.bal == 40_000 * 10 ** 8)

# 19. falling from the start: -1%/min. The 7% pullback triggers within ~8 minutes.
pool = Pool(path=lambda m: 0.99 ** m); p0 = pool.price()
e = Env(200_000, pool=pool)
run("falling from the start", e, ["run"])
expect("falling: first sale within 10 min", e.sale_times and min(e.sale_times) <= 10)
expect("falling: 80% sold within 15 min", e.bal == 40_000 * 10 ** 8 and max(e.sale_times) <= 15)

# 20. rising for hours: the schedule after the 20-min warmup forces completion by the 120-min deadline
pool = Pool(path=lambda m: 1.005 ** m)
e = Env(500_000, pool=pool)
run("rising for hours", e, ["run"])
expect("rising: no sale during the warmup", e.sale_times and min(e.sale_times) >= 20)
expect("rising: 80% sold by the deadline (+5 min)", e.bal == 100_000 * 10 ** 8 and max(e.sale_times) <= 125)

# 21. flat price: schedule only, done by the deadline
e = Env(300_000)
run("flat price", e, ["run"])
expect("flat: 80% sold, last sale by the deadline (+5 min)", e.bal == 60_000 * 10 ** 8 and max(e.sale_times) <= 125)

# 22. --mode now: sells at once
e = Env(300_000)
run("mode now", e, ["run", "--mode", "now"])
expect("mode now: first sale at minute 0", e.sale_times and min(e.sale_times) < 1)

# 23. chunks at least --gap apart
e = Env(1_000_000, pool=Pool(path=lambda m: 0.99 ** m))
run("gap between sales", e, ["run"])
expect("sales at least 60 s apart", all(b - a >= 1 - 1e-9 for a, b in zip(e.sale_times, e.sale_times[1:])))

# ---- regression cases, set 1 ----
# 24. swap CLI timeout although the backend took the order: stop, never a second swap
e = Env(300_000, swap_timeout=True)
run("swap timeout", e, ["run", "--mode", "now"])
expect("swap timeout: exactly one swap, then stop", e.swaps == [100_000])

# 25. run.pid missing: refuse (the run may be alive)
e = Env(300_000)
err = run("run.pid missing", e, ["run"], no_pid=True)
expect("missing run.pid refuses", err and "may still be alive" in err and e.swaps == [])

# 26. a PENDING order already exists: refuse
e = Env(300_000, pending_exists=True)
err = run("pending order exists", e, ["run"])
expect("pending order refuses", err and "PENDING" in err and e.swaps == [])

# 27. not enough BNB for gas: refuse
e = Env(300_000, bnb=0.001)
err = run("low BNB", e, ["run"])
expect("low BNB refuses", err and "BNB" in err and e.swaps == [])

# 28. restart keeps the first run's kept amount (no 80%-of-80%)
tmpd = tempfile.mkdtemp(prefix="sell-restart-")
e = Env(300_000)
run("first run stops after one swap", e, ["run", "--mode", "now", "--max-swaps", "1"], tmp=tmpd)
run("restart", e, ["run", "--mode", "now"], tmp=tmpd)
expect("restart: ends at the first run's 60,000 kept", e.bal == 60_000 * 10 ** 8)

# 29. FINISHED with a full refund: the balance does not fall -> stop, no loop
e = Env(300_000, full_refund=True)
run("full refund", e, ["run", "--mode", "now"])
expect("full refund: one swap then stop", e.swaps == [100_000] and e.bal == 300_000 * 10 ** 8)

# 30. lagging RPC after fills: progress waits for the balance to fall, never oversells
e = Env(300_000, stale_reads=3)
run("stale balance reads", e, ["run", "--mode", "now"])
expect("stale reads: exactly 80% sold", e.bal == 60_000 * 10 ** 8 and sum(e.swaps) == 240_000)

# 31. FINISHED without toTokenActualQty: no crash
e = Env(300_000, no_actual_qty=True)
run("no toTokenActualQty", e, ["run", "--mode", "now"])
expect("missing toTokenActualQty: still sells 80%", e.bal == 60_000 * 10 ** 8)

# 32. NaN quotes are unusable, not a crash
e = Env(300_000, nan_quotes=2)
run("NaN quotes", e, ["run", "--mode", "now"])
expect("NaN quotes: recovers, sells 80%", e.bal == 60_000 * 10 ** 8)

# 33. --dust 0 refused
e = Env(300_000)
err = run("dust 0", e, ["run", "--dust", "0"])
expect("--dust 0 refused", err == "bad arguments")

# 34. halving works from the actual quantity on the last small chunk
e = Env(60_000, pool=Pool(L=int(7.06e17 / 25)))
run("thin pool small last chunk", e, ["run", "--mode", "now"])
expect("small last chunk: sold in pieces, all under 2%", len(e.swaps) >= 2 and all(i <= 0.02 for i in e.impacts)
       and e.bal >= 12_000 * 10 ** 8)

# ---- regression cases, set 2 ----
# 35. swap returns a JSON error although the backend executed it: stop at once
e = Env(300_000, swap_reply={"success": False, "error": {"code": "NETWORK_ERROR", "message": "x"}})
run("json error but executed", e, ["run", "--mode", "now"])
expect("json error: exactly one swap, keep intact", e.swaps == [100_000] and e.bal >= 60_000 * 10 ** 8)

# 36. success without an orderId: stop
e = Env(300_000, swap_reply={"success": True, "data": {}})
run("success without orderId", e, ["run", "--mode", "now"])
expect("no orderId: exactly one swap", e.swaps == [100_000])

# 37. error given as a string: no crash, stop
e = Env(300_000, swap_reply={"success": False, "error": "boom"})
err = run("string error", e, ["run", "--mode", "now"])
expect("string error: no crash (a stop message, not a traceback), one swap",
       err is not None and "stopped before the sale was done" in err and e.swaps == [100_000])

# 38. two RPC nodes alternate (one a minute behind): the low-watermark never oversells
e = Env(300_000, alternate_stale=True)
run("alternating stale node", e, ["run", "--mode", "now"])
expect("alternating stale node: ends at 60,000", e.bal == 60_000 * 10 ** 8)

# 39. one outlier reference quote (x1.5) on a flat price: no early liquidation
e = Env(300_000, outlier_at=10)
run("one outlier quote", e, ["run"])
expect("outlier: first sale only from the schedule (>= 40 min)", e.sale_times and min(e.sale_times) >= 40)

# 40. sporadic quote failures (every 12th) on a flat price: still sells 80%
e = Env(300_000, flaky_every=12)
run("sporadic quote failures", e, ["run"])
expect("sporadic failures: 80% sold", e.bal == 60_000 * 10 ** 8)

# 41. an old journal (> 12 h) with a smaller keep is ignored
tmpd = tempfile.mkdtemp(prefix="sell-old-")
with open(os.path.join(tmpd, "sell.jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps({"ev": "start", "start_bal": 100_000 * 10 ** 8, "keep": 20_000 * 10 ** 8, "args": {},
                        "t": CLOCK.now() - 20 * 3600}) + "\n")
e = Env(300_000)
run("old journal", e, ["run", "--mode", "now"], tmp=tmpd)
expect("old journal ignored: keeps 60,000", e.bal == 60_000 * 10 ** 8)

# 42. a pending order appears after the first swap: stop before the next
e = Env(300_000, pending_after=1)
run("pending appears", e, ["run", "--mode", "now"])
expect("pending after first swap: stop", e.swaps == [100_000])

# 43. restart continues the schedule (no new 20-min warmup)
tmpd = tempfile.mkdtemp(prefix="sell-sched-")
e = Env(300_000)
run("first run, flat, one schedule sale", e, ["run", "--max-swaps", "1"], tmp=tmpd)
t_first = e.sale_times[-1]
run("restart on schedule", e, ["run"], tmp=tmpd)
expect("restart: ends at 60,000 and the schedule continued", e.bal == 60_000 * 10 ** 8 and max(e.sale_times) <= 130)

# 44. --poll 0 refused
e = Env(300_000)
err = run("poll 0", e, ["run", "--poll", "0"])
expect("--poll 0 refused", err == "bad arguments")

# ---- regression cases, set 3 ----
# 45. restart 13 h later without new coins: still the first session's keep (B1)
tmpd = tempfile.mkdtemp(prefix="sell-13h-")
e = Env(300_000)
run("first run, one sale", e, ["run", "--mode", "now", "--max-swaps", "1"], tmp=tmpd)
CLOCK.t += 13 * 3600
run("restart 13 h later", e, ["run", "--mode", "now"], tmp=tmpd)
expect("13 h restart keeps 60,000", e.bal == 60_000 * 10 ** 8)

# 46. another night's session in the journal (balance grew): new session, fresh schedule (B2)
tmpd = tempfile.mkdtemp(prefix="sell-night2-")
e = Env(300_000)
run("night 1", e, ["run", "--mode", "now"], tmp=tmpd)
e.bal += 300_000 * 10 ** 8  # night 2 mining: 60k kept + 300k new
CLOCK.t += 8 * 3600
e.sale_times = []
run("night 2", e, ["run"], tmp=tmpd)
expect("night 2: new session keeps 20% of 360k and waits for the schedule",
       e.bal == 72_000 * 10 ** 8 and e.sale_times and min(e.sale_times) >= 20)

# 47. outlier on the first or second reference quote does not set the high (B3)
for k in (1, 2):
    e = Env(300_000, outlier_at=k)
    run(f"outlier on quote {k}", e, ["run"])
    expect(f"outlier on quote {k}: first sale only from the schedule", e.sale_times and min(e.sale_times) >= 40)

# 48. the pending list is unreadable once before a swap: retry, not stop (B4)
e = Env(300_000, pending_unreadable=1)
run("pending list unreadable once", e, ["run", "--mode", "now"])
expect("unreadable pending list once: still sells 80%", e.bal == 60_000 * 10 ** 8)

# ---- regression cases, set 4 ----
# 49. a dry run between two real runs does not reset the keep (P1)
tmpd = tempfile.mkdtemp(prefix="sell-p1-")
e = Env(300_000)
run("p1 real, one sale", e, ["run", "--mode", "now", "--max-swaps", "1"], tmp=tmpd)
saved = e.bal
run("p1 dry run on the same journal", e, ["run", "--mode", "now", "--dry-run",
                                         "--journal", os.path.join(tmpd, "sell.jsonl")], tmp=tmpd)
e.bal = saved
run("p1 real restart", e, ["run", "--mode", "now"], tmp=tmpd)
expect("dry run between real runs: still keeps 60,000", e.bal == 60_000 * 10 ** 8)

# 50. one raw unit flowing in between stop and restart: keep never drops (P2)
tmpd = tempfile.mkdtemp(prefix="sell-p2-")
e = Env(300_000)
run("p2 real, one sale", e, ["run", "--mode", "now", "--max-swaps", "1"], tmp=tmpd)
e.bal += 1
run("p2 restart after a 1-unit inflow", e, ["run", "--mode", "now"], tmp=tmpd)
expect("1-unit inflow: keeps at least 60,000", e.bal >= 60_000 * 10 ** 8 and e.bal < 60_001 * 10 ** 8)

# 51. the order list never readable before a swap: stops after max_waits (P3)
e = Env(300_000, pending_unreadable=10 ** 6)
run("list never readable", e, ["run", "--mode", "now"])
expect("list never readable: stops without a swap", e.swaps == [])

# 52. a dry run with the default journal does not write sell.jsonl
tmpd = tempfile.mkdtemp(prefix="sell-dryj-")
e = Env(300_000)
run("dry run default journal", e, ["run", "--mode", "now", "--dry-run"], tmp=tmpd, default_journal=True)
expect("dry run: writes sell-dry.jsonl, not sell.jsonl", not os.path.exists(os.path.join(tmpd, "sell.jsonl"))
       and os.path.exists(os.path.join(tmpd, "sell-dry.jsonl")))

# ---- regression cases, set 5 ----
# 53. --wallet is not the wallet baw is signed in to: refuse before any quote or swap
e = Env(300_000, signed_in="0x" + "22" * 20)
err = run("wallet mismatch", e, ["run", "--mode", "now"])
expect("wallet mismatch: refuses, nothing sold", err and "not the wallet baw is signed in to" in err
       and e.swaps == [] and e.bal == 300_000 * 10 ** 8)

# 54. baw gives no address: refuse (fail closed)
e = Env(300_000, signed_in="")
err = run("no signed-in address", e, ["run", "--mode", "now"])
expect("no signed-in address: refuses, nothing sold", err and "gave no BSC address" in err and e.swaps == [])

# 55. same wallet in lower case: accepted
e = Env(300_000, signed_in=os.environ["PEARL_WALLET"].lower())
run("wallet in lower case", e, ["run", "--mode", "now"])
expect("signed-in wallet in lower case: sells 80%", e.bal == 60_000 * 10 ** 8)

# 56. a malformed or mistyped --wallet is refused before anything else
for what, bad in (("38 hex digits", "0x" + "ab" * 19), ("a wrong EIP-55 checksum", "0x" + "0" * 32 + "DeadBeef"),
                  ("empty", ""), ("no 0x", "deadbeef" * 5)):
    e = Env(300_000)
    err = run(f"bad wallet: {what}", e, ["run", "--mode", "now", "--wallet", bad])
    expect(f"bad --wallet refused: {what}", err and "--wallet or PEARL_WALLET" in err and e.swaps == [])

# 57. the mining-run check with real processes (Windows: OpenProcess, never os.kill): a running child is alive,
# the same child after it exits is not, and pid files in UTF-16 or with a byte-order mark (Windows PowerShell 5)
# are read; an unreadable one counts as alive
tmpd = tempfile.mkdtemp(prefix="sell-pid-")
pidf = os.path.join(tmpd, "run.pid")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
try:
    with open(pidf, "w", encoding="utf-8") as f:
        f.write(f"{child.pid}\n")
    alive_running = ps.mining_run_alive(pidf)
    with open(pidf, "w", encoding="utf-16") as f:  # what `$p.Id > run.pid` writes in Windows PowerShell 5
        f.write(f"{child.pid}\r\n")
    alive_utf16 = ps.mining_run_alive(pidf)
    with open(pidf, "w", encoding="utf-8-sig") as f:
        f.write(f"{child.pid}")
    alive_bom = ps.mining_run_alive(pidf)
finally:
    child.kill()
    child.wait()
alive_after = ps.mining_run_alive(pidf)
with open(pidf, "w", encoding="utf-8") as f:
    f.write("not a pid")
alive_garbage = ps.mining_run_alive(pidf)
print(f"[run.pid with real processes] running={alive_running} utf16={alive_utf16} bom={alive_bom} "
      f"exited={alive_after} garbage={alive_garbage}")
expect("run.pid: a running process is alive", alive_running)
expect("run.pid: UTF-16 and byte-order-mark pid files are read", alive_utf16 and alive_bom)
expect("run.pid: the process after it exited is not alive", not alive_after)
expect("run.pid: an unreadable pid file counts as alive", alive_garbage)

# 58. baw not on PATH: refuse at once (never fall back to a bare "baw", which Windows would also look for in
# python's own folder and the working directory)
ps._submit.find_baw = lambda: None
e = Env(300_000)
err = run("baw not found", e, ["run", "--mode", "now"])
ps._submit.find_baw = lambda: "baw"
expect("baw not found: refuses before anything else, nothing sold", err and "baw not found" in err and e.swaps == [])

# ---- review round: --max-sell-coins, exact --keep-coins, signals, baw in its own group
def journal_events(tmp):
    with open(os.path.join(tmp, "sell.jsonl"), encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


# 59. --max-sell-coins caps the session's sales below the fraction
e = Env(300_000)
run("max sell 50k", e, ["run", "--mode", "now", "--max-sell-coins", "50000"])
expect("A4: --max-sell-coins 50000: sells 50,000, not 80%", sum(e.swaps) == 50_000 and e.bal == 250_000 * 10 ** 8)

# 60. ... also with --keep-coins lower than that, and with coins arriving while it sells
e = Env(300_000)
e.inflow = True
orig_baw = e.baw


def baw_with_inflow(args, timeout=60, _e=e, _orig=orig_baw):
    r = _orig(args, timeout)
    if args[:2] == ["market-order", "swap"]:
        _e.bal += 400_000 * 10 ** 8  # coins from elsewhere land right after the first swap
    return r


e.baw = baw_with_inflow
run("max sell with inflow", e, ["run", "--mode", "now", "--keep-coins", "0", "--max-sell-coins", "120000"])
expect("A4: --max-sell-coins holds while coins arrive mid-session and --keep-coins is 0", 0 < sum(e.swaps) <= 120_000)

# 61. --keep-coins is exact: no float on the way (987654321.98765432 is not a float)
tmpd = tempfile.mkdtemp(prefix="sell-dec-")
e = Env(1_000_000_000)
run("exact keep", e, ["run", "--mode", "now", "--keep-coins", "987654321.98765432", "--max-swaps", "1"], tmp=tmpd)
start = next((r for r in journal_events(tmpd) if r.get("ev") == "start"), {})
expect("A4: --keep-coins 987654321.98765432 is kept to the last unit", start.get("keep") == 98765432198765432)
expect("A4: the journal records Decimal arguments as text", start.get("args", {}).get("keep_coins") == "987654321.98765432")
for bad in ("NaN", "-1", "Infinity", "abc"):
    e = Env(300_000)
    err = run(f"bad --keep-coins {bad}", e, ["run", "--mode", "now", "--keep-coins", bad])
    expect(f"A4: --keep-coins {bad} refused, nothing sold", err is not None and e.swaps == [])

# 62. Ctrl-C while an order is in flight: it is followed to FINISHED and journaled, then the seller stops
tmpd = tempfile.mkdtemp(prefix="sell-sig-")
e = Env(300_000, signal_after_swaps=1)
run("signal during the first order", e, ["run", "--mode", "now"], tmp=tmpd)
evs = [r.get("ev") for r in journal_events(tmpd)]
expect("D4: a signal during an order: that order finishes and is journaled, no second swap, an end record",
       e.swaps == [100_000] and e.bal == 200_000 * 10 ** 8 and "filled" in evs and evs[-1] == "end")

# 63. Ctrl-C while it waits for the balance to settle: no sale at all
e = Env(300_000, signal_on_read=1)
err = run("signal before any sale", e, ["run", "--mode", "now"])
expect("D4: a signal before any sale: stops with a reason, nothing sold",
       err and "signal before any sale" in err and e.swaps == [])

# 64. a third run after the signalled ones sells normally (the flag starts cleared)
e = Env(300_000)
run("normal after signals", e, ["run"])
expect("D4: the next session starts without the signal flag", e.swaps == [100_000, 100_000, 40_000])

# 65. baw starts in a group of its own: the terminal's Ctrl-C never cuts an order in flight
seen = {}
saved_start, saved_finish = ps._submit.start_baw, ps._submit.finish_baw
ps._submit.start_baw = lambda args, own_group=False: seen.setdefault("own_group", own_group) or object()
ps._submit.finish_baw = lambda p, timeout: ('{"success": true, "data": {}}', "")
try:
    r = REAL_BAW(["wallet", "address"])
finally:
    ps._submit.start_baw, ps._submit.finish_baw = saved_start, saved_finish
expect("D4: baw runs with own_group=True", seen.get("own_group") is True and r.get("success") is True)

# 66b. only a finished sale exits 0: STOP, a signal or an error part way exits non-zero, after the end record
tmpd = tempfile.mkdtemp(prefix="sell-stopmid-")
e = Env(300_000, stop_after_swaps=1)
err = run("STOP during the first order", e, ["run", "--mode", "now"], tmp=tmpd)
evs = journal_events(tmpd)
expect("sell exit: STOP part way exits non-zero after following the order and writing end (done false)",
       err is not None and "stopped before the sale was done" in err and e.swaps == [100_000]
       and evs[-1].get("ev") == "end" and evs[-1].get("done") is False and any(r.get("ev") == "filled" for r in evs))
e = Env(300_000, signal_after_swaps=1)
err = run("signal during the first order (exit code)", e, ["run", "--mode", "now"])
expect("sell exit: a signal part way exits non-zero", err is not None and "stopped before the sale was done" in err)
e = Env(300_000)
err = run("a full sale", e, ["run", "--mode", "now"])
expect("sell exit: a sale that finished exits 0", err is None and sum(e.swaps) == 240_000)
e = Env(300_000)
said_done, real_say = [False], ps.say


def say_hook(*x):
    if any(str(v).startswith(("done:", "--max-sell-coins")) for v in x):
        said_done[0] = True
    real_say(*x)


ps.say, e.fail_reads_if = say_hook, (lambda: said_done[0])
try:
    err = run("end balance unreadable", e, ["run", "--mode", "now"])
finally:
    ps.say = real_say
expect("sell exit: the end balance unreadable after the sale exits non-zero",
       said_done[0] and err is not None and "end balance is unreadable" in err and sum(e.swaps) == 240_000)

# 66c. STOP while it waits for the balance to settle (30 s): stops at once, before any sale, non-zero
e = Env(300_000, stop_on_read=1)
t0 = CLOCK.now()
err = run("STOP while settling", e, ["run", "--mode", "now"])
expect("sell STOP: during the 30 s settling wait it stops at once, before any sale, non-zero",
       err is not None and "STOP file before any sale" in err and e.swaps == [] and CLOCK.now() - t0 < 30)

# 66d. every stop before the start record says "before any sale" (pearl.py then reports that nothing was sold)
cases = (("another wallet signed in", Env(300_000, signed_in="0x" + "22" * 20), {}),
         ("the mining run alive", Env(300_000), {"run_alive": True}),
         ("too little BNB", Env(300_000, bnb=0.00001), {}),
         ("an order PENDING", Env(300_000, pending_exists=True), {}),
         ("STOP while settling", Env(300_000, stop_on_read=1), {}))
for label, e, kw in cases:
    err = run(f"before any sale: {label}", e, ["run", "--mode", "now"], **kw)
    expect(f"F3: {label}: refused before any sale, nothing sold, and it says so",
           err is not None and "before any sale" in err and e.swaps == [])
e = Env(300_000)
e.fail_reads_if = lambda: True
err = run("start balance unreadable", e, ["run", "--mode", "now"])
expect("F3: start balance unreadable: one line (no traceback) saying nothing was sold",
       err is not None and "token balance unreadable" in err and "before any sale" in err and e.swaps == [])
e = Env(300_000, stop_after_swaps=1)
err = run("STOP part way (no 'before any sale')", e, ["run", "--mode", "now"])
expect("F3: a stop after the start record never claims 'before any sale'", err is not None and "before any sale" not in err)

# 66. the seller's signal handlers are put back when it returns (an in-process caller keeps its Ctrl-C)
expect("D4: SIGINT handler restored after run", signal.getsignal(signal.SIGINT) is INITIAL_SIGINT)

print("FAILED:", FAILED or "none")
sys.exit(1 if FAILED else 0)
