# Ultimate Betting Static GPT

This is the replacement architecture for ChatGPT Web.

The failed approach was:

```text
Custom GPT -> live Cloudflare Worker -> hydrate huge Superbet board during chat
```

That fails because a single live GPT call can require hundreds of Superbet event hydrations and 100k+ odds rows. Free Cloudflare Workers will hit CPU/body/resource limits, and ChatGPT will not reason cleanly over that much raw data anyway.

The working no-paid approach is:

```text
Scheduled GitHub Action / local script
-> fetches Superbet full football board
-> writes static JSON files
-> publishes them with GitHub Pages
-> Custom GPT reads small static files through Actions
-> GPT uses web research only after odds candidates are available
```

No paid odds API is used.

## What The GPT Gets

Generated files:

- `latest/manifest.json`: timestamp, counts, file map
- `latest/events-index.json`: all fixtures with IDs and market counts
- `latest/main-markets.json`: compact main-market odds for all fixtures
- `latest/candidates.json`: candidate work queue for GPT research
- `latest/events/{event_id}.json`: all active odds/markets for one fixture
- `latest/shards/selections-000.json`, etc.: all flattened selections split into pages

The GPT should not hydrate the whole board live. It should:

1. Read `manifest.json`.
2. Read `main-markets.json` or `candidates.json`.
3. Pick candidate fixtures/markets.
4. Read the selected `events/{event_id}.json`.
5. Use Web Search for current team/news/stat context.
6. Calculate EV and return `BET` or `NO BET`.

## Setup

1. Create a public GitHub repo, for example `ultimate-betting-data`.
2. Copy this folder into it.
3. Enable GitHub Pages from the `gh-pages` branch, or use the included workflow.
4. Run the workflow every 15 minutes, or manually.
5. In GPT Builder, use `actions/static-openapi.yaml`, replacing the server URL with your GitHub Pages URL.

Example server URL:

```text
https://USERNAME.github.io/ultimate-betting-data
```

## Local Build

From `/mnt/c/SZPONT`:

```bash
python3 ultimate-betting-static-gpt/scripts/build_static_superbet.py \
  --output ultimate-betting-static-gpt/site \
  --start-date 2026-04-26 \
  --end-date 2026-04-26
```

The script uses the existing standalone Superbet fetcher:

```text
/mnt/c/SZPONT/superbet_odds_chatgpt_web.py
```

## Why This Solves The Problem

The expensive part happens before ChatGPT is asked anything.

ChatGPT no longer has to ask a Worker to hydrate all events live. It reads already-built static files, which is cheap and stable.

This also gives the GPT true access to the whole fetched Superbet football board, split into usable pieces.
