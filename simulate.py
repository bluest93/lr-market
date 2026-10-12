"""Generate SYNTHETIC quote data with dislocations of KNOWN decay constants,
in the exact CSV format produced by collect.py. Used to validate that
analyze.py recovers the injected half-lives before you trust it on real data.

Injected decay constants tau (seconds) -> expected half-life = tau * ln 2:
    binance  0.5   ->  0.35 s
    bybit    0.8   ->  0.55 s
    coinbase 1.5   ->  1.04 s
    kraken   2.5   ->  1.73 s
A dislocation on exchange X appears in every pair containing X, so each
pair's recovered distribution mixes the two members' taus.

Usage:
    python simulate.py --out data_sim --hours 2
"""

import argparse
import os

import numpy as np

TAUS = {"binance": 0.5, "bybit": 0.8, "coinbase": 1.5, "kraken": 2.5}
BASIS_BPS = {"binance": 0.0, "bybit": 1.0, "coinbase": -1.5, "kraken": 2.0}
SYMBOLS = {"binance": "BTCUSDT", "bybit": "BTCUSDT",
           "coinbase": "BTC-USDT", "kraken": "BTC/USDT"}

GRID_MS = 100
JUMPS_PER_EXCHANGE = 45
JUMP_BPS = (10.0, 30.0)
NOISE_BPS = 0.15          # AR(1) idiosyncratic noise scale per exchange
WALK_BPS = 0.30           # efficient-price random-walk step (bps / 100 ms)


def main(out_dir, hours, seed):
    rng = np.random.default_rng(seed)
    os.makedirs(out_dir, exist_ok=True)
    n = int(hours * 3600 * 1000 / GRID_MS)
    dt_s = GRID_MS / 1000.0
    start_ns = 1_755_000_000_000_000_000     # arbitrary fixed epoch (ns)
    t_ns = start_ns + (np.arange(n, dtype=np.int64) * GRID_MS * 1_000_000)

    # common efficient price
    eff = 60000.0 * np.exp(np.cumsum(rng.standard_normal(n) * WALK_BPS * 1e-4))

    for exch, tau in TAUS.items():
        # AR(1) idiosyncratic noise
        eps = rng.standard_normal(n) * NOISE_BPS
        noise = np.empty(n)
        noise[0] = eps[0]
        for k in range(1, n):
            noise[k] = 0.9 * noise[k - 1] + eps[k]

        # injected dislocations with known tau
        disloc = np.zeros(n)
        jump_idx = rng.choice(np.arange(600, n - 600),
                              size=JUMPS_PER_EXCHANGE, replace=False)
        for i0 in jump_idx:
            j_bps = rng.uniform(*JUMP_BPS) * rng.choice([-1.0, 1.0])
            w = min(int(10 * tau / dt_s), n - i0)
            tt = np.arange(w) * dt_s
            disloc[i0:i0 + w] += j_bps * np.exp(-tt / tau)

        mid = eff * (1 + (BASIS_BPS[exch] + noise + disloc) * 1e-4)
        hs_bps = 1.0 + np.abs(rng.standard_normal(n)) * 0.2
        bid = mid * (1 - hs_bps * 1e-4)
        ask = mid * (1 + hs_bps * 1e-4)

        # small local-timestamp jitter (feed dissemination noise)
        jitter = rng.integers(0, 20_000_000, size=n)   # up to 20 ms
        path = os.path.join(out_dir, f"quotes_{exch}_20260815.csv")
        with open(path, "w") as f:
            f.write("exchange,symbol,bid,ask,bid_qty,ask_qty,"
                    "exch_ts_ms,local_ts_ns\n")
            sym = SYMBOLS[exch]
            for k in range(n):
                f.write(f"{exch},{sym},{bid[k]:.2f},{ask[k]:.2f},1,1,,"
                        f"{t_ns[k] + jitter[k]}\n")
        print(f"wrote {path}  ({n} rows, tau={tau}s, "
              f"expected half-life={tau * 0.6931:.2f}s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data_sim")
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    main(args.out, args.hours, args.seed)
