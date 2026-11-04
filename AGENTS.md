# AGENTS.md

Rules for AI agents (Codex, Cursor, Gemini CLI, OpenCode, Claude Code and others) working in this repository.
For people: [README.md](README.md) and [docs/QUICKSTART.md](docs/QUICKSTART.md).

## What this repository does

pearl-miner mines PearlMiningV2 (珍珠币) proofs on a GPU. It then claims them on BNB Smart Chain as soon as the daily
window opens (11:08:54 UTC), through the user's Binance Agentic Wallet and its `baw` CLI. Afterwards it can sell
part of the coins it mined. Details are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/CONTRACT.md](docs/CONTRACT.md) and [docs/PLATFORMS.md](docs/PLATFORMS.md).

## The entry point is `pearl.py`

| Command | What it does |
|---|---|
| `python3 pearl.py auto --plan` | Read-only `check`, then prints the plan. Sends nothing. |
| `python3 pearl.py auto [options]` | `check`, plan, waits for `yes`, submits, then sells part of what this run mined. |
| `python3 pearl.py status` | Whether a run is going, and the end of `run.log` and `sell.log`. |

Before that: mining (`miner/miner.py`), the export (`submit/export_proofs.py`) and the free fork rehearsal
(`submit/fork_rehearsal/run_fork.py --autopilot`). Do not call `pearl_submit.py run` or `pearl_sell.py run`
yourself unless the user asks: `pearl.py` sets up `run.pid`, the logs and the amount to keep for them.
`pearl_submit.py rehearse` sends real (zero-value) transactions: only on the user's request.

## Safety rules

1. **Keys.** Never ask for, read, print or store a private key, seed phrase or password, or the files `baw` keeps
   its session in. None is needed: the wallet is an MPC Agentic Wallet that the user signed in to `baw` themselves.
2. **Wallet access.** Use the wallet only through `baw`, as these scripts do. Use no other signer and no other
   way to send transactions.
3. **Check and rehearse first.** Before a real run, `python3 pearl.py auto --plan` must show `CHECK OK` and the
   plan, and `python3 submit/fork_rehearsal/run_fork.py --autopilot` must end with `FORK REHEARSAL: PASS`.
   Otherwise stop and show the user the output.
4. **The user decides.** Show the user the plan: day and opening time, gas budget, keep reserve, sell mode, sell
   fraction and any check `WARNING`. Run `pearl.py auto` only after an explicit "yes", with the same options and
   always the plan's `--day N`, and answer its question with that yes (for example
   `echo yes | nohup python3 pearl.py auto --day N ...`). pearl.py refuses a piped yes without `--day`. After
   exit 3, ask again before any rerun; if that day's window is over, do not rerun.
5. **`--yes`** only when the user wrote the strategy themselves and told you to use it. `--allow-used-elsewhere`
   (needed with `--yes` or a piped yes after a check `WARNING`) only when the user decides so.
6. **Stopping.** Create `submit/STOP` (the miner: `miner/STOP`). Do not kill processes to stop a run. Do not
   delete `journal.jsonl`, `run.pid` or a `STOP` file unless the user asks.
7. **Settings.** Do not change defaults or limits (gas budget, keep reserve, sell fraction, minimum reward) on
   your own.
8. **Privacy.** Never commit `proofs.json`, `proofs.csv`, journals, logs or `run.pid` (all are in `.gitignore`).
   Share logs only through `--anonymize` (`run_fork.py`, `tools/replay_log.py`).

## Do not change

- **Journal logic**: `Journal`, `blocked_from_journal`, the fsynced `attempt` record written before every
  `execute`, `SenderLock`, and the replay on restart. A proof in an attempt stays blocked until a receipt proves
  that its transaction reverted.
- **Gate logic**:
  - in `pearl_submit.py`: `judge_preview` (risk items, `requireConfirmation`, simulation, `UNSAFE_ARG`), the
    `launch_lock` and gate check before each `execute`, `Runner.stop` / `stopped`, the opening-grace rules
    (`after_sim_failure`, `confirm_backend_lag`), the budget and keep-reserve stops, and `--executors 1`;
  - in `pearl_sell.py`: the refusals on `run.pid`, on pending orders and on a wallet other than the signed-in one;
    `--max-sell-coins`; signals that only set a flag, so an order in flight is followed to its end;
  - in `pearl.py`: the `CHECK OK` gate, the `yes` confirmation, the `--day` / `--allow-used-elsewhere` rules, the
    `STOP`, `run.pid` and journal-lock checks right before the start, "no `SUMMARY` line, no sale", the list of
    normal stop reasons after which a sale may follow (`CLEAN_STOP_RES`), and the sale cap (`sale_plan`: at most the
    fraction of what the run's proofs can have earned).

If one of these seems wrong, stop and explain it to the user instead of changing it.

## Tests

Six offline suites: no network, no wallet, no GPU. Each must end with `FAILED: none`; run all six after any
change.

```sh
python3 submit/test_submit.py          # the submitter against a fake chain and a fake baw
python3 submit/test_sell.py            # the seller against a pool model
python3 tools/test_replay_log.py       # log redaction
python3 tools/test_pearl_autopilot.py  # pearl.py with fake child scripts
python3 tools/test_pearl_e2e.py        # pearl.py with the real submitter and seller, real STOP and Ctrl-C (~1 min)
python3 miner/test_autotune.py         # the miner's launch sizing and its missing-package message; no GPU needed
```

End to end: `python3 submit/fork_rehearsal/run_fork.py --autopilot`. It needs Foundry's `anvil` and a public BSC
RPC to fork from, but no wallet and no key. The fake `baw` only talks to `127.0.0.1`.

## Code conventions

- The submitter and the seller use the Python standard library only.
- Everything runs on macOS, Linux and Windows. Start child processes with `sys.executable` and a list of
  arguments, never through a shell. Find `baw` with `pearl_submit.find_baw()`. Check whether a process is alive
  with `pearl_sell.mining_run_alive`; never with `os.kill(pid, 0)`, which on Windows sends Ctrl-C (signal 0 is
  `CTRL_C_EVENT` there) instead of checking.
- Keep diffs small and run the tests.
