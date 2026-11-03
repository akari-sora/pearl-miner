# Using pearl-miner with an AI agent

English · [繁體中文](AGENTS-SETUP.zh-TW.md)

pearl-miner ships two things for AI agents:

- **`AGENTS.md`** at the repository root: the rules for any agent working in a clone (what the repository is, the
  entry point, the safety rules, the tests, what not to change). `CLAUDE.md` and `GEMINI.md` import it.
- **`skills/pearl-miner/SKILL.md`**: a skill in the Agent Skills format, the same format as Binance's official
  [binance-skills-hub](https://github.com/binance/binance-skills-hub). The file works on its own: it clones a
  pinned version of this repository and then follows [QUICKSTART.md](QUICKSTART.md).

Use it together with Binance's official **binance-agentic-wallet** skill, which drives `baw`. You need an Agent ID,
`baw` signed in with its status `CONNECTED`, and Developer Mode on. The agent never needs a private key.

## Install the skill

Get the file first: `git clone https://gitlab.com/aa22396584/pearl-miner.git`, or download
`skills/pearl-miner/SKILL.md` alone.

### Claude Code

For all your projects:

```sh
mkdir -p ~/.claude/skills/pearl-miner
cp skills/pearl-miner/SKILL.md ~/.claude/skills/pearl-miner/
```

For one project only, use `.claude/skills/pearl-miner/` inside that project instead. In a clone of this
repository, Claude Code also reads `CLAUDE.md`, which imports `AGENTS.md`.

### Codex CLI

Codex's [skills documentation](https://developers.openai.com/codex/skills) (checked 2026-11-03) lists
`~/.agents/skills` for your own skills and `.agents/skills` inside a repository:

```sh
mkdir -p ~/.agents/skills/pearl-miner
cp skills/pearl-miner/SKILL.md ~/.agents/skills/pearl-miner/
```

Skill locations can change between Codex versions. If yours does not list the skill, follow the skills page of
the documentation for your version. In a clone of this repository, Codex also reads `AGENTS.md`.

### Grok Bot

Add the file to your skill library, either with the `gbot` CLI (`gbot skills add skills/pearl-miner/SKILL.md`;
every bot on the account sees it), or by sending the file to a bot and asking it to save it as a skill.

- Only `SKILL.md` is uploaded. That is why it holds every step itself.
- A Grok bot works on its own cloud computer (its "box"). pearl-miner has to run on **your** computer, where `baw`
  is signed in. The bot reaches it through Grok Bot Shell on a machine you registered, and that can ask for your
  approval. The bot's box is shared by all the bots of the same account; think about that before you decide
  whether to sign `baw` in there.

### Cursor, Gemini CLI, OpenCode

Open a clone of this repository. Cursor and OpenCode read `AGENTS.md` at the root. Gemini CLI reads `GEMINI.md`,
which imports `AGENTS.md`; alternatively, set `context.fileName` in its `settings.json` to include `AGENTS.md`.

## Prompts to copy

With the skill installed:

```text
Use the pearl-miner skill to mine 珍珠币 with my Binance Agentic Wallet. Follow its safety rules: no private
keys, check and the fork rehearsal first, show me the plan and wait for my yes before pearl.py auto, and no
--yes unless I wrote the strategy myself. Report the summary at the end.
```

```text
用 pearl-miner skill 和我的幣安 Agentic Wallet 挖 珍珠币。遵守它的安全規則：不碰私鑰；先跑 check 和 fork 預演；
跑 pearl.py auto 之前先把計畫給我看，等我說 yes；除非策略是我自己寫的，否則不用 --yes。結束後回報總結。
```

Without the skill, the full prompt is in [QUICKSTART.md](QUICKSTART.md#hand-it-to-your-ai-agent) (English) and
[QUICKSTART.zh-TW.md](QUICKSTART.zh-TW.md#交給你的-ai-agent) (中文).

## Risks

The same as for running it by hand: small and uncertain profit, a price that fell about 51% in the 14 minutes
after the day-1 opening, and wallet terms that may not allow automation. An agent does not change any of that.
See the [disclaimer](../README.md#disclaimer).
