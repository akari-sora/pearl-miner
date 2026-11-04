---
name: pearl-miner
description: "Mine and claim 珍珠币 (PearlMiningV2) on BNB Smart Chain with the open-source pearl-miner, through the user's Binance Agentic Wallet (baw). Use when the user mentions 珍珠币, 珍珠幣, PearlMiningV2, pearl-miner, or racing for the daily mining cap (搶挖礦額度). Works together with the official binance-agentic-wallet skill. Never needs a private key."
---

# pearl-miner

pearl-miner mines PearlMiningV2 (珍珠币) proofs on a GPU. It then claims them on BNB Smart Chain as soon as the
daily window opens (every day at 11:08:54 UTC, 19:08:54 in Taipei), through the user's Binance Agentic Wallet and
its `baw` CLI. One command, `pearl.py auto`, runs the day: check, plan, the user's yes, submit, then sell part of
what was mined.

This file works on its own: everything needed to start is below. After cloning, the repository's `AGENTS.md` and
`docs/QUICKSTART.md` have the details. The safety rules here always apply.

PINNED: tag=v0.1.2 commit=75bbedae1b12113cd3cca5ae2b3b0885cee5418c

## Prerequisites

- The official **binance-agentic-wallet** skill (`baw` CLI, npm package `@binance/agentic-wallet`), from
  https://github.com/binance/binance-skills-hub (`skills/binance-web3/binance-agentic-wallet`). Use it for
  anything about the wallet itself: signing in, Developer Mode, balances.
- A Binance **Agent ID** (the `.ai` name) owned by the wallet. You need its raw handle: lower-case letters and
  digits, without `.ai`.
- `baw` signed in, and `baw wallet status --json` shows `"status": "CONNECTED"`. Otherwise the user re-pairs in
  the Binance App.
- **Developer Mode** on: `baw wallet settings --json` shows `devMode.enabled` true. Otherwise the user turns it on
  in the Binance App (Agentic Wallet, Settings, Developer Mode). This tool's transactions are contract calls.
- About 0.05 BNB in the wallet for gas, Python 3.9+ and git. For mining, a GPU with OpenCL. For the free rehearsal,
  Foundry's `anvil` (https://getfoundry.sh).
- The commands run on the computer where the user's `baw` is signed in.

## Safety rules (never break these)

1. Never ask for, read, print or store a private key, seed phrase or password, or the files `baw` keeps its
   session in. None is needed. If a step seems to need one, stop and tell the user.
2. Touch the wallet only through `baw`, by way of these scripts.
3. Before a real run, `check` must pass and the fork rehearsal must end with `FORK REHEARSAL: PASS`.
4. Show the user the plan and wait for an explicit "yes" before `pearl.py auto`.
5. Use `--yes` only when the user wrote the strategy themselves and told you to use it.
6. To stop, create the `STOP` file. Do not kill processes. Do not delete `journal.jsonl` or `run.pid`.
7. Do not change the code, the defaults or the limits unless the user asks.
8. Do not publish logs, proofs or journals. To share output, use `--anonymize`.

## Steps

### 0. Get the pinned code

```sh
git clone --branch <tag> https://gitlab.com/aa22396584/pearl-miner.git
cd pearl-miner
git rev-parse HEAD
```

Use the `tag` and `commit` from the PINNED line above. The output of `git rev-parse HEAD` must equal `commit`
exactly. If the PINNED line still says TBD, or the hash differs, stop and tell the user. Run nothing from that
copy unless the user explicitly accepts an unpinned copy; in that case, tell them the commit you are on.

### 1. Prepare

Get the wallet address and the handle from the user. The binance-agentic-wallet skill can look them up; confirm
both with the user. Neither is a secret. Set them in the shell that runs the commands:

```sh
export PEARL_WALLET=0x...          # Windows PowerShell: $env:PEARL_WALLET = "0x..."
export PEARL_HANDLE=yourhandle     # Windows PowerShell: $env:PEARL_HANDLE = "yourhandle"
```

On Windows use `python` where this file says `python3`; see `docs/PLATFORMS.md`.

### 2. Mine (any time before the opening)

```sh
pip install -r miner/requirements.txt
python3 miner/miner.py --test
python3 miner/miner.py --out miner/proofs.csv     # long-running: start it in the background
```

To stop the miner, create `miner/STOP` (`touch miner/STOP`; Windows: `New-Item -ItemType File miner\STOP`). Then
run `python3 submit/export_proofs.py miner/proofs.csv`, which checks every proof again and writes
`submit/proofs.json`.

### 3. Rehearse and check

```sh
python3 submit/fork_rehearsal/run_fork.py --autopilot   # free; must end with FORK REHEARSAL: PASS
python3 pearl.py auto --plan                            # read-only check + the plan; sends nothing
```

If either one fails, stop, show the user the output and use the troubleshooting table below.

### 4. Show the plan and wait

Show the user the plan from step 3:
- the day, with its opening time in UTC and local time;
- the number of usable proofs;
- the gas budget and the keep reserve. The budget is a running total for `submit/journal.jsonl`, not per day: to
  give today X BNB, pass `--budget-bnb` = X + what earlier runs spent (the plan shows what is left);
- the sell mode (`now`, `trail` or `off`) and the sell fraction (by default at most 80% of what this run's proofs
  can have earned, read as an upper bound before the start; coins held before are sold at most up to that fraction of
  the gap between the bound and the real reward);
- any `WARNING` line from check. With one, `--yes` and a piped yes are refused unless `--allow-used-elsewhere` is
  added; add that only if the user decides to.

Ask whether to change any of it (`--sell`, `--sell-fraction`, `--budget-bnb`, `--keep-bnb`, `--day`). Wait for
an explicit yes. Silence is not a yes, and neither is "sounds good, but...".

