#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Submit precomputed PearlMiningV2 proofs through the Binance Agentic Wallet CLI (baw).

Standard library only. Needs `baw` on PATH, already signed in, Developer Mode on.

Configuration (every subcommand):
  --wallet / env PEARL_WALLET   the Agentic Wallet address that owns the Agent ID (0x + 40 hex digits;
                                a mixed-case address must carry a valid EIP-55 checksum)
  --handle / env PEARL_HANDLE   the raw Agent ID handle, without the display ".ai" suffix: 3-20 characters,
                                a-z and 0-9 only (the registry's isValidHandle); nothing is corrected for
                                you. agentId = uint256(keccak256(handle)), computed here

Subcommands
  check      read-only: registry identity, baw status/settings/tx-lock, RPC, clock vs chain, and the
             proofs file once it exists (run it before mining to confirm wallet and handle)
  rehearse   harmless txs (transfer 0 珍珠币 to self). --parallel N runs N previews and then
             N executes at the same time. Result on 2026-11-03: only one execute per address
             is accepted at a time; the others get 351716 and are not sent.
  run        the real submission. Pipeline: --previewers threads preview batches of 16 proofs,
             one executor sends ready previews back to back (waiting up to --settle s for the
             previous receipt, retrying on 351716), a tracker
             thread collects receipts, a watcher thread reads chain state and enforces stop rules.
             Starts on its own when the target day opens on chain.

Stop rules: stop on any preview risk item, requireConfirmation, an incomplete
preview, or a simulation failure that is not explained (we re-run every failed simulation with our
own eth_call to read the revert reason); stop when the day budget is full, when gas spent would
pass min(--budget-bnb, start balance - --keep-bnb), at the STOP file, or at the deadline.

Journal rule: an `attempt` record (requestId + proofs) is fsynced before every execute. Proofs in
an attempt stay blocked forever unless a receipt later shows that very tx reverted.
"""
import argparse
import collections
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl

WALLET = ""   # set by configure(): --wallet or PEARL_WALLET
HANDLE = ""   # set by configure(): --handle or PEARL_HANDLE (raw handle, no ".ai")
AGENT_ID = 0  # set by configure(): uint256(keccak256(HANDLE))
CONTRACT = "0x7E2DA878217EB995F9e5d36eD32c9Cea52442681"
START = 1793617734  # startsAt; day N opens at START + N*86400
CHAIN = "56"
RPCS = ["https://bsc-rpc.publicnode.com", "https://1rpc.io/bnb", "https://bsc-dataseed.bnbchain.org"]

SEL_MINE_BATCH = "b124a1c7"   # mineBatch(uint256,uint256[],uint256)
SEL_TRANSFER = "a9059cbb"     # transfer(address,uint256)
SEL_MINING_DAY = "549c22a1"
SEL_DAILY_REMAINING = "a7003616"
SEL_CURRENT_REWARD = "07621eca"
SEL_USED_PROOFS = "c30a0f25"
SEL_OWNER = "8da5cb5b"         # owner()
SEL_PAUSED = "5c975abb"        # paused()
REGISTRY = "0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F"  # Agent ID registry (ERC-721, token id = agentId)
SEL_OWNER_OF = "6352211e"      # ownerOf(uint256)
BURNED_OWNERS = {"0x" + "0" * 40, "0x000000000000000000000000000000000000dead"}
MINED_TOPIC = "0xbf3fc84d3727e2674940a192b38f04dcfd0573fdf1cf3b7fce957c9933ad858b"
DAILY_LIMIT = "0xf402e5b1"
PROOF_USED = "0xc9838a65"
ERRORS = {"0xf402e5b1": "DailyLimitReached", "0xd9c1988f": "InvalidSolution",
          "0x178423dd": "InvalidIdentity", "0xc9838a65": "ProofAlreadyUsed",
          "0x577742ca": "RewardChanged", "0xefdc789e": "MiningNotStarted",
          "0x8a7d2016": "MiningFinished", "0x7862e959": "InvalidBatchSize",
          "0xd93c0665": "EnforcedPause", "0xe82a5329": "InvalidStage"}

MAX_BATCH = 16
MIN_REWARD_PER_PROOF = 90 * 10**8   # minTotalReward per proof (8 decimals); run/check set it per day, --min-reward overrides
MINING_SUPPLY = 900_000_000 * 10**8  # contract constants, for the per-day reward floor
EMISSION_K = 9_000_000
DAILY_CAP = 20_000_000 * 10**8
DEFAULT_GAS_LIMIT = 700_000
MIN_GAS_PRICE_EST = 150_000_000     # 0.15 gwei floor for fee estimates
GOOD_SIM = "000000000"
LAG_GRACE = 30.0  # s: a preview sent within this long of our first target-day read may meet a lagging backend
BUSY_CODE = "351716"                # "A transaction to the same address ... is already in progress"
TRANSIENT_CODES = {"TIMEOUT", "NO_JSON", "BAD_JSON", "OSERROR"}
TRANSIENT_TEXT = re.compile(r"\b429\b|rate.?limit|too many requests|timed? ?out|\btimeout\b|ECONNRESET|"
                            r"ECONNREFUSED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|socket hang up|fetch failed|"
                            r"\bnetwork error\b|\b50[234]\b", re.I)

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- keccak / configuration
_KECCAK_RC = (0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
              0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
              0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
              0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
              0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
              0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008)
_KECCAK_ROT = ((0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61), (28, 55, 25, 21, 56), (27, 20, 39, 8, 14))
_M64 = (1 << 64) - 1


def _rol(v, n):
    return ((v << n) | (v >> (64 - n))) & _M64 if n else v


def keccak256(data):
    """Ethereum's Keccak-256 (original Keccak padding, not NIST SHA3-256). Pure Python, for short inputs."""
    rate = 136
    buf = bytearray(data) + b"\x01" + bytes((-len(data) - 1) % rate)
    buf[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]  # a[x][y] is lane x + 5y
    for off in range(0, len(buf), rate):
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(buf[off + 8 * i:off + 8 * i + 8], "little")
        for rc in _KECCAK_RC:
            c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y] ^ d[x], _KECCAK_ROT[x][y])
            a = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
            a[0][0] ^= rc
    return b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def cumulative_mining(claims):
    return MINING_SUPPLY * claims // (claims + EMISSION_K)


def min_reward_for_day(day):
    """Lowest reward per proof possible on `day` (the claim that fills its cap, if every day filled), minus 1%.
    0 from day 44 on: 45 full days would reach the whole mining supply, so the caps give no bound any more."""
    if day < 0:
        raise SystemExit(f"day {day}: mining days start at 0")
    cap_end = DAILY_CAP * (day + 1)
    if cap_end >= MINING_SUPPLY:
        return 0
    n = EMISSION_K * cap_end // (MINING_SUPPLY - cap_end) + 1
    return (cumulative_mining(n + 1) - cumulative_mining(n)) * 99 // 100


