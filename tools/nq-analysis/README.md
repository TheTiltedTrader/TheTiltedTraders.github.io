# NQ Multi-Timeframe Setup Scanner

A command-line tool that pulls NQ futures price data and screens the daily,
4H, 1H, 30m, 15m, 5m, 3m, 2m and 1m charts for clean EMA/HMA/VWAP confluence
setups where a stop no wider than `--max-stop` points has room to a prior
swing extreme of at least `--target-points` points.

This is a **screening aid**, not a trading system. It does not place
orders, does not predict future price, and the swing/target logic is a
simple structural heuristic — always confirm any level shown here against
your own broker/platform chart before risking capital.

## What it computes, per timeframe

- **EMA 9 / 14 / 21** and **50-period Hull MA** on that timeframe's own bars.
- **Session VWAP** (resets each trading day) on every intraday timeframe.
- **Daily 50 SMA**, computed once off the daily chart and carried into every
  timeframe's output as a higher-timeframe reference level.
- **ATR(14)** on that timeframe, used to size how "tight" a confluence zone
  has to be to count (tighter cluster required on faster timeframes).
- **Trend bias**: `up` when EMA9 > EMA14 > EMA21 and price is above the 50
  HMA, `down` when stacked the other way, otherwise `mixed`.
- **Stop reference**: nearest confirmed swing low (for an up-trend) or swing
  high (for a down-trend) behind price, found via a simple left/right
  fractal pivot scan (`--pivot-lookback`, default 3 bars each side).
- **Target room**: distance from price to the most extreme high/low in the
  recent lookback window in the direction of the trend (a proxy for "space
  to run" before the next real resistance/support).
- **Verdict**: `ACTIONABLE LONG/SHORT PULLBACK` only when all of trend,
  confluence tightness, stop distance, and target room line up; otherwise
  `NO SETUP` with the specific reason it was rejected.

## Data source and limitations

Data comes from Yahoo Finance's continuous front-month futures contract
(`NQ=F` by default) via the `yfinance` library — free, no API key, but
**delayed** (typically 10–20 minutes) and a continuous-contract series
rather than raw exchange ticks or your broker's exact fill prices. Yahoo
also caps how far back intraday data goes:

| Timeframe | Source interval | Max lookback |
|-----------|-----------------|---------------|
| 1d        | 1d               | ~2 years |
| 4h        | 1h (resampled)   | ~59 days |
| 1h        | 60m              | ~59 days |
| 30m/15m/5m/2m | native      | ~59 days |
| 3m        | 1m (resampled)   | ~7 days |
| 1m        | 1m               | ~7 days |

The 4h and 3m bars are built by resampling finer data on calendar-day
boundaries, so they may not line up exactly bar-for-bar with your
platform's own 4H/3M aggregation (which often anchors sessions
differently) — treat them as close approximations.

## Setup

```bash
cd tools/nq-analysis
pip install -r requirements.txt
```

## Usage

```bash
# Full scan, all 9 timeframes, defaults (100pt target / 45pt max stop)
python3 nq_analysis.py

# Micro futures, custom target/stop, only a few timeframes
python3 nq_analysis.py --symbol MNQ=F --timeframes 1h,15m,5m --target-points 80 --max-stop 30

# Save a shareable Markdown report and raw JSON
python3 nq_analysis.py --md-out report.md --json-out report.json

# Dump full OHLCV + indicator history (all bars, not just the latest) to CSV
python3 export_csv.py NQ=F nq_full_data.csv
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--symbol` | `NQ=F` | Yahoo ticker (`NQ=F`, `MNQ=F`, `ES=F`, etc.) |
| `--timeframes` | all 9 | Comma-separated subset of `1d,4h,1h,30m,15m,5m,3m,2m,1m` |
| `--target-points` | `100` | Minimum points of "room" required in the trend direction |
| `--max-stop` | `45` | Maximum acceptable distance to the structural stop |
| `--confluence-mult` | `0.35` | ATR multiple defining how tight the EMA/HMA/VWAP cluster must be |
| `--pivot-lookback` | `3` | Bars each side required to confirm a swing high/low |
| `--json-out` | — | Path to write full results as JSON |
| `--md-out` | — | Path to write a Markdown report |

## Example output

```
TF        Price Trend  EMA9/14/21                    HMA50       VWAP     Stop     Room  Verdict
----------------------------------------------------------------------------------------------
1h     29917.25 up     29831.67/29815.74/29792.36   29867.36   29796.76    45.25    100.0  NO SETUP
...
[1h] no setup — price 75.0pt from tight confluence (limit 27.0pt)
```

Run it any time you want a fresh cross-timeframe read; it always fetches
current data rather than caching anything.
