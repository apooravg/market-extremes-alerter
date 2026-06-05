# Market Extremes Alerter

[![CI](https://github.com/apooravg/market-extremes-alerter/actions/workflows/ci.yml/badge.svg)](https://github.com/apooravg/market-extremes-alerter/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org)

A single-file, zero-infrastructure market monitor that sends a Telegram message **only when something
is actionable** — a deep dip to accumulate, a froth top to trim, a notable single-day or multi-day
move, a sentiment regime change, or a golden/death cross. Silence is the normal state: this is a
**signal detector, not a daily digest**.

It tracks Indian (NSE/BSE) and US indices side by side, runs unattended on a free-tier VM via `cron`,
and is dependency-light (`requests` + `yfinance`).

## Sample alert

```
🔻 Sensex -0.2% · 📊 Thu 04 Jun 9:33AM IST
🇮🇳 SENSEX  74202   -0.2% today 🔻
▰▰▱▱▱▱▱▱▱▱  +3.1% above low · -13.5% below high
🇺🇸 Nasdaq100   -0.2% today 🔻
▰▰▰▰▰▰▰▰▰▰  +42.5% above low · at 1y high
——————————
🇮🇳 Nifty Large & Midcap 250   -0.0% today 🔻
▰▰▰▰▰▰▱▱▱▱  +10.5% above low · -5.4% below high
50DMA +1.2% · 200DMA -1.1%
🇮🇳 Nifty Smallcap 250   +0.2% today 📈
▰▰▰▰▰▰▰▱▱▱  +19.3% above low · -5.7% below high
50DMA +5.0% · 200DMA +3.3%
——————————
🧭 Mood index
🇮🇳 Greed   ▰▰▰▰▰▱▱▱▱▱ ↘
🇺🇸 Neutral ▰▰▰▰▰▱▱▱▱▱ ↘
🔔 US sentiment Greed → Neutral
📰 Sensex prediction today: how the Indian market is expected to trade — Mint
📰 Sensex dips 213 pts ahead of RBI policy announcement — News On AIR
```

The fill-bar shows position in the 52-week range (`▰` filled = near the high). Adding entries to the
`HOLDINGS` config appends per-fund lines (live NAV + DMA distances) at the bottom.

## What makes it interesting (the engineering)

- **Distance-from-moving-average signal model**, not fixed price targets — a "+18% above the 200DMA"
  threshold stays meaningful as the market drifts over years, where a hard price level goes stale.
- **Escalate-only state machine with hysteresis** — fires ~once per tier (T1, T2, then "back to
  normal") and re-arms only after price recovers, so a months-long trend never spams.
- **Hybrid live + EOD data** — Indian indices use the NSE live spot, but the NSE endpoint has no
  history, so the 50/100/200-day moving averages and 1y range are borrowed from a tracking index
  fund's NAV and **scaled into index points**, then nudged by the intraday move. Falls back to EOD
  NAV automatically if the live source is unreachable.
- **A gated snapshot** that only sends on a fresh tier crossing, a risk/cross banner, a notable
  daily/weekly move, or a periodic reminder — plus an optional **cutoff-sequence** of re-runs for
  markets with a daily mutual-fund NAV cutoff (alert only on *fresh* movement vs the day's anchor).
- **Sentiment & trend overlays** — RISK-ON/RISK-OFF banners (CNN Fear & Greed + India MMI), a
  golden/death cross detector, and a "Mood index" with fear/greed fill-bars and trend arrows.
- **Production hygiene** — atomic, crash-safe JSON state; pooled HTTP connections; once-a-day caching
  of the heavy history pulls; HTML-escaped output (a stray `<` in a headline can't break a message);
  and a `namedtuple` row type for self-documenting, refactor-safe access.

## How the signal model works

Each instrument is scored on two sides every run:

- **BUY** = drawdown from its rolling 1-year peak, in tiers (e.g. −15 / −22 / −30%). "Accumulate on
  the dip." Labelled `DEPLOY` in the alert.
- **SELL** = % stretch above a moving average, in tiers — the fast **50DMA** for trim/profit-booking
  or the slow **200DMA** for froth. "Trim into strength."

The two never collide (a deep drawdown is never a +froth state). An escalate-only state machine with
a hysteresis gap between the trigger band and the re-arm level means slow drift never trips it — only
a genuine move does, and never twice at the same tier.

## Quick start

Requires **Python 3.9+** (uses the stdlib `zoneinfo`).

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in TELEGRAM_TOKEN + TELEGRAM_CHAT_ID
set -a; . ./.env; set +a      # load creds into the environment

python market_alerts.py --test       # delivery ping
python market_alerts.py --digest      # force the full snapshot now
python market_alerts.py --calibrate   # dry-run: print current DMA distances (no Telegram)
```

Create a Telegram bot with [@BotFather](https://t.me/BotFather) for the token; add the bot to your
chat/group and use that chat id (group ids are negative).

## Configuration

Everything is in the dicts near the top of `market_alerts.py`:

- `INDIA_IDX`, `US_IDX` — index instruments (live).
- `HOLDINGS` — optional EOD mutual-fund / ETF positions (mfapi.in NAV).

Each entry sets a data `src` (`yf` Yahoo symbol / `nse` live index / `mf` fund scheme code) and
optional `buy` / `sell` ladders, `sell_ref` (`d50` or `d200`), `wide` (looser bands for high-beta
names), `range_days` (peak lookback), and `critical` (fetch-failure alert). Thresholds (`BUY_DD`,
`SELL_STR`, `RISK_ON`, `SNAP_DAILY_*`, `WEEK_*`, …) are named constants — tune freely.

## Deployment

It is a plain script — schedule it with `cron`. Example (IST), sending a gated full snapshot during
the Indian session and an alert-only US run hourly:

```cron
59 13 * * 1-5  set -a; . $HOME/.env; set +a; cd ~/market-extremes-alerter && python3 market_alerts.py --scope all >> run.log 2>&1
30 13-22 * * 1-5  set -a; . $HOME/.env; set +a; cd ~/market-extremes-alerter && python3 market_alerts.py --scope us >> run.log 2>&1
```

For markets with a mutual-fund NAV cutoff, add the optional `--recheck` / `--cutoff` re-runs a few
minutes before the cutoff.

## CLI

| Flag | Purpose |
|---|---|
| `--scope all` | Full snapshot (sends only if noteworthy) |
| `--scope us` | US-only, alert-only |
| `--recheck` / `--cutoff` | Cutoff re-runs: fresh-move checks vs the day's anchor |
| `--digest` | Force the full snapshot now |
| `--calibrate` | Dry-run: print current DMA distances (no Telegram) |
| `--test` / `--demo` | Delivery ping / sample message |

## Data sources

Yahoo Finance (yfinance), NSE `allIndices`, [mfapi.in](https://www.mfapi.in/) (mutual-fund NAV),
CNN Fear & Greed, Tickertape Market Mood Index, and Google News RSS — all public, keyless endpoints.

## Tests

No network or credentials required — the module imports cleanly (`yfinance` is loaded lazily, only at
fetch time), so the signal engine is unit-tested in isolation:

​```bash
python -m unittest discover -s tests -v
​```

## Known limitations & future work

Where the happy path ends — these are deliberate scope choices for a personal, low-noise alerter,
each with a clear path forward, not oversights:

- **No file locking on the state file.** Overlapping cron runs could race `alert_state.json`. The
  atomic write (temp file + `os.replace`) prevents a *corrupt* file but not a lost update. In
  practice the cron slots don't overlap; the proper fix is an `fcntl` advisory lock around the run.
- **Configuration lives in the source** (`INDIA_IDX` / `US_IDX` / `HOLDINGS` as Python dicts). Fine
  for a single owner and keeps it dependency-light, but externalizing to a `config.yaml` would let
  others reuse it without touching Python — the right move if it grows past one user.
- **No backtesting / historical replay.** The thresholds are reasoned and hand-tuned, not validated
  against history. A `--backtest` mode that replays the last N days of closes and prints which
  alerts *would* have fired is the natural next step — and the right way to calibrate the bands
  objectively rather than by intuition.
- **Yahoo Finance is delayed (~15 min) and unofficial**, and the NSE endpoint, while closer to
  real-time, is also unofficial. The tool targets daily redeem/invest/nothing decisions, not
  execution timing, so the latency is acceptable — but it is a real constraint, not free real-time.
- **Best-effort, keyless data sources.** Any source (Yahoo, NSE, mfapi, CNN, MMI, Google News) can
  rate-limit or change shape; failures degrade gracefully (a missing source is skipped, NSE falls
  back to EOD NAV) rather than crashing — but there are no uptime guarantees.
- **Single asset-class focus** (Indian + US equity indices); other asset classes would need new
  data adapters.

## Disclaimer

Not financial advice. "Stretched / oversold" are statistical signals, not recommendations. The
project is a personal engineering exercise in low-noise, high-signal alerting.

## License

Apache License 2.0 — see [LICENSE](LICENSE). Reuse is welcome; the license requires
keeping the copyright/attribution notice (see [NOTICE](NOTICE)), so credit travels with the code.