def wallet_problem(addr):
    """None for 0x + 40 hex digits (all lower case, all upper case, or a valid EIP-55 checksum); else why not."""
    if not isinstance(addr, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        return "expected 0x followed by 40 hex digits"
    h = addr[2:]
    if h != h.lower() and h != h.upper():
        k = keccak256(h.lower().encode()).hex()
        if any(c.isalpha() and c.isupper() != (int(k[i], 16) >= 8) for i, c in enumerate(h)):
            return "mixed case with a wrong EIP-55 checksum: a typo, or a different address"
    return None


HANDLE_RE = re.compile(r"[a-z0-9]{3,20}")  # the registry's isValidHandle: a-z and 0-9, minLen 3, maxLen 20


def handle_problem(handle):
    """None if `handle` passes the registry's isValidHandle; else why not. Nothing is corrected silently."""
    if not isinstance(handle, str) or handle == "":
        return "missing"
    if handle != handle.strip() or any(c.isspace() for c in handle):
        return "contains whitespace"
    if handle.lower().endswith(".ai"):
        return "pass the raw handle without the display '.ai' suffix"
    if handle != handle.lower():
        return "contains upper-case letters; registered handles are lower case"
    if not HANDLE_RE.fullmatch(handle):
        return "registered handles are 3 to 20 characters, only a-z and 0-9"
    return None


def configure(wallet, handle, min_reward=None):
    """Set WALLET, HANDLE and AGENT_ID = uint256(keccak256(raw handle)); optionally the minimum reward per proof
    in coins. The contract hashes the raw handle unchanged: never add or strip a display ".ai" suffix."""
    global WALLET, HANDLE, AGENT_ID, MIN_REWARD_PER_PROOF
    why = wallet_problem(wallet)
    if why:
        raise SystemExit(f"set the wallet address with --wallet or PEARL_WALLET ({why}; got {wallet!r})")
    why = handle_problem(handle)
    if why:
        raise SystemExit(f"set the raw Agent ID handle with --handle or PEARL_HANDLE ({why}; got {handle!r})")
    WALLET, HANDLE = wallet, handle
    AGENT_ID = int.from_bytes(keccak256(handle.encode()), "big")
    if min_reward is not None:
        if not 1 <= min_reward < 1000:
            raise SystemExit("--min-reward is in whole coins per proof, at least 1 (for example 85)")
        MIN_REWARD_PER_PROOF = int(round(min_reward * 10**8))


def now():
    return time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"


_print_lock = threading.Lock()


def say(*a):
    with _print_lock:
        print(now(), *a, flush=True)


# ---------------------------------------------------------------- platform (macOS, Linux, Windows)
def safe_stdio():
    """Printing 珍珠币 must never crash. A Windows console takes Unicode, but redirected output (a log file) uses the
    ANSI code page (cp1252, cp950, ...): there it becomes UTF-8. Elsewhere a non-UTF-8 stream prints '?' instead."""
    for s in (sys.stdout, sys.stderr):
        enc = str(getattr(s, "encoding", "") or "").lower().replace("-", "").replace("_", "")
        if enc == "utf8" or not hasattr(s, "reconfigure"):
            continue
        try:
            s.reconfigure(**({"encoding": "utf-8"} if WINDOWS else {}), errors="replace")
        except (ValueError, OSError):
            pass


WIN_LOCK_AT = 1 << 40  # Windows: the locked byte, far past any journal data (byte locks also block reads there)


class SenderLock:
    """Keeps a second sender off a journal: exclusive and non-blocking (OSError while another holder has it),
    released by release() or when the process exits. The lock belongs to the file itself, so another spelling of
    its path (relative, symlink, 8.3 name) meets the same lock.
    POSIX: flock on the journal's open file. Windows: msvcrt.locking on one byte at WIN_LOCK_AT, through a second
    handle on the journal; Windows byte locks are mandatory, and none of the journal's data is ever at that offset."""

    def __init__(self, journal):
        if WINDOWS:
            fd = os.open(journal.path, os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                os.lseek(fd, WIN_LOCK_AT, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                os.close(fd)
                raise
        else:
            fd = journal.f.fileno()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.fd, self.journal = fd, journal  # POSIX: the journal's open file carries the lock, keep it open

    def release(self):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        if WINDOWS:
            try:
                os.lseek(fd, WIN_LOCK_AT, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(fd)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)


BAW = None  # full path of the baw CLI, set by main() with find_baw(); None runs plain "baw"
UNSAFE_ARG = "UNSAFE_ARG"  # error code of a baw call refused before any process was started
_CMD_SAFE = re.compile(r"[A-Za-z0-9_.,:=+/@-]+\Z")  # one or more of these, nothing else (\Z: not even a newline)
# cmd.exe would expand % and ! even inside the quotes subprocess puts around a path with spaces, and npm's baw.cmd
# runs `SET dp0=%~dp0` unquoted, where & | < > ^ split the path anyway; a path without spaces is not quoted at all,
# and then ( ) ; , = split it too
_CMD_PATH_BAD_QUOTED = re.compile(r'[&|<>^%!"\r\n]')
_CMD_PATH_BAD_BARE = re.compile(r'[&|<>^%!"();,=]')


class UnsafeArg(Exception):
    """An argument (or a baw path) cmd.exe would interpret; never handed to a .cmd/.bat shim."""


def cmd_path_problem(path):
    """Why cmd.exe would misread this .cmd/.bat path, or None."""
    path = path or ""
    rx = _CMD_PATH_BAD_QUOTED if (" " in path or "\t" in path) else _CMD_PATH_BAD_BARE
    bad = sorted(set(rx.findall(path)))
    return f"cmd.exe would misread its path ({' '.join(bad)})" if bad else None


def find_baw():
    """Full path of the baw CLI on PATH, or None. On Windows npm installs global CLIs as baw.cmd next to an
    extensionless shell script that Windows cannot start, so only .exe/.cmd/.bat/.com count there."""
    if not WINDOWS:
        return shutil.which("baw")
    exts = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)]
    exts = [e for e in exts if e in (".com", ".exe", ".bat", ".cmd")] or [".exe", ".cmd", ".bat"]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip().strip('"')
        for e in exts:
            p = os.path.join(d, "baw" + e) if d else ""
            if p and os.path.isfile(p):
                return p
    return None


def baw_cmd(args):
    """[baw, *args, "--json"]. A .cmd/.bat shim runs through cmd.exe, which would act on & | < > ^ % ! " and
    spaces: such arguments are refused (UnsafeArg). baw arguments here are flags, hex and numbers."""
    exe = BAW or "baw"
    if WINDOWS and exe.lower().endswith((".cmd", ".bat")):
        why = cmd_path_problem(exe)
        if why:
            raise UnsafeArg(f"baw at {exe}: {why}")
        bad = [x for x in args if not _CMD_SAFE.match(str(x))]
        if bad:
            raise UnsafeArg(f"argument not safe to pass through cmd.exe: {bad[0]!r}")
    return [exe, *args, "--json"]


def start_baw(args, own_group=False):
    """Start baw with UTF-8 pipes and no stdin. own_group: the terminal's Ctrl-C (and on Windows Ctrl-Break and
    closing the console) does not reach it, so a call in flight finishes. POSIX: a new session. Windows: a new
    process group with its own hidden console (CREATE_NO_WINDOW), detached from ours."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
              errors="replace")
    if own_group and WINDOWS:
        kw.update(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    elif own_group:
        kw.update(start_new_session=True)
    p = subprocess.Popen(baw_cmd(args), **kw)
    p.pearl_own_group = own_group
    return p


def kill_tree(p):
    """Kill a baw process and its children. On Windows baw.cmd runs cmd.exe -> node.exe; killing cmd.exe alone
    would leave node.exe holding the output pipes. On POSIX a baw in its own session is killed as a group."""
    if not WINDOWS and getattr(p, "pearl_own_group", False):
        try:
            try:
                pg = os.getpgid(p.pid)
            except ProcessLookupError:  # macOS: an exited, unreaped leader has no pgid, its group may still live
                pg = p.pid if p.returncode is None else None
            if pg == p.pid and pg != os.getpgrp():  # its own group (start_new_session), never ours
                os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    if WINDOWS:
        tk = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run([tk, "/T", "/F", "/PID", str(p.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        p.kill()
    except OSError:
        pass


def finish_baw(p, timeout):
    """(stdout, stderr) of a started baw; on timeout the process tree is killed and TimeoutExpired raised."""
    try:
        return p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise


# ---------------------------------------------------------------- journal
class Journal:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.f = open(path, "a", buffering=1, encoding="utf-8")

    def write(self, **rec):
        rec["t"] = time.time()
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self.lock:
            self.f.write(line)
            self.f.flush()
            os.fsync(self.f.fileno())

    def read(self):
        out = []
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        return out


# ---------------------------------------------------------------- chain
def _post(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "content-type": "application/json", "user-agent": "pearl-submit/3"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.load(r)
    if not isinstance(j, dict):
        raise ValueError("non-object JSON-RPC response")
    return j


def rpc(method, params, timeout=6, allow_null=False, start=0):
    """First usable answer from the RPC list, starting at index `start`. A null result counts as
    unusable unless allow_null (receipts of pending txs are null)."""
    last = None
    order = RPCS[start % len(RPCS):] + RPCS[:start % len(RPCS)]
    for url in order:
        try:
            j = _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout)
        except Exception as e:  # network/HTTP/JSON problem on this endpoint: try the next one
            last = repr(e)
            continue
        if j.get("error"):
            last = j["error"]
            continue
        if "result" not in j or (j["result"] is None and not allow_null):
            last = f"null result from {url}"
            continue
        return j["result"]
    raise RuntimeError(f"all RPCs failed for {method}: {last}")


def hexint(v):
    if not isinstance(v, str) or not v.startswith("0x") or len(v) < 3:
        raise RuntimeError(f"bad hex value {v!r}")
    try:
        return int(v, 16)
    except ValueError:
        raise RuntimeError(f"bad hex value {v!r}")


def w32(x):
    return format(x, "064x")


def call(data):
    return rpc("eth_call", [{"to": CONTRACT, "data": "0x" + data}, "latest"])


MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"


def _aggregate3_calldata(selectors):
    """Multicall3.aggregate3((address target, bool allowFailure, bytes callData)[]) for 4-byte calls."""
    n = len(selectors)
    head = [w32(0x20), w32(n)] + [w32(32 * n + 160 * i) for i in range(n)]
    body = []
    for sel in selectors:
        body += [CONTRACT[2:].lower().rjust(64, "0"), w32(0), w32(0x60), w32(4), sel.ljust(64, "0")]
    return "0x82ad56cb" + "".join(head + body)


def _aggregate3_decode(hexdata, n):
    if not isinstance(hexdata, str):
        raise RuntimeError("bad multicall result (not a string)")
    d = hexdata[2:] if hexdata.startswith("0x") else hexdata
    if len(d) % 64 or not re.fullmatch(r"[0-9a-fA-F]*", d):  # whole 32-byte words only: no truncated values
        raise RuntimeError("bad multicall result (length or hex)")
    wd = [d[i:i + 64] for i in range(0, len(d), 64)]

    def w(i):
        if not 0 <= i < len(wd):
            raise RuntimeError("bad multicall result (offset out of range)")
        return int(wd[i], 16)
    if len(wd) < 2 or w(1) != n:
        raise RuntimeError("bad multicall result")
    out = []
    for i in range(n):
        t = w(2 + i) // 32 + 2       # tuple start (word index)
        ok = w(t)
        off = w(t + 1) // 32 + t      # bytes start
        ln = w(off)
        if ok != 1 or ln != 32:
            raise RuntimeError(f"multicall item {i} failed")
        out.append(w(off + 1))
    return out


def chain_state():
    """(day, remaining, reward) read in ONE eth_call, so the three values come from the same block.
    Separate calls could mix a new-day `miningDay` with an old-day `remaining` from a lagging node and
    falsely look like a full day at the opening."""
    sels = [SEL_MINING_DAY, SEL_DAILY_REMAINING, SEL_CURRENT_REWARD]
    res = rpc("eth_call", [{"to": MULTICALL3, "data": _aggregate3_calldata(sels)}, "latest"])
    try:
        day, rem, rew = _aggregate3_decode(res, 3)
    except (ValueError, IndexError, TypeError, AttributeError) as e:  # malformed answer: treat as an RPC failure
        raise RuntimeError(f"bad multicall result: {e!r}")
    if rew <= 0:  # the contract never pays 0 today; a 0 here is a bad answer, and it is a divisor below
        raise RuntimeError("bad multicall result: reward 0")
    return day, rem, rew


def latest_block():
    b = rpc("eth_getBlockByNumber", ["latest", False])
    if not isinstance(b, dict):
        raise RuntimeError("bad block")
    return hexint(b.get("number")), hexint(b.get("timestamp"))


def bnb_balance():
    return hexint(rpc("eth_getBalance", [WALLET, "latest"]))


def owner_burned():
    """True when nobody can pause/resume the contract (owner() is 0x0 or 0x...dEaD; renounce is disabled in
    PearlMiningV2, so a burned owner is the only way). Then, within one mining day, a fixed batch can only go
    from success to failure, never back: the day rollover is the only reset."""
    v = hexint(call(SEL_OWNER))
    return ("0x" + format(v, "040x")) in BURNED_OWNERS


_rr = [0]


def receipt(txh):
    _rr[0] += 1  # rotate RPCs so one lagging node does not hide receipts
    r = rpc("eth_getTransactionReceipt", [txh], allow_null=True, start=_rr[0])
    if r is not None and not isinstance(r, dict):
        raise RuntimeError("bad receipt")
    return r


def registry_owner():
    """Owner of token AGENT_ID in the Agent ID registry (lower case), or None if the handle is not registered."""
    try:
        r = rpc("eth_call", [{"to": REGISTRY, "data": "0x" + SEL_OWNER_OF + w32(AGENT_ID)}, "latest"])
    except RuntimeError:  # ownerOf reverts for a token that does not exist
        return None
    return "0x" + r[-40:].lower() if isinstance(r, str) and len(r) >= 42 else None


def proof_used(proof_hex):
    return hexint(call(SEL_USED_PROOFS + proof_hex.removeprefix("0x").rjust(64, "0"))) != 0


def simulate(calldata):
    """Our own eth_call from the wallet. Returns ('ok', None), ('revert', selector) or ('unknown', why)."""
    last = None
    for url in RPCS:
        try:
            j = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                            "params": [{"from": WALLET, "to": CONTRACT, "data": calldata}, "latest"]}, 6)
        except Exception as e:
            last = repr(e)
            continue
        err = j.get("error")
        if err:
            data = err.get("data") if isinstance(err, dict) else None
            if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
                return "revert", data[:10].lower()
            last = err
            continue
        if "result" in j:
            return "ok", None
    return "unknown", last


def mine_batch_calldata(nonces, min_total):
    return ("0x" + SEL_MINE_BATCH + w32(AGENT_ID) + w32(0x60) + w32(min_total)
            + w32(len(nonces)) + "".join(w32(n) for n in nonces))


def batch_calldata(batch):
    return mine_batch_calldata([n for n, _ in batch], MIN_REWARD_PER_PROOF * len(batch))


def transfer0_calldata():
    return "0x" + SEL_TRANSFER + w32(int(WALLET, 16)) + w32(0)


def parse_receipt(r):
    status = hexint(r.get("status"))
    gp = r.get("effectiveGasPrice") or r.get("gasPrice")
    if gp is None:
        raise RuntimeError("receipt without gas price")
    fee = hexint(r.get("gasUsed")) * hexint(gp)
    mined = 0
    for lg in r.get("logs") or []:
        topics = lg.get("topics") or [] if isinstance(lg, dict) else []
        if topics and str(topics[0]).lower() == MINED_TOPIC and str(lg.get("address", "")).lower() == CONTRACT.lower():
            mined += 1
    return status, fee, mined, hexint(r.get("blockNumber")), hexint(gp)


# ---------------------------------------------------------------- baw
NOT_LAUNCHED = "NOT_LAUNCHED"


def baw(args, journal=None, timeout=60, launch_lock=None, gate=None):
    """Run `baw ... --json`. With launch_lock/gate, the gate is checked and the process started while
    holding the lock; Runner.stop() takes the same lock, so no execute starts after a stop."""
    t0 = time.time()
    out = err = ""
    try:
        if launch_lock is not None:
            with launch_lock:
                if gate is not None and not gate():
                    j = {"success": False, "error": {"code": NOT_LAUNCHED, "message": "stopped before launch"}}
                    if journal:
                        journal.write(ev="baw", args=args, secs=0, resp=j)
                    return j, 0.0
                p = start_baw(args, own_group=True)
        else:
            p = start_baw(args, own_group=True)
        out, err = finish_baw(p, timeout)
        out, err = out or "", err or ""
        i = out.find("{")
        j = json.loads(out[i:]) if i >= 0 else {"success": False, "error": {
            "code": "NO_JSON", "message": (out + err)[-800:]}}
        if not isinstance(j, dict):
            j = {"success": False, "error": {"code": "BAD_JSON", "message": out[-800:]}}
    except subprocess.TimeoutExpired:
        j = {"success": False, "error": {"code": "TIMEOUT", "message": f"baw {args[:2]} > {timeout}s"}}
    except UnsafeArg as e:
        j = {"success": False, "error": {"code": UNSAFE_ARG, "message": str(e)}}
    except ValueError:
        j = {"success": False, "error": {"code": "BAD_JSON", "message": (out + err)[-800:]}}
    except OSError as e:
        j = {"success": False, "error": {"code": "OSERROR", "message": repr(e)}}
    dt = time.time() - t0
    if journal:
        journal.write(ev="baw", args=args, secs=round(dt, 3), resp=j)
    return j, dt


def preview(calldata, journal, timeout=60):
    return baw(["contract-call", "preview", "--binanceChainId", CHAIN, "--from", WALLET,
                "--to", CONTRACT, "--value", "0", "--inputData", calldata], journal, timeout=timeout)


def execute(request_id, journal, launch_lock=None, gate=None):
    return baw(["contract-call", "execute", "--requestId", request_id], journal,
               launch_lock=launch_lock, gate=gate)


def err_of(j):
    e = j.get("error")
    return e if isinstance(e, dict) else {"message": str(e)}


def judge_preview(j):
    """('ok', (requestId, gasLimit, expiresAt)) | ('sim', msg) | ('transient', msg) | ('refuse', msg)."""
    if j.get("success") is not True:
        err = err_of(j)
        code, msg = str(err.get("code")), str(err.get("message", ""))
        if code == "351805":
            return "sim", msg[:200]
        if code == BUSY_CODE:
            return "busy", msg[:200]
        if code == UNSAFE_ARG:  # before the transient check: its message may contain anything
            return "refuse", f"{code} {msg}"[:300]
        if code in TRANSIENT_CODES or TRANSIENT_TEXT.search(msg):
            return "transient", f"{code} {msg}"[:300]
        return "refuse", f"error {code} {err.get('name', '')} {msg}"[:300]
    d = j.get("data")
    if not isinstance(d, dict) or not isinstance(d.get("requestId"), str) or not d["requestId"]:
        return "refuse", "preview without requestId"
    if not _CMD_SAFE.match(d["requestId"]):  # on every OS, so a rehearsal elsewhere predicts Windows (cmd.exe)
        return "refuse", f"requestId {d['requestId'][:80]!r} is not a plain token: refusing it before any execute"
    if d.get("requireConfirmation") is not False:
        return "refuse", f"requireConfirmation={d.get('requireConfirmation')!r} (needs Binance App approval)"
    risks = d.get("risks")
    if (not isinstance(risks, dict) or not isinstance(risks.get("riskDetails"), list)
            or not isinstance(risks.get("addresses"), dict) or not isinstance(risks.get("riskBehaviors"), list)):
        return "refuse", "preview without a complete risks section"
    bad = list(risks["riskDetails"])
    addrs = risks["addresses"]
    for addr, info in addrs.items():
        if not isinstance(info, dict):
            return "refuse", f"unreadable risk info for {addr}"
        bad += [dict(r, address=addr) if isinstance(r, dict) else {"address": addr, "item": r}
                for r in (info.get("riskDetails") or [])]
    if risks.get("riskBehaviors"):
        bad.append({"riskBehaviors": risks["riskBehaviors"]})
    if bad:
        return "refuse", "risk " + json.dumps(bad, ensure_ascii=False)[:400]
    sim = d.get("simulationResult")
    if not isinstance(sim, dict) or not sim.get("simulationCode"):
        return "refuse", "preview without a simulation result"
    if sim["simulationCode"] != GOOD_SIM:
        return "sim", f"simulationCode {sim.get('simulationCode')} {sim.get('simulationErrorDetail')}"[:300]
    gl = d.get("gasLimit")
    try:
        gl = int(gl, 16) if isinstance(gl, str) and gl.startswith("0x") else int(gl)
    except (TypeError, ValueError):
        gl = DEFAULT_GAS_LIMIT
    if gl <= 0:
        gl = DEFAULT_GAS_LIMIT
    exp = d.get("expiresAt")
    exp = exp / 1000 if isinstance(exp, (int, float)) and exp > 0 else time.time() + 250
    return "ok", (d["requestId"], gl, exp)


# ---------------------------------------------------------------- proofs
def canon(h):
    return "0x" + format(int(str(h), 16), "064x")


def load_proofs(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    assert d["wallet"].lower() == WALLET.lower(), "proofs are for another wallet"
    seen, seen_n, out = set(), set(), []
    for nonce, proof in d["proofs"]:
        n, p = int(nonce, 16), canon(proof)
        if p in seen or n in seen_n:
            continue
        seen.add(p)
        seen_n.add(n)
        assert int(p, 16) >> 226 == 0, f"proof above stage-0 target: {p}"
        out.append((n, p))
    return out


def blocked_from_journal(recs):
    """Replay the journal in order. Proofs are blocked from their first attempt; a reverted receipt
    of the tx that carried them unblocks them (a later attempt blocks them again).
    Returns (blocked proofs, confirmed fees, fee reserved for attempts with unknown outcome,
    broadcasts lacking a receipt as {tx: record}, proofs known to be used on chain)."""
    blocked, fees, tx_rec, receipted, open_attempts, used = set(), 0, {}, set(), {}, set()
    early_status = {}  # receipts journaled before their broadcast record (older journals)
    attempt_proofs = {}
    default_est = DEFAULT_GAS_LIMIT * MIN_GAS_PRICE_EST
    for r in recs:
        ev = r.get("ev")
        prs = [canon(p) for p in (r.get("proofs") or [])]
        if ev == "attempt":
            blocked.update(prs)
            open_attempts[r.get("requestId")] = r.get("est", default_est)
            attempt_proofs[r.get("requestId")] = frozenset(prs)
        elif ev == "used_elsewhere":
            used.update(prs)
        elif ev == "exec_rejected":  # 351716 / stopped or refused before execute: nothing was sent
            blocked.difference_update(prs)
            open_attempts.pop(r.get("requestId"), None)
        elif ev == "exec_unknown":
            blocked.update(prs)  # stays in open_attempts: its fee stays reserved
        elif ev == "broadcast":
            rid = r.get("requestId")
            if rid is None:  # older records: close the open attempt that carried the same proofs
                rid = next((k for k in open_attempts if attempt_proofs.get(k) == frozenset(prs)), None)
            open_attempts.pop(rid, None)
            tx_rec[r["tx"]] = dict(r, proofs=prs, est=r.get("est", default_est))
            blocked.update(prs)
            if r["tx"] in early_status and early_status[r["tx"]] != 1:
                blocked.difference_update(prs)  # its reverted receipt was journaled first
        elif ev == "receipt":
            receipted.add(r["tx"])
            fees += r.get("fee", 0)
            if r["tx"] not in tx_rec:
                early_status[r["tx"]] = r.get("status")
            if r.get("status") != 1 and r["tx"] in tx_rec:
                blocked.difference_update(tx_rec[r["tx"]]["proofs"])
    reserved = sum(open_attempts.values())
    return blocked | used, fees, reserved, {t: rec for t, rec in tx_rec.items() if t not in receipted}, used


# ---------------------------------------------------------------- check
def cmd_check(a):
    ok = True
    say("python", sys.version.split()[0], "baw at", BAW or find_baw())
    say(f"wallet {WALLET}; agentId = keccak256({HANDLE!r}) = {AGENT_ID:#066x}")
    owner = registry_owner()
    say(f"registry ownerOf(agentId) = {owner or 'none (handle not registered)'}; "
        f"matches the wallet: {owner == WALLET.lower()}")
    ok &= owner == WALLET.lower()
    for args in (["wallet", "status"], ["wallet", "settings"], ["wallet", "tx-lock", "--binanceChainId", CHAIN]):
        j, dt = baw(args)
        say(f"baw {' '.join(args)} ({dt:.2f}s):", json.dumps(j, ensure_ascii=False)[:1500])
        ok &= j.get("success") is True
        if args[:2] == ["wallet", "status"]:
            st = (j.get("data") or {}).get("status") if isinstance(j.get("data"), dict) else None
            say(f"wallet session: {st} (must be CONNECTED; otherwise re-pair with the Binance App)")
            ok &= st == "CONNECTED"
    n, ts = latest_block()
    say(f"chain block {n} ts {ts}; local clock - block ts = {time.time() - ts:+.2f}s")
    day, rem, rew = chain_state()
    say(f"miningDay={day} dailyRemaining={rem / 1e8:.2f} currentReward={rew / 1e8:.4f}")
    global MIN_REWARD_PER_PROOF
    if getattr(a, "min_reward", None) is None:
        MIN_REWARD_PER_PROOF = min_reward_for_day(day + 1)
    say(f"min reward per proof for day {day + 1}: {MIN_REWARD_PER_PROOF / 1e8:.4f}"
        + (" (--min-reward)" if getattr(a, "min_reward", None) is not None else " (computed for the day)"))
    if MIN_REWARD_PER_PROOF == 0:
        say("no reward floor from day 44 on: pass --min-reward to set one")
    burned = owner_burned()
    paused = hexint(call(SEL_PAUSED)) != 0
    say(f"owner burned (nobody can pause/resume): {burned}; paused now: {paused}")
    ok &= burned and not paused
    say(f"BNB balance {bnb_balance() / 1e18:.6f}")
    nxt = START + 86400 * (day + 1)
    say(f"next day opens at unix {nxt} = {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(nxt))} local, "
        f"in {nxt - time.time():.0f}s")
    if not os.path.exists(a.proofs):
        say(f"no proofs file at {a.proofs} yet: identity and chain checks only")
    else:
        pr = load_proofs(a.proofs)
        jpath = getattr(a, "journal", None) or os.path.join(HERE, "journal.jsonl")
        blocked = blocked_from_journal(read_journal(jpath))[0]  # the rule run uses: sent proofs are skipped
        unsent = [x for x in pr if x[1] not in blocked]
        say(f"proofs file: {len(pr)} unique proofs; {len(pr) - len(unsent)} already sent according to {jpath}")
        if not unsent:
            say("no proof is left that has not been sent: mine new ones")
            ok = False
        else:
            used = {}  # proof -> used on chain: used but not in the journal means sent from elsewhere; run drops it
            for _, proof in unsent[:: max(1, len(unsent) // 5)][:5]:
                used[proof] = proof_used(proof)
                say(f"proof {proof[:18]}… used={used[proof]}")
            start = 0  # simulate batches of 16 in run's order until one holds no proof used elsewhere
            batch = unsent[:MAX_BATCH]
            kind, sel = simulate(batch_calldata(batch))
            while kind == "revert" and sel == PROOF_USED and start + MAX_BATCH < min(len(unsent), CHECK_SCAN):
                start += MAX_BATCH
                batch = unsent[start:start + MAX_BATCH]
                kind, sel = simulate(batch_calldata(batch))
            if kind == "revert" and sel == PROOF_USED:
                say(f"every batch in the first {min(len(unsent), CHECK_SCAN)} proofs not in the journal holds a proof "
                    "used on chain: copy the journal of the run that sent them here (or pass --journal)")
                ok, batch = False, []
            n_used = sum(used.values())
            if n_used:
                say(f"WARNING: {n_used} sampled proofs are already used on chain and will be skipped by run")
            if start:
                say(f"WARNING: the first {start} proofs not in the journal hold proofs used on chain; run skips them")
            if n_used or start:
                say("  (each batch holding one costs run a preview at the opening; if they were sent from another "
                    "machine or with another journal, use that journal here)")
            if batch:
                say(f"eth_call mineBatch({len(batch)}) from wallet now: {kind} {ERRORS.get(sel, sel)}"
                    " (DailyLimitReached before the day opens means identity and proofs pass)")
                ok &= (kind == "revert" and sel == DAILY_LIMIT) or kind == "ok"
    say("CHECK", "OK" if ok else "HAS PROBLEMS")


CHECK_SCAN = 1024  # check simulates at most this many proofs (64 batches) to get past proofs used elsewhere


def read_journal(path):
    """The journal's records, read only: check never creates, opens for writing or locks a journal."""
    recs = []
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    recs.append(r)
    return recs


# ---------------------------------------------------------------- rehearse
def cmd_rehearse(a):
    j = Journal(a.journal)
    data = transfer0_calldata()
    n = a.parallel
    b2b = getattr(a, "back_to_back", False)
    say(f"rehearsal: {n} x transfer(self, 0); previews concurrently, then executes "
        + ("back to back (each starts when the previous execute returns, without waiting for its receipt)"
           if b2b else "concurrently"))
    pv = [None] * n

    def do_preview(k):
        pv[k] = preview(data, j)

    th = [threading.Thread(target=do_preview, args=(k,)) for k in range(n)]
    [t.start() for t in th]
    [t.join() for t in th]
    ids = []
    for k, (pj, pdt) in enumerate(pv):
        verdict, info = judge_preview(pj)
        d = pj.get("data") if isinstance(pj.get("data"), dict) else {}
        say(f"#{k} preview {pdt:.2f}s {verdict} {'' if verdict == 'ok' else info} gasLimit={d.get('gasLimit')} "
            f"requireConfirmation={d.get('requireConfirmation')} risks={json.dumps(d.get('risks'), ensure_ascii=False)[:300]}")
        if verdict == "ok":
            ids.append(info[0])
    if len(ids) < n:
        say("not every preview was clean; executing none")
        return
    ex = [None] * n

    def do_execute(k):
        t0 = time.time()
        ex[k] = execute(ids[k], j) + (t0,)

    bn_after = [None] * n  # highest block number any RPC reports right after execute k returned
    samplers = []

    prev_seen = [None] * n  # did any RPC already have the previous tx's receipt right after execute k returned?

    def sample_bn(k):
        best, seen = None, None  # seen: None = no receipt query answered, False = all answered null
        prev = ex[k - 1][0].get("data") if k and isinstance(ex[k - 1][0].get("data"), dict) else {}
        for url in RPCS:
            try:
                r = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}, 4)
                best = max(best or 0, hexint(r.get("result")))
                if prev.get("txHash"):
                    rr = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt",
                                     "params": [prev["txHash"]]}, 4)
                    if "result" in rr:
                        seen = bool(seen) or rr["result"] is not None
            except Exception:  # noqa: BLE001 - a sample from the other RPCs is enough
                pass
        bn_after[k], prev_seen[k] = best, seen
    if b2b:
        for k in range(n):
            do_execute(k)
            t = threading.Thread(target=sample_bn, args=(k,))
            t.start()
            samplers.append(t)
        for t in samplers:
            t.join()
    else:
        th = [threading.Thread(target=do_execute, args=(k,)) for k in range(n)]
        [t.start() for t in th]
        [t.join() for t in th]
    sent = []
    for k, (ej, edt, t0) in enumerate(ex):
        say(f"#{k} execute {edt:.2f}s (started +{t0 - ex[0][2]:.2f}s"
            + (f", chain head right after it returned: {bn_after[k]}, previous receipt already seen: {prev_seen[k]}"
               if b2b else "") + "):",
            json.dumps(ej, ensure_ascii=False)[:400])
        d = ej.get("data") if isinstance(ej.get("data"), dict) else {}
        if d.get("txHash"):
            sent.append((d["txHash"], t0 + edt))
            j.write(ev="rehearse_broadcast", tx=d["txHash"])
    nonces = []
    blocks = {}
    for txh, t_b in sent:
        r = None
        while r is None and time.time() - t_b < 90:
            try:
                r = receipt(txh)
            except RuntimeError:
                pass
            if r is None:
                time.sleep(0.2)
        if r:
            tx = rpc("eth_getTransactionByHash", [txh])
            st, fee, _, blk, gp = parse_receipt(r)
            blocks[txh] = blk
            nonces.append(hexint(tx.get("nonce")))
            say(f"receipt {txh} block {blk} status {st} nonce {nonces[-1]} fee {fee / 1e18:.8f} "
                f"gasPrice {gp / 1e9:.4f} gwei, seen {time.time() - t_b:.2f}s after execute returned")
        else:
            say(f"receipt {txh}: none after 90s")
    say(f"RESULT nonces {sorted(nonces)} distinct={len(set(nonces)) == len(nonces)} sent={len(sent)}/{n}")
    if b2b:
        proven = rejected = unclear = 0
        for k in range(1, n):
            prev = ex[k - 1][0].get("data") if isinstance(ex[k - 1][0].get("data"), dict) else {}
            cur = ex[k][0].get("data") if isinstance(ex[k][0].get("data"), dict) else {}
            pb = blocks.get(prev.get("txHash"))
            if not prev.get("txHash"):
                continue
            if cur.get("txHash"):
                # proof: right after execute k returned, no RPC had the previous receipt, and the previous tx
                # landed more than 2 blocks above the highest head any RPC reported (a lagging head is not proof)
                if (pb is not None and bn_after[k] is not None and prev_seen[k] is False
                        and pb > bn_after[k] + 2):
                    proven += 1
                else:
                    unclear += 1
            elif str(err_of(ex[k][0]).get("code")) == BUSY_CODE:
                rejected += 1
            else:
                unclear += 1
        verdict = ("KEEP --settle DEFAULT: an execute was rejected while the previous tx was in progress" if rejected
                   else "SETTLE 0 IS SAFE: an execute was accepted while the previous tx was still pending" if proven
                   else "INCONCLUSIVE: keep --settle default")
        say(f"BACK-TO-BACK: proven accepted while pending={proven}, rejected 351716={rejected}, "
            f"inconclusive={unclear} -> {verdict}")
    lj, _ = baw(["wallet", "tx-lock", "--binanceChainId", CHAIN], j)
    say(f"tx-lock after: {json.dumps(lj.get('data') or lj.get('error'), ensure_ascii=False)}")


