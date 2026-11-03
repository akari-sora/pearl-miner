# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Akari Sora
"""Build submit/proofs.json from the GPU miner's proofs.csv, re-verifying every proof.

  python3 export_proofs.py <proofs.csv> [--wallet 0x...] [--out proofs.json]

The wallet (--wallet or env PEARL_WALLET) gives the seed: keccak256(abi.encode(publicChallenge, wallet)).
It must be 0x + 40 hex digits (a mixed-case address needs a valid EIP-55 checksum).
Uses pycryptodome when it is installed and the pure-Python Keccak from pearl_submit.py otherwise (slower,
about 30 s per 100k proofs on a laptop), so the standard library is enough.
"""
import argparse
import json
import os

from pearl_submit import keccak256 as _keccak_py, safe_stdio, wallet_problem  # same folder

try:
    from Crypto.Hash import keccak as _keccak_c
except ImportError:
    _keccak_c = None

PUBLIC_CHALLENGE = bytes.fromhex("30a0e07e58b98de26bb9881b31249b6aa4fc9a479a715fad16dfd3474eb39bdf")


def k256(b):
    return _keccak_c.new(digest_bits=256, data=b).digest() if _keccak_c else _keccak_py(b)


def main():
    safe_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--wallet", default=os.environ.get("PEARL_WALLET"),
                    help="wallet the proofs were mined for (default: env PEARL_WALLET)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "proofs.json"))
    a = ap.parse_args()
    why = wallet_problem(a.wallet)
    if why:
        raise SystemExit(f"set the wallet address with --wallet or PEARL_WALLET ({why}; got {a.wallet!r})")
    WALLET = a.wallet
    SEED = k256(PUBLIC_CHALLENGE + bytes(12) + bytes.fromhex(WALLET.lower()[2:]))  # abi.encode(bytes32, address)
    seen, out, bad = set(), [], 0
    with open(a.csv, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or "," not in line:
                continue
            n_hex, p_hex = line.split(",")
            nonce = bytes.fromhex(n_hex[2:])
            h = k256(SEED + nonce)
            if len(nonce) != 32 or "0x" + h.hex() != p_hex or int.from_bytes(h, "big") >> 226:
                bad += 1
                continue
            if p_hex in seen:
                continue
            seen.add(p_hex)
            out.append([n_hex, p_hex])
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"wallet": WALLET, "seed": "0x" + SEED.hex(), "count": len(out), "proofs": out}, f)
    os.replace(tmp, a.out)
    print(f"{len(out)} verified unique proofs -> {a.out}; rejected {bad}")


if __name__ == "__main__":
    main()
