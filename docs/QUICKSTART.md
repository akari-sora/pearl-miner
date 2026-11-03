# Quick start: one command a day

English · [繁體中文](QUICKSTART.zh-TW.md)

`python3 pearl.py auto` does the whole day for you: it checks your setup, shows you the plan, waits for you to type
`yes`, waits for the opening, mines, and then sells part of what it mined. This page is for people who do not write
code. On Windows, type `python` (or `py`) wherever this page says `python3`; setup and other differences per
system are in [PLATFORMS.md](PLATFORMS.md).

> **Read this first: the risks**
>
> - **Profit is small and not guaranteed.** Competition is intense: on day 1 the whole daily cap was gone in about
>   a minute, and this tool's own wallet mined 192 proofs (about 95 coins each).
> - **The price can fall fast.** On day 1 the price fell about 51% in the 14 minutes after the opening. The coin
>   trades in a single pool and can fall to nothing.
> - **Automation may break the wallet's terms.** Driving a wallet with scripts may get the account limited. Typing
>   `yes` is *your* approval of the plan; it does not mean Binance approves unattended use.
> - Gas and electricity are on you. Use only money you can afford to lose. Read the
>   [disclaimer](../README.md#disclaimer).

## What you need

- A Binance **Agent ID** and its **Agentic Wallet**, with the `baw` CLI installed, signed in, and Developer Mode
  on. There is no private key or seed phrase to give anyone: the tools only talk to `baw`.
- About 0.05 BNB in that wallet. The defaults allow at most 0.045 BNB of gas and always keep 0.005 BNB; day 1
  used about 0.0006 BNB of gas.
- Python 3.9 or newer.
- For mining: a GPU with OpenCL, and `pip install -r miner/requirements.txt`.
- For the free rehearsal: `anvil` from [Foundry](https://getfoundry.sh).

Get the code and tell it your wallet address and your Agent ID handle (lower-case letters and digits, without
`.ai`). Neither is a secret.

macOS / Linux:

```sh
git clone https://gitlab.com/aa22396584/pearl-miner.git && cd pearl-miner
export PEARL_WALLET=0xYourAgenticWalletAddress
export PEARL_HANDLE=yourhandle
```

Windows (PowerShell):

```powershell
git clone https://gitlab.com/aa22396584/pearl-miner.git; cd pearl-miner
$env:PEARL_WALLET = "0xYourAgenticWalletAddress"
$env:PEARL_HANDLE = "yourhandle"
```

Set the two variables again in every new terminal window.

## Three steps

### 1. Mine (any time before the opening)

```sh
pip install -r miner/requirements.txt
python3 miner/miner.py --out miner/proofs.csv
```

The miner runs until you stop it. To stop it, create a file named `STOP` in the `miner` folder:
`touch miner/STOP` (Windows: `New-Item -ItemType File miner\STOP`). Delete that file before you mine again. Proofs keep until they are
used, so you can mine over several days; but once 90M coins have been mined (around day 4) the difficulty rises and
about a third of older proofs stop qualifying (see [CONTRACT.md](CONTRACT.md#difficulty-stages)).

### 2. Rehearse for free on a local copy of the chain

```sh
python3 submit/export_proofs.py miner/proofs.csv
python3 submit/fork_rehearsal/run_fork.py --autopilot
```

The first line checks every proof again and writes `submit/proofs.json`. The second copies the real contract into
a local test chain on your computer, opens the mining day about a minute later, and runs step 3's check and mining
(`pearl.py auto --sell off`: the local chain has no market to sell on, so the sale is covered only by the offline
tests). The wallet in this rehearsal is a fake that can only talk to your own computer, so no transaction reaches
the real chain and no BNB is spent. It must end with `FORK REHEARSAL: PASS`. Add `--anonymize` before you share the
output.

### 3. Run it on the day

Every mining day opens at **11:08:54 UTC** (19:08:54 in Taipei, Beijing and Singapore). A few minutes before:

```sh
python3 pearl.py auto
```

It checks your setup, shows the plan, and waits for you to type `yes`. Then it waits for the opening and mines.
When the run ends normally (the day's cap is full, the gas budget is spent, the proofs are used up or the time is
over) it sells as planned; after STOP, Ctrl-C, a crash, a risk warning or any other error it sells nothing.
Leave the window open until you see the summary.

- To stop early, create `submit/STOP` (`touch submit/STOP`; Windows: `New-Item -ItemType File submit\STOP`) or press
  Ctrl-C. The run finishes what is already in flight, and nothing is sold. Delete `submit/STOP` before the next start.
- Closing the window: on macOS and Linux the run goes on in the background without the sale; on Windows it ends the
  run. To keep a run going without a window on Windows, start it as in "Run the submitter in the background" in
  [PLATFORMS.md](PLATFORMS.md).
- To see what is going on from another window: `python3 pearl.py status`.
- To see the plan without starting anything: `python3 pearl.py auto --plan`.

## The confirmation screen

Before anything is sent, `pearl.py auto` runs the read-only `check` and prints a plan like this:

```text
==== the plan ====
Wallet  0x12345678…, Agent ID keccak256('yourhandle')
Day     2: opens 2026-11-04 11:08:54 UTC = 2026-11-04 19:08:54 local, in 6 min 3 s.
        The run waits for the opening, then stops when the day's cap is full, the gas budget is spent,
        90 minutes after the opening, or when submit/STOP exists.
Proofs  9,600 usable (10,000 in submit/proofs.json, 400 already sent according to submit/journal.jsonl).
Gas     budget 0.045 BNB of fees in total for submit/journal.jsonl (every run that used it, not per day);
        earlier runs used 0.000600, so up to 0.044400 BNB for this run.
        Keep reserve 0.005 BNB: the run stops before the wallet's BNB would fall below it.
Sell    now: right after the run ends, in chunks of up to 100k coins; a chunk over 2% price impact is halved or waits.
        Sells at most 80% of the coins THIS run mines (never more than its proofs can have earned); the rest and every coin you held before the run stay in the wallet.
        Only after a normal end (cap full, budget spent, proofs used up, deadline): never after STOP, Ctrl-C,
        a crash, a risk warning or an error.
```

| Line | What it means |
|---|---|
| Day | Which day the run waits for, and when it opens in UTC and in your local time. |
| Proofs | How many of your proofs can still be used. Proofs sent on an earlier day are skipped. |
| Gas | The most gas the run may spend. The budget is a total for every run that uses the same journal, so the plan shows what is left for today (see below). |
| Keep reserve | BNB that always stays in the wallet. |
| Sell | What happens after mining, and only after a normal end. Only coins mined by this run are sold, at most the fraction shown, and never more than the proofs mined can have earned, so coins that arrive from elsewhere meanwhile are not sold. |

Typing `yes` approves this plan in advance: the mining transactions and the sales then go out through `baw`
without asking you again. This is your advance approval of this plan, not the Binance App's confirmation of each
transaction; whether Binance accepts it is up to Binance. It does not mean Binance approves unattended use, and
driving a wallet with scripts may break its terms. Anything other than `yes` cancels, and nothing is sent.

### Options and defaults

| Option | Default | What it does |
|---|---|---|
| `--day N` | the next opening | Wait for day N instead. Required when the answer does not come from a terminal (an agent piping `yes`), so the yes cannot apply to a later day. |
| `--budget-bnb X` | `0.045` | Most gas in BNB for the journal, earlier runs included. |
| `--keep-bnb X` | `0.005` | BNB that always stays in the wallet. |
| `--sell now` | `now` | Sell right after the run ends. On day 1 the price fell fast after the opening. |
| `--sell trail` | | Watch the price for up to 2 hours and sell on a 7% pullback from its high, or on schedule. |
| `--sell off` | | Sell nothing. |
| `--sell-fraction F` | `0.8` | Share of the coins mined by this run to sell. Coins you held before are never sold. |
| `--minutes M` | `90` | Stop this many minutes after the opening. |
| `--plan` | | Run `check` and show the plan, then exit. Nothing is sent. |
| `--yes` | | Skip the question. Use it only for a plan you decided yourself beforehand. Refused when check printed a WARNING. |
| `--allow-used-elsewhere` | | Let `--yes` start although check warned about proofs already used on chain but not in the journal (a lost journal, or another machine sending). |

### The gas budget adds up over the days

`--budget-bnb` is a running total for `submit/journal.jsonl`, not a new allowance each day: every run that uses
the journal counts against it. Day 1 used about 0.0006 BNB, so with the default 0.045 there is 0.0444 left on day 2,
and the plan shows that number. To give a later day the full amount, raise `--budget-bnb` by what earlier runs
spent, for example `--budget-bnb 0.0456` on day 2.

Keep the same journal for the same proofs. A new journal starts the count at zero, but it does not know which proofs
were already sent: they are tried again, and each batch that holds one wastes a moment at the opening before it is
dropped. Use a new journal only together with newly mined proofs that were never sent.

### When it ends

The summary shows the proofs mined, the gas spent, the coins sold and the BNB received, and where the logs are
(`submit/run.log`, `submit/journal.jsonl`, `submit/sell.log`, `submit/sell.jsonl`). The exit code says how it went:

| Exit code | Meaning | What to do |
|---|---|---|
| 0 | Done. | Nothing. |
| 1 | Refused before anything was sent. | Read the one-line reason, fix it, start again. |
| 3 | The run did not end cleanly: no summary, or an error after it. | `python3 pearl.py status`. If that day's window is still open, start again with `--day N` (the day shown in the plan): the journal keeps any proof that may have been sent from being sent twice. |
| 4 | The run ended, but the sale was skipped or stopped (STOP, Ctrl-C, a run that did not end normally, a balance that could not be read). | The message ends with the command to sell later, `--keep-coins` and `--max-sell-coins` already filled in. Run it when you are ready. |

## Check the code yourself (optional)

Six offline test suites need no network, no wallet and no GPU. Each must end with `FAILED: none`:

```sh
python3 submit/test_submit.py          # the submitter against a fake chain and a fake baw
python3 submit/test_sell.py            # the seller against a pool model
python3 tools/test_replay_log.py       # log redaction
python3 tools/test_pearl_autopilot.py  # pearl.py with fake child scripts
python3 tools/test_pearl_e2e.py        # pearl.py with the real submitter and seller, real STOP and Ctrl-C (~1 min)
python3 miner/test_autotune.py         # the miner's launch sizing and its missing-package message; no GPU needed
```

## Hand it to your AI agent

You can give this job to a coding agent such as Claude Code, Codex or Cursor. Copy the prompt below into your
agent, in your own words if you like. The agent never needs a private key. To install the pearl-miner skill or
set up a particular agent, see [AGENTS-SETUP.md](AGENTS-SETUP.md).

```text
Help me mine 珍珠币 (PearlMiningV2) with pearl-miner: https://gitlab.com/aa22396584/pearl-miner
1. Clone the repository, read docs/QUICKSTART.md and the disclaimer in README.md, and follow them.
2. Never ask for, read or store a private key, seed phrase or password. The wallet is my Binance Agentic Wallet,
   used only through the `baw` CLI I signed in myself. If anything seems to need a key, stop and tell me.
3. Set PEARL_WALLET and PEARL_HANDLE to the address and handle I give you.
4. Run the free rehearsal `python3 submit/fork_rehearsal/run_fork.py --autopilot`. Stop and show me the output
   unless it ends with FORK REHEARSAL: PASS.
5. Run `python3 pearl.py auto --plan`: it runs the read-only check and prints the plan. Stop if check fails.
   Show me the plan: day and time, gas budget, keep reserve, sell mode, sell fraction and any WARNING.
   Wait until I answer yes.
6. After my yes, start it a few minutes before the opening so it survives your shell's time limit, with the same
   options as the plan and always the plan's --day N, for example
   `echo yes | nohup python3 pearl.py auto --day N > submit/auto.log 2>&1 &`.
   Never use --yes unless I wrote the strategy myself and told you to use it.
7. To stop a run, create submit/STOP; never kill processes or delete journal.jsonl or run.pid. Do not change the
   code, the defaults or the safety limits unless I ask.
8. When it ends, report the summary: proofs mined, gas spent, coins sold, BNB received, the stop reason, and the
   exit code. Use `python3 pearl.py status` to check on it. If it ends with exit 3, ask me again before any rerun,
   with the same --day N; if that day's window is over, do not rerun.
```

中文版提示詞在 [QUICKSTART.zh-TW.md](QUICKSTART.zh-TW.md#交給你的-ai-agent)。
