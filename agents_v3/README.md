# CGC Agent V3

Codex-style local autonomous engineering agent for the Bitget AI trading bot.
Runs fully local on Ollama — no API tokens, no rate limits.

Goals:
- Read and understand the whole repo (repo map + multi-file context + explore tools)
- Measure live trading performance and chase the biggest loss driver
- Plan safe changes, write patches, run tests, roll back on failure
- Restart the bot through the proven start path
- Never touch .env
- Never change live risk/leverage/execution safeguards without human approval
- Never push/deploy without approval

## The autonomous loop

```
analyze -> pick biggest loss driver -> explore code with tools -> propose patch
        -> safety guard -> apply -> pytest -> rollback on failure -> restart bot -> journal
```

Run it:

```bash
python -m agents_v3.cli cycle "verbeter winstgevendheid"            # proposal only
python -m agents_v3.cli cycle "verbeter winstgevendheid" --approve  # applies autonomous-path patches
```

## Modes

- `analyze` — live trading performance report (pnl, winrate, fees, per strategy/direction/duration)
- `agent` — agentic tool loop on any task: the model explores the repo with tools
  (read_file, search_code, list_files, trade_stats, run_tests, git_diff) over up to
  12 steps before proposing a change; `--approve` also applies it
- `cycle` — full self-improvement cycle: analyze -> top backlog item -> agent loop -> apply lifecycle
- `improve` — show the backlog (live performance signals first, then docs/TODO.md)
- `auto` — pick the top backlog item and run the single-shot task engine on it
- `do` — single-shot task engine (one LLM call, saves a pending patch)
- `plan` / `propose` — read-only analysis / patch proposal without applying
- `patch` — dry-run, or with `--approve` apply the pending patch (human approval; may touch protected paths)
- `audit` / `status` / `test` / `safety` — repo index, git status, core tests, guard demo

## Safety model

Two layers, enforced in code (`agents_v3/safety/safety_guard.py`, `lifecycle/patch_lifecycle.py`):

1. **Hard blocks (always):** `.env*` files; added lines containing
   api_key/secret/password/leverage/disable_sl/disable_tp/remove_stop/live_trade.
2. **Path policy:** autonomous applies (`agent --approve`, `cycle --approve`, `auto --approve`)
   may only touch `strategies/`, `planning/`, `docs/`, `agents_v3/`, `tests/`.
   Patches touching `risk/`, `execution/`, `app/`, `clients/` etc. are saved as pending
   and require a human: `python -m agents_v3.cli patch apply --approve`.

Every apply runs the full pytest suite; failure rolls back only the patched files.
Bot restarts go through `scripts/start_bot.sh` and kill both `app.main` and
`app.runner` patterns first, so a restart can never leave two live instances trading.
Every lifecycle outcome is journaled to `docs/JOURNAL.md`.

## Models

Local via Ollama, overridable via env:
- `CGC_FAST_MODEL` (default qwen2.5-coder:14b) for read-only analysis
- `CGC_STRONG_MODEL` (default qwen2.5-coder:14b; set to qwen2.5-coder:32b only on 24GB+ RAM)
- `CGC_OLLAMA_TIMEOUT_SECONDS` (default 600), `CGC_OLLAMA_NUM_CTX` (default 12288)

## Quick Start

```bash
python -m agents_v3.cli analyze "laatste 14 dagen"
python -m agents_v3.cli improve "backlog"
python -m agents_v3.cli agent "onderzoek waarom low_vol_reclaim verliest"
python -m agents_v3.cli cycle "verbeter winstgevendheid" --approve
python -m agents_v3.cli status "show current repo changes"
```

## Status Command

Use `status` to inspect current repository changes before applying patches.

## Tests

```bash
python -m pytest agents_v3/tests/ -q
```
