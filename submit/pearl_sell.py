#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Sell a fraction of the wallet's 珍珠币 for BNB in chunks through `baw market-order` (Binance Web3 Wallet).

Default policy: after the mining run has finished, sell 80% in chunks of about 100k coins,
quoting first; halve a chunk whose price impact exceeds 50% (--max-impact) and never sell below half the
session's first rate (--floor); keep the rest.
Wallet: --wallet or env PEARL_WALLET (the Agentic Wallet address that holds the coins).

Timing (--mode trail, default; from the pool's history): do not dump at the moment the day's cap fills.
Day 0 (11/2) bottomed right as the cap filled and rebounded about 2x within 30 minutes before a multi-hour decline.
- a 1,000-coin reference quote every --poll seconds tracks the running high of the per-coin rate;
- a chunk is sold when the rate has pulled back --pullback (7%) from that high (the run-up is over), or when sales
  fall behind a linear schedule that starts after --warmup (20 min) and reaches 100% at --deadline (120 min),
  or after the deadline; chunks are at least --gap seconds apart. --mode now sells at once.
Backtest on the pool's swaps (40 start times): beats selling at once in 20/40, median +0.6%, worst -14.9%,
best +70%; from day 0's cap-full moment +19–23%. Not a guarantee.

Rules this script enforces:
- refuses to sell unless --wallet is the wallet baw is signed in to (`baw wallet address`, BSC entry):
  balances are read for --wallet, but swaps go out from the signed-in wallet;
- refuses to start while the mining run is alive, or when submit/run.pid is missing (start the mining run with
  `& echo $! > submit/run.pid`, or on Windows with Start-Process -PassThru, see docs/PLATFORMS.md): one tx per
  address at a time;
- every chunk: a small reference quote and a chunk quote; impact = 1 - chunk rate / reference rate, at most
  --max-impact. That compares with the price right now, so a second limit, --floor (default 0.5), holds against the
  session's first rate (the lower of two reference quotes, kept in the journal across a restart): no chunk sells
  below --floor x that rate, so many chunks in a row, or a market falling meanwhile, cannot walk the sale down
  without limit (--floor 0 turns it off). A chunk over either limit is halved down to --min-chunk; still over at
  the smallest chunk,
  --mode now quotes again twice 10 s apart and then stops (exit non-zero, nothing more sold; its line says
  "before any sale" when this session placed no order) and --mode trail waits 2 x --gap and re-quotes;
- why the cap is 50% (--max-impact 0.5) and slippage 3% (until v0.1.3: 2% and 1.5%): on 2026-11-04 the only pool
  was about 0.01 BNB deep. Selling 23,958 coins took 42% impact, so a 2% cap sold nothing and kept waiting while
  the price fell 88% within 268 s of the opening. In a pool this thin the impact is highest exactly when the
  price is highest (few coins in the pool), and the coins lose far more by waiting than by the impact;
- after a swap it polls `market-order list --orderId` to FINISHED / FAILED; an order still PENDING or with an
  unreadable status stops the script (state unknown: never resubmit blindly);
- progress is measured from the on-chain token balance, never from assumed fills;
- a STOP file next to this script, Ctrl-C, SIGTERM or (Windows) Ctrl-Break stops it between steps (also during
  a wait): an order already placed is followed to FINISHED / FAILED and journaled first; everything is journaled
  to sell.jsonl;
- the exit code is 0 only when the sale is done (nothing sellable left above the kept part, or --max-sell-coins
  reached) and the end balance was read; any other stop exits non-zero after the end record;
- --max-sell-coins caps what this run sells, whatever the balance reads later (coins that arrive from
  elsewhere are never sold beyond it). It is not journaled as kept: a later run of the same session keeps the
  most any of its runs kept (the fraction or --keep-coins), not this run's cap.

Usage:
  python3 pearl_sell.py plan                 # read-only: balance, target, one reference quote
  python3 pearl_sell.py run --dry-run [--assume-balance 200000]   # real quotes, no swap (simulated fills)
  python3 pearl_sell.py run [--fraction 0.8] [--chunk 100000] [--mode trail|now]
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

import pearl_submit as _submit  # same folder: baw lookup and process helpers, safe console output
from pearl_submit import wallet_problem  # same folder: 0x + 40 hex, EIP-55 checksum

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "0x7E2DA878217EB995F9e5d36eD32c9Cea52442681"
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
WALLET = ""  # set in main(): --wallet or PEARL_WALLET
CHAIN = "56"
RPCS = ["https://bsc-rpc.publicnode.com", "https://1rpc.io/bnb", "https://bsc-dataseed.bnbchain.org"]
DECIMALS = 8
REF_QTY = 1000
ORDER_TIMEOUT = 150  # seconds to wait for FINISHED / FAILED


def now():
    return time.time()


def sleep(sec):
    time.sleep(sec)


SIGNALLED = [False]  # set by Ctrl-C / SIGTERM / Ctrl-Break; the loop stops at its next STOP check
STOP_FILE = [None]   # set by cmd_run: the STOP file cuts a pause short too
STARTED = [False]    # set once the start record is written: before it, nothing can have been sold
NOTHING_SOLD = "before any sale"  # every stop before the start record, and a stop that placed no order (pearl.py looks for it)


def on_signal(signum, frame):
    SIGNALLED[0] = True


def stopping():
    return SIGNALLED[0] or bool(STOP_FILE[0] and os.path.exists(STOP_FILE[0]))


def pause(sec):
    """A wait between steps that a signal or the STOP file cuts short (an order in flight is never waited on with
    this)."""
    end = now() + sec
    while not stopping() and now() < end:
        sleep(min(1.0, end - now()))


def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Journal:
    def __init__(self, path):
        self.path = path

    def write(self, **rec):
        rec["t"] = round(now(), 3)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def baw(args, timeout=60):
    """Run `baw ... --json`; returns a dict ({"success": False, ...} on any failure)."""
    try:
        out, err = _submit.finish_baw(_submit.start_baw(args, own_group=True), timeout)  # Ctrl-C never cuts it
        out, err = out or "", err or ""
    except subprocess.TimeoutExpired:
        return {"success": False, "error": {"code": "TIMEOUT", "message": f"baw {' '.join(args[:2])} timed out"}}
    except _submit.UnsafeArg as e:
        return {"success": False, "error": {"code": _submit.UNSAFE_ARG, "message": str(e)}}
    except OSError as e:
        return {"success": False, "error": {"code": "NO_BAW", "message": str(e)}}
    try:
        j = json.loads(out)
        return j if isinstance(j, dict) else {"success": False, "error": {"code": "NO_JSON", "message": out[:300]}}
    except ValueError:
        return {"success": False, "error": {"code": "NO_JSON", "message": (out or err)[:300]}}


def baw_wallet_address(tries=3):
    """BSC address of the wallet baw is signed in to (lower case), or None if `baw wallet address` does not say so
    in `tries` calls 2 s apart (one backend blip after a long run must not cancel the whole sale)."""
    for i in range(tries):
        if i:
            sleep(2)
        r = baw(["wallet", "address"])
        d = r.get("data") if r.get("success") is True else None
        items = d.get("addresses") if isinstance(d, dict) else None
        for x in items if isinstance(items, list) else []:
            if isinstance(x, dict) and str(x.get("binanceChainId")) == CHAIN and isinstance(x.get("address"), str):
                return x["address"].lower()
    return None


def token_balance():
    """Raw token balance (8 decimals) from the first RPC that answers; RuntimeError if none."""
    data = "0x70a08231" + WALLET[2:].lower().rjust(64, "0")
    last = None
    for url in RPCS:
        try:
            req = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                                          "params": [{"to": TOKEN, "data": data}, "latest"]}).encode(),
                                         {"Content-Type": "application/json", "User-Agent": "pearl-sell"})
            r = json.load(urllib.request.urlopen(req, timeout=8)).get("result")
            if isinstance(r, str) and r.startswith("0x") and len(r) == 66:
                return int(r, 16)
            last = r
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"token balance unreadable: {last}")


