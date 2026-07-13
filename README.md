# Portfolio Dashboard

Auto-refreshing NASDAQ portfolio dashboard (Saxo + Moomoo), published at
https://muthuramanathanm.github.io/portfolio-dashboard/

## How it works

- `portfolio.json` — your holdings (qty, avg cost, broker) and watchlist. **Edit this file to change positions.**
- `update_dashboard.py` — pulls prices + analyst data from Yahoo Finance, computes RSI(14), 50/200-day MA trend, analyst consensus and upside to mean target, builds a composite score, and regenerates `index.html`.
- `.github/workflows/daily-update.yml` — runs Mon–Fri at 8:15pm SGT (before US open), commits the refreshed page, and GitHub Pages redeploys it automatically.
- `signals.json` — previous day's signals; used to detect flips.

## Signals

| Score | Signal |
|-------|--------|
| ≥ 5   | Strong Buy |
| 3–4   | Buy |
| 0–2   | Wait |
| < 0   | Reduce |

Score = RSI band (+2 oversold … −2 overbought) + trend vs MA50/MA200 incl. golden cross (−1…+3) + analyst consensus (−1…+2) + upside to mean 12-mo target (−2…+2).

## Telegram alerts

When a ticker's signal changes vs the previous run, the workflow sends a Telegram message. Setup:

1. Message **@BotFather** on Telegram → `/newbot` → copy the bot token.
2. Message your new bot anything (e.g. "hi"), then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy your `chat.id`.
3. In this repo: **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

No secrets = the dashboard still refreshes daily, just without alerts.

## Manual run

Actions tab → "Daily dashboard update" → Run workflow. Locally: `pip install -r requirements.txt && python update_dashboard.py` (or `--mock` for offline testing).

*Data-driven indicators, not financial advice.*
