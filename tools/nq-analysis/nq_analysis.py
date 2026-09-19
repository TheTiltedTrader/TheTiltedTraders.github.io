#!/usr/bin/env python3
"""
NQ Futures Multi-Timeframe Setup Scanner
=========================================

Pulls NQ (or any futures/ticker) price data from Yahoo Finance across the
daily, 4H, 1H, 30m, 15m, 5m, 3m, 2m and 1m timeframes, computes 9/14/21 EMA,
50 HMA, daily 50 SMA and session VWAP, and flags timeframes where price is
sitting at a clean EMA/HMA/VWAP confluence zone with a realistic stop
(<= --max-stop points) and enough room to a prior swing extreme to plausibly
reach a (--target-points) point move.

This is a screening tool, not a signal generator or execution system. It
does not place trades, does not guarantee the target/stop levels shown will
hold, and Yahoo's futures data is delayed and continuous-contract based
(not raw exchange tick data). Always confirm levels against your own
broker/platform feed before risking capital.

Usage:
    python3 nq_analysis.py
    python3 nq_analysis.py --symbol MNQ=F --target-points 100 --max-stop 45
    python3 nq_analysis.py --timeframes 1h,15m,5m --md-out report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf

# Each timeframe maps to the Yahoo interval it's fetched at (Yahoo's own
# menu doesn't offer 4h or 3m), the lookback period allowed for that
# interval, and, if needed, a pandas resample rule used to build it from
# finer data. Yahoo enforces hard lookback caps per interval:
#   1m -> 7 days, 2m/5m/15m/30m/90m -> 60 days, 60m -> 730 days, 1d -> years.
TIMEFRAME_CONFIG = {
    "1d": {"interval": "1d", "period": "2y", "resample": None},
    "4h": {"interval": "1h", "period": "59d", "resample": "4h"},
    "1h": {"interval": "1h", "period": "59d", "resample": None},
    "30m": {"interval": "30m", "period": "59d", "resample": None},
    "15m": {"interval": "15m", "period": "59d", "resample": None},
    "5m": {"interval": "5m", "period": "59d", "resample": None},
    "3m": {"interval": "1m", "period": "7d", "resample": "3min"},
    "2m": {"interval": "2m", "period": "59d", "resample": None},
    "1m": {"interval": "1m", "period": "7d", "resample": None},
}
TIMEFRAME_ORDER = ["1d", "4h", "1h", "30m", "15m", "5m", "3m", "2m", "1m"]


def fetch_raw(symbol: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(
        symbol, period=period, interval=interval, progress=False, auto_adjust=False
    )
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna(how="all")


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = df.resample(rule, label="right", closed="right").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return out.dropna(subset=["Open", "High", "Low", "Close"])


def wma(series: pd.Series, length: int) -> pd.Series:
    weights = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def hma(series: pd.Series, length: int = 50) -> pd.Series:
    half = wma(series, max(length // 2, 1))
    full = wma(series, length)
    raw = 2 * half - full
    return wma(raw, max(int(round(np.sqrt(length))), 1))


def session_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    pv = typical * df["Volume"]
    day = df.index.tz_convert(None).date if df.index.tz is not None else df.index.date
    day = pd.Series(day, index=df.index)
    cum_pv = pv.groupby(day).cumsum()
    cum_vol = df["Volume"].groupby(day).cumsum().replace(0, np.nan)
    return cum_pv / cum_vol


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(length).mean()


def find_swings(df: pd.DataFrame, left: int = 3, right: int = 3):
    highs, lows = df["High"].to_numpy(), df["Low"].to_numpy()
    n = len(df)
    swing_highs, swing_lows = [], []
    for i in range(left, n - right):
        window_h = highs[i - left : i + right + 1]
        if highs[i] >= window_h.max():
            swing_highs.append(i)
        window_l = lows[i - left : i + right + 1]
        if lows[i] <= window_l.min():
            swing_lows.append(i)
    return swing_highs, swing_lows


@dataclass
class TimeframeResult:
    timeframe: str
    bars: int
    as_of: str
    price: float
    ema9: float
    ema14: float
    ema21: float
    hma50: float
    vwap: float | None
    daily_sma50_ref: float | None
    atr14: float
    trend: str
    confluence_spread: float
    confluence_threshold: float
    confluence_tight: bool
    stop_ref: float | None
    stop_distance: float | None
    target_ref: float | None
    target_room: float | None
    verdict: str
    reason: str


def analyze_timeframe(
    name: str,
    df: pd.DataFrame,
    daily_sma50_ref: float | None,
    target_points: float,
    max_stop: float,
    confluence_mult: float,
    pivot_lookback: int,
) -> TimeframeResult | None:
    if len(df) < 60:
        return None

    close = df["Close"]
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema14 = close.ewm(span=14, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    hma50 = hma(close, 50)
    atr14 = atr(df, 14)
    vwap = session_vwap(df) if name != "1d" else None

    i = -1
    while pd.isna(hma50.iloc[i]) or pd.isna(atr14.iloc[i]):
        i -= 1
        if -i > len(df):
            return None

    price = float(close.iloc[i])
    e9, e14, e21, h50 = (
        float(ema9.iloc[i]),
        float(ema14.iloc[i]),
        float(ema21.iloc[i]),
        float(hma50.iloc[i]),
    )
    vw = float(vwap.iloc[i]) if vwap is not None and not pd.isna(vwap.iloc[i]) else None
    a14 = float(atr14.iloc[i])

    if e9 > e14 > e21 and price > h50:
        trend = "up"
    elif e9 < e14 < e21 and price < h50:
        trend = "down"
    else:
        trend = "mixed"

    confluence_values = [e9, e14, e21, h50] + ([vw] if vw is not None else [])
    confluence_spread = max(confluence_values) - min(confluence_values)
    confluence_threshold = max(8.0, min(30.0, confluence_mult * a14))
    confluence_tight = confluence_spread <= confluence_threshold

    swing_highs, swing_lows = find_swings(df.iloc[: len(df) + i + 1], left=pivot_lookback, right=pivot_lookback)

    stop_ref = stop_distance = target_ref = target_room = None
    reason = ""

    if trend == "up":
        below = [df["Low"].iloc[idx] for idx in swing_lows if df["Low"].iloc[idx] < price]
        if below:
            stop_ref = float(max(below))
            stop_distance = price - stop_ref
        lookback_highs = df["High"].iloc[max(0, len(df) + i - 150) : len(df) + i + 1]
        target_ref = float(lookback_highs.max())
        target_room = target_ref - price
        if target_room < target_points:
            target_ref = price + target_points
            target_room = target_points
    elif trend == "down":
        above = [df["High"].iloc[idx] for idx in swing_highs if df["High"].iloc[idx] > price]
        if above:
            stop_ref = float(min(above))
            stop_distance = stop_ref - price
        lookback_lows = df["Low"].iloc[max(0, len(df) + i - 150) : len(df) + i + 1]
        target_ref = float(lookback_lows.min())
        target_room = price - target_ref
        if target_room < target_points:
            target_ref = price - target_points
            target_room = target_points
    else:
        reason = "no clean trend (EMAs/HMA not stacked)"

    verdict = "NO SETUP"
    if trend == "mixed":
        reason = reason or "no clean trend"
    elif not confluence_tight:
        reason = f"price {confluence_spread:.1f}pt from tight confluence (limit {confluence_threshold:.1f}pt)"
    elif stop_distance is None:
        reason = "no recent swing to anchor a stop"
    elif stop_distance > max_stop:
        reason = f"nearest structural stop is {stop_distance:.1f}pt (> {max_stop:.0f}pt max)"
    elif target_room is None or target_room < target_points - 1e-6:
        reason = f"only {target_room:.1f}pt of room to prior structure (< {target_points:.0f}pt target)"
    else:
        verdict = f"ACTIONABLE {'LONG' if trend == 'up' else 'SHORT'} PULLBACK"
        reason = "EMA/HMA(/VWAP) confluence + valid stop + room to target"

    ts = df.index[len(df) + i]
    as_of = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)

    return TimeframeResult(
        timeframe=name,
        bars=len(df),
        as_of=as_of,
        price=round(price, 2),
        ema9=round(e9, 2),
        ema14=round(e14, 2),
        ema21=round(e21, 2),
        hma50=round(h50, 2),
        vwap=round(vw, 2) if vw is not None else None,
        daily_sma50_ref=round(daily_sma50_ref, 2) if daily_sma50_ref is not None else None,
        atr14=round(a14, 2),
        trend=trend,
        confluence_spread=round(confluence_spread, 2),
        confluence_threshold=round(confluence_threshold, 2),
        confluence_tight=confluence_tight,
        stop_ref=round(stop_ref, 2) if stop_ref is not None else None,
        stop_distance=round(stop_distance, 2) if stop_distance is not None else None,
        target_ref=round(target_ref, 2) if target_ref is not None else None,
        target_room=round(target_room, 2) if target_room is not None else None,
        verdict=verdict,
        reason=reason,
    )


def build_dataset(symbol: str, timeframe: str) -> pd.DataFrame:
    cfg = TIMEFRAME_CONFIG[timeframe]
    raw = fetch_raw(symbol, cfg["interval"], cfg["period"])
    if raw.empty:
        return raw
    return resample_ohlcv(raw, cfg["resample"]) if cfg["resample"] else raw


def run(symbol: str, timeframes: list[str], target_points: float, max_stop: float,
        confluence_mult: float, pivot_lookback: int) -> list[TimeframeResult]:
    daily_df = build_dataset(symbol, "1d")
    daily_sma50_ref = None
    if not daily_df.empty and len(daily_df) >= 50:
        daily_sma50_ref = float(daily_df["Close"].rolling(50).mean().iloc[-1])

    results = []
    for tf in timeframes:
        try:
            df = daily_df if tf == "1d" else build_dataset(symbol, tf)
        except Exception as exc:  # yfinance/network hiccup for one timeframe shouldn't kill the run
            print(f"  ! failed to fetch {tf}: {exc}", file=sys.stderr)
            continue
        if df.empty:
            print(f"  ! no data returned for {tf}", file=sys.stderr)
            continue
        result = analyze_timeframe(
            tf, df, daily_sma50_ref, target_points, max_stop, confluence_mult, pivot_lookback
        )
        if result is None:
            print(f"  ! not enough bars on {tf} to compute indicators", file=sys.stderr)
            continue
        results.append(result)
    return results


def format_report(symbol: str, results: list[TimeframeResult], target_points: float, max_stop: float) -> str:
    lines = []
    lines.append(f"# NQ Multi-Timeframe Scan: {symbol}")
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat()} — target {target_points:.0f}pt / max stop {max_stop:.0f}pt_")
    lines.append("")
    lines.append(
        "> Data via Yahoo Finance continuous front-month futures (delayed, not raw exchange "
        "ticks). Screening output only — verify every level against your own platform before "
        "risking capital. Not investment advice."
    )
    lines.append("")
    lines.append("| TF | Price | Trend | EMA9/14/21 | HMA50 | VWAP | Stop dist | Target room | Verdict |")
    lines.append("|----|-------|-------|------------|-------|------|-----------|-------------|---------|")
    for r in results:
        emas = f"{r.ema9}/{r.ema14}/{r.ema21}"
        vwap_s = r.vwap if r.vwap is not None else "-"
        stop_s = f"{r.stop_distance}pt" if r.stop_distance is not None else "-"
        room_s = f"{r.target_room}pt" if r.target_room is not None else "-"
        lines.append(
            f"| {r.timeframe} | {r.price} | {r.trend} | {emas} | {r.hma50} | {vwap_s} | {stop_s} | {room_s} | {r.verdict} |"
        )
    lines.append("")
    actionable = [r for r in results if r.verdict.startswith("ACTIONABLE")]
    if actionable:
        lines.append("## Actionable timeframes")
        for r in actionable:
            lines.append(
                f"- **{r.timeframe} {r.verdict}** — price {r.price}, stop ref {r.stop_ref} "
                f"({r.stop_distance}pt risk), target ref {r.target_ref} ({r.target_room}pt reward). {r.reason}"
            )
    else:
        lines.append("## No timeframe currently meets the criteria")
        for r in results:
            lines.append(f"- {r.timeframe}: {r.verdict} — {r.reason}")
    lines.append("")
    lines.append("## Full detail")
    for r in results:
        lines.append(f"### {r.timeframe} (as of {r.as_of}, {r.bars} bars)")
        d = asdict(r)
        for k, v in d.items():
            if k in ("timeframe", "bars", "as_of"):
                continue
            lines.append(f"- {k}: {v}")
        lines.append("")
    return "\n".join(lines)


def print_console(symbol: str, results: list[TimeframeResult], target_points: float, max_stop: float) -> None:
    print(f"\nNQ Multi-Timeframe Scan: {symbol}  (target {target_points:.0f}pt / max stop {max_stop:.0f}pt)")
    print("Data: Yahoo Finance continuous futures, delayed. Screening only — not investment advice.\n")
    header = f"{'TF':<4} {'Price':>10} {'Trend':<6} {'EMA9/14/21':<24} {'HMA50':>10} {'VWAP':>10} {'Stop':>8} {'Room':>8}  Verdict"
    print(header)
    print("-" * len(header))
    for r in results:
        emas = f"{r.ema9}/{r.ema14}/{r.ema21}"
        vwap_s = f"{r.vwap}" if r.vwap is not None else "-"
        stop_s = f"{r.stop_distance}" if r.stop_distance is not None else "-"
        room_s = f"{r.target_room}" if r.target_room is not None else "-"
        print(
            f"{r.timeframe:<4} {r.price:>10} {r.trend:<6} {emas:<24} {r.hma50:>10} {vwap_s:>10} {stop_s:>8} {room_s:>8}  {r.verdict}"
        )
    print()
    for r in results:
        if r.verdict.startswith("ACTIONABLE"):
            print(f"[{r.timeframe}] {r.verdict}: entry ~{r.price}, stop ~{r.stop_ref} ({r.stop_distance}pt), "
                  f"target ~{r.target_ref} ({r.target_room}pt). {r.reason}")
        else:
            print(f"[{r.timeframe}] no setup — {r.reason}")


def main():
    parser = argparse.ArgumentParser(description="NQ futures multi-timeframe EMA/HMA/VWAP setup scanner")
    parser.add_argument("--symbol", default="NQ=F", help="Yahoo ticker, e.g. NQ=F or MNQ=F (default: NQ=F)")
    parser.add_argument("--timeframes", default=",".join(TIMEFRAME_ORDER),
                         help="Comma-separated subset of: " + ",".join(TIMEFRAME_ORDER))
    parser.add_argument("--target-points", type=float, default=100.0)
    parser.add_argument("--max-stop", type=float, default=45.0)
    parser.add_argument("--confluence-mult", type=float, default=0.35,
                         help="ATR multiple used as the EMA/HMA/VWAP confluence-tightness threshold")
    parser.add_argument("--pivot-lookback", type=int, default=3,
                         help="Bars on each side required to confirm a swing high/low")
    parser.add_argument("--json-out", default=None, help="Optional path to dump results as JSON")
    parser.add_argument("--md-out", default=None, help="Optional path to write a Markdown report")
    args = parser.parse_args()

    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    for t in timeframes:
        if t not in TIMEFRAME_CONFIG:
            parser.error(f"unknown timeframe '{t}', choose from {list(TIMEFRAME_CONFIG)}")
    timeframes = [t for t in TIMEFRAME_ORDER if t in timeframes]

    results = run(args.symbol, timeframes, args.target_points, args.max_stop,
                  args.confluence_mult, args.pivot_lookback)

    if not results:
        print("No results — check symbol/timeframes and network access.", file=sys.stderr)
        sys.exit(1)

    print_console(args.symbol, results, args.target_points, args.max_stop)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)
        print(f"\nJSON written to {args.json_out}")

    if args.md_out:
        with open(args.md_out, "w") as f:
            f.write(format_report(args.symbol, results, args.target_points, args.max_stop))
        print(f"Markdown report written to {args.md_out}")


if __name__ == "__main__":
    main()
