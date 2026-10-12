"""Configuration: exchanges, matching-engine locations, analysis parameters.

Data center locations are APPROXIMATE, based on public reporting:
  - Binance spot:  AWS ap-northeast-1 (Tokyo)
  - Coinbase:      AWS us-east-1 (N. Virginia)
  - Kraken:        Equinix LD4 area (Slough / London)  [colocation via Beeks]
  - Bybit:         AWS ap-southeast-1 (Singapore)
Verify these before publishing results; exchanges migrate infrastructure.
"""

EXCHANGES = {
    "binance": {
        "ws_url": "wss://stream.binance.com:9443/ws/btcusdt@bookTicker",
        "rest_host": "api.binance.com",
        "symbol": "BTCUSDT",
        "dc_name": "Tokyo (AWS ap-northeast-1)",
        "lat": 35.68, "lon": 139.76,
    },
    "coinbase": {
        "ws_url": "wss://ws-feed.exchange.coinbase.com",
        "rest_host": "api.exchange.coinbase.com",
        "symbol": "BTC-USD",   # BTC-USDT confirmed too thin; USDT/USD basis is
                               # slow-moving and absorbed by the rolling-median
                               # baseline in analyze.py
        "dc_name": "N. Virginia (AWS us-east-1)",
        "lat": 38.95, "lon": -77.45,
    },
    "kraken": {
        "ws_url": "wss://ws.kraken.com/v2",
        "rest_host": "api.kraken.com",
        "symbol": "BTC/USD",   # Kraken's deepest book (fiat-first venue);
                               # USDT/USD basis vs Binance/Bybit is absorbed
                               # by the rolling-median baseline
        "dc_name": "London (Equinix LD4 / Slough)",
        "lat": 51.52, "lon": -0.63,
    },
    "bybit": {
        "ws_url": "wss://stream.bybit.com/v5/public/spot",
        "rest_host": "api.bybit.com",
        "symbol": "BTCUSDT",
        "dc_name": "Singapore (AWS ap-southeast-1)",
        "lat": 1.35, "lon": 103.99,
    },
}

# ---------------- analysis parameters ----------------
GRID = "100ms"          # common time grid for merged mid-price series
FFILL_LIMIT = 50        # max forward-fill = 5 s; beyond that treat as gap
BASELINE_WIN = 3001     # rolling-median window (~5 min at 100 ms) for basis removal
SIGMA_WIN = 3001        # rolling robust-sigma window
K_SIGMA = 4.0           # entry threshold: |e| > K_SIGMA * robust sigma ...
K_SPREAD = 1.0          # ... AND |e| > K_SPREAD * (half_spread_A + half_spread_B)
EXIT_FRAC = 0.25        # event ends when |e| < EXIT_FRAC * entry threshold
MIN_EVENT_PTS = 5       # minimum samples to attempt a decay fit
MAX_EVENT_S = 120.0     # cap event duration (regime changes, not dislocations)
MIN_R2 = 0.5            # keep fits with r^2 above this for the summary

SPEED_OF_LIGHT_KM_S = 299792.458
