# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""GPU proof search for PearlMiningV2 (BSC 0x7E2DA878217EB995F9e5d36eD32c9Cea52442681).

proof = keccak256(seed || nonce) with nonce as 32-byte big-endian; valid at stage 0 when the
top 30 bits of the proof are zero. Every GPU hit is re-hashed on the CPU before it is written.

Output: append-only CSV lines "nonce_hex,proof_hex" (both 0x-prefixed, 64 hex digits).

The seed is puzzleSeed(wallet) = keccak256(abi.encode(publicChallenge, wallet)): give --wallet (or env
PEARL_WALLET) and it is derived here, or pass --seed directly. The wallet must be 0x + 40 hex digits (a
mixed-case address needs a valid EIP-55 checksum); given both, --seed must equal the wallet's seed.

Usage:
  python3 miner.py --list-devices                          # OpenCL devices and their numbers
  python3 miner.py --wallet 0x... --out proofs.csv         # mine until stopped
  python3 miner.py --wallet 0x... --test                   # kernel self-test, easy mask
  python3 miner.py --wallet 0x... --bench 20               # hash rate for N seconds
  python3 miner.py ... --device 1                          # use device 1 of --list-devices
Stop a running miner by creating the file named by --stop-file (default STOP next to --out).

Any OpenCL 1.2+ device works: NVIDIA, AMD, Intel, Apple (also CPUs, through --device). By default the first NVIDIA
GPU, else the first GPU of any vendor. Each kernel launch is sized to about --launch-ms (default 250 ms), so a GPU
that also drives the display never stalls long enough for the Windows (TDR, 2 s) or macOS GPU watchdog.
"""
import argparse
import os
import re
import sys
import time

import numpy as np
import pyopencl as cl
from Crypto.Hash import keccak

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC_CHALLENGE = bytes.fromhex("30a0e07e58b98de26bb9881b31249b6aa4fc9a479a715fad16dfd3474eb39bdf")
STAGE0_MASK = 0xFCFFFFFF  # low 32 bits of lane 0 = hash bytes 0..3; bytes 0-2 zero, byte 3 <= 3


def keccak256(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


def wallet_problem(addr):
    """None for 0x + 40 hex digits (all lower, all upper, or a valid EIP-55 checksum); else why not."""
    if not isinstance(addr, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        return "expected 0x followed by 40 hex digits"
    h = addr[2:]
    if h != h.lower() and h != h.upper():
        k = keccak256(h.lower().encode()).hex()
        if any(c.isalpha() and c.isupper() != (int(k[i], 16) >= 8) for i, c in enumerate(h)):
            return "mixed case with a wrong EIP-55 checksum: a typo, or a different address"
    return None


def seed_for(wallet: str) -> bytes:
    """puzzleSeed(wallet) = keccak256(abi.encode(publicChallenge, wallet))."""
    return keccak256(PUBLIC_CHALLENGE + bytes(12) + bytes.fromhex(wallet.lower()[2:]))


def resolve_seed(seed_arg, wallet):
    """The 32-byte seed from --seed and/or --wallet; SystemExit on a malformed value or a mismatch."""
    if wallet is not None:
        why = wallet_problem(wallet)
        if why:
            raise SystemExit(f"--wallet / PEARL_WALLET: {why} (got {wallet!r})")
    if seed_arg is not None:
        if not re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", seed_arg):
            raise SystemExit(f"--seed: expected 64 hex digits (got {seed_arg!r})")
        seed = bytes.fromhex(seed_arg.lower().removeprefix("0x"))
        if wallet is not None and seed != seed_for(wallet):
            raise SystemExit("--seed is not puzzleSeed(--wallet / PEARL_WALLET): proofs would be useless; "
                             "give only one of them, or fix the one that is wrong")
        return seed
    if wallet is None:
        raise SystemExit("give --wallet (or set PEARL_WALLET) or --seed")
    return seed_for(wallet)


def lanes(b: bytes):
    return [int.from_bytes(b[i:i + 8], "little") for i in range(0, len(b), 8)]


def safe_stdio():
    """Never crash on printing: redirected output on Windows uses the ANSI code page and becomes UTF-8 here."""
    for s in (sys.stdout, sys.stderr):
        enc = str(getattr(s, "encoding", "") or "").lower().replace("-", "").replace("_", "")
        if enc != "utf8" and hasattr(s, "reconfigure"):
            try:
                s.reconfigure(**({"encoding": "utf-8"} if os.name == "nt" else {}), errors="replace")
            except (ValueError, OSError):
                pass


def all_devices():
    """[(platform, device)] over every OpenCL platform, in a stable order (the --device numbers)."""
    try:
        platforms = cl.get_platforms()
    except cl.Error as e:
        raise SystemExit(f"no OpenCL platform found ({e}): install the OpenCL runtime of your GPU driver, "
                         "see docs/PLATFORMS.md")
    out = []
    for p in platforms:
        try:
            out += [(p, d) for d in p.get_devices()]
        except cl.Error:  # a platform without devices (e.g. a GPU driver for a card that is not installed)
            pass
    return out


def default_index(devs):
    """The first NVIDIA GPU, else the first GPU of any vendor, else None."""
    for i, (p, d) in enumerate(devs):
        if d.type & cl.device_type.GPU and "NVIDIA" in p.name:
            return i
    for i, (p, d) in enumerate(devs):
        if d.type & cl.device_type.GPU:
            return i
    return None


def describe(p, d):
    kinds = [k for k in ("GPU", "CPU", "ACCELERATOR") if d.type & getattr(cl.device_type, k)] or ["OTHER"]
    try:
        unified = " unified-memory" if d.host_unified_memory else ""
    except (cl.Error, AttributeError):
        unified = ""
    return (f"{d.name.strip()} [{'/'.join(kinds)}{unified}] platform={p.name.strip()!r} "
            f"units={d.max_compute_units} mem={d.global_mem_size / 2**30:.1f}GiB {d.version.strip()}")


def list_devices():
    devs = all_devices()
    if not devs:
        raise SystemExit("OpenCL platforms found, but no devices: see docs/PLATFORMS.md")
    dflt = default_index(devs)
    for i, (p, d) in enumerate(devs):
        print(f"  {i}: {describe(p, d)}{'   <- default' if i == dflt else ''}")
    if dflt is None:
        print("no GPU: pick a device with --device N (a CPU works, slowly)")


def pick_device(index):
    devs = all_devices()
    if index is not None:
        if not 0 <= index < len(devs):
            raise SystemExit(f"--device {index}: there are {len(devs)} OpenCL devices, see --list-devices")
        return devs[index][1]
    i = default_index(devs)
    if i is None:
        raise SystemExit("no OpenCL GPU found: see --list-devices and pick one with --device N")
    return devs[i][1]


def work_group_limit(knl, dev):
    """The largest work-group size this kernel may use on this device (CL_KERNEL_WORK_GROUP_SIZE)."""
    return min(knl.get_work_group_info(cl.kernel_work_group_info.WORK_GROUP_SIZE, dev), dev.max_work_group_size)


def main():
    safe_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-devices", action="store_true", help="list the OpenCL devices and exit")
    ap.add_argument("--device", type=int, default=None,
                    help="device number from --list-devices (default: first NVIDIA GPU, else first GPU)")
    ap.add_argument("--seed", help="puzzleSeed(wallet) as 0x + 64 hex digits (or give --wallet)")
    ap.add_argument("--wallet", default=os.environ.get("PEARL_WALLET"),
                    help="derive the seed from this wallet (default: env PEARL_WALLET)")
    ap.add_argument("--out", default=os.path.join(HERE, "proofs.csv"))
    ap.add_argument("--stop-file", default=None)
    ap.add_argument("--global-size", type=int, default=1 << 22, help="work-items per launch (default 4194304)")
    ap.add_argument("--iters", type=int, default=0,
                    help="hashes per work-item per launch; 0 (default) adapts it to --launch-ms")
    ap.add_argument("--launch-ms", type=float, default=250.0,
                    help="target time per kernel launch in ms (default 250; keep it well under 2000 on a GPU "
                         "that drives a display)")
    ap.add_argument("--local-size", type=int, default=0,
                    help="work-group size; 0 (default) lets the driver choose")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--bench", type=float, default=0.0)
    ap.add_argument("--max-proofs", type=int, default=0)
    args = ap.parse_args()
    if args.list_devices:
        list_devices()
        return
    if args.global_size < 1 or args.iters < 0 or not 1 <= args.launch_ms <= 10000 or args.local_size < 0:
        raise SystemExit("bad --global-size / --iters / --launch-ms / --local-size")

    seed = resolve_seed(args.seed, args.wallet or None)
    prefix = os.urandom(24)
    s = lanes(seed)
    p = lanes(prefix)

    dev = pick_device(args.device)
    ctx = cl.Context([dev])
    q = cl.CommandQueue(ctx)
    with open(os.path.join(HERE, "keccak_miner.cl"), encoding="utf-8") as f:
        src = f.read()
    try:
        prg = cl.Program(ctx, src).build()
    except cl.Error as e:
        raise SystemExit(f"OpenCL kernel build failed on {dev.name.strip()}: {e}\n"
                         "see docs/PLATFORMS.md (Troubleshooting); --list-devices and --device pick another device")
    knl = prg.search
    mf = cl.mem_flags
    count = np.zeros(1, dtype=np.uint32)
    found = np.zeros(1024, dtype=np.uint64)
    count_buf = cl.Buffer(ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=count)
    found_buf = cl.Buffer(ctx, mf.READ_WRITE, found.nbytes)

    mask = 0xFFF if args.test else STAGE0_MASK
    limit = work_group_limit(knl, dev)
    if args.local_size > limit:
        raise SystemExit(f"--local-size {args.local_size}: this kernel allows at most {limit} on {dev.name.strip()}")
    local = args.local_size or None  # None: the driver picks the work-group size, as it did on mainnet
    unit = args.local_size or 1 << (min(limit, 256).bit_length() - 1)  # global size stays a multiple of this
    gsz = -(-args.global_size // unit) * unit
    auto = args.iters == 0
    iters = args.iters or 1
    target = args.launch_ms / 1000.0
    stop_file = args.stop_file or os.path.join(os.path.dirname(os.path.abspath(args.out)), "STOP")
    print(f"seed=0x{seed.hex()} device={dev.name.strip()} prefix=0x{prefix.hex()} mask=0x{mask:x} global={gsz} "
          f"local={local or 'driver'} (kernel limit {limit}) "
          f"iters={'auto, ~%g ms per launch' % args.launch_ms if auto else iters}", flush=True)

    def launch(base, gsz, iters, mask):
        cl.enqueue_copy(q, count_buf, np.zeros(1, dtype=np.uint32))
        knl(q, (gsz,), None if local is None else (local,),
            np.uint64(s[0]), np.uint64(s[1]), np.uint64(s[2]), np.uint64(s[3]),
            np.uint64(p[0]), np.uint64(p[1]), np.uint64(p[2]),
            np.uint64(base), np.uint32(iters), np.uint64(mask),
            count_buf, found_buf)
        cl.enqueue_copy(q, count, count_buf)
        n = int(count[0])
        if n:
            cl.enqueue_copy(q, found, found_buf)
        q.finish()
        return n

    launch(1 << 63, unit, 1, mask)  # warm-up (first-launch setup is not timed); counters far from the search range

    out = None if (args.test or args.bench) else open(args.out, "a", buffering=1, encoding="utf-8")
    base = 0
    t0 = time.time()
    last = t0
    hashes = 0
    proofs = 0
    checked = 0
    counted = 0
    while True:
        per_launch = gsz * iters
        tl = time.time()
        n = launch(base, gsz, iters, mask)
        dt = time.time() - tl
        counted += n
        if n > 1024 and not args.test:  # --test expects overflow: its mask is easy
            print(f"WARNING: {n} hits in one launch, only 1024 kept", flush=True)
        for ctr in found[:min(n, 1024)].tolist():
            nonce = prefix + int(ctr).to_bytes(8, "big")
            h = keccak256(seed + nonce)
            low32 = int.from_bytes(h[0:4], "little")
            if (low32 & mask) != 0:
                print(f"KERNEL MISMATCH ctr={ctr} hash=0x{h.hex()}", flush=True)
                sys.exit(2)
            checked += 1
            if out is not None:
                assert int.from_bytes(h, "big") >> 226 == 0
                out.write(f"0x{nonce.hex()},0x{h.hex()}\n")
                out.flush()
                os.fsync(out.fileno())
                proofs += 1
        base += per_launch
        hashes += per_launch
        if auto:  # aim each launch at the target time: more work when short, less when long
            if dt > 1.5 * target and iters == 1 and gsz > unit:
                gsz = max(unit, gsz // 2 // unit * unit)
            elif dt > 1.25 * target or dt < 0.8 * target:
                iters = max(1, min(4096, int(iters * target / max(dt, 1e-4))))
        now = time.time()
        if args.test and checked >= 2000:
            expected = hashes / 4096
            rate = hashes / (now - t0) / 1e6
            if abs(counted - expected) > 0.1 * expected:  # every hit is valid, but too few means missed nonces
                print(f"TEST FAILED: the GPU counted {counted} hits, expected ~{expected:.0f}", flush=True)
                sys.exit(2)
            print(f"TEST OK: {checked} GPU hits re-verified on CPU (mask 0x{mask:x}); the GPU counted {counted}, "
                  f"expected ~{expected:.0f}; {rate:.1f} MH/s, last launch {dt * 1000:.0f} ms", flush=True)
            return
        if args.bench and now - t0 >= args.bench:
            print(f"BENCH {hashes / (now - t0) / 1e6:.1f} MH/s over {now - t0:.1f}s "
                  f"(global={gsz} local={local or 'driver'} iters={iters}, last launch {dt * 1000:.0f} ms)", flush=True)
            return
        if now - last >= 30:
            print(f"{time.strftime('%H:%M:%S')} rate={hashes / (now - t0) / 1e6:.1f}MH/s "
                  f"proofs={proofs} hashes={hashes} launch={dt * 1000:.0f}ms", flush=True)
            last = now
        if os.path.exists(stop_file) or (args.max_proofs and proofs >= args.max_proofs):
            print(f"stopping: proofs={proofs}", flush=True)
            return


if __name__ == "__main__":
    main()
