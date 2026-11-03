# 快速開始：每天一個指令

[English](QUICKSTART.md) · 繁體中文

`python3 pearl.py auto` 會替你跑完一整天：檢查設定、列出計畫、等你輸入 `yes`、等開放、送單挖礦，最後賣掉這次挖到的
一部分。這頁寫給不寫程式的人。Windows 上，本頁的 `python3` 請改打 `python`（或 `py`）；各作業系統的安裝與差異見
[PLATFORMS.zh-TW.md](PLATFORMS.zh-TW.md)。

> **先看風險**
>
> - **利潤很小，也沒有保證。** 競爭非常激烈：第 1 天整天的額度大約一分鐘就被搶光，本工具自己的錢包挖到 192 個
>   proof（每個約 95 枚）。
> - **價格可能跌得很快。** 第 1 天開放後 14 分鐘內價格跌了約 51%。這個幣只在單一池子交易，可能跌到零。
> - **自動化可能違反錢包條款。** 用程式操作錢包可能讓帳號被限制。輸入 `yes` 是**你自己**核准這份計畫，不代表幣安
>   同意無人看管的使用方式。
> - gas 與電費都由你負擔。只用賠得起的錢。請先讀[免責聲明](../README.zh-TW.md#免責聲明)。

## 需要準備

- 幣安 **Agent ID** 與它的 **Agentic Wallet**，已安裝並登入 `baw` CLI、開啟 Developer Mode。沒有任何私鑰或助記詞
  要交給任何人：工具只透過 `baw` 操作錢包。
- 錢包裡約 0.05 BNB。預設 gas 上限 0.045 BNB、永遠保留 0.005 BNB；第 1 天實際只花了約 0.0006 BNB 的 gas。
- Python 3.9 以上。
- 挖礦：支援 OpenCL 的 GPU，並執行 `pip install -r miner/requirements.txt`。
- 免費預演：[Foundry](https://getfoundry.sh) 的 `anvil`。

下載程式，並設定你的錢包地址與 Agent ID 的 handle（小寫英文與數字，不含 `.ai`）。這兩個都不是秘密。

macOS / Linux：

```sh
git clone https://gitlab.com/aa22396584/pearl-miner.git && cd pearl-miner
export PEARL_WALLET=0x你的AgenticWallet地址
export PEARL_HANDLE=yourhandle
```

Windows（PowerShell）：

```powershell
git clone https://gitlab.com/aa22396584/pearl-miner.git; cd pearl-miner
$env:PEARL_WALLET = "0x你的AgenticWallet地址"
$env:PEARL_HANDLE = "yourhandle"
```

每開一個新的終端機視窗，都要重新設定這兩個變數。

## 三個步驟

### 1. 挖礦（開放前任何時間都可以）

```sh
pip install -r miner/requirements.txt
python3 miner/miner.py --out miner/proofs.csv
```

礦機會一直跑到你叫它停。要停的時候，在 `miner` 資料夾建立一個叫 `STOP` 的檔案：`touch miner/STOP`
（Windows：`New-Item -ItemType File miner\STOP`）。下次挖之前先刪掉這個檔案。proof 用掉之前不會過期，可以分好幾天挖；不過全網挖出 9000 萬枚後（約第 4 天）難度會提高，
舊 proof 約有三分之一不再合格（見 [CONTRACT.md](CONTRACT.md#difficulty-stages)）。

### 2. 在本機的鏈副本上免費預演

```sh
python3 submit/export_proofs.py miner/proofs.csv
python3 submit/fork_rehearsal/run_fork.py --autopilot
```

第一行重新驗證每個 proof 並寫出 `submit/proofs.json`。第二行把真正的合約複製到你電腦上的測試鏈，約一分鐘後開放
挖礦日，跑第 3 步的 check 和送單（`pearl.py auto --sell off`：本機的鏈上沒有可以賣的市場，所以賣出只有離線測試涵蓋）。
預演用的錢包是假的，只能連到你自己的電腦，所以沒有任何交易會送到真正的鏈上，也不花任何 BNB。最後必須出現
`FORK REHEARSAL: PASS`。要分享輸出前，加上 `--anonymize`。

### 3. 當天執行

每個挖礦日在 **UTC 11:08:54**（台北、北京、新加坡時間 19:08:54）開放。開放前幾分鐘執行：

```sh
python3 pearl.py auto
```

它會檢查設定、列出計畫、等你輸入 `yes`，然後等開放、送單。送單正常結束時（當天額度滿、gas 預算用完、proof 用完或時間到）
才照計畫賣出；遇到 STOP、Ctrl-C、程式出錯、風險警告或其他錯誤時，一律不賣。看到最後的總結之前，不要關掉視窗。

- 想提早停：建立 `submit/STOP`（`touch submit/STOP`；Windows：`New-Item -ItemType File submit\STOP`）或按 Ctrl-C。
  已送出的交易會處理完，不會賣出任何東西。下次啟動前刪掉 `submit/STOP`。
- 關掉視窗：在 macOS 和 Linux 上送單會在背景繼續，但不會賣出；在 Windows 上會結束送單。在 Windows 上想不開視窗
  繼續跑，請用 [PLATFORMS.zh-TW.md](PLATFORMS.zh-TW.md) 裡在背景執行送單程式的寫法。
- 想在另一個視窗看進度：`python3 pearl.py status`。
- 只想看計畫、不啟動任何東西：`python3 pearl.py auto --plan`。

## 確認畫面

送出任何東西之前，`pearl.py auto` 會先跑唯讀的 `check`，然後印出像這樣的計畫：

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

| 項目 | 意思 |
|---|---|
| Day | 要等哪一天開放，以及開放時間（UTC 與你的當地時間）。 |
| Proofs | 還能用的 proof 數量。之前已送過的會自動跳過。 |
| Gas | 這次最多能花多少 gas。上限是同一份 journal 所有執行的累計，所以計畫會列出今天還剩多少（見下方）。 |
| Keep reserve | 永遠留在錢包裡的 BNB。 |
| Sell | 挖完之後做什麼，而且只在正常結束時才賣。只賣這次挖到的幣，最多是列出的比例，也絕不超過這次的 proof 可能賺到的量，所以期間從別處轉入的幣不會被賣。 |

輸入 `yes` 就是事先核准這份計畫：之後的送單交易與賣出都會透過 `baw` 送出，不會再問你。這是你對本計畫的預先核准，
不是 Binance App 內逐筆的確認；Binance 是否接受由 Binance 決定。這也不代表 Binance 同意無人值守的使用方式，用程式操作
錢包可能違反它的條款。輸入 `yes` 以外的任何內容都會取消，不送出任何東西。

### 參數與預設值

| 參數 | 預設 | 作用 |
|---|---|---|
| `--day N` | 下一次開放 | 改等第 N 天。回答不是從終端機輸入時（例如 agent 用管線送 `yes`）一定要帶，這樣 yes 就不會套用到之後的某一天。 |
| `--budget-bnb X` | `0.045` | 這份 journal 最多花多少 BNB 的 gas，包含之前的執行。 |
| `--keep-bnb X` | `0.005` | 永遠留在錢包裡的 BNB。 |
| `--sell now` | `now` | 送單結束就賣。第 1 天開放後價格跌得很快。 |
| `--sell trail` | | 最多觀察價格 2 小時，從高點回落 7% 或進度落後時分批賣。 |
| `--sell off` | | 不賣。 |
| `--sell-fraction F` | `0.8` | 這次挖到的幣要賣掉的比例。之前就有的幣永遠不會賣。 |
| `--minutes M` | `90` | 開放後幾分鐘停止。 |
| `--plan` | | 跑 `check` 並列出計畫後就結束，不送出任何東西。 |
| `--yes` | | 不問就開始。只在計畫是你自己事先決定的時候使用。check 印出 WARNING 時會被拒絕。 |
| `--allow-used-elsewhere` | | check 警告有 proof 鏈上已用、但 journal 沒有紀錄時（journal 遺失，或另一台機器也在送），仍讓 `--yes` 開始。 |

### gas 上限是跨天累計的

`--budget-bnb` 是 `submit/journal.jsonl` 從頭到現在的累計上限，不是每天重新計算：所有用這份 journal 的執行都算在裡面。
第 1 天花了約 0.0006 BNB，所以預設 0.045 到了第 2 天只剩 0.0444，計畫畫面會列出這個數字。想讓之後某一天有完整的額度，
就把 `--budget-bnb` 加上之前已經花掉的量，例如第 2 天用 `--budget-bnb 0.0456`。

同一批 proof 要一直沿用同一份 journal。換新的 journal 雖然會從 0 開始算，但它不知道哪些 proof 已經送過：這些 proof
會被再試一次，每個含有它們的批次都會在開放時浪費一點時間才被剔除。只有搭配全新、從沒送過的 proof 時才換新的 journal。

### 結束時

總結會列出挖到的 proof、花掉的 gas、賣出的幣、換到的 BNB，以及紀錄檔的位置（`submit/run.log`、
`submit/journal.jsonl`、`submit/sell.log`、`submit/sell.jsonl`）。結束代碼代表結果：

| 結束代碼 | 意思 | 怎麼辦 |
|---|---|---|
| 0 | 完成。 | 不用做什麼。 |
| 1 | 送出任何東西之前就拒絕了。 | 看那一行原因，修好後重新開始。 |
| 3 | 送單沒有乾淨結束：沒有總結，或總結之後出錯。 | 先 `python3 pearl.py status`。如果那一天的時段還沒過，帶上計畫裡的 `--day N` 重新開始：journal 會擋住可能已送出的 proof，不會重複送。 |
| 4 | 送單結束了，但賣出被跳過或中途停止（STOP、Ctrl-C、送單不是正常結束、讀不到餘額）。 | 訊息最後會印出之後補賣的指令，`--keep-coins` 和 `--max-sell-coins` 都已算好填上。準備好時執行它。 |

## 自己檢查程式（選用）

五套離線測試不需要網路、錢包或 GPU。每一套最後都必須是 `FAILED: none`：

```sh
python3 submit/test_submit.py          # 送單器：假的鏈與假的 baw
python3 submit/test_sell.py            # 賣出器：池子模型
python3 tools/test_replay_log.py       # 紀錄檔遮蔽
python3 tools/test_pearl_autopilot.py  # pearl.py：假的子程式
python3 miner/test_autotune.py         # 礦機的批次大小調整，不需要 GPU
```

## 交給你的 AI agent

你可以把這件事交給 Claude Code、Codex 或 Cursor 這類程式 agent。把下面的提示詞貼給你的 agent（也可以改成你自己的話）。
agent 完全不需要私鑰。要安裝 pearl-miner skill 或設定特定的 agent，請看 [AGENTS-SETUP.zh-TW.md](AGENTS-SETUP.zh-TW.md)。

```text
請用 pearl-miner 幫我挖 珍珠币（PearlMiningV2）：https://gitlab.com/aa22396584/pearl-miner
1. clone 這個 repo，讀 docs/QUICKSTART.zh-TW.md 和 README.zh-TW.md 的免責聲明，照著做。
2. 絕對不要向我要、讀取或儲存任何私鑰、助記詞或密碼。錢包是我的幣安 Agentic Wallet，只透過我自己登入的 `baw` CLI
   操作。如果有任何步驟看起來需要私鑰，停下來告訴我。
3. 用我給你的地址和 handle 設定 PEARL_WALLET 和 PEARL_HANDLE。
4. 先跑免費預演 `python3 submit/fork_rehearsal/run_fork.py --autopilot`。除非最後是 FORK REHEARSAL: PASS，
   否則停下來把輸出給我看。
5. 跑 `python3 pearl.py auto --plan`：它會跑唯讀的 check 並印出計畫，check 失敗就停下來。把計畫給我看：哪一天、幾點、
   gas 上限、保留的 BNB、賣出模式和比例，以及任何 WARNING。等我回答 yes。
6. 我說 yes 之後，在開放前幾分鐘啟動，並讓它不受你的 shell 時間限制，參數要和計畫一樣，而且一定要帶計畫裡的
   --day N，例如在背景執行 `echo yes | nohup python3 pearl.py auto --day N > submit/auto.log 2>&1 &`。
   除非策略是我自己寫的而且我叫你用，否則絕對不要用 --yes。
7. 要停止送單就建立 submit/STOP；絕對不要砍程序，也不要刪 journal.jsonl 或 run.pid。除非我要求，不要改程式碼、
   預設值或安全上限。
8. 結束後回報總結：挖到的 proof、花掉的 gas、賣出的幣、換到的 BNB、停止原因和結束代碼。可以用
   `python3 pearl.py status` 查看進度。如果以結束代碼 3 結束，重跑前要再問我，並帶同一個 --day N；
   那一天的時段已過就不要重跑。
```

English prompt: [QUICKSTART.md](QUICKSTART.md#hand-it-to-your-ai-agent).
