"""Health check for the running collector. Run any time from the project folder:

    python check_health.py [--data-dir data]

What to look for:
  - span (h) should be similar across all four venues; a lagging span means
    that feed died silently -> check the collector terminal for reconnects.
  - Binance median gap should be tiny (ms). Others can be 0.1-5 s.
  - Median spread: healthy is < ~5 bps. If coinbase shows > ~10 bps or
    median gaps of many seconds, its BTC-USDT book is too thin: switch its
    config.py entry to "symbol": "BTC-USD" and restart the collector.
  - staleness p90: how old the newest quote typically is between updates.
"""

import argparse
import glob
import time

import pandas as pd


def main(data_dir):
    files = sorted(glob.glob(f"{data_dir}/quotes_*.csv"))
    if not files:
        print(f"no quote files in {data_dir}/ yet -- is collect.py running?")
        return
    now_ns = time.time_ns()
    print(f"{'file':44s} {'rows':>9} {'span_h':>7} {'gap_med_s':>10} "
          f"{'gap_p90_s':>10} {'spread_bps':>11} {'age_s':>7}")
    for f in files:
        try:
            df = pd.read_csv(f, on_bad_lines="skip")
        except Exception as e:
            print(f"{f}: unreadable ({e})")
            continue
        if len(df) < 10:
            print(f"{f:44s} {len(df):>9}   (too few rows so far)")
            continue
        df = df.dropna(subset=["bid", "ask", "local_ts_ns"])
        mid = (df.bid + df.ask) / 2
        spread_bps = (df.ask - df.bid) / mid * 1e4
        gaps = df.local_ts_ns.diff() / 1e9
        span_h = (df.local_ts_ns.iloc[-1] - df.local_ts_ns.iloc[0]) / 3.6e12
        age_s = (now_ns - df.local_ts_ns.iloc[-1]) / 1e9
        flag = ""
        if spread_bps.median() > 10:
            flag = "  <-- THIN BOOK (consider BTC-USD)"
        if age_s > 120:
            flag = "  <-- FEED STALE, check collector"
        print(f"{f:44s} {len(df):>9} {span_h:>7.1f} {gaps.median():>10.3f} "
              f"{gaps.quantile(0.9):>10.3f} {spread_bps.median():>11.2f} "
              f"{age_s:>7.0f}{flag}")

    lat = sorted(glob.glob(f"{data_dir}/latency_*.csv"))
    if lat:
        ldf = pd.concat([pd.read_csv(f) for f in lat], ignore_index=True)
        print("\nTCP round-trip to exchange REST hosts (min / median ms):")
        for ex, g in ldf.groupby("exchange"):
            print(f"  {ex:9s} {g.rtt_ms.min():7.1f} / {g.rtt_ms.median():7.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()
    main(args.data_dir)