# ---------------------------------------------------------------- run
class Runner:
    def __init__(self, a):
        self.a = a
        self.j = Journal(a.journal)
        try:
            self.sender_lock = SenderLock(self.j)
        except OSError:
            sys.exit(f"another run already holds {a.journal}; refusing to start a second sender")
        self.lock = threading.Lock()
        self.launch_lock = threading.Lock()   # held while checking stop + starting an execute
        self.signalled = False
        self.diagnosing = 0
        self.busy_since = None
        self.target = a.day
        self.boundary = START + 86400 * a.day
        self.deadline = self.boundary + a.minutes * 60
        proofs = load_proofs(a.proofs)
        self.max_gp = 100_000_000     # highest effective gas price seen (wei)
        self.inflight = {}            # txh -> (batch, broadcast time, fee estimate)
        blocked, self.fees, self.reserved, open_, self.used = blocked_from_journal(self.j.read())
        for txh, rec in open_.items():  # earlier broadcasts without a receipt: resolve or keep tracking
            rc = None
            try:
                rc = receipt(txh)
                parsed = parse_receipt(rc) if rc else None
            except RuntimeError:
                parsed = None
            if parsed:
                st, fee, mined, blk, gp = parsed
                self.j.write(ev="receipt", tx=txh, status=st, fee=fee, mined=mined, block=blk, note="at start")
                self.fees += fee
                self.max_gp = max(self.max_gp, gp)
                if st != 1:
                    blocked.difference_update(rec["proofs"])
            else:
                batch = list(zip([int(n, 16) for n in rec.get("nonces") or []], rec["proofs"]))
                self.inflight[txh] = (batch, time.time(), rec["est"])
        self.burned = None  # owner_burned(); None = unreadable at start (read again when it matters)
        try:  # one bounded attempt: never delay the start near the opening
            self.burned = owner_burned()
        except RuntimeError as e:
            say("rpc", e)
        for i in range(5):
            try:
                self.start_balance = bnb_balance()
                break
            except RuntimeError as e:
                if i == 4:
                    raise
                say("rpc", e)
                time.sleep(1)
        self.keep = int(a.keep_bnb * 1e18)
        self.budget = min(int(a.budget_bnb * 1e18), self.fees + self.start_balance - self.keep)
        usable = [p for p in proofs if p[1] not in blocked and p[1] not in self.used]
        self.batches = collections.deque(usable[i:i + MAX_BATCH] for i in range(0, len(usable), MAX_BATCH))
        self.n_usable, self.n_blocked = len(usable), len(blocked)
        self.ready = queue.Queue()
        self.retried = set()          # first proof of batches already retried after a revert
        self.executing = False
        self.bad_receipts = collections.Counter()
        self.unknown_sims = 0
        self.stop_reason = None
        self.drain = False
        self.opened_at = None
        self.state = None
        self.sent = self.mined = self.reverted = 0
        self.unexplained_reverts = 0
        self.lagging = 0
        self.transient = 0
        self.skew = 0  # near-cap node skew (explained), counted apart from unexplained failures
        self.exec_fail = 0
        self.last_tx = None
        self.previewers_left = a.previewers
        self.executors_left = a.executors

    # -- shared helpers
    def stop(self, reason):
        with self.launch_lock:
            self._stop_locked(reason)

    def _stop_locked(self, reason):
        """Record the stop; the caller holds launch_lock (so no execute can start meanwhile)."""
        with self.lock:
            if self.stop_reason is None:
                self.stop_reason = reason
                self.j.write(ev="stop", reason=reason)  # first: a failing print below must not lose the record
                try:
                    say("STOP:", reason)
                except (OSError, ValueError):  # stdout gone (a closed pipe or file): the journal holds it
                    pass

    def stopped(self):
        return (self.stop_reason is not None or self.signalled or os.path.exists(self.a.stop_file)
                or time.time() > self.deadline)

    def take(self):
        """(batch, outstanding): outstanding tells an idle previewer whether work may still come back."""
        with self.lock:
            if self.batches:
                return self.batches.popleft(), True
            return None, bool(self.inflight) or self.ready.qsize() > 0 or self.executing

    def note_busy(self):
        with self.lock:
            if self.busy_since is None:
                self.busy_since = time.time()
            waited = time.time() - self.busy_since
        if waited > 180:
            self.stop(f"wallet backend busy (351716) for {waited:.0f}s")

    def give_back(self, batch):
        batch = [x for x in (batch or []) if x[1] not in self.used]  # never requeue a known-used proof
        if batch:
            with self.lock:
                self.batches.appendleft(batch)

    def fee_est(self, gas_limit):
        return gas_limit * max(MIN_GAS_PRICE_EST, self.max_gp * 3 // 2)

    def committed(self):
        with self.lock:
            return self.fees + self.reserved + sum(e for _, _, e in self.inflight.values())

    def fresh_state(self):
        """Current (day, remaining, reward), or None when the RPCs fail. Never returns old data."""
        try:
            st = chain_state()
        except RuntimeError as e:
            say("rpc", e)
            return None
        with self.lock:
            if self.state is None or st[0] >= self.state[0]:  # an older read never moves the day back
                self.state = st
            newly_open = st[0] >= self.target and self.opened_at is None
            if newly_open:
                self.opened_at = time.time()
        if newly_open:
            say(f"chain is on day {st[0]}: open (remaining {st[1] / 1e8:.0f}, reward {st[2] / 1e8:.4f})")
        return st

    def opened(self):
        return self.opened_at is not None

    # -- threads
    def watcher(self):
        k = 0
        while not self.finished():
            st = self.fresh_state()
            if st:
                day, rem, rew = st
                if day > self.target:
                    self.stop(f"chain moved past day {self.target}")
                elif day == self.target and rem < rew:
                    self.stop(f"day {day} budget full (remaining {rem / 1e8:.2f} < reward {rew / 1e8:.2f})")
            if k % 6 == 0 and self.opened():
                try:
                    bal = bnb_balance()
                    with self.lock:
                        pending = self.reserved + sum(e for _, _, e in self.inflight.values())
                    if bal - pending < self.keep:
                        self.stop(f"BNB {bal / 1e18:.6f} minus in-flight fees would drop under keep {self.keep / 1e18}")
                except RuntimeError as e:
                    say("rpc", e)
            if self.signalled:
                self.stop("signal (Ctrl-C / SIGTERM)")
            if os.path.exists(self.a.stop_file):
                self.stop("STOP file")
            if time.time() > self.deadline:
                self.stop("deadline")
            k += 1
            time.sleep(0.5)

    def previewer(self, k):
        start = self.boundary - self.a.lead + k * self.a.stagger
        while time.time() < start and not self.stopped():
            time.sleep(min(1.0, max(0.02, start - time.time())))
        try:
            while not self.stopped():
                if self.ready.qsize() >= self.a.executors * 2 or len(self.inflight) >= self.a.max_inflight:
                    time.sleep(0.05)
                    continue
                batch, outstanding = self.take()
                if batch is None:
                    if outstanding:  # a reverted or expired batch may still come back
                        time.sleep(0.2)
                        continue
                    say("no proofs left; draining")
                    break
                if self.opened() and self.state and self.state[0] == self.target:
                    _, rem, rew = self.state
                    with self.lock:
                        flying = sum(len(b) for b, _, _ in self.inflight.values()) + self.ready.qsize() * MAX_BATCH
                    if flying and (flying + len(batch)) * rew > rem:
                        self.give_back(batch)  # our own in-flight txs already cover what is left
                        time.sleep(0.2)
                        continue
                cd = batch_calldata(batch)
                t_prev = time.time()
                pj, pdt = preview(cd, self.j)
                with self.launch_lock:  # classify and publish together: no execute can start in between
                    verdict, info = judge_preview(pj)
                    if verdict == "sim":
                        with self.lock:
                            self.diagnosing += 1  # executor holds off until this failure is classified
                    elif verdict == "refuse":
                        self._stop_locked(f"preview refused: {info}")
                if verdict == "ok":
                    self.ready.put((batch, info))
                    with self.lock:
                        self.transient = 0
                        if self.opened_at is None or time.time() - self.opened_at < 60:
                            self.lagging = 0  # after the opening minute, unexplained failures accumulate
                    continue
                if verdict == "busy":  # previous tx still in progress at the wallet backend
                    self.give_back(batch)
                    self.note_busy()
                    time.sleep(0.2)
                    continue
                if verdict == "sim":  # always classify first; "not open yet" is one of the explained outcomes
                    try:
                        delay = self.after_sim_failure(batch, cd, t_prev)  # owns the batch until classified
                    finally:
                        with self.lock:
                            self.diagnosing -= 1
                    time.sleep(delay or 0)
                    continue
                if verdict == "refuse":  # the stop is already recorded above
                    self.give_back(batch)
                    break
                self.give_back(batch)
                if verdict == "transient":
                    with self.lock:
                        self.transient += 1
                        n = self.transient
                    say(f"preview transient error [{n}]: {info}")
                    time.sleep(min(3.0, 0.5 * n))  # keep trying until the deadline
                    continue
        finally:
            with self.lock:
                self.previewers_left -= 1

    def note_lag(self, msg):
        with self.lock:
            self.lagging += 1
            n = self.lagging
        early = self.opened_at is None or time.time() - self.opened_at < 60
        limit = 20 if early else 3
        say(f"simulation failed: {msg} [{n}/{limit}]")
        if n >= limit:
            self.stop(f"{n} unexplained simulation failures: {msg}")
        return 0.3

    def after_sim_failure(self, batch, cd, t_prev, deadline=None):
        """baw only says 'execution reverted'; re-run the call ourselves to read the reason.
        The batch goes back to the queue only on paths that retry. `deadline` (from confirm_backend_lag):
        the opening grace; past it nothing is classified, so the executor is never released late."""
        def late():
            if deadline is None or time.time() <= deadline:
                return False
            self.give_back(batch)
            if not self.stopped():  # a STOP file or signal seen first keeps its own reason
                self.stop("re-checking a batch baw keeps failing ran past the opening grace: unexplained")
            return True
        kind, sel = simulate(cd)
        tries = 0
        t_u = time.time()
        limit = deadline if deadline is not None else max(t_u + 10, (self.opened_at or 0) + LAG_GRACE)
        while kind == "unknown" and not self.stopped() and time.time() < limit:  # executor stays held
            tries += 1
            time.sleep(min(1.0, 0.3 * tries))
            kind, sel = simulate(cd)
        if late():
            return
        if kind == "revert" and sel == PROOF_USED:
            used = []
            for _, p in batch:
                try:
                    if proof_used(p):
                        used.append(p)
                except RuntimeError:
                    pass
            if not used:
                self.stop("ProofAlreadyUsed but no proof in the batch reads as used")
                return
            self.j.write(ev="used_elsewhere", proofs=used)
            with self.lock:
                self.used.update(used)
            if late():
                return
            say(f"dropping {len(used)} proofs that are already used on chain")
            self.give_back([x for x in batch if x[1] not in used])
            return
        if kind == "revert" and sel != DAILY_LIMIT:
            self.stop(f"unexpected simulation failure: {ERRORS.get(sel, sel)}")
            return
        if kind == "unknown":
            if not self.opened():
                # not open on our reads yet: nothing can be sent (the executor needs a target-day read first)
                say(f"could not re-simulate ({sel}) before the opening; retrying later")
                self.give_back(batch)
                return 0.5
            self.stop(f"simulation failure we could not re-simulate after {tries + 1} tries ({sel})")
            return
        st = self.fresh_state()
        if late():
            return
        if kind == "ok":
            with self.lock:
                flying = sum(len(b) for b, _, _ in self.inflight.values())
            if st and st[0] == self.target and st[1] < st[2] * (flying + len(batch) + MAX_BATCH):
                # near the cap a backend node a block ahead of ours (or one that already counts our pending txs)
                # sees less room than ours: expected skew. Counted apart from unexplained failures.
                self.give_back(batch)
                with self.lock:
                    self.skew += 1
                    n = self.skew
                say(f"near the cap: baw's node sees less room than ours (remaining {st[1] / 1e8:.0f}, "
                    f"{flying} of our proofs in flight) [{n}/40]")
                if n >= 40:
                    self.stop(f"{n} near-cap simulation mismatches")
                    return
                return 0.3
            if self.opened_at is not None and t_prev > self.opened_at + LAG_GRACE:
                self.give_back(batch)
                self.stop("baw's simulation failed while ours succeeds, not at the opening: unexplained")
                return
            return self.confirm_backend_lag(batch, cd, deadline)
        # DailyLimitReached
        if not st:
            self.give_back(batch)
            return self.note_lag("DailyLimitReached, chain state unreadable")
        day, rem, rew = st
        if day < self.target:
            self.give_back(batch)
            if time.time() > self.boundary + 60:
                self.stop(f"chain still on day {day} 60s after the boundary")
            return 0.1  # not open yet
        if rem < rew:
            self.give_back(batch)
            self.stop(f"day {day} budget full (remaining {rem / 1e8:.2f} < reward {rew / 1e8:.2f})")
            return
        if rem < rew * len(batch):
            fit = max(1, int(rem // rew) - 1)
            self.give_back(batch[fit:])
            self.give_back(batch[:fit])
            say(f"remaining {rem / 1e8:.2f} fits ~{fit} proofs; shrinking batch")
            return
        self.give_back(batch)
        return self.note_lag("DailyLimitReached while our RPC shows room")

    def confirm_backend_lag(self, batch, cd, deadline=None):
        """Called while diagnosing (the executor is held). Positive evidence for 'baw simulated on the old day':
        baw itself approves the same calldata on a re-preview. Within one day a fixed batch can only go from
        success to failure (remaining only falls, proofs only get used), never back, so a baw failure followed
        by a baw approval of the same calldata means the first simulation ran before the target day began.
        No approval by the end of the opening grace: unexplained, stop."""
        end = deadline if deadline is not None else (self.opened_at or time.time()) + LAG_GRACE
        while self.burned is None and not self.stopped() and time.time() < end:
            try:
                self.burned = owner_burned()
            except RuntimeError as e:
                say("rpc", e)
                time.sleep(0.5)
        if self.stopped():  # stopped meanwhile (STOP file, cap full): keep that reason
            self.give_back(batch)
            return
        if self.burned is None:
            self.give_back(batch)
            self.stop("baw's simulation failed while ours succeeds and owner() is unreadable: cannot confirm")
            return
        if not self.burned:  # an owner could pause and resume: a later approval would prove nothing
            self.give_back(batch)
            self.stop("baw's simulation failed while ours succeeds and the owner could pause/resume: unexplained")
            return
        self.note_lag("our eth_call succeeds, baw's simulation is behind; re-previewing the same batch")
        last, n_tr = "sim", 0

        def past_grace():
            """Checked after every blocking call: nothing is accepted or reclassified after the grace."""
            if time.time() <= end:
                return False
            self.give_back(batch)
            if last == "ok":
                self.stop("baw's approval of the failed batch came back after the opening grace: unexplained")
            elif last == "sim":
                self.stop("baw keeps failing a batch our eth_call accepts, past the opening grace: unexplained")
            else:
                self.stop(f"baw unavailable ({last}) while confirming a failed simulation, past the opening grace")
            return True
        while not self.stopped():
            if past_grace():
                return
            t_new = time.time()
            pj, _ = preview(cd, self.j, timeout=max(3.0, min(10.0, end - t_new + 2.0)))  # bounded hold
            with self.launch_lock:
                v, info = judge_preview(pj)
                if v == "refuse":
                    self._stop_locked(f"preview refused: {info}")
            if v == "refuse":
                break
            last = v
            if past_grace():  # also for an approval: one that comes back after the grace does not count
                return
            if v == "ok":
                with self.lock:
                    self.lagging = max(0, self.lagging - 1)  # explained: it no longer counts as unexplained
                    self.transient = 0
                self.ready.put((batch, info))
                say("baw now approves the same batch: its failed simulation ran on the old day (explained)")
                return 0
            if v == "sim":
                n_tr = 0
                kind, _ = simulate(cd)
                if past_grace():
                    return
                if kind != "ok":  # the chain moved on (cap reached, proof used, RPC trouble): classify that instead
                    return self.after_sim_failure(batch, cd, t_new, deadline=end)
                time.sleep(0.2)
            else:  # transient / busy: baw gave no simulation result; back off
                n_tr += 1
                time.sleep(min(2.0, 0.3 * n_tr))
        self.give_back(batch)
        return

    def executor(self, k):
        try:
            while True:
                if self.stopped():
                    break
                with self.lock:  # dequeue and ownership in one step (previewers read `executing`)
                    try:
                        batch, (rid, gas_limit, exp) = self.ready.get_nowait()
                        self.executing = True
                    except queue.Empty:
                        self.executing = False
                        if self.previewers_left == 0:
                            break
                        batch = None
                if batch is None:
                    time.sleep(0.03)
                    continue
                if self.send_one(batch, rid, gas_limit, exp) == "break":
                    break
        finally:
            with self.lock:
                self.executing = False
                self.executors_left -= 1

    def requeue_ready(self, batch, rid, gas_limit, exp):
        self.ready.put((batch, (rid, gas_limit, exp)))

    def send_one(self, batch, rid, gas_limit, exp):
        """Returns 'break' to end the executor, anything else to continue."""
        if self.stopped():
            self.give_back(batch)
            return "break"
        if not self.opened():
            st = self.fresh_state()
            if not (st and st[0] >= self.target):
                self.requeue_ready(batch, rid, gas_limit, exp)  # keep the clean preview, try again soon
                time.sleep(0.1)
                return "continue"
        self.wait_settle()
        # hold off while a previewer is classifying an unexplained simulation failure
        while self.diagnosing and not self.stopped():
            time.sleep(0.05)
        if self.stopped():
            self.give_back(batch)
            return "break"
        st = self.state
        if self.opened() and st and st[0] == self.target:
            with self.lock:
                flying = sum(len(b) for b, _, _ in self.inflight.values())
            if (flying + len(batch) + 2 * MAX_BATCH) * st[2] > st[1]:  # near the cap: confirm with a fresh read
                st = self.fresh_state()
                if not st or st[2] <= 0:  # cannot confirm the room: never launch on a stale view
                    self.requeue_ready(batch, rid, gas_limit, exp)
                    time.sleep(0.2)
                    return "continue"
                if st[0] == self.target:
                    room = int(st[1] // st[2]) - flying
                    if room < len(batch):  # would revert as a whole
                        if room <= 0:  # our own in-flight txs cover what is left (or it is full: watcher stops)
                            self.give_back(batch)
                            time.sleep(0.2)
                            return "continue"
                        self.give_back(batch[room:])
                        self.give_back(batch[:room])  # re-previewed first, sized to the room left
                        say(f"room for ~{room} proofs after our in-flight txs; splitting the batch")
                        return "continue"
        if time.time() > exp - 15:  # after any refresh above
            self.give_back(batch)
            return "continue"
        est = self.fee_est(gas_limit)  # after settle: uses the latest fees and gas price
        if self.committed() + est > self.budget:
            self.give_back(batch)
            self.stop(f"gas budget {self.budget / 1e18:.6f} BNB reached (committed {self.committed() / 1e18:.6f})")
            return "break"
        proofs = [p for _, p in batch]
        self.j.write(ev="attempt", requestId=rid, proofs=proofs, est=est)

        def gate():  # runs under launch_lock, the same lock stop() takes
            return (not self.stopped() and not self.diagnosing and self.fee_est(gas_limit) <= est
                    and self.committed() + est <= self.budget)

        ej, edt = execute(rid, self.j, launch_lock=self.launch_lock, gate=gate)
        d = ej.get("data") if isinstance(ej.get("data"), dict) else {}
        txh = d.get("txHash")
        code = str(err_of(ej).get("code"))
        if ej.get("success") is True and d.get("status") == "BROADCASTED" and isinstance(txh, str):
            # journal first: the tracker may write this tx's receipt as soon as it is in inflight
            self.j.write(ev="broadcast", tx=txh, requestId=rid, proofs=proofs, est=est,
                         nonces=[hex(n) for n, _ in batch])
            with self.lock:
                self.inflight[txh] = (batch, time.time(), est)
                self.sent += 1
                self.exec_fail = 0
                self.busy_since = None
                self.last_tx = txh
                n_sent = self.sent
            say(f"tx#{n_sent} broadcast {txh} ({len(batch)} proofs, execute {edt:.2f}s)")
            return "continue"
        if code == NOT_LAUNCHED:  # the gate said no at launch time: nothing was sent
            self.j.write(ev="exec_rejected", requestId=rid, proofs=proofs, code=NOT_LAUNCHED)
            if self.stopped():
                self.give_back(batch)
                return "break"
            if self.committed() + self.fee_est(gas_limit) > self.budget:
                self.give_back(batch)
                self.stop(f"gas budget {self.budget / 1e18:.6f} BNB reached at launch")
                return "break"
            self.requeue_ready(batch, rid, gas_limit, exp)  # diagnosis running or estimate moved
            time.sleep(0.05)
            return "continue"
        if code == UNSAFE_ARG:  # refused before any process started: provably not sent
            self.j.write(ev="exec_rejected", requestId=rid, proofs=proofs, code=UNSAFE_ARG)
            self.give_back(batch)
            self.stop(f"execute refused before launch: {err_of(ej).get('message', '')}"[:300])
            return "break"
        if code == BUSY_CODE:
            # rehearsal on 2026-11-03: concurrent executes got 351716 and were not sent
            # (the wallet nonce did not move). The previous tx is still in progress at the backend.
            self.j.write(ev="exec_rejected", requestId=rid, proofs=proofs, code=BUSY_CODE)
            self.give_back(batch)
            self.note_busy()
            time.sleep(0.2)
            return "continue"
        # anything else: the outcome is not provably "not sent", so these proofs stay blocked
        self.j.write(ev="exec_unknown", requestId=rid, proofs=proofs, resp=ej)
        with self.lock:
            self.reserved += est  # it may still land: keep its fee reserved
        if d.get("status") == "PENDING_CONFIRMATION":
            self.stop(f"execute needs Binance App approval (order {d.get('orderId')})")
            return "break"
        with self.lock:
            self.exec_fail += 1
            n = self.exec_fail
        say(f"execute failed [{n}/3] ({edt:.2f}s):", json.dumps(ej, ensure_ascii=False)[:400])
        if n >= 3:
            self.stop("3 execute failures in a row")
            return "break"
        time.sleep(0.3)
        return "continue"

    def wait_settle(self):
        """The wallet backend handles one tx per address at a time (351716 otherwise). Give the
        previous broadcast up to --settle seconds to get a receipt before the next execute."""
        t0 = time.time()
        while time.time() - t0 < self.a.settle and not self.stopped():
            with self.lock:
                pending = self.last_tx in self.inflight
            if not pending:
                return
            time.sleep(0.05)

    def tracker(self):
        while True:
            with self.lock:
                items = list(self.inflight.items())
                workers_done = self.previewers_left == 0 and self.executors_left == 0
            if not items and workers_done:
                break
            for txh, (batch, t_b, _) in items:
                try:
                    r = receipt(txh)
                except RuntimeError:
                    r = None
                parsed = None
                if r is not None:
                    try:
                        parsed = parse_receipt(r)
                    except RuntimeError as e:
                        self.bad_receipts[txh] += 1
                        say(f"unreadable receipt for {txh}: {e} [{self.bad_receipts[txh]}/20]")
                if parsed is None:
                    if time.time() - t_b > 180 or self.bad_receipts[txh] >= 20:
                        with self.launch_lock, self.lock:  # never between a gate check and its launch
                            _, _, est = self.inflight.pop(txh, (None, None, 0))
                            self.reserved += est
                        self.j.write(ev="stale", tx=txh)
                        say(f"{txh} unresolved; its proofs stay blocked and its fee stays reserved")
                    continue
                st, fee, mined, blk, gp = parsed
                with self.launch_lock, self.lock:  # real cost is known now: publish it before any I/O
                    self.fees += fee
                    self.max_gp = max(self.max_gp, gp)
                    if txh in self.inflight:
                        b_, t_, _ = self.inflight[txh]
                        self.inflight[txh] = (b_, t_, 0)  # its fee now sits in self.fees, not as an estimate
                self.j.write(ev="receipt", tx=txh, status=st, fee=fee, mined=mined, block=blk)
                say(f"receipt {txh[:12]} block {blk} status {st} mined {mined} fee {fee / 1e18:.7f}")
                if st != 1:
                    self.on_revert(batch)  # before the pop, so idle previewers do not exit early
                with self.launch_lock, self.lock:  # never between a gate check and its launch
                    self.inflight.pop(txh, None)
                    if st == 1:
                        self.mined += mined
                    else:
                        self.reverted += 1
            time.sleep(0.15)

    def on_revert(self, batch):
        st = self.fresh_state()
        if not st:
            return  # proofs stay usable only if we retry; without state, do not retry
        day, rem, rew = st
        if day == self.target and rem < rew:
            self.stop("tx reverted and the day budget is full")
            return
        if day == self.target and rem < rew * MAX_BATCH * 2:
            self.give_back(batch)  # end-of-day pressure: expected, smaller batches follow
            return
        if not batch:
            return
        key = batch[0][1]
        with self.lock:
            first = key not in self.retried
            self.retried.add(key)
            self.unexplained_reverts += 1
            n = self.unexplained_reverts
        if first:
            self.give_back(batch)
        say(f"tx reverted although day {day} has room [{n}/3]")
        if n >= 3:
            self.stop("3 reverted txs while the day still had room")

    def guarded(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:  # a crash in any worker stops the whole run instead of stalling it
            self.j.write(ev="crash", where=fn.__name__, error=repr(e), tb=traceback.format_exc()[-2000:])
            try:
                self.stop(f"crash in {fn.__name__}: {e!r}")  # first: a failing print below must not skip the stop
            finally:
                try:
                    say(f"crash in {fn.__name__}: {e!r}")  # always on the output, also when an earlier stop reason stands
                except (OSError, ValueError):  # stdout gone (a closed pipe or file): the journal and stop reason hold it
                    pass

    def finished(self):
        with self.lock:
            return self.previewers_left == 0 and self.executors_left == 0 and not self.inflight

    def run(self):
        a = self.a
        st = None  # a disconnected session would otherwise only show at the opening
        for i in range(3):
            ws, _ = baw(["wallet", "status"], self.j, timeout=15)
            if ws.get("success") is True:
                st = (ws.get("data") or {}).get("status") if isinstance(ws.get("data"), dict) else None
                break
            say(f"baw wallet status unavailable ({json.dumps(err_of(ws), ensure_ascii=False)[:200]}); retrying")
            time.sleep(2)
        else:
            raise SystemExit("baw wallet status unavailable 3 times: check baw, then start again")
        if st != "CONNECTED":
            raise SystemExit(f"baw wallet session is {st!r}, not CONNECTED: re-pair with the Binance App, then start again")
        say("wallet session CONNECTED")
        if self.burned is None:
            say("WARNING owner() unreadable at start: restart before the opening if this line repeats")
        say(f"owner burned (nobody can pause/resume): {self.burned}")
        say(f"run: {self.n_usable} usable proofs in {len(self.batches)} batches ({self.n_blocked} blocked); "
            f"balance {self.start_balance / 1e18:.6f} BNB, fees so far {self.fees / 1e18:.6f}, "
            f"effective budget {self.budget / 1e18:.6f}, keep {self.keep / 1e18}; previewers {a.previewers}, "
            f"executors {a.executors}; day {self.target} opens unix {self.boundary} "
            f"({self.boundary - time.time():.0f}s from now)")
        self.j.write(ev="run_start", usable=self.n_usable, boundary=self.boundary, budget=self.budget,
                     keep=self.keep, previewers=a.previewers, executors=a.executors)
        if (self.n_usable == 0 or self.budget <= 0) and not self.inflight:
            say("nothing to send (no proofs or no budget)")
            return
        if self.budget <= 0:
            self.stop("no budget left; only tracking earlier transactions")

        def on_signal(signum, frame):  # no locks here: the watcher turns this into a stop
            self.signalled = True
        if EARLY_SIGNAL[0]:  # signalled while starting up
            self.signalled = True

        old = {}  # Windows: SIGINT is Ctrl-C, SIGBREAK Ctrl-Break; SIGTERM is accepted but never sent (use the STOP file)
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                try:
                    old[getattr(signal, name)] = signal.signal(getattr(signal, name), on_signal)
                except (ValueError, OSError):
                    pass
        threads = [threading.Thread(target=self.guarded, args=(self.watcher,), daemon=True),
                   threading.Thread(target=self.guarded, args=(self.tracker,))]
        threads += [threading.Thread(target=self.guarded, args=(self.previewer, k)) for k in range(a.previewers)]
        threads += [threading.Thread(target=self.guarded, args=(self.executor, k)) for k in range(a.executors)]
        for t in threads:
            t.start()
        last = 0
        while any(t.is_alive() for t in threads[1:]):
            if time.time() - last > 15 and self.opened():
                with self.lock:
                    say(f"status: sent {self.sent} mined {self.mined} proofs, reverted {self.reverted}, "
                        f"in-flight {len(self.inflight)}, ready {self.ready.qsize()}, fees {self.fees / 1e18:.6f} BNB")
                last = time.time()
            time.sleep(0.5)
        for sig, h in old.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError, TypeError):
                pass
        # workers can see a stop condition and exit before the watcher records it: record it here
        if self.signalled:
            self.stop("signal (Ctrl-C / SIGTERM)")
        elif os.path.exists(self.a.stop_file):
            self.stop("STOP file")
        elif time.time() > self.deadline:
            self.stop("deadline")
        say(f"SUMMARY txs={self.sent} proofs_mined={self.mined} reverted={self.reverted} "
            f"fees={self.fees / 1e18:.6f} BNB stop={self.stop_reason}")
        self.j.write(ev="run_end", txs=self.sent, mined=self.mined, reverted=self.reverted,
                     fees=self.fees, stop=self.stop_reason)


def resolve_day(a):
    """--day defaults to the next opening; refuse a day before 0 or one whose --minutes window is over."""
    if a.day is None:
        a.day = int((time.time() - START) // 86400) + 1
    if a.day < 0:
        raise SystemExit(f"--day {a.day}: mining days start at 0")
    end = START + 86400 * a.day + a.minutes * 60
    if end < time.time():
        raise SystemExit(f"--day {a.day} opened at unix {int(START + 86400 * a.day)} and its {a.minutes:g}-minute window "
                         f"is over; leave out --day to wait for the next opening (day "
                         f"{int((time.time() - START) // 86400) + 1})")


EARLY_SIGNAL = [False]  # Ctrl-C / SIGTERM / Ctrl-Break before Runner.run installs its own handler


def cmd_run(a):
    """From the very start a signal becomes a clean stop (stop=signal in SUMMARY), not a crash in the middle of
    reading the chain or starting baw; the earlier handlers come back at the end."""
    EARLY_SIGNAL[0] = False
    old = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                old[getattr(signal, name)] = signal.signal(getattr(signal, name),
                                                           lambda signum, frame: EARLY_SIGNAL.__setitem__(0, True))
            except (ValueError, OSError):
                pass
    try:
        _cmd_run(a)
    finally:
        for sig, h in old.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError, TypeError):
                pass


def _cmd_run(a):
    global MIN_REWARD_PER_PROOF
    resolve_day(a)
    if getattr(a, "min_reward", None) is None:  # --min-reward overrides the per-day floor
        MIN_REWARD_PER_PROOF = min_reward_for_day(a.day)
    say(f"day {a.day} opens at unix {int(START + 86400 * a.day)}; "
        f"min reward per proof: {MIN_REWARD_PER_PROOF / 1e8:.4f}")
    if MIN_REWARD_PER_PROOF == 0:
        say("no reward floor from day 44 on: pass --min-reward to set one")
    r = Runner(a)
    try:
        r.run()
    finally:
        r.sender_lock.release()


def main():
    safe_stdio()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "rehearse", "run"):
        s = sub.add_parser(name)
        s.add_argument("--wallet", default=os.environ.get("PEARL_WALLET"),
                       help="Agentic Wallet address (default: env PEARL_WALLET)")
        s.add_argument("--handle", default=os.environ.get("PEARL_HANDLE"),
                       help="raw Agent ID handle without '.ai' (default: env PEARL_HANDLE)")
        s.add_argument("--proofs", default=os.path.join(HERE, "proofs.json"))
        s.add_argument("--journal", default=os.path.join(HERE, "journal.jsonl"))
        if name != "rehearse":
            s.add_argument("--min-reward", type=float, default=None,
                           help="minimum reward per proof in coins for minTotalReward (default: the lowest "
                                "reward possible on the target day, minus 1%%)")
    sub.choices["rehearse"].add_argument("--parallel", type=int, default=3)
    sub.choices["rehearse"].add_argument("--back-to-back", action="store_true",
                                         help="execute one after another without waiting for receipts")
    r = sub.choices["run"]
    r.add_argument("--day", type=int, default=None,
                   help="mining day to wait for (day N opens at startsAt + N*86400; default: the next opening)")
    r.add_argument("--budget-bnb", type=float, default=0.045)
    r.add_argument("--keep-bnb", type=float, default=0.005)
    r.add_argument("--minutes", type=float, default=90, help="stop this many minutes after the boundary")
    r.add_argument("--previewers", type=int, default=3)
    r.add_argument("--executors", type=int, default=1,
                   help="keep 1: the wallet backend rejects concurrent executes with 351716")
    r.add_argument("--settle", type=float, default=3.0,
                   help="max seconds to wait for the previous tx receipt before the next execute")
    r.add_argument("--max-inflight", type=int, default=30)
    r.add_argument("--lead", type=float, default=0.6, help="first preview this many seconds before the boundary")
    r.add_argument("--stagger", type=float, default=0.25)
    r.add_argument("--stop-file", default=os.path.join(HERE, "STOP"))
    a = ap.parse_args()
    if a.cmd == "run" and a.executors != 1:
        sys.exit("--executors must be 1: the wallet backend rejects concurrent executes (351716)")
    configure(a.wallet, a.handle, getattr(a, "min_reward", None))
    global BAW
    BAW = find_baw()
    if not BAW:
        sys.exit("baw not found on PATH" + (" (Windows: baw.cmd from `npm install -g`; see docs/PLATFORMS.md)"
                                            if WINDOWS else ""))
    if WINDOWS and BAW.lower().endswith((".cmd", ".bat")) and cmd_path_problem(BAW):
        sys.exit(f"baw is at {BAW}, but {cmd_path_problem(BAW)}: move npm's global folder (npm config set prefix) "
                 "to a path without these characters; see docs/PLATFORMS.md")
    {"check": cmd_check, "rehearse": cmd_rehearse, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