SLIPPAGE = "3.0"  # run and plan set it from --slippage


def quote(qty):
    """BNB out for selling `qty` whole coins, as Decimal; None if the quote is unusable."""
    j = baw(["market-order", "quote", "--fromTokenQty", str(qty), "--fromToken", TOKEN, "--toToken", NATIVE,
             "--binanceChainId", CHAIN, "--slippage", SLIPPAGE])
    d = j.get("data") if j.get("success") is True else None
    try:
        out = Decimal(str(d["toCoinAmount"])) if isinstance(d, dict) else None
        ok = out is not None and out.is_finite() and out > 0
    except (KeyError, InvalidOperation, TypeError):
        ok = False
    return (out if ok else None), j


def native_balance():
    """BNB balance in wei; RuntimeError if no RPC answers."""
    last = None
    for url in RPCS:
        try:
            req = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                                                          "params": [WALLET, "latest"]}).encode(),
                                         {"Content-Type": "application/json", "User-Agent": "pearl-sell"})
            r = json.load(urllib.request.urlopen(req, timeout=8)).get("result")
            if isinstance(r, str) and r.startswith("0x"):
                return int(r, 16)
            last = r
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"BNB balance unreadable: {last}")


def pending_orders():
    """Our token's PENDING market orders; None when baw cannot tell (treated as 'maybe')."""
    r = baw(["market-order", "list", "--status", "PENDING", "--binanceChainId", CHAIN])
    d = r.get("data") if r.get("success") is True else None
    items = d.get("list") if isinstance(d, dict) else None
    if not isinstance(items, list):
        return None
    return [x for x in items if isinstance(x, dict) and x.get("status") == "PENDING"]


