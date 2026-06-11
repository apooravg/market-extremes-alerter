#!/usr/bin/env python3
# Copyright 2026 Apoorav Gupta
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. See the LICENSE file, or
# http://www.apache.org/licenses/LICENSE-2.0
"""
Market extremes alerter -> Telegram.

A single-file, zero-infra market monitor that pings a Telegram chat ONLY when something is
actionable — a deep dip to accumulate, a froth top to trim, a notable daily/weekly move, a
sentiment regime change, or a golden/death cross. Silence is the normal state: this is a signal
detector, not a daily digest.

SIGNAL MODEL (two-sided, distance-from-moving-average — not fixed price levels)
- BUY  : drawdown from the rolling 1-year peak (accumulate on dips), tiered.
- SELL : stretch above a moving average (trim on froth) — the fast 50DMA or the slow 200DMA.
- An escalate-only state machine with hysteresis fires ~once per tier, so a months-long move
  never spams (one ping per tier, plus a single "back to normal").

OVERLAYS (context, surfaced only when relevant)
- RISK-ON / RISK-OFF banners from the CNN Fear & Greed + India MMI sentiment gauges.
- Golden / Death cross (50DMA vs 200DMA) — fires once on the crossing.
- A "Mood index" section: a fear/greed fill-bar + a week-trend arrow per market.

DATA SOURCES
- yfinance (Yahoo)   : index / ETF symbols (e.g. ^BSESN, QQQ) — delayed-intraday + 2y history.
- NSE allIndices     : live intraday spot for Indian indices; the DMA/peak are borrowed from a
                       tracking index-fund NAV and scaled to index points (NSE returns spot only),
                       with an EOD-NAV fallback if NSE is unreachable.
- mfapi.in           : EOD NAV for a mutual fund / ETF, pinned by scheme code.

CONFIG is the dicts below (INDIA_IDX / US_IDX / HOLDINGS). Credentials come from the environment
(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID) — see .env.example. Times in comments are IST; tune freely.

OPTIONAL CUTOFF SEQUENCE (for markets with a daily mutual-fund NAV cutoff): a few re-runs near the
cutoff that alert only on FRESH movement vs the day's anchor, so a standing position never re-pings.

USAGE
    python market_alerts.py --scope all    # full snapshot (sends only if something is noteworthy)
    python market_alerts.py --scope us     # US-only, alert-only
    python market_alerts.py --recheck      # cutoff re-run: late-dip check vs the day's anchor
    python market_alerts.py --cutoff       # cutoff re-run: fresh rally/drop check
    python market_alerts.py --digest       # force the full snapshot now
    python market_alerts.py --calibrate    # dry-run: print current DMA distances (no Telegram)
    python market_alerts.py --test         # delivery ping
"""
import json
import os
import sys
import time
import datetime as dt
from zoneinfo import ZoneInfo

import html
from collections import namedtuple

import requests

# One snapshot/alert line item. namedtuple = self-documenting (r.side, r.eod, ...) and still
# index/unpack compatible, so existing positional unpacks keep working.
Row = namedtuple("Row", "glabel is_us snap name m side tier fire dmv crit eod nudge")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")      # required — Telegram bot token from @BotFather
CHAT_ID        = os.environ.get("TELEGRAM_CHAT_ID")    # required — target chat/group id (groups are negative)
FINNHUB_TOKEN  = os.environ.get("FINNHUB_TOKEN", "")    # optional: real-time US quote (Finnhub /quote, free IEX)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert_state.json")
DIGEST_DATES = []

IST = ZoneInfo("Asia/Kolkata")
ET = ZoneInfo("America/New_York")

# ---- what we watch --------------------------------------------------------
# Each instrument has a data source and optional alert ladders (% thresholds):
#   src "yf"  : Yahoo Finance symbol (e.g. ^BSESN, QQQ).
#   src "nse" : NSE allIndices live spot; the 50/100/200DMA + 1y range are borrowed from a tracking
#               index fund's NAV (mfapi.in `dma_code`) and scaled to index points. EOD-NAV fallback.
#   src "mf"  : mfapi.in EOD NAV for a fund / ETF, pinned by scheme `code`.
# Ladders: buy = drawdown-from-1y-peak tiers (accumulate on dips); sell = stretch above a moving
# average (trim on froth); sell_ref "d50" uses the fast 50DMA, else the 200DMA. wide=True loosens
# the froth/daily bands for high-beta names. The example config below tracks public market indices.
INDIA_IDX = {
    # DEPLOY (buy) only on a deep dip off the 1y peak; TRIM (sell) on a genuine stretch. The two
    # never collide (a deep drawdown is never a +froth 50DMA).
    "SENSEX":            {"src": "yf", "symbol": "^BSESN", "wide": False, "critical": True,
                          "buy": [-0.15, -0.22, -0.30], "buy_rearm": -0.10, "buy_label": "DEPLOY"},
    "Nifty LMC250":      {"src": "nse", "nse_name": "NIFTY LARGEMIDCAP 250", "dma_code": 152482,
                          "sell_ref": "d50",
                          "buy": [-0.15, -0.22, -0.30], "buy_rearm": -0.10, "buy_label": "DEPLOY",
                          "sell": [0.070, 0.090, 0.110], "sell_rearm": 0.040, "daily": 0.010},
    # Smallcaps are higher-beta -> deeper deploy band; wide=True loosens froth/daily.
    "Nifty Smallcap250": {"src": "nse", "nse_name": "NIFTY SMALLCAP 250", "dma_code": 148519,
                          "sell_ref": "d50", "wide": True,
                          "buy": [-0.20, -0.28, -0.36], "buy_rearm": -0.15, "buy_label": "DEPLOY",
                          "sell": [0.080, 0.110, 0.140], "sell_rearm": 0.050, "daily": 0.010},
}
US_IDX = {
    # Broad US-tech barometer. Buy dips sensitive; froth-sell only at a genuine top (+22/+30 vs 200DMA).
    "Nasdaq100 (QQQ)":   {"src": "yf", "symbol": "QQQ", "finnhub": "QQQ", "wide": True, "critical": True,
                          "sell": [0.22, 0.30], "sell_rearm": 0.15},
}
# Alert-only watch list (never shown in the snapshot; pings only on a tier cross). Empty by default.
FUNDS = {}

# Optional EOD mutual-fund / ETF holdings (mfapi.in NAV, pinned by scheme `code`). Shown at the end
# of the snapshot. Empty by default -- add your own. range_days widens the peak/trough lookback for
# cyclical names whose real high is older than 52 weeks. Example:
#   "My Smallcap Fund": {"src": "mf", "code": 148519, "eod": True, "range_days": 756,
#                        "sell_ref": "d50", "sell": [0.08, 0.11, 0.14], "sell_rearm": 0.05},
HOLDINGS = {}

SHORT = {"Nifty LMC250": "Nifty Large & Midcap 250", "Nifty Smallcap250": "Nifty Smallcap 250",
         "Nasdaq100 (QQQ)": "Nasdaq100"}
# Snapshot layout: groups separated by a delimiter line.
DISPLAY_GROUPS = [["SENSEX", "Nasdaq100 (QQQ)"],
                  ["Nifty LMC250", "Nifty Smallcap250"]]
DELIM = "\u2014" * 10
# Levels where the absolute number is not meaningful (index points / proxy NAV) -> show % move only.
NO_VALUE = {"Nasdaq100 (QQQ)", "Nifty LMC250", "Nifty Smallcap250"}
# Trim set: names that get the 50/200DMA context line, the snapshot "standing opportunity" check,
# and the pre-cutoff rally trigger.
BOOK_NAMES = {"Nifty LMC250", "Nifty Smallcap250"}
# Deploy-the-dip targets: staged BUY on a drawdown from the 1y peak, surfaced in the snapshot's
# "Deploy signal" section.
DEPLOY_NAMES = {"SENSEX", "Nifty LMC250", "Nifty Smallcap250"}
# Golden/Death cross watch (50DMA vs 200DMA) on the trend barometers. Fires once on the crossing.
CROSS_NAMES = {"SENSEX", "Nasdaq100 (QQQ)", "Nifty LMC250", "Nifty Smallcap250"}

