"""Refit detected events at fine time resolution (default 1 ms), two ways.

Stage 1 (already done): analyze.py detects events on the coarse 100 ms grid.
Stage 2 (this script): for each detected event, go back to the RAW
nanosecond-stamped quotes, rebuild the pair gap on a fine grid inside the
event's window, and re-fit the exponential half-life.

This version fits each event TWICE, so both methods can be compared:

  half_life_fine_s      -- original method: fit from the fine-grid peak to
                           the end of the coarse event window.
  half_life_noisestop_s -- upgraded method:
      * the fine window starts LOOKBACK_S before coarse t0, so a peak the
        100 ms detector sampled late can be recovered;
      * the fit stops at the first post-peak point with |e| <= 2*sigma_pre
        (sigma_pre = robust noise scale of the pre-event minute), so the
        flat noise tail cannot drag the slope;
      * span gate: if peak < 2 * stopping level (less than one half-life of
        headroom), the event is UNFITTABLE (NaN) rather than a slope fitted
        to a plateau.

Diagnostics recorded per event: upd_A/upd_B (real quote updates on each leg
inside [t0, t1] -- a 1 ms grid is not 1 ms data), sigma_pre_bps, and
t_peak_offset_s (fine peak time minus coarse t0; negative = the look-back
recovered an earlier peak).

Honest limits at fine resolution: the effective floor is feed dissemination
plus network jitter, tens of ms. Treat refined half-lives below ~20-50 ms
as "at the jitter floor", not exact. And no refit can recover an event the
100 ms detector never saw at all.

Usage:
    python refine_events.py --data-dir data_week \
        --events results_week/events.csv \
        --out results_week/refined_events.csv --grid-ms 1
"""

import argparse
import glob
import math
import os

import numpy as np
import pandas as pd

from config import EXCHANGES, MIN_EVENT_PTS, MIN_R2, MAX_EVENT_S

PRE_BASELINE_S = 60.0      # trailing window before the fine window: basis + noise
BASE_STEP_MS = 100         # sampling step for that pre-window
MAX_AGE_S = 10.0           # ignore grid points where a leg's quote is older
LOOKBACK_S = 0.3           # fine window starts this long before coarse t0
NOISE_K = 2.0              # noise-stop level = NOISE_K * sigma_pre
SPAN_RATIO = 2.0           # span gate: peak must be >= SPAN_RATIO * stop level


def load_venue_raw(data_dir, exch):
    files = sorted(glob.glob(os.path.join(data_dir, f"quotes_{exch}_*.csv")))
    if not files:
        return None, None
    parts = []
    for f in files:
        df = pd.read_csv(f, usecols=["bid", "ask", "local_ts_ns"],
                         on_bad_lines="skip")
        parts.append(df)
    df = pd.concat(parts, ignore_index=True)
    df["bid"] = pd.to_numeric(df.bid, errors="coerce")
    df["ask"] = pd.to_numeric(df.ask, errors="coerce")
    df["local_ts_ns"] = pd.to_numeric(df.local_ts_ns, errors="coerce")
    df = df.dropna()
    df = df[(df.bid > 0) & (df.ask >= df.bid)]
    ts = df.local_ts_ns.astype("int64").to_numpy()
    order = np.argsort(ts, kind="stable")
    ts = ts[order]
    mid = ((df.bid + df.ask) / 2).to_numpy()[order]
    keep = np.ones(len(ts), bool)
    keep[:-1] = ts[1:] != ts[:-1]          # dedupe identical stamps, keep last
    return ts[keep], mid[keep]


def asof(ts, vals, grid_ns):
    """Last value at or before each grid time, plus its age in seconds."""
    idx = np.searchsorted(ts, grid_ns, side="right") - 1
    ok = idx >= 0
    out = np.full(len(grid_ns), np.nan)
    age = np.full(len(grid_ns), np.inf)
    out[ok] = vals[idx[ok]]
    age[ok] = (grid_ns[ok] - ts[idx[ok]]) / 1e9
    return out, age