def _windows_pid_alive(pid):
    """Windows: is process `pid` still running? Never os.kill there: signal 0 is CTRL_C_EVENT and other values
    terminate the process. Unknown counts as alive."""
    import ctypes
    from ctypes import wintypes
    if not 0 < pid <= 0xFFFFFFFF:
        return True
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such process; access denied: alive
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except (OSError, AttributeError, ValueError, TypeError, ctypes.ArgumentError):
        return True


def mining_run_alive(pid_path):
    """True while the mining run may still be sending; a missing or unreadable pid file counts as alive."""
    try:
        with open(pid_path, "rb") as f:
            raw = f.read(64)
        # Windows PowerShell 5 writes UTF-16 with `>` / Out-File and may add a byte-order mark
        pid = int(raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig").strip())
    except (OSError, ValueError):
        return True
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # Linux: a zombie (state Z) has finished
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def wait_order(order_id, j):
    """Poll an order to FINISHED / FAILED. Returns (status, record); status None means unknown."""
    t0 = now()
    rec = None
    while now() - t0 < ORDER_TIMEOUT:
        r = baw(["market-order", "list", "--orderId", str(order_id)])
        j.write(ev="order_poll", order_id=order_id, resp=r)
        d = r.get("data") if r.get("success") is True else None
        items = d if isinstance(d, list) else (d.get("list") or d.get("orders") or d.get("items") or [d]) if isinstance(d, dict) else []
        rec = next((x for x in items if isinstance(x, dict) and str(x.get("orderId")) == str(order_id)), None)
        st = rec.get("status") if rec else None
        if st in ("FINISHED", "FAILED"):
            return st, rec
        sleep(4)
    return None, rec


def cmd_plan(a):
    global SLIPPAGE
    SLIPPAGE = str(a.slippage)
    bal = token_balance()
    say(f"wallet {WALLET} holds {bal / 10 ** DECIMALS:,.2f} 珍珠币; selling {a.fraction:.0%} = "
        f"{int(bal * a.fraction) // 10 ** DECIMALS:,} coins in chunks of up to {a.chunk:,}")
    ref, rj = quote(REF_QTY)
    say(f"reference quote {REF_QTY} coins:", json.dumps(rj, ensure_ascii=False)[:400])
    if ref is not None:
        say(f"≈ {ref / REF_QTY:.10f} BNB per coin; whole sale ≈ {ref / REF_QTY * int(bal * a.fraction) / 10 ** DECIMALS:.4f} BNB before impact")
    say("mining run alive:", mining_run_alive(a.run_pid))
    signed_in = baw_wallet_address()
    say(f"baw is signed in to {signed_in or '? (baw wallet address gave no BSC address)'}; "
        f"matches --wallet: {signed_in == WALLET.lower()}")


def cmd_run(a):
    """Signals only set a flag while selling: the loop stops at its next STOP check, after any order in flight."""
    SIGNALLED[0] = False
    old = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                old[getattr(signal, name)] = signal.signal(getattr(signal, name), on_signal)
            except (ValueError, OSError):
                pass
    STARTED[0] = False
    try:
        _cmd_run(a)
    except SystemExit as e:  # a refusal before the start record: say that nothing was sold
        if not STARTED[0] and isinstance(e.code, str) and NOTHING_SOLD not in e.code:
            raise SystemExit(f"{e.code} ({NOTHING_SOLD}: nothing was sold)") from None
        raise
    except RuntimeError as e:  # the start balance unreadable: one line, no traceback
        if not STARTED[0]:
            raise SystemExit(f"{e} ({NOTHING_SOLD}: nothing was sold)") from None
        raise
    finally:
        for sig, h in old.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError, TypeError):
                pass