# ---- bands (defaults; per-instrument keys override these) -----------------
# BUY = drawdown from the 1y peak (accumulate on dips). SELL = stretch above the moving average
# (trim on froth). wide=True loosens the SELL froth bands + the daily-move threshold for high-beta
# names. Bias on the buy side: a false dip alarm is cheaper than a missed one.
BUY_DD     = [-0.10, -0.18, -0.28]   # pullback / accumulation / deep value
BUY_REARM  = -0.07                   # recover past this -> buy state resets (hysteresis)
SELL_STR,   SELL_STR_WIDE   = [0.12, 0.18], [0.18, 0.27]
SELL_REARM, SELL_REARM_WIDE = 0.08, 0.12
DAILY_MOVE, DAILY_MOVE_WIDE = 0.016, 0.025
# Overnight (after the local market close): wake only for a big single-day move, asymmetric -- a
# downside shock is the actionable one; an upside rip needs to be bigger before it is worth a ping.
LATE_DOWN = 0.04
LATE_UP   = 0.06

# FYI / awareness tier -- a gentle "the market moved" notice that kills FOMO without implying a
# trade. Asymmetric (a drop nags sooner than a rip), once/day/side, never double-fires with an
# actionable alert, self-caps ~11pm IST so it does not run deep into the overnight session.
INFO_DOWN   = 0.020
INFO_UP     = 0.025
INFO_CUTOFF = dt.time(23, 5)
INFO_NAMES  = {"Nasdaq100 (QQQ)", "SENSEX", "Nifty LMC250", "Nifty Smallcap250"}

# "Everything's hot" / capitulation master flags: both sentiment gauges + smallcap-vs-200DMA stretch.
RISK_ON  = {"cnn": 80, "mmi": 75, "smallcap_d200": 0.15}
RISK_OFF = {"cnn": 20, "mmi": 30, "smallcap_d200": -0.10}

# Cutoff sequence (for markets with a daily mutual-fund NAV cutoff). The re-runs compare live values
# to the day's anchor snapshot and alert ONLY on fresh movement, never repeating a standing position:
#   recheck : a DROP >= RECHECK_DROP since the anchor (a late dip before the buy cutoff).
#   cutoff  : a name that RALLIED >= CUTOFF_RISE while in a sell tier, or a DROP >= CUTOFF_BUY_DROP.
RECHECK_DROP    = 0.006
CUTOFF_RISE     = 0.007
CUTOFF_BUY_DROP = 0.009

# The main snapshot is NOT a daily digest: it sends only when something is fresh (a tier crossing,
# a risk banner, a cross, or a fetch failure), a standing opportunity is due (~every REMIND_DAYS),
# or a notable same-day / multi-day move occurs. Otherwise it records state silently.
REMIND_DAYS = 7
# Same-day move trigger on a live index -- asymmetric (a fall is the more actionable one).
SNAP_DAILY_DOWN = 0.020
SNAP_DAILY_UP   = 0.025
# Multi-day (~5 session) move trigger -- catches a slow bleed / rally that no single day tripped.
WEEK_DOWN = 0.030
WEEK_UP   = 0.045

CRITICAL_HINT = "source may have changed — check the data source"

ENABLE_HEADLINES = True   # market news via Google News RSS. If it's flaky/noisy on the VM, set False.
HEAD_KEYWORDS = ["fed", "rate", "rbi", "inflation", "cpi", "tariff", "oil", "crude", "war",
                 "nvidia", "semiconductor", "jobs", "yield", "recession", "nifty", "sensex",
                 "nasdaq", "rupee", "fii"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SESSION = requests.Session()        # pool + reuse TLS connections across same-host calls (mfapi x5)
SESSION.headers.update(UA)


# ---- state / telegram -----------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError, OSError) as ex:
        # corrupt / half-written state (e.g. killed mid-write) -> start clean instead of crashing
        # every run. State is just dedup memory; worst case is one duplicate alert.
        print("   [state] unreadable (%s) -> resetting" % ex)
        return {}

def save_state(state):
    # atomic: write a temp file then replace, so a crash/reboot mid-write can't corrupt it.
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)

def resolve_chat_id():
    # This system delivers to a Telegram GROUP. Group/supergroup ids are negative.
    # A positive id is a personal DM -> refuse it so a bad env/config can never
    # silently route alerts to one person instead of the group.
    cid = str(CHAT_ID).strip() if CHAT_ID else ""
    if cid:
        if not cid.lstrip("-").isdigit() or not cid.startswith("-"):
            sys.exit("Refusing to send: TELEGRAM_CHAT_ID=%r is not a group id "
                     "(group ids are negative). Set the group id." % cid)
        return cid
    sys.exit("No TELEGRAM_CHAT_ID set. Refusing getUpdates auto-discovery "
             "(it can resolve to a personal DM). Set the group id explicitly.")

