#!/usr/bin/env python3
"""
Daily portfolio dashboard generator.

- Pulls price history + analyst data via yfinance
- Computes RSI(14), 50/200-day MA trend, analyst consensus, upside to mean target
- Builds a composite score -> signal (Strong Buy >=5, Buy 3-4, Wait 0-2, Reduce <0)
- Regenerates index.html
- Detects signal flips vs signals.json and sends a Telegram alert (if secrets set)

Usage:
    python update_dashboard.py            # real run (needs yfinance)
    python update_dashboard.py --mock     # offline test with synthetic data
"""

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SGT = timezone(timedelta(hours=8))
MOCK = "--mock" in sys.argv


# ---------------------------------------------------------------- data fetch

def fetch_ticker_data(ticker: str) -> dict:
    """Return dict with price, prev_close, closes (list, oldest->newest),
    rec_mean, target_mean, analysts, name. Raises on hard failure."""
    if MOCK:
        return _mock_data(ticker)

    import yfinance as yf
    t = yf.Ticker(ticker)
    hist = t.history(period="1y", interval="1d", auto_adjust=True)
    if hist is None or len(hist) < 60:
        raise RuntimeError(f"{ticker}: insufficient history")
    closes = [float(c) for c in hist["Close"].dropna().tolist()]

    info = {}
    try:
        info = t.info or {}
    except Exception:
        pass

    return {
        "price": closes[-1],
        "prev_close": closes[-2],
        "closes": closes,
        "rec_mean": _num(info.get("recommendationMean")),
        "rec_key": info.get("recommendationKey") or "",
        "target_mean": _num(info.get("targetMeanPrice")),
        "analysts": _num(info.get("numberOfAnalystOpinions")),
        "name": info.get("shortName") or info.get("longName") or "",
    }


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mock_data(ticker: str) -> dict:
    """Deterministic synthetic data so HTML/signal logic can be tested offline."""
    import math
    seed = sum(ord(c) for c in ticker)
    base = 50 + (seed % 200)
    closes = [
        base * (1 + 0.15 * math.sin(i / 21 + seed) + 0.001 * i)
        for i in range(250)
    ]
    return {
        "price": closes[-1],
        "prev_close": closes[-2],
        "closes": closes,
        "rec_mean": 1.5 + (seed % 20) / 10.0,
        "rec_key": "buy",
        "target_mean": closes[-1] * (1 + ((seed % 45) - 10) / 100.0),
        "analysts": 10 + seed % 30,
        "name": ticker,
    }


# ------------------------------------------------------------- indicators

def rsi14(closes: list) -> float | None:
    """Wilder-smoothed RSI(14)."""
    if len(closes) < 15:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    period = 14
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def sma(closes: list, n: int) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


# ------------------------------------------------------------------ scoring

def score_ticker(d: dict) -> dict:
    price = d["price"]
    r = rsi14(d["closes"])
    ma50 = sma(d["closes"], 50)
    ma200 = sma(d["closes"], 200)

    # RSI component: reward oversold, punish overbought
    s_rsi = 0
    if r is not None:
        if r < 30:
            s_rsi = 2
        elif r < 45:
            s_rsi = 1
        elif r <= 65:
            s_rsi = 0
        elif r <= 75:
            s_rsi = -1
        else:
            s_rsi = -2

    # Trend component
    s_trend = 0
    if ma50 is not None:
        s_trend += 1 if price > ma50 else 0
    if ma200 is not None:
        s_trend += 1 if price > ma200 else -1
    if ma50 is not None and ma200 is not None and ma50 > ma200:
        s_trend += 1  # golden-cross regime

    # Analyst consensus (1=strong buy .. 5=sell)
    s_cons = 0
    rm = d.get("rec_mean")
    if rm is not None:
        if rm <= 1.8:
            s_cons = 2
        elif rm <= 2.4:
            s_cons = 1
        elif rm <= 3.0:
            s_cons = 0
        else:
            s_cons = -1

    # Upside to mean 12-mo target
    s_up, upside = 0, None
    tm = d.get("target_mean")
    if tm and price:
        upside = (tm / price - 1) * 100
        if upside >= 30:
            s_up = 2
        elif upside >= 15:
            s_up = 1
        elif upside >= 0:
            s_up = 0
        else:
            s_up = -2

    total = s_rsi + s_trend + s_cons + s_up
    if total >= 5:
        signal = "Strong Buy"
    elif total >= 3:
        signal = "Buy"
    elif total >= 0:
        signal = "Wait"
    else:
        signal = "Reduce"

    return {
        "rsi": r, "ma50": ma50, "ma200": ma200,
        "s_rsi": s_rsi, "s_trend": s_trend, "s_cons": s_cons, "s_up": s_up,
        "upside": upside, "score": total, "signal": signal,
    }


