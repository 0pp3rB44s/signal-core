# Starting the DEMO/PAPER bot — owner instructions

You do not need to know Python, git, or anything technical to do this. Follow the steps in order.

**This can never place a real trade with real money.** It only ever pretends to trade, so you can watch
how it behaves before deciding anything about real capital later.

## Step 1 — Open the Terminal app

On a Mac: press `Cmd+Space`, type `Terminal`, press Enter.

## Step 2 — Run one command

Copy and paste this exact line into the Terminal window, then press Enter:

```
cd "/Users/bryonprivee/Desktop/bitget_ai_agent/restore-signal-core-funnel-20260927" && ./start_demo.sh
```

## Step 3 — Read what it prints

It will run a list of safety checks and print `[PASS]` or `[FAIL]` next to each one. If anything says
`[FAIL]`, stop and don't type anything further — take a screenshot and send it back for review.

If everything says `[PASS]`, it will print a summary of the bot that is already running (how long it's
been running, how many trades it has made, whether its data feed is healthy), and then it will show:

```
MODE=DEMO/PAPER
REAL_MONEY=NO
```

## Step 4 — Type the confirmation phrase

When it asks you to type a phrase, type exactly:

```
START LIVE PILOT
```

and press Enter. (Typing this only confirms you want to watch the DEMO bot — it cannot start any real
trading. There is no version of this phrase, today or in this file, that touches real money.)

## Step 5 — Done

It will print one more readiness summary and then exit. The bot itself is a separate, already-running
process — it keeps running in the background whether or not you run this ceremony again. You can re-run
`./start_demo.sh` any time you want to check on it or show someone else.

## Checking on it later without running the whole ceremony

To just see the current numbers without going through the safety checks again, paste this:

```
cat "/Users/bryonprivee/Desktop/bitget_ai_agent/restore-signal-core-funnel-20260927/data_store/continuous_paper/baseline/status.json"
```

## Stopping the DEMO bot

If you ever want to stop it:

```
kill "$(cat /Users/bryonprivee/Desktop/bitget_ai_agent/restore-signal-core-funnel-20260927/state/continuous_paper_baseline.pid)"
kill "$(cat /Users/bryonprivee/Desktop/bitget_ai_agent/restore-signal-core-funnel-20260927/state/continuous_paper_shadow.pid)"
```

This stops it cleanly (it finishes what it's doing and saves its state so it can pick up where it left
off later — nothing is lost).