def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        sys.exit("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID (see .env.example).")
    if len(text) > 4096:                 # Telegram hard cap -> degrade instead of 400-failing
        text = text[:4090] + "\n\u2026"
    payload = {"chat_id": resolve_chat_id(), "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    last = None
    for attempt in range(3):   # a transient Telegram failure must not eat an alert: retry, then raise
        try:
            r = SESSION.post("https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_TOKEN,
                             json=payload, timeout=20)
            r.raise_for_status()
            return
        except requests.RequestException as ex:
            sc = getattr(getattr(ex, "response", None), "status_code", None)
            if sc is not None and 400 <= sc < 500:
                raise          # config/formatting error (bad HTML, bad chat id) - retrying won't help
            last = ex
            print("   [tg] send fail (try %d): %s" % (attempt + 1, ex))
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    raise last

def notify_failure(text):
    try:
        send_telegram("⚠️ <b>market_alerts problem</b>\n" + text)
    except Exception:
        pass


# ---- time -----------------------------------------------------------------
def now_ist(): return dt.datetime.now(IST)
def now_et():  return dt.datetime.now(ET)
def today_str(): return now_ist().date().isoformat()
def stamp(): return now_ist().strftime("%a %d %b %-I:%M%p IST")

def within_us_window():
    """Pre-market (4:00 ET) through the regular close (16:00 ET), weekdays. The hourly cron is
    generous; this guard is the real gate, so US daylight-saving never breaks it."""
    e = now_et()
    return e.weekday() < 5 and dt.time(4, 0) <= e.time() <= dt.time(16, 0)

def us_phase():
    """'day' until 10:30pm IST (normal thresholds), then 'late' until the US close — overnight,
    so only a big single-day move (>=3% either side) is worth waking you for."""
    i = now_ist().time()
    return "day" if dt.time(13, 0) <= i <= dt.time(22, 30) else "late"

def within_india_window():
    """India alert runs gate: weekdays, 9am IST (pre-open) through ~midnight (so the evening
    run still catches the day's NAV after funds publish). The hourly cron is generous; this is
    the real gate."""
    i = now_ist()
    return i.weekday() < 5 and dt.time(9, 0) <= i.time() <= dt.time(23, 59)


# ---- data -----------------------------------------------------------------
def yf_closes(symbol):
    import yfinance as yf
    h = yf.Ticker(symbol).history(period="2y", auto_adjust=True)
    return [float(x) for x in h["Close"].dropna().tolist()] if not h.empty else []

def yf_stats(symbol):
    """Slow-moving daily figures (200DMA, 1y high/low, last close). Cached once per day —
    they don't change intraday, so the hourly runs reuse them instead of re-pulling 2y history."""
    closes = [c for c in yf_closes(symbol) if c is not None]
    if len(closes) < 2:
        return None
    n = len(closes)
    w = closes[-252:]
    return {"d50": (sum(closes[-50:]) / min(50, n) if n >= 20 else None),
            "d100": (sum(closes[-100:]) / min(100, n) if n >= 50 else None),
            "d200": (sum(closes[-200:]) / min(200, n) if n >= 100 else None),
            "peak": max(w), "trough": min(w), "lastclose": closes[-1],
            "ref5": (closes[-6] if n >= 6 else None)}

def finnhub_quote(symbol):
    """Real-time US quote via Finnhub /quote (free, IEX). Returns (live, prev) or (None, None) so
    the caller can fall back to yfinance. c=current price, pc=previous close; c==0 => no data."""
    if not FINNHUB_TOKEN:
        return None, None
    try:
        d = SESSION.get("https://finnhub.io/api/v1/quote",
                        params={"symbol": symbol, "token": FINNHUB_TOKEN}, timeout=15).json()
        c, pc = d.get("c"), d.get("pc")
        if c and pc:
            return float(c), float(pc)
    except Exception as ex:
        print("   [finnhub] %s quote fail: %s" % (symbol, ex))
    return None, None

def yf_live(symbol):
    import yfinance as yf
    t = yf.Ticker(symbol)
    live = prev = None
    try:
        fi = t.fast_info
        live = fi.get("last_price") or fi.get("lastPrice")
        prev = (fi.get("previous_close") or fi.get("previousClose")
                or fi.get("regular_market_previous_close"))
    except Exception:
        pass
    if live is None:
        try:
            h = t.history(period="1d", interval="1m", prepost=True)
            if not h.empty:
                live = float(h["Close"].dropna().iloc[-1])
        except Exception:
            pass
    return (float(live) if live else None, float(prev) if prev else None)

STALE_NAV_DAYS = 7   # a fund NAV older than this = feed frozen / scheme merged -> surface it
_STALE_NAVS = []     # collected per run (module-global: one run per process)

def _note_stale(name, last_date):
    """Record a stale-NAV fund so the snapshot can warn (weekly nag per fund, see run())."""
    if not last_date:
        return
    try:
        d = dt.datetime.strptime(last_date, "%d-%m-%Y").date()
    except ValueError:
        return
    if (now_ist().date() - d).days > STALE_NAV_DAYS:
        s = "%s (last NAV %s)" % (name, last_date)
        if s not in _STALE_NAVS:
            _STALE_NAVS.append(s)

def _navs_for_code(code):
    h = SESSION.get("https://api.mfapi.in/mf/%s" % code, headers=UA, timeout=20).json()
    navs, last_date = [], None
    for x in h.get("data", []):          # mfapi returns newest-first
        try:
            navs.append(float(x["nav"]))
            if last_date is None:
                last_date = x.get("date")        # newest NAV's date (dd-mm-YYYY)
        except (ValueError, KeyError):
            pass
    navs.reverse()  # oldest-first
    return navs, h.get("meta", {}).get("scheme_name", ""), last_date

def mf_closes(c):
    """Resolve a fund robustly. Returns (navs_oldest_first, resolved_name, code)."""
    if c.get("code"):
        navs, name, last_date = _navs_for_code(c["code"])
        _note_stale(name or str(c["code"]), last_date)
        return navs, name, c["code"]
    must = [t.lower() for t in c.get("must", [])]
    avoid = [t.lower() for t in c.get("avoid", [])]
    s = SESSION.get("https://api.mfapi.in/mf/search", params={"q": c["query"]},
                     headers=UA, timeout=20).json()
    def ok(name):
        n = name.lower()
        return all(t in n for t in must) and not any(t in n for t in avoid)
    cands = [it for it in s if ok(it.get("schemeName", ""))] or s
    def score(it):
        n = it.get("schemeName", "").lower()
        sc = sum(1 for w in c["query"].lower().split() if w in n)
        sc += 3 if "direct" in n else 0
        sc += 2 if "growth" in n else 0
        sc -= 4 if "regular" in n else 0
        sc -= 4 if ("idcw" in n or "dividend" in n or "income" in n) else 0
        return sc
    best = max(cands, key=score, default=None)
    if not best:
        return [], None, None
    navs, _, last_date = _navs_for_code(best["schemeCode"])
    _note_stale(best["schemeName"], last_date)
    return navs, best["schemeName"], best["schemeCode"]


_NSE = {}
def nse_index(name):
    """Live spot for an NSE index (allIndices). One HTTP call per run, module-cached (NOT
    persisted across runs). Returns the index row dict or None. NSE needs a cookie handshake
    + a browser UA, so we hit the homepage first to seed cookies."""
    if "data" not in _NSE:
        for attempt in range(2):   # NSE intermittently 401s from cloud IPs -> one retry
            try:
                sess = requests.Session()
                sess.headers.update({"User-Agent": UA["User-Agent"], "Accept": "application/json",
                                     "Accept-Language": "en-US,en;q=0.9"})
                sess.get("https://www.nseindia.com", timeout=20)
                r = sess.get("https://www.nseindia.com/api/allIndices", timeout=20); r.raise_for_status()
                _NSE["data"] = {x["index"]: x for x in r.json().get("data", [])}
                break
            except Exception as ex:
                print("   [nse] fetch fail (try %d): %s" % (attempt + 1, ex)); _NSE["data"] = {}
    return _NSE["data"].get(name)



# ---- sentiment ------------------------------------------------------------
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def _num(x):
    """Coerce a value that may be a scalar OR a dict like {'value':..} / {'indicator':..}."""
    if isinstance(x, dict):
        x = x.get("value", x.get("indicator", x.get("currentValue")))
    return _f(x)

def mmi_zone(v):
    if v <= 30: return "Extreme Fear"
    if v <= 50: return "Fear"
    if v <= 70: return "Greed"
    return "Extreme Greed"

def cnn_zone(v):
    if v < 25:  return "Extreme Fear"
    if v < 45:  return "Fear"
    if v <= 55: return "Neutral"
    if v <= 75: return "Greed"
    return "Extreme Greed"

def fetch_sentiment():
    out = {"cnn": None, "mmi": None}
    try:
        d = SESSION.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                         headers=UA, timeout=20).json()["fear_and_greed"]
        out["cnn"] = {"now": round(d["score"]), "label": d["rating"].title(),
                      "week": round(d["previous_1_week"]) if d.get("previous_1_week") else None}
    except Exception:
        pass
    try:
        d = SESSION.get("https://api.tickertape.in/mmi/now", headers=UA, timeout=20).json()["data"]
        now = _f(d.get("currentValue", d.get("indicator")))
        if now is not None:
            wk = _num(d.get("lastWeek"))
            if wk is None:
                wk = _num(d.get("lastDay"))   # fall back to yesterday so the India arrow still shows
            out["mmi"] = {"now": now, "label": mmi_zone(now), "week": wk}
    except Exception:
        pass
    return out

def _mood_bar(v, cells=10):
    """0-100 fear/greed as a fill bar (empty = extreme fear, full = extreme greed). Replaces the
    bare number (found unintuitive)."""
    f = max(0, min(cells, int(round((v / 100.0) * cells))))
    return "\u25b0" * f + "\u25b1" * (cells - f)

def _mood_trend(now, week):
    """Direction arrow for the week-over-week drift — the intuitive bit, without the raw +N."""
    if week is None:
        return ""
    d = now - week
    if d >= 2:  return " \u2197"   # rising
    if d <= -2: return " \u2198"   # easing
    return " \u2192"               # ~flat

def sentiment_line(sent):
    """A small 'Mood index' section: a sub-heading + one flagged line each for India and US,
    each a fear/greed fill-bar + week-trend arrow (no bare number)."""
    rows = []
    if sent.get("mmi"):
        m = sent["mmi"]
        rows.append("🇮🇳 %s %s%s" % (m["label"], _mood_bar(m["now"]), _mood_trend(m["now"], m.get("week"))))
    if sent.get("cnn"):
        c = sent["cnn"]
        rows.append("🇺🇸 %s %s%s" % (c["label"], _mood_bar(c["now"]), _mood_trend(c["now"], c.get("week"))))
    return "🧭 <b>Mood index</b>\n" + "\n".join(rows) if rows else ""

def sentiment_transition(sent, state):
    notes, cur = [], state.setdefault("_sent", {})
    if sent.get("cnn"):
        z, pz = cnn_zone(sent["cnn"]["now"]), cur.get("cnn")
        if pz and pz != z:
            notes.append("US sentiment %s \u2192 %s" % (pz, z))
        cur["cnn"] = z
    if sent.get("mmi"):
        z, pz = sent["mmi"]["label"], cur.get("mmi")
        if pz and pz != z:
            notes.append("India MMI %s \u2192 %s" % (pz, z))
        cur["mmi"] = z
    return notes