# --------------------------------------------------------------- telegram

def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("Telegram secrets not set - skipping alert")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    ).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=30) as resp:
            ok = resp.status == 200
            print(f"Telegram alert sent: {ok}")
            return ok
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False


# ------------------------------------------------------------------- html

CSS = """
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff;--purple:#bc8cff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:24px;max-width:1200px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px}
h2{font-size:16px;margin:28px 0 10px;color:var(--blue)}
.sub{color:var(--muted);font-size:12px;margin-bottom:18px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px 18px;min-width:150px}
.card .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.card .v{font-size:20px;font-weight:600;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--border);border-radius:8px;overflow:hidden;font-size:13px}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--border);white-space:nowrap}
th{background:#1c2128;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
td:first-child,th:first-child{text-align:left}
td:nth-child(2){text-align:left;color:var(--muted)}
tr:last-child td{border-bottom:none}
.pos{color:var(--green)}.neg{color:var(--red)}
.sig{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.sig-strongbuy{background:#1a4d2e;color:#3fb950}
.sig-buy{background:#1c3d5c;color:#58a6ff}
.sig-wait{background:#4d3c11;color:#d29922}
.sig-reduce{background:#5c1e1e;color:#f85149}
.note{color:var(--muted);font-size:12px;margin-top:20px;line-height:1.6}
.err{color:var(--red);font-size:12px;margin-top:8px}
"""


def sig_span(signal: str) -> str:
    cls = "sig-" + signal.lower().replace(" ", "")
    return f'<span class="sig {cls}">{signal}</span>'


def fmt(v, dec=2, pct=False, dollar=False):
    if v is None:
        return "\u2013"
    s = f"{v:,.{dec}f}"
    if dollar:
        s = "$" + s
    if pct:
        s += "%"
    return s


def chg_cell(v, dec=2, pct=False, dollar=False):
    if v is None:
        return "<td>\u2013</td>"
    cls = "pos" if v >= 0 else "neg"
    sign = "+" if v >= 0 else ""
    return f'<td class="{cls}">{sign}{fmt(v, dec, pct, dollar)}</td>'


def build_html(holdings_rows, watch_rows, totals, errors, now_sgt):
    h_rows = "\n".join(holdings_rows)
    w_rows = "\n".join(watch_rows)
    err_html = (
        '<div class="err">Data fetch failed for: ' + ", ".join(errors) + " (showing without live data)</div>"
        if errors else ""
    )
    tot_pl_cls = "pos" if totals["pl"] >= 0 else "neg"
    tot_day_cls = "pos" if totals["day"] >= 0 else "neg"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NASDAQ Portfolio Dashboard</title>
