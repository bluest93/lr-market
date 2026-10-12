"""Analyze cross-exchange BTC/USDT dislocations. See README for methodology."""

import argparse
import glob
import math
import os

import numpy as np
import pandas as pd

from config import (EXCHANGES, GRID, FFILL_LIMIT, BASELINE_WIN, SIGMA_WIN,
                    K_SIGMA, K_SPREAD, EXIT_FRAC, MIN_EVENT_PTS, MAX_EVENT_S,
                    MIN_R2, SPEED_OF_LIGHT_KM_S)

# ---- network-distance option (AWS inter-region RTT via cloudping.co) ----
VENUE_REGION = {"binance": "ap-northeast-1", "bybit": "ap-southeast-1",
                "coinbase": "us-east-1", "kraken": "eu-west-2"}
CLOUDPING_URLS = [
    "https://www.cloudping.co/api/latencies?percentile=p_50&timeframe=1W",
    "https://api.cloudping.co/latencies?percentile=p_50&timeframe=1W",
]
# Fallback snapshot: APPROXIMATE values; refresh with --fetch-latency or at
# cloudping.co/grid (P50, 1W) and update here.
NETWORK_RTT_SNAPSHOT_MS = {
    "binance-bybit": 70.0, "coinbase-kraken": 76.0, "binance-coinbase": 150.0,
    "kraken-bybit": 170.0, "binance-kraken": 220.0, "coinbase-bybit": 225.0,
}


def fetch_network_rtt(timeout=20):
    """Live AWS inter-region P50 RTTs from cloudping.co for our venue pairs.
    Returns (dict pair->rtt_ms, source_url) or (None, None) on failure."""
    import json
    import urllib.request
    names = list(EXCHANGES)
    for url in CLOUDPING_URLS:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "lr-market-research"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read())
            grid = payload.get("data") or {}
            out = {}
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    a, b = names[i], names[j]
                    ra, rb = VENUE_REGION[a], VENUE_REGION[b]
                    v = grid.get(ra, {}).get(rb)
                    if v is None:
                        v = grid.get(rb, {}).get(ra)
                    if isinstance(v, dict):
                        v = v.get("latency", v.get("p_50"))
                    if v is not None:
                        out[f"{a}-{b}"] = float(v)
            if len(out) == len(names) * (len(names) - 1) // 2:
                return out, url
            print(f"cloudping: incomplete grid from {url} "
                  f"({len(out)} of 6 pairs)")
        except Exception as e:
            print(f"cloudping fetch failed ({url}): {type(e).__name__}: {e}")
    return None, None


def _spearman(x, y):
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    rx = x.argsort().argsort().astype(float)
    ry = y.argsort().argsort().astype(float)
    rx -= rx.mean()
    ry -= ry.mean()
    den = math.sqrt(float((rx ** 2).sum()) * float((ry ** 2).sum()))
    return float((rx * ry).sum()) / den if den else float("nan")


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def pair_distance_table():
    rows = []
    names = list(EXCHANGES)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            ca, cb = EXCHANGES[a], EXCHANGES[b]
            km = haversine_km(ca["lat"], ca["lon"], cb["lat"], cb["lon"])
            rows.append({
                "pair": f"{a}-{b}",
                "great_circle_km": round(km, 0),
                "light_time_ms": round(km / SPEED_OF_LIGHT_KM_S * 1000, 2),
            })
    return pd.DataFrame(rows)


def load_exchange(data_dir, exch, grid=GRID, ffill=FFILL_LIMIT,
                  start=None, end=None):
    files = sorted(glob.glob(os.path.join(data_dir, f"quotes_{exch}_*.csv")))
    if not files:
        return None
    df = pd.concat([pd.read_csv(f, on_bad_lines="skip") for f in files],
                   ignore_index=True)
    df["bid"] = pd.to_numeric(df["bid"], errors="coerce")
    df["ask"] = pd.to_numeric(df["ask"], errors="coerce")
    df = df.dropna(subset=["bid", "ask"])
    df = df[(df.bid > 0) & (df.ask > 0) & (df.ask >= df.bid)]
    df["ts"] = pd.to_datetime(df["local_ts_ns"], utc=True)
    if start is not None:
        df = df[df.ts >= pd.Timestamp(start, tz="UTC")]
    if end is not None:
        df = df[df.ts <= pd.Timestamp(end, tz="UTC")]
    df["mid"] = (df.bid + df.ask) / 2
    df["hs_bps"] = (df.ask - df.bid) / 2 / df["mid"] * 1e4
    s = (df.set_index("ts")[["mid", "hs_bps"]]
           .sort_index())
    s = s[~s.index.duplicated(keep="last")]
    g = s.resample(grid).last().ffill(limit=ffill)
    return g


