"""GPU proof search for PearlMiningV2 (BSC 0x7E2DA878217EB995F9e5d36eD32c9Cea52442681).

proof = keccak256(seed || nonce) with nonce as 32-byte big-endian; valid at stage 0 when the
top 30 bits of the proof are zero. Every GPU hit is re-hashed on the CPU before it is written.

Output: append-only CSV lines "nonce_hex,proof_hex" (both 0x-prefixed, 64 hex digits).

The seed is puzzleSeed(wallet) = keccak256(abi.encode(publicChallenge, wallet)): give --wallet (or env
PEARL_WALLET) and it is derived here, or pass --seed directly. The wallet must be 0x + 40 hex digits (a
mixed-case address needs a valid EIP-55 checksum); given both, --seed must equal the wallet's seed.

Usage:
  python3 miner.py --wallet 0x... --out proofs.csv         # mine until stopped
  python3 miner.py --wallet 0x... --test                   # kernel self-test, easy mask
  python3 miner.py --wallet 0x... --bench 20               # hash rate for N seconds
Stop a running miner by creating the file named by --stop-file (default STOP next to --out).
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


def pick_device():
    for p in cl.get_platforms():
        for d in p.get_devices():
            if d.type & cl.device_type.GPU and "NVIDIA" in p.name:
                return d
    for p in cl.get_platforms():
        for d in p.get_devices(device_type=cl.device_type.GPU):
            return d
    raise SystemExit("no OpenCL GPU found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", help="puzzleSeed(wallet) as 0x + 64 hex digits (or give --wallet)")
    ap.add_argument("--wallet", default=os.environ.get("PEARL_WALLET"),
                    help="derive the seed from this wallet (default: env PEARL_WALLET)")
    ap.add_argument("--out", default=os.path.join(HERE, "proofs.csv"))
    ap.add_argument("--stop-file", default=None)
    ap.add_argument("--global-size", type=int, default=1 << 22)
    ap.add_argument("--iters", type=int, default=64)
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--bench", type=float, default=0.0)
    ap.add_argument("--max-proofs", type=int, default=0)
    args = ap.parse_args()

    seed = resolve_seed(args.seed, args.wallet or None)
    prefix = os.urandom(24)
    s = lanes(seed)
    p = lanes(prefix)

    dev = pick_device()
    ctx = cl.Context([dev])
    q = cl.CommandQueue(ctx)
    src = open(os.path.join(HERE, "keccak_miner.cl")).read()
    prg = cl.Program(ctx, src).build()
    knl = prg.search
    mf = cl.mem_flags
    count = np.zeros(1, dtype=np.uint32)
    found = np.zeros(1024, dtype=np.uint64)
    count_buf = cl.Buffer(ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=count)
    found_buf = cl.Buffer(ctx, mf.READ_WRITE, found.nbytes)

    mask = 0xFFF if args.test else STAGE0_MASK
    gsz, iters = args.global_size, args.iters
    per_launch = gsz * iters
    stop_file = args.stop_file or os.path.join(os.path.dirname(os.path.abspath(args.out)), "STOP")
    print(f"seed=0x{seed.hex()} device={dev.name} prefix=0x{prefix.hex()} mask=0x{mask:x} per_launch={per_launch}", flush=True)

    out = None if (args.test or args.bench) else open(args.out, "a", buffering=1)
    base = 0
    t0 = time.time()
    last = t0
    hashes = 0
    proofs = 0
    checked = 0
    while True:
        cl.enqueue_copy(q, count_buf, np.zeros(1, dtype=np.uint32))
        knl(q, (gsz,), None,
            np.uint64(s[0]), np.uint64(s[1]), np.uint64(s[2]), np.uint64(s[3]),
            np.uint64(p[0]), np.uint64(p[1]), np.uint64(p[2]),
            np.uint64(base), np.uint32(iters), np.uint64(mask),
            count_buf, found_buf)
        cl.enqueue_copy(q, count, count_buf)
        n = int(count[0])
        if n:
            cl.enqueue_copy(q, found, found_buf)
        q.finish()
        if n > 1024:
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
        now = time.time()
        if args.test and checked >= 2000:
            print(f"TEST OK: {checked} GPU hits re-verified on CPU (mask 0x{mask:x}), "
                  f"expected ~{hashes / 4096:.0f}", flush=True)
            return
        if args.bench and now - t0 >= args.bench:
            print(f"BENCH {hashes / (now - t0) / 1e6:.1f} MH/s over {now - t0:.1f}s", flush=True)
            return
        if now - last >= 30:
            print(f"{time.strftime('%H:%M:%S')} rate={hashes / (now - t0) / 1e6:.1f}MH/s "
                  f"proofs={proofs} hashes={hashes}", flush=True)
            last = now
        if os.path.exists(stop_file) or (args.max_proofs and proofs >= args.max_proofs):
            print(f"stopping: proofs={proofs}", flush=True)
            return


if __name__ == "__main__":
    main()
