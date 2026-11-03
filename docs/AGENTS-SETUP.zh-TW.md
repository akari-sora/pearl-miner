# 搭配 AI Agent 使用 pearl-miner

[English](AGENTS-SETUP.md) · 繁體中文

pearl-miner 為 AI agent 準備了兩樣東西：

- repo 根目錄的 **`AGENTS.md`**：任何 agent 在 clone 下來的 repo 裡工作時要遵守的規則（這個 repo 是什麼、入口、
  安全規則、測試、哪些不能改）。`CLAUDE.md` 和 `GEMINI.md` 都會匯入它。
- **`skills/pearl-miner/SKILL.md`**：Agent Skills 格式的 skill，和幣安官方
  [binance-skills-hub](https://github.com/binance/binance-skills-hub) 同一種格式。這個檔案單獨就能用：它會
  clone 固定版本的 repo，再照 [QUICKSTART.zh-TW.md](QUICKSTART.zh-TW.md) 執行。

請和幣安官方的 **binance-agentic-wallet** skill（操作 `baw`）一起使用。需要：Agent ID、`baw` 已登入且狀態是
`CONNECTED`、Developer Mode 已開啟。agent 完全不需要私鑰。

## 安裝 skill

先拿到檔案：`git clone https://gitlab.com/aa22396584/pearl-miner.git`，或只下載
`skills/pearl-miner/SKILL.md`。

### Claude Code

所有專案都能用：

```sh
mkdir -p ~/.claude/skills/pearl-miner
cp skills/pearl-miner/SKILL.md ~/.claude/skills/pearl-miner/
```

只給某個專案用的話，改放在那個專案裡的 `.claude/skills/pearl-miner/`。在這個 repo 的 clone 裡，Claude Code
也會讀 `CLAUDE.md`，它會匯入 `AGENTS.md`。

### Codex CLI

Codex 的 [skills 文件](https://developers.openai.com/codex/skills)（2026-11-03 查核）列出：個人的 skill 放
`~/.agents/skills`，repo 內的放 `.agents/skills`：

```sh
mkdir -p ~/.agents/skills/pearl-miner
cp skills/pearl-miner/SKILL.md ~/.agents/skills/pearl-miner/
```

不同版本的 Codex 讀 skill 的位置可能不同。如果你的版本沒有列出這個 skill，請照你那個版本的官方 skills 文件放。
在這個 repo 的 clone 裡，Codex 也會讀 `AGENTS.md`。

### Grok Bot

把檔案加進你的 skill 庫：可以用 `gbot` CLI（`gbot skills add skills/pearl-miner/SKILL.md`；帳號裡每個 bot
都看得到；依 gbot 0.11.2 的 `--help`，2026-11-03 查核），或把檔案傳給 bot，請它存成 skill。

- 只有 `SKILL.md` 會被上傳，所以所有步驟都寫在這個檔案裡。
- Grok bot 在它自己的雲端電腦（它的「box」）上運作。pearl-miner 必須在**你自己的電腦**上執行，也就是 `baw`
  登入的那台。bot 透過 Grok Bot Shell 連到你註冊過的電腦，這可能需要你核准。建議不要在 bot 的 box 上登入 `baw`：
  同一帳號的 bot 共用那台機器。

### Cursor、Gemini CLI、OpenCode

開啟這個 repo 的 clone。Cursor 和 OpenCode 會讀根目錄的 `AGENTS.md`。Gemini CLI 讀 `GEMINI.md`，它會匯入
`AGENTS.md`；或者在 Gemini CLI 的 `settings.json` 把 `context.fileName` 設成包含 `AGENTS.md`。

## 可以直接複製的提示詞

已安裝 skill 時：

```text
用 pearl-miner skill 和我的幣安 Agentic Wallet 挖 珍珠币。遵守它的安全規則：不碰私鑰；先跑 check 和 fork 預演；
跑 pearl.py auto 之前先把計畫給我看，等我說 yes；除非策略是我自己寫的，否則不用 --yes。結束後回報總結。
```

```text
Use the pearl-miner skill to mine 珍珠币 with my Binance Agentic Wallet. Follow its safety rules: no private
keys, check and the fork rehearsal first, show me the plan and wait for my yes before pearl.py auto, and no
--yes unless I wrote the strategy myself. Report the summary at the end.
```

沒有安裝 skill 時，完整的提示詞在 [QUICKSTART.zh-TW.md](QUICKSTART.zh-TW.md#交給你的-ai-agent)（中文）和
[QUICKSTART.md](QUICKSTART.md#hand-it-to-your-ai-agent)（English）。

## 風險

和手動執行一樣：利潤小且不確定；第 1 天開放後 14 分鐘內價格跌了約 51%；錢包條款可能不允許自動化。交給 agent
並不會改變這些。請看[免責聲明](../README.zh-TW.md#免責聲明)。