def fit_half_life(t_sec, abs_e):
    k0 = int(np.argmax(abs_e))
    t = t_sec[k0:] - t_sec[k0]
    y = abs_e[k0:]
    keep = y > 1e-9
    t, y = t[keep], np.log(y[keep])
    if len(t) < MIN_EVENT_PTS or t[-1] <= 0:
        return np.nan, np.nan, len(t)
    slope, intercept = np.polyfit(t, y, 1)
    if slope >= 0:
        return np.nan, np.nan, len(t)
    pred = slope * t + intercept
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
    tau = -1.0 / slope
    return tau * math.log(2), r2, len(t)


def pair_events(gA, gB, pair_name, basewin=BASELINE_WIN):
    df = pd.DataFrame({
        "midA": gA["mid"], "midB": gB["mid"],
        "hsA": gA["hs_bps"], "hsB": gB["hs_bps"],
    }).dropna()
    if len(df) < basewin:
        return pd.DataFrame()
    minp = max(50, basewin // 10)

    m = (df.midA + df.midB) / 2
    d_bps = (df.midA - df.midB) / m * 1e4
    base = d_bps.rolling(basewin, min_periods=minp).median()
    e = (d_bps - base)
    q75 = e.rolling(basewin, min_periods=minp).quantile(0.75)
    q25 = e.rolling(basewin, min_periods=minp).quantile(0.25)
    sigma = (q75 - q25) / 1.349
    thr = np.maximum(K_SIGMA * sigma.values,
                     K_SPREAD * (df.hsA + df.hsB).values)

    ae = e.abs().values
    idx = df.index
    valid = ~np.isnan(ae) & ~np.isnan(thr)
    events = []
    n = len(ae)
    i = 0
    while i < n:
        if valid[i] and ae[i] > thr[i]:
            entry_thr = thr[i]
            exit_lvl = EXIT_FRAC * entry_thr
            j = i + 1
            while (j < n and valid[j] and ae[j] > exit_lvl
                   and (idx[j] - idx[i]).total_seconds() <= MAX_EVENT_S):
                j += 1
            t_sec = (idx[i:j] - idx[i]).total_seconds().to_numpy()
            seg = ae[i:j]
            hl, r2, npts = fit_half_life(t_sec, seg)
            events.append({
                "pair": pair_name,
                "t_start": idx[i],
                "duration_s": float(t_sec[-1]) if len(t_sec) else 0.0,
                "peak_bps": float(seg.max()),
                "entry_thr_bps": float(entry_thr),
                "half_life_s": hl,
                "r2": r2,
                "n_pts": npts,
            })
            i = j
        else:
            i += 1
    return pd.DataFrame(events)


def main(data_dir, out_dir, grid=GRID, start=None, end=None,
         fetch_latency=False):
    os.makedirs(out_dir, exist_ok=True)
    grid_s = pd.Timedelta(grid).total_seconds()
    ffill = max(1, int(round(5.0 / grid_s)))
    basewin = max(301, int(round(300.0 / grid_s)) + 1)
    print(f"grid={grid} ({grid_s}s/tick), ffill={ffill} samples, "
          f"baseline window={basewin} samples")
    grids = {}
    for name in EXCHANGES:
        g = load_exchange(data_dir, name, grid, ffill, start, end)
        if g is not None and len(g):
            grids[name] = g
            print(f"loaded {name}: {len(g)} grid points "
                  f"({g.index[0]} .. {g.index[-1]})")
        else:
            print(f"no data for {name}, skipping")
    if len(grids) < 2:
        print("need at least two exchanges with data")
        return

    all_events = []
    names = list(grids)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            ev = pair_events(grids[a], grids[b], f"{a}-{b}", basewin)
            print(f"{a}-{b}: {len(ev)} events")
            if len(ev):
                all_events.append(ev)
    if not all_events:
        print("no events detected -- collect more data or lower K_SIGMA")
        return
    events = pd.concat(all_events, ignore_index=True)
    events.to_csv(os.path.join(out_dir, "events.csv"), index=False)

    good = events[(events.r2 >= MIN_R2) & events.half_life_s.notna()
                  & (events.half_life_s < MAX_EVENT_S)]
    summary = (good.groupby("pair")
                   .agg(n_events=("half_life_s", "size"),
                        median_half_life_s=("half_life_s", "median"),
                        p25_half_life_s=("half_life_s",
                                         lambda x: x.quantile(0.25)),
                        p75_half_life_s=("half_life_s",
                                         lambda x: x.quantile(0.75)),
                        median_conv_time_s=("duration_s", "median"),
                        median_peak_bps=("peak_bps", "median"))
                   .reset_index())
    dist = pair_distance_table()
    summary = summary.merge(dist, on="pair", how="left")
    net, net_src = (fetch_network_rtt() if fetch_latency else (None, None))
    if net is None:
        net = NETWORK_RTT_SNAPSHOT_MS
        net_src = "bundled snapshot (APPROXIMATE -- run with --fetch-latency)"
    summary["net_oneway_ms"] = summary.pair.map(net) / 2.0
    print(f"\nnetwork distance source: {net_src}")
    summary = summary.sort_values("light_time_ms").reset_index(drop=True)
    summary.to_csv(os.path.join(out_dir, "summary.csv"), index=False)
    print("\n=== per-pair summary (fits with r^2 >= %.2f) ===" % MIN_R2)
    print(summary.to_string(index=False))
    for dist_col, label in [("light_time_ms", "great-circle light-time"),
                            ("net_oneway_ms", "network RTT/2")]:
        rho = _spearman(summary[dist_col], summary.median_half_life_s)
        print(f"spearman, median half-life vs {label}: {rho:+.2f} "
              f"(6 pairs; ~|0.83|+ needed for p<0.05)")

    make_plots(events, good, summary, grids, out_dir)
    print(f"\nwrote {out_dir}/events.csv, summary.csv, summary.png")


def make_plots(events, good, summary, grids, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))

    ax = axes[0]
    pairs = summary["pair"].tolist()
    data = [good[good.pair == p]["half_life_s"].values for p in pairs]
    if any(len(d) for d in data):
        ax.boxplot(data, tick_labels=pairs, showfliers=False)
    ax.set_ylabel("half-life (s)")
    ax.set_title("Dislocation half-life by exchange pair")
    ax.tick_params(axis="x", rotation=45)

    ax = axes[1]
    s = summary.dropna(subset=["light_time_ms", "median_half_life_s"])
    ax.scatter(s.light_time_ms, s.median_half_life_s)
    for _, r in s.iterrows():
        ax.annotate(r["pair"], (r.light_time_ms, r.median_half_life_s),
                    fontsize=8, xytext=(4, 4), textcoords="offset points")
    ax.set_xlabel("one-way light time between venues (ms, great circle)")
    ax.set_ylabel("median half-life (s)")
    ax.set_title("Convergence speed vs geographic distance")

    ax = axes[2]
    s2 = summary.dropna(subset=["net_oneway_ms", "median_half_life_s"])
    ax.scatter(s2.net_oneway_ms, s2.median_half_life_s)
    for _, r in s2.iterrows():
        ax.annotate(r["pair"], (r.net_oneway_ms, r.median_half_life_s),
                    fontsize=8, xytext=(4, 4), textcoords="offset points")
    ax.set_xlabel("one-way network latency between regions (ms, cloudping P50/2)")
    ax.set_ylabel("median half-life (s)")
    ax.set_title("Convergence speed vs network distance")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "summary.png"), dpi=150)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--grid", default=GRID,
                    help="analysis tick, e.g. 100ms or 10ms (memory scales 1/tick)")
    ap.add_argument("--start", default=None,
                    help="UTC start filter, e.g. 2026-08-19")
    ap.add_argument("--end", default=None)
    ap.add_argument("--fetch-latency", action="store_true",
                    help="pull live AWS inter-region P50 RTTs from cloudping.co")
    args = ap.parse_args()
    main(args.data_dir, args.out_dir, args.grid, args.start, args.end,
         args.fetch_latency)