def _cmd_run(a):
    j = Journal(a.journal)
    stop_file = os.path.join(HERE, "STOP")
    STOP_FILE[0] = stop_file
    global SLIPPAGE
    SLIPPAGE = str(a.slippage)
    signed_in = baw_wallet_address()  # balances are read for WALLET, swaps go out from the signed-in wallet
    if signed_in != WALLET.lower():
        raise SystemExit(f"--wallet {WALLET} is not the wallet baw is signed in to "
                         f"({signed_in or 'baw wallet address gave no BSC address'}): refusing to sell")
    if not a.dry_run:
        if mining_run_alive(a.run_pid):
            raise SystemExit(f"the mining run may still be alive ({a.run_pid} missing or its process running): "
                             "sell only after it has finished; never create run.pid by hand")
        if native_balance() < int(a.min_bnb * 10 ** 18):
            raise SystemExit(f"less than {a.min_bnb} BNB for gas")
        if pending_orders() != []:
            raise SystemExit("a 珍珠币 market order is PENDING (or the order list is unreadable): resolve it first")
    virtual = [None]  # dry run: simulated balance (raw units)

    low = [None]

    def balance():
        """Lowest balance seen: after the mining run it only falls, so a lagging RPC cannot raise it."""
        if a.dry_run:
            if virtual[0] is None:
                virtual[0] = (int(Decimal(str(a.assume_balance)) * 10 ** DECIMALS) if a.assume_balance
                              else token_balance())
            return virtual[0]
        b = token_balance()
        low[0] = b if low[0] is None else min(low[0], b)
        return low[0]
    if a.dry_run:
        say("DRY RUN: real quotes, no swap is sent; fills are simulated at the quoted amount")
    start_bal = balance()
    if not a.dry_run:
        for _ in range(10):
            pause(30)
            if stopping():
                break
            try:
                b2 = token_balance()
            except RuntimeError as e:
                say(e)
                continue
            if b2 == start_bal:
                break
            say(f"balance changed {start_bal} -> {b2}; reading again")
            start_bal = b2
            low[0] = b2
        else:
            raise SystemExit("the token balance keeps changing: is something still sending?")
        if stopping():
            raise SystemExit(f"stopped by {'a signal' if SIGNALLED[0] else 'the STOP file'} {NOTHING_SOLD}")
    keep = start_bal - int(Decimal(start_bal) * Decimal(str(a.fraction)))  # raw units that must stay
    prev, last_bal, floor, prev_rate0, prev_swapped = None, None, 0, None, False
    anchor_t = anchor_bal = None  # the session's start, or its last restart before the first order
    anchor_keep = None  # the most any run of the session kept (journaled keeps never include --max-sell-coins)
    if os.path.exists(a.journal) and not a.dry_run:  # the last session's start, and its last known balance
        for line in open(a.journal, encoding="utf-8", errors="replace"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if prev is not None and rec.get("ev") == "swap":  # only real runs send swaps (a dry run writes dry_fill)
                prev_swapped = True
                continue
            args = rec.get("args")
            if not isinstance(args, dict) or args.get("dry_run"):  # dry runs (and unmarked records) never count
                continue
            if rec.get("ev") == "start" and isinstance(rec.get("start_bal"), int) and isinstance(rec.get("keep"), int):
                prev, last_bal, prev_rate0, prev_swapped = rec, rec["start_bal"], None, False
                anchor_t, anchor_bal, anchor_keep = rec.get("t"), rec["start_bal"], rec["keep"]
            elif prev is not None and rec.get("ev") == "rate0" and not prev_swapped:  # the rate the swaps went by
                try:
                    r0 = Decimal(str(rec.get("rate0")))
                    prev_rate0 = r0 if r0.is_finite() and r0 > 0 else None
                except InvalidOperation:
                    pass
            elif prev is not None and rec.get("ev") == "restart" and isinstance(rec.get("start_bal"), int):
                last_bal = rec["start_bal"]
                if isinstance(rec.get("keep"), int):  # a run that kept more (--keep-coins) raises it for the rest
                    anchor_keep = max(anchor_keep, rec["keep"])
                if not prev_swapped:  # no order before it: the schedule and first rate start at this restart
                    anchor_t, anchor_bal, prev_rate0 = rec.get("t"), rec["start_bal"], None
            elif prev is not None and rec.get("ev") == "end" and isinstance(rec.get("end_bal"), int):
                last_bal = rec["end_bal"]
        if prev is not None and start_bal > last_bal:  # new coins came in (another night's mining): new session
            say(f"balance {start_bal / 10 ** DECIMALS:,.2f} is above the last session's "
                f"{last_bal / 10 ** DECIMALS:,.2f}: new session")
            prev, floor = None, int(anchor_keep)
    keep = max(keep, floor)  # a new session never keeps less than the last one did
    if prev is not None:
        keep = max(int(anchor_keep), keep)
        say(f"restart: keeping {keep / 10 ** DECIMALS:,.2f} coins (this session's keep, journal {a.journal})")
    if a.keep_coins is not None:
        kc = int((a.keep_coins * 10 ** DECIMALS).to_integral_value(rounding=ROUND_CEILING))  # exact, never less
        if kc < keep and prev is not None:
            raise SystemExit("--keep-coins is below this session's kept amount")
        keep = kc
    session_keep = keep  # journaled: what later runs of this session inherit (never this run's --max-sell-coins)
    max_sell = None  # raw units this run may sell at most
    if a.max_sell_coins is not None:
        max_sell = int((a.max_sell_coins * 10 ** DECIMALS).to_integral_value(rounding=ROUND_FLOOR))
        keep = max(keep, start_bal - max_sell)
    say(f"start balance {start_bal / 10 ** DECIMALS:,.2f}; keeping {keep / 10 ** DECIMALS:,.2f}; "
        f"selling {(start_bal - keep) / 10 ** DECIMALS:,.2f}")
    j.write(ev="start" if prev is None else "restart", start_bal=start_bal, keep=session_keep, run_keep=keep,
            args={k: (str(v) if isinstance(v, Decimal) else v) for k, v in vars(a).items()})
    STARTED[0] = True
    chunk, failed, waits, swaps, lwaits, sold_req = a.chunk, 0, 0, 0, 0, 0  # sold_req: coins in finished orders
    total_bnb = Decimal(0)
    t_start, last_sell, hist, qfail = now(), -1e18, [], 0
    # a restart keeps the session's schedule and first rate only once that session has sent an order: after a stop
    # that sent none (e.g. --mode now over the limits), a later --mode trail starts its warmup and rate afresh
    rate0 = prev_rate0 if prev is not None and prev_swapped else None
    cap, fl, now_tries = Decimal(str(a.max_impact)), Decimal(str(a.floor)), 0
    start_sellable = max(1, start_bal - keep)
    if prev is not None and prev_swapped:  # the schedule continues from the run that sent the first order
        t_start = float(anchor_t if isinstance(anchor_t, (int, float)) else t_start)
        start_sellable = max(1, int(anchor_bal) - keep)
    done = False  # only the two normal ends set it; any other stop makes the script exit non-zero
    while True:
        if os.path.exists(stop_file) or SIGNALLED[0]:
            say("STOP file: stopping" if os.path.exists(stop_file) else "signal (Ctrl-C / SIGTERM): stopping")
            break
        try:
            bal = balance()
        except RuntimeError as e:
            say(e)
            waits += 1
            if waits > a.max_waits:
                say("balance unreadable too long: stopping")
                break
            pause(5)
            continue
        sellable = (bal - keep) // 10 ** DECIMALS  # whole coins above the kept part
        if sellable < a.dust:
            say(f"done: {sellable:,} coins left above the kept {keep / 10 ** DECIMALS:,.0f}")
            done = True
            break
        ref, rj = quote(REF_QTY)
        if ref is None:
            say("reference quote unusable:", json.dumps(rj, ensure_ascii=False)[:300])
            qfail += 1
            if qfail > a.max_waits:
                say("too many unusable quotes in a row: stopping")
                break
            pause(a.poll)
            continue
        qfail = 0
        if rate0 is None:  # the lower of two quotes, so one high outlier cannot set it
            ref2, _ = quote(REF_QTY)
            if ref2 is not None:
                ref = min(ref, ref2)
            rate0 = ref / REF_QTY
            j.write(ev="rate0", rate0=str(rate0), args={"dry_run": a.dry_run})
            say(f"session's first rate {rate0:.10f} BNB per coin"
                + (f"; no chunk sells below {fl:.0%} of it (--floor)" if fl > 0 else "; --floor 0: no floor"))
        raw_rates = (hist[-1][1] if hist else [])[-2:] + [ref / REF_QTY]
        rate, t = sorted(raw_rates)[len(raw_rates) // 2], now()  # median of the last 3: one outlier moves nothing
        hist.append((rate, raw_rates))
        high = max((r for r, raw in hist if len(raw) == 3), default=rate)  # first two ticks cannot set it
        el = (t - t_start) / 60
        sold_frac = 1 - (bal - keep) / start_sellable if start_sellable > 0 else 1
        sched = 0.0 if el < a.warmup else min(1.0, (el - a.warmup) / max(1e-9, a.deadline - a.warmup))
        behind = sold_frac < sched - (min(chunk, sellable) * 10 ** DECIMALS) / (2 * start_sellable)  # > half a chunk late
        pulled = rate <= high * Decimal(str(1 - a.pullback))
        reason = ("now" if a.mode == "now" else "pullback" if pulled else "behind schedule" if behind
                  else "deadline" if el >= a.deadline else None)
        j.write(ev="tick", rate=str(rate), high=str(high), el=round(el, 2), sold=round(float(sold_frac), 4),
                sched=round(sched, 4), reason=reason)
        if reason is None or t - last_sell < a.gap:
            say(f"hold: {rate:.10f} BNB/coin = {rate / high:.1%} of the high since start; "
                f"sold {sold_frac:.0%} vs schedule {sched:.0%} at {el:.0f} min")
            pause(a.poll)
            continue
        qty = min(chunk, sellable)
        if max_sell is not None:
            qty = min(qty, (max_sell - sold_req) // 10 ** DECIMALS)
            if qty < 1:
                say(f"--max-sell-coins {a.max_sell_coins} reached: done")
                done = True
                break
        out, qj = quote(qty)
        j.write(ev="quote", qty=qty, ref=str(ref), out=str(out), resp=qj, reason=reason)
        if out is None:
            say("quote unusable:", json.dumps(qj, ensure_ascii=False)[:300])
            waits += 1
            if waits > a.max_waits:
                say("too many unusable quotes: stopping")
                break
            pause(a.gap)
            continue
        impact = 1 - (out / qty) / (ref / REF_QTY)
        say(f"[{reason}] quote {qty:,} coins -> {out:.6f} BNB (impact {impact:.2%})")
        why = (f"impact {impact:.0%} is above --max-impact {cap:.0%}" if impact > cap else
               f"{out / qty:.10f} BNB per coin is below --floor {fl:.0%} of the session's first rate {rate0:.10f}"
               if out / qty < rate0 * fl else None)
        if why:
            if qty > a.min_chunk:
                chunk = max(a.min_chunk, qty // 2)
                say(f"{why}: chunk -> {chunk:,}")
                continue
            if a.mode == "now":  # "now" means now: waiting minutes here only sells later into a falling price
                if now_tries < 2:  # one odd quote pair must not end the sale: quote again shortly
                    now_tries += 1
                    say(f"{why} at the smallest chunk ({qty:,} coins); quoting again in 10 s ({now_tries}/2)")
                    pause(10)
                    continue
                say(f"{why} at the smallest chunk ({qty:,} coins): stopping"
                    + (f" {NOTHING_SOLD} in this session" if swaps == 0 and sold_req == 0 else "")
                    + "; raise --max-impact or lower --floor, or sell later with --mode trail")
                break
            waits += 1
            if waits > a.max_waits:
                say("the price stays outside --max-impact / --floor: stopping"
                    + (f" {NOTHING_SOLD} in this session" if swaps == 0 and sold_req == 0 else ""))
                break
            say(f"{why} at the smallest chunk; waiting {a.gap * 2}s")
            pause(a.gap * 2)
            continue
        waits, now_tries = 0, 0
        if a.dry_run:
            virtual[0] -= qty * 10 ** DECIMALS
            sold_req += qty * 10 ** DECIMALS
            total_bnb += out
            j.write(ev="dry_fill", qty=qty, bnb=str(out))
            say(f"DRY: would sell {qty:,} coins for about {out:.6f} BNB (slippage cap {a.slippage}%)")
            last_sell = now()
            continue
        if mining_run_alive(a.run_pid):
            say("the mining run is alive again: stopping")
            break
        if swaps >= a.max_swaps:
            say(f"{swaps} swaps sent: stopping (raise --max-swaps to continue)")
            break
        pend = pending_orders()
        if pend:
            say("a market order is PENDING: stopping")
            break
        if pend is None:
            lwaits += 1
            if lwaits > a.max_waits:
                say("the order list stays unreadable: stopping")
                break
            say("order list unreadable; retrying before the swap")
            pause(a.gap)
            continue
        lwaits = 0
        swaps += 1
        bal_before = bal
        r = baw(["market-order", "swap", "--fromTokenQty", str(qty), "--fromToken", TOKEN, "--toToken", NATIVE,
                 "--binanceChainId", CHAIN, "--slippage", str(a.slippage), "--mev", "true",
                 "--gasLevel", a.gas_level], timeout=90)
        last_sell = now()
        j.write(ev="swap", qty=qty, resp=r)
        oid = (r.get("data") or {}).get("orderId") if r.get("success") is True and isinstance(r.get("data"), dict) else None
        if not oid:  # rejected, timed out or unreadable: the order may exist anyway, never send again blindly
            say("swap without an orderId (", json.dumps(r, ensure_ascii=False)[:300],
                "): stopping; check `baw market-order list --status PENDING` and the balance before any restart")
            break
        st, rec = wait_order(oid, j)
        if st is None:
            say(f"order {oid} has no terminal status after {ORDER_TIMEOUT}s: stopping (check `baw market-order list --orderId {oid}`)")
            break
        if st == "FAILED":
            say(f"order {oid} FAILED:", json.dumps(rec, ensure_ascii=False)[:400])
            failed += 1
            if failed >= 2:
                say("two failed orders in a row: stopping")
                break
            pause(a.gap)
            continue
        failed = 0
        sold_req += qty * 10 ** DECIMALS
        got = (rec or {}).get("toTokenActualQty")
        try:
            total_bnb += Decimal(str(got)) if got is not None else 0
        except InvalidOperation:
            pass
        refunds = [f"{x.get('amount')} {x.get('symbol')}" for x in ((rec or {}).get("refund") or [])
                   if isinstance(x, dict) and x.get("amount")]
        # progress only from the chain: wait for the balance to drop (a lagging RPC or a full refund otherwise)
        sold_raw = 0
        for _ in range(12):
            try:
                sold_raw = bal_before - balance()
            except RuntimeError:
                sold_raw = 0
            if sold_raw > 0:
                break
            sleep(5)
        say(f"order {oid} FINISHED{' (PARTIAL FILL, refund ' + ', '.join(refunds) + ')' if refunds else ''}: "
            f"balance fell by {sold_raw / 10 ** DECIMALS:,.2f} coins for {got if got is not None else '?'} BNB, "
            f"tx {(rec or {}).get('txHash')}")
        j.write(ev="filled", order_id=oid, qty=qty, sold_raw=sold_raw, bnb=str(got), refund=refunds)
        if sold_raw <= 0:
            say("the balance did not fall after a FINISHED order: stopping")
            break
    try:
        end_bal = balance()
        say(f"end balance {end_bal / 10 ** DECIMALS:,.2f} 珍珠币 (sold {(start_bal - end_bal) / 10 ** DECIMALS:,.2f}); "
            f"BNB from finished orders ≈ {total_bnb}")
        j.write(ev="end", end_bal=end_bal, bnb=str(total_bnb), done=done, args={"dry_run": a.dry_run})
    except RuntimeError as e:
        say(e)
        raise SystemExit(f"the end balance is unreadable ({e}): check the balance and {a.journal} before selling more")
    if not done:  # STOP, a signal, or an error stopped the sale part way: some coins may be sold already
        tail = ("this session placed no order" if swaps == 0 and sold_req == 0 else "some coins may be sold already")
        raise SystemExit(f"stopped before the sale was done (see the line above); {tail}: "
                         f"check the balance and {a.journal} before selling more")


def coins_value(s):
    """argparse type: a number of coins as an exact Decimal, finite and not negative (no float rounding)."""
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a number: {s!r}")
    if not d.is_finite() or d < 0:
        raise argparse.ArgumentTypeError(f"must be a finite number of coins, at least 0: {s!r}")
    return d


def main():
    _submit.safe_stdio()
    _submit.BAW = _submit.find_baw()
    if not _submit.BAW:  # never fall back to a bare "baw": Windows would also search python's own folder and cwd
        raise SystemExit("baw not found on PATH" + (" (Windows: baw.cmd from `npm install -g`; see docs/PLATFORMS.md)"
                                                    if _submit.WINDOWS else ""))
    if _submit.WINDOWS and _submit.BAW.lower().endswith((".cmd", ".bat")) and _submit.cmd_path_problem(_submit.BAW):
        raise SystemExit(f"baw is at {_submit.BAW}, but {_submit.cmd_path_problem(_submit.BAW)}: move npm's global "
                         "folder (npm config set prefix) to a path without these characters; see docs/PLATFORMS.md")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run"):
        s = sub.add_parser(name)
        s.add_argument("--wallet", default=os.environ.get("PEARL_WALLET"),
                       help="Agentic Wallet address (default: env PEARL_WALLET)")
        s.add_argument("--fraction", type=float, default=0.8)
        s.add_argument("--chunk", type=int, default=100_000)
        s.add_argument("--min-chunk", type=int, default=20_000)
        s.add_argument("--max-impact", type=float, default=0.5,
                       help="halve a chunk whose quote is this far below the 1,000-coin reference rate (0.5 = 50%%)")
        s.add_argument("--floor", type=float, default=0.5,
                       help="never sell a chunk below this fraction of the session's first rate (0 = no floor)")
        s.add_argument("--slippage", type=float, default=3.0, help="slippage cap in percent, at most 5")
        s.add_argument("--gap", type=float, default=60.0, help="minimum seconds between two sales")
        s.add_argument("--mode", choices=("trail", "now"), default="trail")
        s.add_argument("--poll", type=float, default=30.0, help="seconds between reference quotes while holding")
        s.add_argument("--pullback", type=float, default=0.07, help="sell after the rate falls this far from its high")
        s.add_argument("--warmup", type=float, default=20.0, help="minutes before the catch-up schedule starts")
        s.add_argument("--deadline", type=float, default=120.0, help="minutes by which everything is to be sold")
        s.add_argument("--max-waits", type=int, default=6)
        s.add_argument("--dust", type=int, default=1000, help="stop when fewer coins than this remain to sell")
        s.add_argument("--max-swaps", type=int, default=40)
        s.add_argument("--gas-level", choices=("LOW", "MEDIUM", "HIGH"), default="MEDIUM")
        s.add_argument("--min-bnb", type=float, default=0.002, help="refuse to start with less BNB for gas")
        s.add_argument("--keep-coins", type=coins_value, default=None, help="override the coins to keep (exact)")
        s.add_argument("--max-sell-coins", type=coins_value, default=None,
                       help="sell at most this many coins in this session, whatever the balance shows")
        s.add_argument("--run-pid", default=os.path.join(HERE, "run.pid"))
        s.add_argument("--journal", default=None, help="default sell.jsonl (sell-dry.jsonl for --dry-run)")
        s.add_argument("--dry-run", action="store_true", help="real quotes, never sends a swap")
        s.add_argument("--assume-balance", type=float, default=0, help="dry run only: pretend this many coins")
    a = ap.parse_args()
    if getattr(a, "journal", "x") is None:  # a dry run never writes the real journal
        a.journal = os.path.join(HERE, "sell-dry.jsonl" if getattr(a, "dry_run", False) else "sell.jsonl")
    if (not 0 < a.fraction <= 1 or a.min_chunk < 1 or a.chunk < a.min_chunk or not 0 < a.slippage <= 5
            or not 0 < a.pullback < 0.5 or not 0 <= a.warmup < a.deadline or a.dust < 1 or a.gap < 0
            or not 0 < a.max_impact < 1 or not 0 <= a.floor < 1 or a.max_waits < 0 or a.max_swaps < 1 or a.poll < 5
            or (a.keep_coins is not None and a.keep_coins < 0)):
        raise SystemExit("bad arguments")
    if a.assume_balance and not a.dry_run:
        raise SystemExit("--assume-balance is only for --dry-run")
    global WALLET
    why = wallet_problem(a.wallet)
    if why:
        raise SystemExit(f"set the wallet address with --wallet or PEARL_WALLET ({why}; got {a.wallet!r})")
    WALLET = a.wallet
    {"plan": cmd_plan, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