def loglin_fit(t, logy):
    """Least-squares line on (t, log y). Returns (half_life, r2)."""
    if len(t) < MIN_EVENT_PTS or t[-1] <= t[0]:
        return np.nan, np.nan
    slope, b = np.polyfit(t, logy, 1)
    if slope >= 0:
        return np.nan, np.nan
    pred = slope * t + b
    ss_res = float(np.sum((logy - pred) ** 2))
    ss_tot = float(np.sum((logy - logy.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
    return -math.log(2) / slope, r2


def fit_from_peak(t_sec, abs_e, stop_level=None, span_ratio=None):
    """Fit |e| ~ exp decay from the global peak onward.

    stop_level=None  -> original method: use every point to the window end.
    stop_level=x     -> noise-stopped: truncate at the first post-peak point
                        with |e| <= x; span gate requires peak >= span_ratio*x.
    Returns (half_life, r2).
    """
    k0 = int(np.argmax(abs_e))
    t = t_sec[k0:] - t_sec[k0]
    y = abs_e[k0:]
    if stop_level is not None:
        if y[0] < span_ratio * stop_level:
            return np.nan, np.nan          # span gate: plateau, not a decay
        below = np.nonzero(y <= stop_level)[0]
        if len(below):
            t, y = t[:below[0]], y[:below[0]]
    keep = y > 1e-9
    t, y = t[keep], np.log(y[keep])
    return loglin_fit(t, y)


def refine(data_dir, events_path, out_path, grid_ms, lookback_s):
    ev = pd.read_csv(events_path)
    ev["t_start"] = pd.to_datetime(ev.t_start, utc=True, format="ISO8601")
    venues_needed = sorted({v for p in ev.pair.unique() for v in p.split("-")})
    raw = {}
    for v in venues_needed:
        ts, mid = load_venue_raw(data_dir, v)
        if ts is None:
            print(f"no raw data for {v}; its pairs will be skipped")
            continue
        raw[v] = (ts, mid)
        print(f"loaded raw {v}: {len(ts):,} quote updates")

    step_ns = int(grid_ms * 1e6)
    base_step_ns = int(BASE_STEP_MS * 1e6)
    lb_ns = int(lookback_s * 1e9)
    cols = ["half_life_fine_s", "r2_fine",
            "half_life_noisestop_s", "r2_noisestop",
            "sigma_pre_bps", "t_peak_offset_s", "upd_A", "upd_B"]
    out = {c: [] for c in cols}

    def skip():
        for c in cols:
            out[c].append(np.nan)

    for k, row in enumerate(ev.itertuples()):
        a, b = row.pair.split("-")
        if a not in raw or b not in raw:
            skip()
            continue
        tsA, mA = raw[a]
        tsB, mB = raw[b]
        t0 = int(row.t_start.value)
        t1 = t0 + int(min(row.duration_s, MAX_EVENT_S) * 1e9)

        # frozen baseline AND noise scale from the minute before the fine
        # window (ends at t0 - lookback so the look-back can't contaminate it)
        bgrid = np.arange(t0 - lb_ns - int(PRE_BASELINE_S * 1e9),
                          t0 - lb_ns, base_step_ns)
        bA, ageA = asof(tsA, mA, bgrid)
        bB, ageB = asof(tsB, mB, bgrid)
        okb = (ageA < MAX_AGE_S) & (ageB < MAX_AGE_S)
        if okb.sum() < 30:
            skip()
            continue
        d_pre = (bA[okb] - bB[okb]) / ((bA[okb] + bB[okb]) / 2) * 1e4
        base = float(np.median(d_pre))
        q75, q25 = np.percentile(d_pre, [75, 25])
        sigma_pre = float((q75 - q25) / 1.349)

        # fine grid over [t0 - lookback, t1]
        grid = np.arange(t0 - lb_ns, t1 + step_ns, step_ns)
        gA, ageA = asof(tsA, mA, grid)
        gB, ageB = asof(tsB, mB, grid)
        ok = (ageA < MAX_AGE_S) & (ageB < MAX_AGE_S)
        if ok.sum() < MIN_EVENT_PTS:
            skip()
            continue
        g_ns = grid[ok]
        t_sec = (g_ns - t0) / 1e9          # 0 = coarse t0; look-back is negative
        d = (gA[ok] - gB[ok]) / ((gA[ok] + gB[ok]) / 2) * 1e4
        ae = np.abs(d - base)

        # peak guard: a look-back point may only win the argmax if it also
        # clears the event's entry threshold -- otherwise it is pre-event
        # noise, and the peak search falls back to t >= t0.
        k_all = int(np.argmax(ae))
        if t_sec[k_all] < 0 and ae[k_all] <= row.entry_thr_bps:
            in_evt = np.nonzero(t_sec >= 0)[0]
            if len(in_evt) < MIN_EVENT_PTS:
                skip()
                continue
            k_all = in_evt[int(np.argmax(ae[in_evt]))]
        t_fit, e_fit = t_sec[k_all:], ae[k_all:]

        hl_f, r2_f = fit_from_peak(t_fit, e_fit)
        hl_n, r2_n = fit_from_peak(t_fit, e_fit,
                                   stop_level=NOISE_K * sigma_pre,
                                   span_ratio=SPAN_RATIO)
        out["half_life_fine_s"].append(hl_f)
        out["r2_fine"].append(r2_f)
        out["half_life_noisestop_s"].append(hl_n)
        out["r2_noisestop"].append(r2_n)
        out["sigma_pre_bps"].append(sigma_pre)
        out["t_peak_offset_s"].append(float(t_sec[k_all]))
        out["upd_A"].append(int(np.searchsorted(tsA, t1) - np.searchsorted(tsA, t0)))
        out["upd_B"].append(int(np.searchsorted(tsB, t1) - np.searchsorted(tsB, t0)))
        if (k + 1) % 2000 == 0:
            print(f"  refined {k + 1}/{len(ev)} events")

    for c, vals in out.items():
        ev[c] = vals
    ev.to_csv(out_path, index=False)
    print(f"\nwrote {out_path}")

    # ---------------- side-by-side summary ----------------
    def clean(col, r2col):
        return ev[(ev[r2col] >= MIN_R2) & ev[col].notna() & (ev[col] < MAX_EVENT_S)]

    gf = clean("half_life_fine_s", "r2_fine")
    gn = clean("half_life_noisestop_s", "r2_noisestop")
    print(f"clean fits: fine {len(gf)}, noise-stopped {len(gn)} "
          f"(of {len(ev)} events)")
    neg = (ev.t_peak_offset_s < 0).sum()
    print(f"events whose true peak was recovered in the look-back: {neg} "
          f"({neg / max(len(ev), 1):.0%})\n")
    rows = []
    for p in sorted(ev.pair.unique()):
        f = gf[gf.pair == p].half_life_fine_s
        n = gn[gn.pair == p].half_life_noisestop_s
        rows.append({
            "pair": p,
            "n_fine": len(f), "n_noisestop": len(n),
            "median_fine_s": round(f.median(), 3) if len(f) else np.nan,
            "median_noisestop_s": round(n.median(), 3) if len(n) else np.nan,
            "p10_fine_s": round(f.quantile(0.1), 3) if len(f) else np.nan,
            "p10_noisestop_s": round(n.quantile(0.1), 3) if len(n) else np.nan,
        })
    print(pd.DataFrame(rows).to_string(index=False))
    print("\nIf the two methods rank the pairs the same way, the conclusion "
          "is robust to the fitting-window convention.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--events", required=True)
    ap.add_argument("--out", default="refined_events.csv")
    ap.add_argument("--grid-ms", type=float, default=1.0)
    ap.add_argument("--lookback-s", type=float, default=LOOKBACK_S)
    args = ap.parse_args()
    refine(args.data_dir, args.events, args.out, args.grid_ms, args.lookback_s)