def fetch_headlines(max_n=2):
    if not ENABLE_HEADLINES:
        return []
    try:
        import xml.etree.ElementTree as ET_
        url = "https://news.google.com/rss/search?q=stock%20market%20when:1d&hl=en-IN&gl=IN&ceid=IN:en"
        r = SESSION.get(url, headers=UA, timeout=20); r.raise_for_status()
        root = ET_.fromstring(r.content)
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if any(k in title.lower() for k in HEAD_KEYWORDS):
                out.append(html.escape(title))   # escape & < > so HTML parse_mode never 400s
            if len(out) >= max_n:
                break
        return out
    except Exception:
        return []


# ---- metrics / engine -----------------------------------------------------
def metrics(closes, live=None, prev=None, rng=252):
    closes = [c for c in closes if c is not None]
    if not closes:
        return None
    n = len(closes)
    d50  = sum(closes[-50:]) / min(50, n) if n >= 20 else None
    d100 = sum(closes[-100:]) / min(100, n) if n >= 50 else None
    d200 = sum(closes[-200:]) / min(200, n) if n >= 100 else None
    window = closes[-rng:]
    peak, trough = max(window), min(window)
    if live is not None:
        px, ref = live, (prev if prev else (closes[-2] if n >= 2 else closes[-1]))
    else:
        px, ref = closes[-1], (closes[-2] if n >= 2 else closes[-1])
    m = _metrics(d50, d100, d200, peak, trough, px, ref)
    allpeak = max(closes) if closes else None       # full-history (all-time) peak
    m["dd_ath"] = (px / allpeak - 1) if allpeak else None
    m["wk"] = (px / closes[-6] - 1) if n >= 6 else None   # ~5-trading-day change
    return m

def metrics_yf(stats, live, prev):
    """Build metrics from cached daily stats + a fresh live/prev (the only intraday-varying bits)."""
    px = live if live is not None else stats["lastclose"]
    # both live+prev missing -> px IS lastclose; a fake "0.0% today" misleads -> daily n/a instead
    ref = prev if prev else (stats["lastclose"] if live is not None else None)
    m = _metrics(stats.get("d50"), stats.get("d100"), stats["d200"], stats["peak"], stats["trough"], px, ref)
    m["wk"] = (px / stats["ref5"] - 1) if stats.get("ref5") else None
    return m

def _metrics(d50, d100, d200, peak, trough, px, ref):
    return dict(px=px, d50=d50, d100=d100, d200=d200, peak=peak, trough=trough,
                dist50=(px / d50 - 1) if d50 else None,
                dist100=(px / d100 - 1) if d100 else None,
                dist200=(px / d200 - 1) if d200 else None,
                drawdown=(px / peak - 1) if peak else None,
                runup=(px / trough - 1) if trough else None,
                daily=(px / ref - 1) if ref else None)


def nav_levels(navs, rng=252):
    """EOD-cached levels from a fund's NAV history (DMA/peak/trough). Refreshed once a day."""
    navs = [c for c in navs if c is not None]
    if not navs:
        return None
    n = len(navs)
    window = navs[-rng:]
    return {"d50":  sum(navs[-50:]) / min(50, n) if n >= 20 else None,
            "d100": sum(navs[-100:]) / min(100, n) if n >= 50 else None,
            "d200": sum(navs[-200:]) / min(200, n) if n >= 100 else None,
            "peak": max(window), "trough": min(window), "allpeak": max(navs),
            "latest": navs[-1], "prev": navs[-2] if n >= 2 else navs[-1],
            "ref5": (navs[-6] if n >= 6 else None), "n": n}

def metrics_nse(levels, live, prev):
    """Hybrid metrics: live index spot (NSE) + NAV-derived DMA/peak/trough scaled into index
    points by k = prev_index / latest_nav (both ~ last close). The % distances are then the
    index's own, now carrying today's intraday move. (Tiny expense-ratio drift in k is < ~0.2%
    over the DMA window — negligible vs the bands.)"""
    nav_latest = levels.get("latest")
    if not nav_latest or not prev:
        return None
    k = prev / nav_latest
    sc = lambda v: v * k if v is not None else None
    m = _metrics(sc(levels.get("d50")), sc(levels.get("d100")), sc(levels.get("d200")),
                 sc(levels.get("peak")), sc(levels.get("trough")), live, prev)
    ref5 = levels.get("ref5")
    m["wk"] = ((nav_latest / ref5) * (live / prev) - 1) if (ref5 and prev) else None
    ap = levels.get("allpeak")
    m["dd_ath"] = (live / (ap * k) - 1) if ap else None   # ATH context (the NAV history carries it)
    return m

def resolve_bands(c):
    """BUY bands are uniformly sensitive (the actionable side). wide=True only loosens the
    SELL froth bands + daily threshold for high-beta names. Any band can be overridden per
    instrument with buy/sell/daily/buy_rearm/sell_rearm keys."""
    wide = c.get("wide", False)
    return dict(
        buy   = c.get("buy",        BUY_DD),
        sell  = c.get("sell",       SELL_STR_WIDE if wide else SELL_STR),
        brearm= c.get("buy_rearm",  BUY_REARM),
        srearm= c.get("sell_rearm", SELL_REARM_WIDE if wide else SELL_REARM),
        daily = c.get("daily",      DAILY_MOVE_WIDE if wide else DAILY_MOVE),
    )

def classify(sell_dist, drawdown, bands):
    """BUY off drawdown-from-1y-peak; SELL off whichever DMA distance is passed in (200DMA
    for most, 50DMA for the trim candidates). Variable number of tiers each side."""
    buy, sell = bands["buy"], bands["sell"]
    if drawdown is not None:
        for i in range(len(buy) - 1, -1, -1):
            if drawdown <= buy[i]:
                return ("buy", i + 1)
    if sell_dist is not None:
        for i in range(len(sell) - 1, -1, -1):
            if sell_dist >= sell[i]:
                return ("sell", i + 1)
    return (None, 0)

def past_rearm(side, sell_dist, drawdown, bands):
    if side == "buy":
        return drawdown is not None and drawdown > bands["brearm"]
    if side == "sell":
        return sell_dist is not None and sell_dist < bands["srearm"]
    return True

def step_state(name, side, tier, sell_dist, drawdown, bands, state):
    prev = state.get(name, {"side": None, "tier": 0})
    if prev["side"] is None:
        if side is not None:
            state[name] = {"side": side, "tier": tier}; return "enter"
        state[name] = prev; return None
    if side is not None and side != prev["side"]:
        state[name] = {"side": side, "tier": tier}; return "flip"
    if side == prev["side"]:
        if tier > prev["tier"]:
            state[name] = {"side": side, "tier": tier}; return "escalate"
        state[name] = prev; return None
    if past_rearm(prev["side"], sell_dist, drawdown, bands):
        state[name] = {"side": None, "tier": 0}; return "recover"
    state[name] = prev; return None

def daily_move_alert(name, daily, thr, side, tier, dstate, today):
    if daily is None or abs(daily) < thr:
        return False
    mv = "down" if daily < 0 else "up"
    if side == "buy" and tier >= 2 and mv == "down":
        return False
    if side == "sell" and tier >= 2 and mv == "up":
        return False
    rec = dstate.get(name, {})
    if rec.get("date") == today and rec.get("side") == mv:
        return False
    dstate[name] = {"date": today, "side": mv}
    return True

def _late_big(daily):
    """Overnight: asymmetric threshold — +5% up (FYI) / -3% down (actionable)."""
    return daily >= LATE_UP or daily <= -LATE_DOWN

def big_move(name, daily, state, etdate):
    """Overnight big-move dedup. Keyed on the US (ET) trading date so the post-midnight IST
    rollover doesn't re-fire the same session's move. Fires once per side per US session."""
    mv = "down" if daily < 0 else "up"
    d = state.setdefault("_late", {})
    rec = d.get(name, {})
    if rec.get("date") == etdate and rec.get("side") == mv:
        return False
    d[name] = {"date": etdate, "side": mv}
    return True


# ---- formatting -----------------------------------------------------------
def pct(x): return ("%+.1f%%" % (x * 100)) if x is not None else "n/a"

def fmt_level(v):
    if v is None: return "n/a"
    return "{:.0f}".format(v) if abs(v) >= 1000 else "%.2f" % v

def short(name): return SHORT.get(name, name)