<style>{CSS}</style>
</head>
<body>
<h1>NASDAQ Portfolio Dashboard</h1>
<div class="sub">Saxo + Moomoo combined &middot; Last updated: {now_sgt} SGT &middot; auto-refreshed daily ~8:20pm SGT (before US open)</div>
{err_html}
<div class="cards">
  <div class="card"><div class="k">Market Value</div><div class="v">{fmt(totals['mv'], dollar=True)}</div></div>
  <div class="card"><div class="k">Total P/L</div><div class="v {tot_pl_cls}">{'+' if totals['pl'] >= 0 else ''}{fmt(totals['pl'], dollar=True)} ({'+' if totals['pl_pct'] >= 0 else ''}{fmt(totals['pl_pct'], pct=True)})</div></div>
  <div class="card"><div class="k">Day Change</div><div class="v {tot_day_cls}">{'+' if totals['day'] >= 0 else ''}{fmt(totals['day'], dollar=True)}</div></div>
</div>

<h2>Holdings</h2>
<table>
<thead><tr><th>Ticker</th><th>Name / Broker</th><th>Qty</th><th>Avg Cost</th><th>Price</th><th>Day %</th><th>P/L $</th><th>P/L %</th><th>Weight</th><th>Signal</th></tr></thead>
<tbody>
{h_rows}
</tbody>
</table>

<h2>Daily Buy Watch</h2>
<div class="sub">Composite of RSI(14), trend vs 50/200-day MAs, analyst consensus &amp; upside to mean price target. Strong Buy &ge;5 &middot; Buy 3&ndash;4 &middot; Wait 0&ndash;2 &middot; Reduce &lt;0</div>
<table>
<thead><tr><th>Ticker</th><th>Name</th><th>Price</th><th>RSI(14)</th><th>vs MA50</th><th>vs MA200</th><th>Consensus</th><th>Target Upside</th><th>Score</th><th>Signal</th></tr></thead>
<tbody>
{w_rows}
</tbody>
</table>

