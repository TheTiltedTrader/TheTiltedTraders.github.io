#!/usr/bin/env python3
"""Dump full OHLCV + indicator history per timeframe to CSV (for manual review/plotting)."""

import sys

import pandas as pd

from nq_analysis import TIMEFRAME_ORDER, build_dataset, hma, session_vwap, atr


def build_indicator_frame(symbol: str, timeframe: str) -> pd.DataFrame:
    df = build_dataset(symbol, timeframe)
    if df.empty:
        return df
    close = df["Close"]
    out = df.copy()
    out["ema9"] = close.ewm(span=9, adjust=False).mean()
    out["ema14"] = close.ewm(span=14, adjust=False).mean()
    out["ema21"] = close.ewm(span=21, adjust=False).mean()
    out["hma50"] = hma(close, 50)
    out["atr14"] = atr(df, 14)
    out["vwap"] = session_vwap(df) if timeframe != "1d" else pd.NA
    out.insert(0, "timeframe", timeframe)
    out.index.name = "datetime"
    return out.round(2)


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "NQ=F"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "nq_full_data.csv"

    frames = []
    for tf in TIMEFRAME_ORDER:
        print(f"fetching {tf}...", file=sys.stderr)
        frame = build_indicator_frame(symbol, tf)
        if frame.empty:
            print(f"  ! no data for {tf}", file=sys.stderr)
            continue
        frames.append(frame.reset_index())

    combined = pd.concat(frames, ignore_index=True)
    combined.to_csv(out_path, index=False)
    print(f"wrote {len(combined)} rows across {len(frames)} timeframes to {out_path}")


if __name__ == "__main__":
    main()