def range_line(m):
    """52-week position as a fill bar (empty = at 1y low, full = at 1y high) + exact distances.
    Falls back to plain text if peak/trough aren't available."""
    peak, trough, px = m.get("peak"), m.get("trough"), m.get("px")
    dd, ru = m.get("drawdown"), m.get("runup")
    hi = "at 1y high" if (dd is not None and dd >= -0.005) else ("%s below high" % pct(dd) if dd is not None else None)
    lo = "at 1y low"  if (ru is not None and ru <= 0.005) else ("%s above low" % pct(ru) if ru is not None else None)
    txt = " \u00b7 ".join(x for x in (lo, hi) if x)
    if not (peak and trough and px) or peak <= trough:
        return txt
    f = max(0.0, min(1.0, (px - trough) / (peak - trough)))
    cells = 10; filled = int(round(f * cells))
    bar = "\u25b0" * filled + "\u25b1" * (cells - filled)   # filled / empty blocks
    return ("%s  %s" % (bar, txt)) if txt else bar

def snap_block(r):
    """Two lines per instrument: name (+ price only when meaningful) + move + direction,
    then range. 'at 1y high/low' instead of +0.0%."""
    glabel, is_us, snap, name, m, side, tier, fire, dmv, crit, eod, nudge = r
    flag = "🇺🇸" if is_us else "🇮🇳"
    arrow = "📈" if (m["daily"] is not None and m["daily"] >= 0) else "🔻"
    when = "EOD" if eod else "today"
    head = "%s <b>%s</b>" % (flag, short(name))
    if name not in NO_VALUE:
        head += "  %s" % fmt_level(m["px"])
    l1 = "%s   %s %s %s" % (head, pct(m["daily"]), when, arrow)
    ctx = range_line(m)
    if name in BOOK_NAMES:   # trim names: 50DMA (timing trigger) + 200DMA (strategic context)
        dparts = ["%s %s" % (lbl, pct(m[k])) for lbl, k in
                  (("50DMA", "dist50"), ("200DMA", "dist200")) if m.get(k) is not None]
        if dparts:
            ctx = (ctx + "\n" if ctx else "") + " \u00b7 ".join(dparts)
        if m.get("dd_ath") is not None:   # funds + NSE hybrids (NAV history carries the ATH)
            dd = m.get("drawdown")
            if dd is not None and abs(m["dd_ath"] - dd) < 0.001:
                ctx = ctx.replace("below high", "below all-time high")   # identical -> one line, ATH label
            else:
                ath = "at all-time high" if m["dd_ath"] >= -0.005 else "%s below all-time high" % pct(m["dd_ath"])
                ctx = (ctx + "\n" if ctx else "") + ath
    return l1, ctx, nudge


# ---- run ------------------------------------------------------------------
def fetch_one(c, group_is_us, cache):
    is_us = c.get("is_us", group_is_us) if group_is_us is not None else c.get("is_us", False)
    try:
        if c["src"] == "yf":
            sym = c["symbol"]
            cc = cache.get(sym)
            if not (cc and cc.get("date") == today_str() and cc.get("d200") is not None):
                st = yf_stats(sym)                 # heavy 2y pull — once per day
                if st and st.get("d200") is not None:
                    st["date"] = today_str(); cache[sym] = st; cc = st
                else:
                    cc = st                         # usable but not cached (no 200DMA yet)
            if cc is None:
                return None, c.get("eod", False), is_us
            live = prev = None
            fhsym = c.get("finnhub")
            if fhsym:                               # real-time (Finnhub/IEX) when a key is set
                live, prev = finnhub_quote(fhsym)
            if live is None:                        # no key / failure -> yfinance (~15-min delayed)
                live, prev = yf_live(sym)
            return metrics_yf(cc, live, prev), c.get("eod", False), is_us
        if c["src"] == "mf":
            navs, name, code = mf_closes(c)
            if name:
                print("   [mf] %s -> %s [%s]" % (c["query"][:28], name, code))
            return metrics(navs, None, None, c.get("range_days", 252)), True, is_us
        if c["src"] == "nse":
            code = c["dma_code"]
            ck = "navlv3_%s" % code   # v3 schema: + allpeak (ATH context)
            cc = cache.get(ck)
            if not (cc and cc.get("date") == today_str() and cc.get("levels")):
                navs, _, _ = mf_closes({"code": code})       # heavy NAV pull -> once per day
                lv = nav_levels(navs, c.get("range_days", 252))
                cache[ck] = {"date": today_str(), "levels": lv} if lv else {}
                cc = cache.get(ck)
            levels = (cc or {}).get("levels")
            if not levels:
                return None, False, is_us
            row = nse_index(c["nse_name"])               # live spot, every run
            if row is not None:
                try:
                    last = float(row["last"]); chg = float(row.get("percentChange") or 0.0)
                    prev = last / (1 + chg / 100.0)
                    return metrics_nse(levels, last, prev), False, is_us   # live intraday
                except Exception as ex:
                    print("   [nse] live parse fail: %s" % ex)
            # NSE unreachable -> EOD NAV fallback (~1 day lagged)
            mm = _metrics(levels.get("d50"), levels.get("d100"), levels.get("d200"),
                          levels.get("peak"), levels.get("trough"),
                          levels.get("latest"), levels.get("prev"))
            mm["wk"] = (levels["latest"] / levels["ref5"] - 1) if levels.get("ref5") else None
            return mm, True, is_us
    except Exception as ex:
        print("   fetch error: %s" % ex)
    return None, c.get("eod", False), is_us

def _days_since(iso):
    try:
        return (now_ist().date() - dt.date.fromisoformat(iso)).days
    except Exception:
        return 9999

def cross_events(rows, state):
    """50DMA vs 200DMA golden/death cross. Returns notes for FRESH crossings only (fire-once),
    remembering the last side per instrument in state["_cross"]. Used as a trend-shift signal."""
    cur, notes = state.setdefault("_cross", {}), []
    for r in rows:
        if r.name not in CROSS_NAMES or not r.m:
            continue
        d50, d200 = r.m.get("d50"), r.m.get("d200")
        if not d50 or not d200:
            continue
        sign = 1 if d50 >= d200 else -1
        prev = cur.get(r.name)
        cur[r.name] = sign
        if prev is not None and prev != sign:
            if sign > 0:
                notes.append("\u2728 <b>Golden cross</b> \u2014 %s 50DMA crossed ABOVE 200DMA (uptrend forming)" % short(r.name))
            else:
                notes.append("\u2694\ufe0f <b>Death cross</b> \u2014 %s 50DMA crossed BELOW 200DMA (downtrend forming)" % short(r.name))
    return notes

def _risk(rows, sent):
    """(risk_on, risk_off, banner_lines) from sentiment + smallcap 200DMA stretch."""
    by  = {r.name: r for r in rows}
    sc  = by.get("Nifty Smallcap250")
    scd = sc.m["dist200"] if (sc and sc.m and sc.m.get("dist200") is not None) else None
    cnn = sent["cnn"]["now"] if sent.get("cnn") else None
    mmi = sent["mmi"]["now"] if sent.get("mmi") else None
    on = off = False
    lines = []
    if cnn is not None and mmi is not None and scd is not None:
        if cnn > RISK_ON["cnn"] and mmi > RISK_ON["mmi"] and scd > RISK_ON["smallcap_d200"]:
            on = True
            lines.append("🔥 <b>RISK-ON / FROTH</b> — CNN %d \u00b7 MMI %.0f \u00b7 Smallcap %s vs 200DMA\n"
                         "prime booking window (poor forward returns from here)" % (cnn, mmi, pct(scd)))
        if cnn < RISK_OFF["cnn"] and mmi < RISK_OFF["mmi"] and scd < RISK_OFF["smallcap_d200"]:
            off = True
            lines.append("🧊 <b>RISK-OFF / OVERSOLD</b> — CNN %d \u00b7 MMI %.0f \u00b7 Smallcap %s vs 200DMA\n"
                         "poor time to redeem (capitulation zone — consider pausing)" % (cnn, mmi, pct(scd)))
    return on, off, lines

def weekly_movers(rows):
    """Live India indices whose ~5-day change crossed the slow-bleed / spike thresholds."""
    out = []
    for r in rows:
        if not r.snap or r.is_us or r.eod or not r.m:
            continue
        wk = r.m.get("wk")
        if wk is not None and (wk <= -WEEK_DOWN or wk >= WEEK_UP):
            out.append((r, wk))
    return sorted(out, key=lambda x: x[1])

