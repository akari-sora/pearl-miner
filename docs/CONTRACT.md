# PearlMiningV2: contract facts

Everything below was read from the verified source (Sourcify) and from BSC mainnet on 2026-11-03.
Re-check anything that matters to you with `python3 submit/pearl_submit.py check` or a block explorer.

## Identity

| | |
|---|---|
| Chain | BNB Smart Chain (chain id 56) |
| Contract | [`0x7E2DA878217EB995F9e5d36eD32c9Cea52442681`](https://bscscan.com/address/0x7E2DA878217EB995F9e5d36eD32c9Cea52442681) |
| Token name / symbol | 珍珠币 / 珍珠币 |
| Decimals | 8 |
| Max supply | 1,000,000,000 (100,000,000 minted to the dev wallet at deploy for liquidity, 900,000,000 by mining) |
| Agent ID registry | `0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F` (pinned by address and code hash) |
| `owner()` | `0x000000000000000000000000000000000000dEaD` (burned; `renounceOwnership` is disabled) |
| `paused()` | `false` |

Because the owner is the burn address, nobody can pause or resume mining any more. The submitter relies on this
(see [ARCHITECTURE.md](ARCHITECTURE.md#the-opening-grace)), and `check` verifies it before you start.

## The daily window

```text
miningDay()  = (block.timestamp - startsAt) / 86400          // consecutive windows, not rolling
startsAt     = 1793617734                                     // 2026-11-02 11:08:54 UTC
day N opens  = startsAt + N * 86400                           // every day at 11:08:54 UTC (19:08:54 UTC+8)
daily cap    = MAX_SUPPLY / 50 = 20,000,000 coins
```

The cap counts coins (not proofs). `dailyRemaining()` is `20M - mintedInBudgetDay` for the current day; the counter
resets on the first successful claim of a new day. A claim whose reward does not fit in what is left reverts the
whole transaction with `DailyLimitReached`.

## Proofs

```text
publicChallenge = 0x30a0e07e58b98de26bb9881b31249b6aa4fc9a479a715fad16dfd3474eb39bdf
seed(wallet)    = keccak256(abi.encode(publicChallenge, wallet))     // 32 + 32 bytes; fixed per wallet
proof           = keccak256(abi.encode(seed, nonce))                  // nonce as a 32-byte big-endian word
valid           = uint256(proof) <= targetForStage(stage)
```

- A proof is bound to the wallet that mined it (through the seed), not to a day: proofs never expire, and you can
  mine them days ahead.
- Each proof can be used once (`usedProofs[proof]`).
- Stage 0 target is `2^256-1 >> 30`: the top 30 bits of the proof must be zero, about 1.07 billion hashes per proof
  on average.

### Difficulty stages

```text
cumulativeMining(claims) = 900M * claims / (claims + 9,000,000)
stage                    = min(9, cumulativeMining(claims) / 90M)
targetForStage(stage)    = (2^256-1 >> 30) * 2^stage / 3^stage
```

Stage 1 starts after 90M coins have been mined (about halfway through day 4 if every day fills). From then on the
target is two thirds of the stage-0 target, so about one third of proofs mined for stage 0 become invalid and a
batch that contains one reverts with `InvalidSolution`. The submitter stops on that error; filter your proofs
against the current target before running on a later stage.

### Reward per proof

```text
reward(claim index n) = cumulativeMining(n + 1) - cumulativeMining(n)
                      ≈ 100 * (1 - C / 900M)^2        where C = coins mined so far
```

| Day | Reward at the opening | Reward when the cap fills |
|---|---|---|
| 0 | 100.00 | 95.60 |
| 1 | 95.60 | 91.31 |
| 2 | 91.31 | 87.11 |
| 3 | 87.11 | 83.01 |
| 4 | 83.01 | 79.01 |

(Assuming every earlier day filled its cap.) `mineBatch` takes a `minTotalReward` and reverts with `RewardChanged`
if the batch pays less. The submitter sends floor × batch size, where the floor is computed for the target day: the
reward of the claim that fills that day's cap, minus 1%.

```python
cap_end = 20M * (day + 1)                                   # coins mined when the day's cap is full
n = 9,000,000 * cap_end // (900M - cap_end) + 1             # that claim's index
floor = (cumulativeMining(n + 1) - cumulativeMining(n)) * 99 // 100
```

This matches the contract's `rewardForClaim` on mainnet (`rewardForClaim(418605) = 91.30862551`,
`rewardForClaim(642858) = 87.11108660`) and gives 90.40 on day 1, 86.24 on day 2 and 82.18 on day 3. A fixed floor
does not work: 90 coins, fine on day 1, makes every batch revert on day 2 once about 46.2M coins have been mined.
`--min-reward` (at least 1 coin) overrides the computed floor. From day 44 on, `cap_end` reaches the whole 900M
mining supply, so the formula gives no bound: the tools use a floor of 0 there and tell you to pass `--min-reward`.
A negative day is refused.

## Submitting

```solidity
function mineBatch(uint256 id, uint256[] calldata nonces, uint256 minTotalReward) external whenNotPaused
```

- `id` (the agentId) is `uint256(keccak256(bytes(handle)))` of the **raw** Agent ID handle: the name without the
  display `.ai` suffix. The contract checks `registry.ownerOf(id) == msg.sender` and
  `keccak256(registry.handleOf(msg.sender)) == id`, otherwise `InvalidIdentity`.
- The registry only registers handles that pass its `isValidHandle`: bytes `a-z` and `0-9`, length between
  `minLen()` and `maxLen()` (3 and 20 on 2026-11-03; the registry owner can change them). The tools apply the same
  rule to `--handle` and refuse anything else instead of correcting it. `check` calls the registry's `ownerOf(id)`
  and compares it with your wallet.
- At most `MAX_BATCH = 16` nonces per call (`InvalidBatchSize` otherwise).
- All or nothing: if any nonce fails (used, invalid, or the cap is reached mid-batch), the whole transaction
  reverts and none of its proofs is consumed.
- Order of the checks per proof: used → target → mining finished → daily cap.

### Gas

| | |
|---|---|
| Gas per proof | ≈ 35.7k |
| 16-proof `mineBatch` | ≈ 570k gas |
| Fee at 0.08–0.10 gwei | ≈ 0.000045–0.000055 BNB per 16-proof transaction |
| A revert at the first proof (cap already full) | ≈ 0.000006 BNB |

The Binance Agentic Wallet chooses the gas price for `contract-call` itself; you cannot outbid other senders.

### Selectors used by the tools

| Function / error | Selector |
|---|---|
| `mineBatch(uint256,uint256[],uint256)` | `0xb124a1c7` |
| `miningDay()` | `0x549c22a1` |
| `dailyRemaining()` | `0xa7003616` |
| `currentReward()` | `0x07621eca` |
| `usedProofs(bytes32)` | `0xc30a0f25` |
| `owner()` / `paused()` | `0x8da5cb5b` / `0x5c975abb` |
| `DailyLimitReached()` | `0xf402e5b1` |
| `ProofAlreadyUsed()` | `0xc9838a65` |
| `InvalidSolution()` | `0xd9c1988f` |
| `InvalidIdentity()` | `0x178423dd` |
| `RewardChanged()` | `0x577742ca` |
| `InvalidBatchSize()` | `0x7862e959` |
| `Mined(address,uint256,bytes32,uint256,uint256,uint256)` topic | `0xbf3fc84d…858b` |

## The wallet

The tools are built around the **Binance Agentic Wallet**: an MPC wallet without a private key that you drive
through its CLI, `baw`. Two properties shape the whole design:

1. Every contract call goes through `baw contract-call preview` (a simulation plus a risk screen that returns a
   `requestId`) and then `baw contract-call execute --requestId …`.
2. The backend accepts **one in-flight transaction per address**. A second `execute` while the previous one is in
   progress is rejected with error `351716` and is not sent.

Official documentation and skills: <https://github.com/binance/binance-skills-hub>.

## Market

On 2026-11-03 the only market for the token was a PancakeSwap Infinity (v4) concentrated-liquidity pool against
BNB with a 0.3726% fee. That evening its active liquidity was about `L = 7.06e17` at roughly 431k coins
per BNB, so a 100k-coin sale moved the price by about 0.2%. One day later it was a different market: on
2026-11-04 the pool was about 0.01 BNB deep, a 20,000-coin sale took about 37% price impact and 23,958 coins took
42%, and the price fell 88% within 268 s of the opening as the day's miners sold. Liquidity and price can change at
any moment. `pearl_sell.py` quotes before every chunk, halves chunks above a price-impact cap (50% per chunk since v0.1.4;
2% before, which sold nothing on 2026-11-04) and never sells below half the session's first rate (`--floor`).

Unrelated projects named "Pearl" or "PRL" (for example a proof-of-useful-work chain) have nothing to do with this
contract.