<div class="note">
Prices &amp; analyst data via Yahoo Finance. Analyst targets are 12-month means and are often optimistic, especially on small caps.
These are data-driven indicators, <b>not financial advice</b>.
</div>
</body>
</html>
"""


# -------------------------------------------------------------------- main

def main():
    cfg = json.loads((ROOT / "portfolio.json").read_text())
    holdings = cfg["holdings"]
    watchlist = cfg.get("watchlist", [])
    all_tickers = [h["ticker"] for h in holdings] + [
        w for w in watchlist if w not in {h["ticker"] for h in holdings}
    ]

    data, errors = {}, []
    for tk in all_tickers:
        try:
            d = fetch_ticker_data(tk)
            d.update(score_ticker(d))
            data[tk] = d
            print(f"{tk}: price={d['price']:.2f} rsi={d['rsi']:.1f} score={d['score']} {d['signal']}")
        except Exception as e:
            print(f"WARN {tk}: {e}")
            errors.append(tk)

    if len(errors) == len(all_tickers):
        print("ERROR: all fetches failed - keeping existing dashboard, exiting nonzero")
        sys.exit(1)

    # ---- holdings table + totals
    mv_total = cost_total = day_total = 0.0
    rows_tmp = []
    for h in holdings:
        tk = h["ticker"]
        d = data.get(tk)
        qty, avg = h["qty"], h["avg_cost"]
        cost = qty * avg
        cost_total += cost
        if d:
            mv = qty * d["price"]
            pl = mv - cost
            day_pct = (d["price"] / d["prev_close"] - 1) * 100
            day_total += qty * (d["price"] - d["prev_close"])
            mv_total += mv
            rows_tmp.append((h, d, mv, pl, day_pct))
        else:
            rows_tmp.append((h, None, None, None, None))

    holdings_rows = []
    for h, d, mv, pl, day_pct in rows_tmp:
        if d:
            weight = mv / mv_total * 100 if mv_total else 0
            pl_pct = pl / (h["qty"] * h["avg_cost"]) * 100
            holdings_rows.append(
                f"<tr><td><b>{h['ticker']}</b></td>"
                f"<td>{h['name']} &middot; {h['broker']}</td>"
                f"<td>{h['qty']}</td><td>{fmt(h['avg_cost'], 2, dollar=True)}</td>"
                f"<td>{fmt(d['price'], 2, dollar=True)}</td>"
                f"{chg_cell(day_pct, pct=True)}"
                f"{chg_cell(pl, dollar=True)}{chg_cell(pl_pct, pct=True)}"
                f"<td>{fmt(weight, 1, pct=True)}</td><td>{sig_span(d['signal'])}</td></tr>"
            )
        else:
            holdings_rows.append(
                f"<tr><td><b>{h['ticker']}</b></td><td>{h['name']} &middot; {h['broker']}</td>"
                f"<td>{h['qty']}</td><td>{fmt(h['avg_cost'], 2, dollar=True)}</td>"
                f"<td>\u2013</td><td>\u2013</td><td>\u2013</td><td>\u2013</td><td>\u2013</td><td>\u2013</td></tr>"
            )

    # ---- watch table (holdings + watchlist), sorted by score desc
    watch_rows = []
    for tk in sorted([t for t in all_tickers if t in data], key=lambda t: -data[t]["score"]):
        d = data[tk]
        vs50 = (d["price"] / d["ma50"] - 1) * 100 if d["ma50"] else None
        vs200 = (d["price"] / d["ma200"] - 1) * 100 if d["ma200"] else None
        cons = f"{d['rec_mean']:.1f}" if d.get("rec_mean") else "\u2013"
        if d.get("analysts"):
            cons += f" ({int(d['analysts'])})"
        watch_rows.append(
            f"<tr><td><b>{tk}</b></td><td>{d.get('name') or tk}</td>"
            f"<td>{fmt(d['price'], 2, dollar=True)}</td><td>{fmt(d['rsi'], 1)}</td>"
            f"{chg_cell(vs50, 1, pct=True)}{chg_cell(vs200, 1, pct=True)}"
            f"<td>{cons}</td>{chg_cell(d['upside'], 1, pct=True)}"
            f"<td><b>{d['score']:+d}</b></td><td>{sig_span(d['signal'])}</td></tr>"
        )

    totals = {
        "mv": mv_total,
        "pl": mv_total - cost_total,
        "pl_pct": (mv_total / cost_total - 1) * 100 if cost_total else 0,
        "day": day_total,
    }
    now_sgt = datetime.now(SGT).strftime("%a %d %b %Y, %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    html = build_html(holdings_rows, watch_rows, totals, errors, now_sgt)
    (ROOT / "index.html").write_text(html)
    print(f"index.html written ({len(html)} bytes)")

    # ---- signal flip detection
    sig_path = ROOT / "signals.json"
    prev = {}
    if sig_path.exists():
        try:
            prev = json.loads(sig_path.read_text())
        except Exception:
            prev = {}
    current = {tk: data[tk]["signal"] for tk in data}
    flips = [
        (tk, prev[tk], cur) for tk, cur in current.items()
        if tk in prev and prev[tk] != cur
    ]
    sig_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")

    if flips:
        held = {h["ticker"] for h in holdings}
        lines = ["<b>Portfolio signal changes</b> (" + now_sgt + " SGT)", ""]
        for tk, old, new in sorted(flips, key=lambda f: (f[0] not in held, f[0])):
            tag = "HELD" if tk in held else "watch"
            lines.append(f"{tk} [{tag}]: {old} \u2192 <b>{new}</b>  (score {data[tk]['score']:+d}, ${data[tk]['price']:.2f})")
        lines += ["", "https://muthuramanathanm.github.io/portfolio-dashboard/"]
        send_telegram("\n".join(lines))
    elif not prev:
        # first run with signals - send a baseline summary
        lines = ["<b>Portfolio dashboard alerts are live</b> \u2705", "", "Current signals:"]
        for tk in sorted(current):
            lines.append(f"{tk}: {current[tk]} ({data[tk]['score']:+d})")
        lines += ["", "You'll only get pinged when a signal changes.",
                  "https://muthuramanathanm.github.io/portfolio-dashboard/"]
        send_telegram("\n".join(lines))
    else:
        print("No signal changes today")


if __name__ == "__main__":
    main()