def run(scope, force_snapshot=False, cutoff_mode=None):
    if scope == "us" and not force_snapshot and not within_us_window():
        print("Outside US window — no-op."); return
    if scope == "india" and not force_snapshot and not within_india_window():
        print("Outside India window — no-op."); return
    if cutoff_mode and now_ist().time() >= dt.time(15, 0):
        print("Post-cutoff (>=3pm) — no-op."); return

    state = load_state()
    snapshot = force_snapshot or scope == "all"
    phase = us_phase() if (scope == "us" and not snapshot) else "day"
    cache = state.setdefault("_cache", {})
    for k in [k for k in cache if k.startswith("navlv") and not k.startswith("navlv3_")]:
        del cache[k]            # prune superseded nav-cache schema versions
    # Sentiment is only used in the snapshot and for day-phase transition pings — skip the two
    # network calls overnight.
    sent = fetch_sentiment() if (snapshot or (phase == "day" and not cutoff_mode)) else {"cnn": None, "mmi": None}

    groups = []
    if scope in ("all", "india"): groups.append(("🇮🇳 India", INDIA_IDX, False, True))
    if scope in ("all", "india") and HOLDINGS: groups.append(("🇮🇳 Holdings", HOLDINGS, False, True))
    if scope in ("all", "us"):    groups.append(("🇺🇸 US Tech", US_IDX, True, True))
    if scope == "all" and FUNDS:  groups.append(("📌 Funds", FUNDS, None, False))

    rows = []   # (glabel, is_us, snap, name, m, side, tier, fire, dmove, crit, eod)
    missing = []
    for glabel, cfg, gus, snap in groups:
        for name, c in cfg.items():
            if not snapshot and c.get("display_only"):     # only ever shown in the snapshot
                continue                                   # -> skip its fetch on hourly runs
            m, eod, is_us = fetch_one(c, gus, cache)
            if m is None:
                if c.get("critical"): missing.append(name)
                rows.append(Row(glabel, is_us, snap, name, None, None, 0, None, False, c.get("critical", False), eod, ""))
                continue
            if c.get("display_only"):                      # shown in snapshot, never alerts
                side, tier, fire, dmv, nudge = None, 0, None, False, ""
            else:
                bands = resolve_bands(c)
                # SELL off the 50DMA for the trim candidates (faster), else the 200DMA.
                sref = c.get("sell_ref", "d200")
                sell_dist = m["dist50"] if sref == "d50" else m["dist200"]
                m["sref"] = sref   # so alert lines reference the DMA the signal fired off
                m["blabel"] = c.get("buy_label")   # "DEPLOY" -> put-money-in wording
                side, tier = classify(sell_dist, m["drawdown"], bands)
                fire = step_state(name, side, tier, sell_dist, m["drawdown"], bands, state)
                dmv = daily_move_alert(name, m["daily"], bands["daily"], side, tier,
                                       state.setdefault("_daily", {}), today_str())
                # Surfaced nudge (shown in snapshot whenever in-zone, not just on a fresh fire).
                if side == "sell" and sell_dist is not None:
                    nudge = "\U0001F534 trim \u2014 %s vs %s" % (
                        pct(sell_dist), "50DMA" if sref == "d50" else "200DMA")
                else:
                    nudge = ""
            rows.append(Row(glabel, is_us, snap, name, m, side, tier, fire, dmv, c.get("critical", False), eod, nudge))

    strans = sentiment_transition(sent, state) if snapshot else []   # zone notes only in the snapshot

    fired  = [r for r in rows if r.fire in ("enter", "escalate", "flip", "recover")]
    dmoved = [r for r in rows if r.dmv]
    # Day hourly (alert-only): tier crossings buy AND sell + DOWN days. Up-days dropped (froth is
    # covered by sell tiers).
    down_moves = [r for r in dmoved if r.m and r.m["daily"] is not None and r.m["daily"] < 0]
    # Late/overnight: a big single-day move (LATE_DOWN/UP) on live US instruments. Fresh tier
    # crossings are ALSO sent late (state advances either way; swallowing one = a lost alert).
    big = []
    if phase == "late":
        etd = now_et().date().isoformat()
        big = [r for r in rows
               if r.m and not r.eod and r.m["daily"] is not None
               and _late_big(r.m["daily"])
               and big_move(r.name, r.m["daily"], state, etd)]

    # FYI / awareness tier: gentle pings for INFO_NAMES that moved past the (asymmetric) info band
    # but are not already going out as actionable. Self-caps ~11pm IST; once per day per side.
    info = []
    if now_ist().time() <= INFO_CUTOFF:
        actionable = {r.name for r in fired}
        if scope == "us":
            actionable |= {r.name for r in (down_moves + big)}
        istate = state.setdefault("_info", {})
        for r in rows:
            if r.name not in INFO_NAMES or r.name in actionable:
                continue
            if snapshot and r.is_us:        # premarket QQQ in the snapshot -> leave to the hourly US run
                continue
            if not (r.m and not r.eod and r.m["daily"] is not None):
                continue
            if info_move_alert(r.name, r.m["daily"], istate, today_str()):
                info.append(r)

    if snapshot:   # record the intraday anchor (1:59 cron) for the MF-cutoff re-runs (2:41 / 2:54)
        state["_anchor"] = {"date": today_str(), "time": now_ist().strftime("%-I:%M%p"),
                            "px": {r.name: r.m["px"] for r in rows
                                   if not r.is_us and r.m and r.m.get("px") is not None}}

    # General snapshot send-gate: not a daily digest. Send only when something is fresh, or a
    # standing booking opportunity is due an occasional reminder. --digest (force) always sends.
    # Stale-NAV warning (feed frozen / scheme merged): weekly nag per fund, snapshot-only.
    stale = []
    if snapshot and _STALE_NAVS:
        warned = state.setdefault("_stale_warned", {})
        for s in _STALE_NAVS:
            nm = s.split(" (")[0]
            if nm not in warned or _days_since(warned[nm]) >= REMIND_DAYS:
                warned[nm] = today_str(); stale.append(s)

    risk_on, risk_off, risk_lines = _risk(rows, sent)
    cross_notes = cross_events(rows, state) if snapshot else []
    wk_movers   = weekly_movers(rows) if snapshot else []
    send_snap = False
    if snapshot:
        in_window = bool(force_snapshot) or (dt.time(9, 0) <= now_ist().time() <= dt.time(15, 5))
        standing  = any(r.snap and ((r.name in BOOK_NAMES and r.side == "sell")
                                     or (r.name in DEPLOY_NAMES and r.side == "buy")) for r in rows)
        due       = state.get("_last_snap") is None or _days_since(state["_last_snap"]) >= REMIND_DAYS
        # reassurance: a meaningful same-day move on a LIVE India index (funds are EOD/stale -> excluded)
        notable   = any(r.snap and not r.is_us and not r.eod and r.m and r.m.get("daily") is not None
                        and (r.m["daily"] <= -SNAP_DAILY_DOWN or r.m["daily"] >= SNAP_DAILY_UP)
                        for r in rows)
        send_snap = in_window and (bool(force_snapshot) or bool(fired) or risk_on or risk_off
                                   or bool(missing) or bool(cross_notes) or notable or bool(wk_movers)
                                   or bool(info) or bool(stale) or (standing and due))
        if send_snap:
            state["_last_snap"] = today_str()
    save_state(state)   # after all state mutations: step_state, sentiment_transition, big_move, anchor

    if cutoff_mode:    # 2:41 recheck / 2:54 pre-cutoff -> alert only on fresh movement vs the anchor
        msg = _india_cutoff(rows, state, cutoff_mode)
        if msg is None:
            print("%s: nothing actionable since anchor." % cutoff_mode); _log(rows); return
        send_telegram("\n".join(msg)); print("Sent (%s, %d lines)." % (cutoff_mode, len(msg))); _log(rows); return

    if snapshot:
        if not send_snap:
            print("Snapshot silent (nothing fresh / out of window)."); _log(rows); return
    elif phase == "late":
        if not fired and not big and not info and not missing:
            print("Late US — no >=3%% move."); _log(rows); return
    else:
        moves = dmoved if scope == "india" else down_moves   # india books on up-moves too
        if not fired and not moves and not info and not strans and not missing:
            print("No new signals."); _log(rows); return

    out = []
    if snapshot:
        by_name = {r.name: r for r in rows if r.snap}
        sx = by_name.get("SENSEX")
        if sx and sx.m and sx.m.get("daily") is not None:   # lead with the Sensex move (chat preview)
            out.append("%s <b>Sensex %s</b> \u00b7 📊 %s" %
                       ("🔻" if sx.m["daily"] < 0 else "📈", pct(sx.m["daily"]), stamp()))
        else:
            out.append("📊 <b>MARKET</b> — %s" % stamp())

        out += ["\n" + b for b in risk_lines]   # risk-on / risk-off banners (if any)
        out += ["\n" + n for n in cross_notes]   # golden/death cross trend shift (rare)
        if wk_movers:
            out.append("\n🌊 <b>5-day move</b>: " + " \u00b7 ".join(
                "%s %s" % (short(r.name), pct(wk)) for r, wk in wk_movers))

        first = True
        for group in DISPLAY_GROUPS:
            grp = [by_name[n] for n in group if n in by_name]
            if not grp:
                continue
            if not first:
                out.append("\n" + DELIM)
            first = False
            for r in grp:
                if r.m is None:
                    out.append("\n%s <b>%s</b>  — no data" % ("🇺🇸" if r.is_us else "🇮🇳", short(r.name)))
                    continue
                l1, ctx, _nudge = snap_block(r)
                out.append("\n" + l1)
                if ctx:
                    out.append(ctx)

        out.append("\n" + DELIM)        # close instruments block before sentiment/news
        sl = sentiment_line(sent)
        if sl: out.append("\n" + sl)
        for n in strans: out.append("🔔 " + n)

        # Deploy signal: India large/large-mid in the staged buy zone (drawdown from the 1y peak).
        deploy = sorted([r for r in rows if r.snap and r.name in DEPLOY_NAMES and r.side == "buy"],
                        key=lambda r: -(r.tier or 0))
        if deploy:
            out.append("\n🟢 <b>Deploy signal</b> — India large/large-mid in the staged buy zone")
            out += ["🟢 DEPLOY-T%d  %s  %s off 1y peak" % (r.tier, short(r.name), pct(r.m["drawdown"]))
                    for r in deploy]

        # alert-only fund fires (only surface when they trip)
        ff = [r for r in fired if not r.snap]
        if ff:
            out.append("\n📌 <b>Fund alerts</b>")
            out += [_reason(r) for r in ff]

        if stale:
            out.append("\n⚠️ <b>stale NAV</b> (feed frozen?): %s" % ", ".join(stale))

        heads_news = fetch_headlines()
        if heads_news:
            out.append("")
            out += ["📰 " + h for h in heads_news]

        if missing:
            out.append("\n⚠️ couldn't fetch: %s — %s" % (", ".join(missing), CRITICAL_HINT))
    elif phase == "late":
        if fired:   # tier crossings are rare + actionable -> never swallow them overnight
            out.append("⚠️ <b>US TECH</b> — %s" % stamp())
            out += [_reason(r) for r in fired]
        if big:
            out.append("⚠️ <b>US — BIG MOVE</b> (overnight) — %s" % stamp())
            out += [_dmove(r) for r in big]
        if info:
            out.append("ℹ️ <b>US — FYI</b> — %s" % stamp())
            out += [_info_line(r) for r in info]
        if missing:
            out.append("⚠️ couldn't fetch: %s — %s" % (", ".join(missing), CRITICAL_HINT))
    else:
        if scope == "india":
            out.append("💰 <b>INDIA — profit-booking watch</b> — %s" % stamp())
        elif fired or down_moves:
            out.append("⚠️ <b>US TECH</b> — %s" % stamp())
        else:
            out.append("ℹ️ <b>US — FYI</b> — %s" % stamp())
        for n in strans: out.append("🔔 " + n)
        fired_names = {r.name for r in fired}
        out += [_reason(r) for r in fired]
        moves = dmoved if scope == "india" else down_moves
        out += [_dmove(r) for r in moves if r.name not in fired_names]
        out += [_info_line(r) for r in info if r.name not in fired_names]
        if missing:
            out.append("⚠️ couldn't fetch: %s — %s" % (", ".join(missing), CRITICAL_HINT))

    send_telegram("\n".join(out))
    print("Sent (%d lines)." % len(out)); _log(rows)