### 5. Run

Start it a few minutes before 11:08:54 UTC, with the same options the user approved and always with the plan's
`--day N` (pearl.py refuses a piped yes without `--day`, so the yes can never apply to a later day). Run it so that
your shell's time limit cannot cut it off:

```sh
echo yes | nohup python3 pearl.py auto --day N [the approved options] > submit/auto.log 2>&1 &
python3 pearl.py status            # any time: is it running, and the end of the logs
```

On Windows, run `"yes" | python pearl.py auto --day N [the approved options]` in a PowerShell window that stays
open: closing that window ends the run. Check on it with `python pearl.py status`.

The run waits for the opening. It stops when the day's cap is full, the gas budget is spent, the proofs are used up,
90 minutes after the opening, or when `submit/STOP` appears. Only after a normal end (cap full, budget spent, proofs
used up, deadline) does it sell as planned, and never more than this run's proofs can have earned. After STOP,
Ctrl-C, a crash, a risk warning or any other abnormal stop it sells nothing and exits 4.

### 6. Report

Tell the user what the summary at the end of `submit/auto.log` shows:
- the proofs mined and the gas spent;
- the coins sold and the BNB received;
- the stop reason and the exit code.

Logs: `submit/run.log`, `submit/journal.jsonl`, `submit/sell.log`, `submit/sell.jsonl`.

## Stop

- **The run**: create `submit/STOP` (`touch submit/STOP`; Windows: `New-Item -ItemType File submit\STOP`). It
  finishes the transactions already in flight, and nothing is sold. Delete `submit/STOP` before the next start.
- **The miner**: create `miner/STOP`.

## Troubleshooting

| Exit code or message | Cause | What to do |
|---|---|---|
| exit 1, `PEARL_WALLET` / `PEARL_HANDLE` | Not set or malformed. | Set them (step 1). The handle has no `.ai` and no capitals. |
| `matches the wallet: False` | The handle is not this wallet's Agent ID. | Check both with the user. |
| `wallet session: ... (must be CONNECTED)` | The `baw` session ended. | The user re-pairs in the Binance App. |
| `baw wallet settings` fails, or Developer Mode is off | Developer Mode is off. | The user turns it on in the Binance App. |
| `WARNING: N sampled proofs are already used on chain` | Proofs sent from another machine or with another journal. | run skips them, at a cost at the opening; better copy that run's `journal.jsonl` into `submit/`. `--yes` or a piped yes then needs `--allow-used-elsewhere`, only if the user decides so. |
| `the answer does not come from a terminal: pass the --day` | A piped yes without `--day`. | Add the `--day N` from the plan the user approved. |
| `another run holds submit/journal.jsonl` or `a run started meanwhile` | A submitter is already running. | `python3 pearl.py status`; tell the user. Never start a second one. |
| `submit/STOP exists` | Left over from an earlier stop. | Ask the user, then delete it. |
| `a run is already going` | `run.pid` names a live process. | `python3 pearl.py status`; if no run is going, delete `submit/run.pid` with the user's ok. |
| `gas budget ... already used` | The budget is a total for the journal; earlier runs used it. | The user decides on a higher `--budget-bnb`. Keep the same journal. |
| `requireConfirmation`, or a preview risk stop | The wallet wants approval in the Binance App, or flagged a risk. | Stop and tell the user. Never work around it. |
| `InvalidSolution` (around day 4 or later) | The difficulty went up. | Filter the proofs; see `docs/CONTRACT.md`, "Difficulty stages". |
| fork: port in use, or `anvil` not found | Another node, or Foundry missing. | Pass `--port`, or install Foundry. |
| `UNSAFE_ARG` | On Windows, a value for `baw` (a `.cmd` shim) that cmd.exe could misread; nothing was sent. | Stop and tell the user. Never work around it. |
| exit 3 | The run did not end cleanly: no summary, or an error after it. | `python3 pearl.py status`. Before any rerun, get the user's yes again and pass the same `--day N`; if that day's window is over, do not rerun. The journal never sends a proof twice. |
| exit 4 | The run ended; the sale was skipped (STOP, Ctrl-C, an abnormal stop, a balance that did not read the same twice) or the seller stopped before it was done. | Tell the user. Skipped: the message ends with the command to sell later, `--keep-coins` and `--max-sell-coins` filled in (none if nothing was mined); run it only with their ok. Seller stopped before any sale (the message says nothing was sold): same as skipped. Seller stopped part way: some coins may be sold already; show the user `python3 pearl.py status` and `submit/sell.log` before any further sale. |

## Offline tests (optional)

To check the code itself, run the six offline suites in the clone (no network, no wallet, no GPU); each must end
with `FAILED: none`:

```sh
python3 submit/test_submit.py          # the submitter against a fake chain and a fake baw
python3 submit/test_sell.py            # the seller against a pool model
python3 tools/test_replay_log.py       # log redaction
python3 tools/test_pearl_autopilot.py  # pearl.py with fake child scripts
python3 tools/test_pearl_e2e.py        # pearl.py with the real submitter and seller, real STOP and Ctrl-C (~1 min)
python3 miner/test_autotune.py         # the miner's launch sizing and its missing-package message; no GPU needed
```

## Risks (tell the user before the first run)

- Profit is small and not guaranteed. On day 1 the whole daily cap was gone in about a minute.
- On day 1 the price fell about 51% in the 14 minutes after the opening. The coin trades in a single pool and
  can fall to nothing.
- Driving a wallet with scripts may break the wallet's terms and get the account limited. The user's yes is
  their approval of the plan; it does not mean Binance approves unattended use.
- Gas and electricity are on the user. Use only money they can afford to lose. Disclaimer: the README's
  "Disclaimer" section.