def _reason(r):
    glabel, is_us, snap, name, m, side, tier, fire, dmv, crit, eod, nudge = r
    sref = m.get("sref", "d200")
    ref  = m.get("dist50") if sref == "d50" else m.get("dist200")
    lbl  = "50DMA" if sref == "d50" else "200DMA"
    if fire == "recover":
        return "\u2705 %s back to normal  %s vs %s" % (short(name), pct(ref), lbl)
    if side == "buy":
        if m.get("blabel") == "DEPLOY":
            return "🟢 DEPLOY-T%d  %s  %s off 1y peak" % (tier, short(name), pct(m["drawdown"]))
        return "🔻 %s  %s off peak  (%s vs 200DMA)" % (
            short(name), pct(m["drawdown"]), pct(m["dist200"]))
    dd = m.get("drawdown")
    ctx = ("  · %s off 1y peak" % pct(dd)) if dd is not None else ""
    return "💰 %s  trim — %s vs %s%s" % (short(name), pct(ref), lbl, ctx)

def info_move_alert(name, daily, dstate, today):
    """FYI awareness ping. Asymmetric band (down -INFO_DOWN, up +INFO_UP), once per day per side."""
    if daily is None:
        return False
    if daily <= -INFO_DOWN:
        mv = "down"
    elif daily >= INFO_UP:
        mv = "up"
    else:
        return False
    rec = dstate.get(name, {})
    if rec.get("date") == today and rec.get("side") == mv:
        return False
    dstate[name] = {"date": today, "side": mv}
    return True

def _info_line(r):
    m = r.m
    return "ℹ️ %s %s %s today — FYI, no action" % (
        "🔻" if m["daily"] < 0 else "📈", short(r.name), pct(m["daily"]))

def _dmove(r):
    m = r.m
    return "%s %s %s today" % ("🔻" if m["daily"] < 0 else "📈", short(r.name), pct(m["daily"]))

def _india_cutoff(rows, state, mode):
    """MF-cutoff re-runs. Compares live India values to the anchor snapshot and alerts only on
    fresh, actionable movement before the 3pm NAV cutoff. Returns lines, or None = stay silent.
      recheck (2:41): buy window — only a DROP >= RECHECK_DROP since the anchor.
      cutoff  (2:54): a trim name that RALLIED >= CUTOFF_RISE while in a sell tier, or a
                      DROP >= CUTOFF_BUY_DROP since the anchor (no daily repeat of a standing one)."""
    anc   = state.get("_anchor", {})
    base  = anc.get("px", {}) if anc.get("date") == today_str() else {}
    atime = anc.get("time", "open")
    irows = [r for r in rows if not r.is_us and r.m is not None]

    def delta(r):
        ap, px = base.get(r.name), r.m.get("px")
        return (px / ap - 1) if (ap and px) else None
    def refpct(r):
        return r.m.get("dist50") if r.m.get("sref") == "d50" else r.m.get("dist200")
    def reflbl(r):
        return "50DMA" if r.m.get("sref") == "d50" else "200DMA"
    def since(r):
        d = delta(r)
        return (" \u00b7 %s since %s" % (pct(d), atime)) if d is not None else ""

    if mode == "recheck":      # 2:41 — buy window (cutoff ~2:45): a fresh DROP is the only trigger
        drops = sorted([r for r in irows if delta(r) is not None and delta(r) <= -RECHECK_DROP],
                       key=lambda r: delta(r))
        if not drops:
            return None
        out = ["🔻 <b>INDIA — dropped since %s</b> (buy window) — %s" % (atime, stamp())]
        for r in drops:
            out.append("🔻 %s  %s since %s (%s vs %s) — buy before ~2:45 cutoff" %
                       (short(r.name), pct(delta(r)), atime, pct(refpct(r)), reflbl(r)))
        return out

    # mode == "cutoff" — 2:54, last call before 3pm. STRINGENT: a FRESH move since the anchor only.
    rallied = sorted([r for r in irows if r.name in BOOK_NAMES and r.side == "sell"
                      and delta(r) is not None and delta(r) >= CUTOFF_RISE],
                     key=lambda r: -(r.tier or 0))
    drops   = sorted([r for r in irows if delta(r) is not None and delta(r) <= -CUTOFF_BUY_DROP],
                     key=lambda r: delta(r))
    if not rallied and not drops:
        return None
    out = ["⏰ <b>INDIA — pre-cutoff (3pm)</b> — %s" % stamp()]
    for r in rallied:         # rallied into/within a sell tier since the anchor -> redeem before 3pm
        out.append("💰 %s  %s vs %s%s — redeem before 3pm" %
                   (short(r.name), pct(refpct(r)), reflbl(r), since(r)))
    for r in drops:           # heavy drop since the anchor — buy only if the bank is fast enough
        if r in rallied:
            continue
        out.append("🔻 %s  %s since %s (%s vs %s) — buy only if bank is fast" %
                   (short(r.name), pct(delta(r)), atime, pct(refpct(r)), reflbl(r)))
    return out

def _log(rows):
    for r in rows:
        glabel, is_us, snap, name, m, side, tier, fire, dmv, crit, eod, nudge = r
        if m is None:
            print(" - %-20s NO DATA" % name)
        else:
            print(" - %-20s px=%s d50=%s d200=%s dd=%s daily=%s %s/T%d fire=%s dmv=%s"
                  % (name, fmt_level(m["px"]), pct(m.get("dist50")), pct(m["dist200"]),
                     pct(m["drawdown"]), pct(m["daily"]), side, tier, fire, dmv))


def _full_dd(c):
    """Drawdown from the FULL-history peak (vs the windowed 1y/range peak) — for the
    'is the 52-week window missing an older cycle top?' question."""
    try:
        if c["src"] == "mf":
            navs, _, _ = mf_closes(c)
        elif c["src"] == "nse":
            navs, _, _ = mf_closes({"code": c["dma_code"]})
        else:
            return None
        navs = [x for x in navs if x is not None]
        return (navs[-1] / max(navs) - 1) if navs else None
    except Exception:
        return None

def calibrate():
    """Dry run (no Telegram): print current distance-from-DMA + drawdowns so the sell ladders
    can be set just above where each trim name sits today."""
    cache = {}
    groups = [("IND", INDIA_IDX, False), ("US", US_IDX, True), ("HOLD", HOLDINGS, False)]
    print("%-22s %10s %8s %8s %9s %9s   sell-ladder" %
          ("name", "px", "dist50", "dist200", "dd(rng)", "dd(full)"))
    print("-" * 92)
    for tag, cfg, gus in groups:
        for name, c in cfg.items():
            m, eod, is_us = fetch_one(c, gus, cache)
            if not m:
                print("%-22s   no data" % short(name)); continue
            ddf = _full_dd(c)
            sref = c.get("sell_ref", "d200")
            bands = resolve_bands(c)
            ladder = "/".join("%.1f" % (x * 100) for x in bands["sell"]) + "%% off " + ("50DMA" if sref == "d50" else "200DMA")
            mark = "  <= REDEEM" if name in BOOK_NAMES else ""
            print("%-22s %10s %8s %8s %9s %9s   %s%s" % (
                short(name), fmt_level(m["px"]), pct(m.get("dist50")), pct(m.get("dist200")),
                pct(m["drawdown"]), pct(ddf), ladder, mark))
    print("\nrng = configured range_days window; full = all available history.")


def main(scope, force_snapshot=False, cutoff_mode=None):
    try:
        run(scope, force_snapshot, cutoff_mode)
    except Exception as e:
        notify_failure("Run crashed: <code>%s</code>\nCheck the VM / run.log." % e)
        raise


# ---- inbound poll (bidirectional "/status" digest) -----------------------------
# On-demand pull: text the bot/group a trigger word and get the full snapshot back. A cron runs
# this every minute (no webhook/daemon needed) — it reads getUpdates, and if the configured chat
# sent a trigger since the last poll, fires a digest. tg_offset is persisted BEFORE the send so a
# crash can't loop-spam, and only the configured CHAT_ID is honoured (everyone else is ignored).
POLL_TRIGGERS = {"/status", "status", "/digest", "digest", "/market", "market",
                 "snapshot", "ping", "hi"}
POLL_HELP = {"/help", "help", "/start"}

def poll_telegram():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        sys.exit("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID (see .env.example).")
    state = load_state()
    offset = state.get("tg_offset", 0)
    try:
        d = SESSION.get("https://api.telegram.org/bot%s/getUpdates" % TELEGRAM_TOKEN,
                        params={"offset": offset + 1, "timeout": 0}, timeout=20).json()
        updates = d.get("result", [])
    except Exception as ex:
        print("   [poll] getUpdates fail: %s" % ex); return
    want, trigger, helped = str(CHAT_ID), False, False
    for u in updates:
        offset = max(offset, u.get("update_id", offset))
        msg = u.get("message") or u.get("edited_message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip().lower()
        word = text.split("@")[0].split()[0] if text else ""   # strip @botname in groups
        if not (want and chat == want):
            continue
        if word in POLL_TRIGGERS:
            trigger = True
        elif word in POLL_HELP:
            helped = True
    state["tg_offset"] = offset           # persist BEFORE sending (crash-safe, no re-trigger)
    save_state(state)
    if trigger:
        print("poll: trigger -> digest."); main("all", force_snapshot=True)
    elif helped:
        print("poll: help.")
        send_telegram("\U0001F916 <b>market_alerts</b> — text <code>status</code> for the live "
                      "snapshot. Otherwise I stay silent unless a market hits an extreme.")
    else:
        print("poll: %d update(s), no trigger." % len(updates))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--test" in args:
        send_telegram("\u2705 <b>market_alerts</b> — delivery works.")
        print("Test sent.")
    elif "--demo" in args:
        send_telegram(
            "📊 <b>MARKET</b> — Sat 30 May 2:30PM IST\n\n"
            "🇮🇳 <b>SENSEX</b>  74,776   -1.4% today 🔻\n"
            "-12.8% from 1y high \u00b7 +6.1% above 1y low\n\n"
            "🇺🇸 <b>Nasdaq100</b>   +0.4% today 📈\n"
            "at 1y high \u00b7 +41.0% above 1y low\n\n"
            "\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\n\n"
            "🇮🇳 <b>Nifty Large & Midcap 250</b>   +0.9% today 📈\n"
            "-5.0% from 1y high \u00b7 +12.0% above 1y low\n"
            "\U0001F534 trim \u2014 +5.4% vs 50DMA\n\n"
            "🇮🇳 <b>Nifty Smallcap 250</b>   +1.3% today 📈\n"
            "-9.0% from 1y high \u00b7 +8.0% above 1y low\n"
            "\U0001F534 trim \u2014 +3.2% vs 50DMA\n\n"
            "🧭 India Mood 70 Extreme Greed (+6 wk) \u00b7 US F&G 60 Greed (+5 wk)\n\n"
            "📰 Fed officials signal patience on rate path\n"
            "📰 Nifty slips as FIIs trim, smallcaps lead decline")
        print("Demo sent.")
    elif "--heartbeat" in args:
        import socket
        send_telegram("💚 <b>market_alerts</b> alive on %s — %s"
                      % (socket.gethostname(), now_ist().strftime("%Y-%m-%d %H:%M IST")))
        print("Heartbeat sent.")
    elif "--calibrate" in args:
        calibrate()
    elif "--poll" in args:
        poll_telegram()
    elif "--digest" in args:
        main("all", force_snapshot=True)
    else:
        scope = "all"
        if "--scope" in args:
            i = args.index("--scope"); scope = args[i + 1] if i + 1 < len(args) else "all"
        if "--us" in args:    scope = "us"
        if "--india" in args: scope = "india"
        if "--all" in args:   scope = "all"
        cutoff_mode = "recheck" if "--recheck" in args else ("cutoff" if "--cutoff" in args else None)
        if cutoff_mode:
            scope = "india"
        main(scope, force_snapshot=("--snapshot" in args or today_str() in DIGEST_DATES),
             cutoff_mode=cutoff_mode)
