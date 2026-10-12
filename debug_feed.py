"""Print the first few RAW WebSocket messages from each exchange, one venue
at a time. Pure diagnostics -- writes nothing. Usage:

    python3 debug_feed.py            # all four venues
    python3 debug_feed.py bybit      # just one
"""

import asyncio
import json
import sys

import websockets

from config import EXCHANGES

N_MSGS = 5
TIMEOUT_S = 15

SUBSCRIBE = {
    "binance": None,
    "coinbase": lambda cfg: {"type": "subscribe",
                             "product_ids": [cfg["symbol"]],
                             "channels": ["ticker"]},
    "kraken": lambda cfg: {"method": "subscribe",
                           "params": {"channel": "ticker",
                                      "symbol": [cfg["symbol"]],
                                      "event_trigger": "bbo"}},
    "bybit": lambda cfg: {"op": "subscribe",
                          "args": [f"tickers.{cfg['symbol']}"]},
}


async def probe(name):
    cfg = EXCHANGES[name]
    print(f"\n===== {name}  ({cfg['ws_url']})  symbol={cfg['symbol']} =====")
    try:
        async with websockets.connect(cfg["ws_url"], ping_interval=20,
                                      open_timeout=10) as ws:
            sub = SUBSCRIBE[name]
            if sub:
                await ws.send(json.dumps(sub(cfg)))
                print("subscribed, waiting for messages...")
            else:
                print("connected (stream URL, no subscribe needed)...")
            for i in range(N_MSGS):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT_S)
                except asyncio.TimeoutError:
                    print(f"  [{i}] TIMEOUT: no message within {TIMEOUT_S}s")
                    break
                text = raw if isinstance(raw, str) else raw.decode(errors="replace")
                print(f"  [{i}] {text[:300]}")
    except Exception as e:
        print(f"  CONNECT FAILED: {type(e).__name__}: {e}")


async def main(targets):
    for name in targets:
        await probe(name)


if __name__ == "__main__":
    targets = sys.argv[1:] or list(EXCHANGES)
    bad = [t for t in targets if t not in EXCHANGES]
    if bad:
        print("unknown exchange(s):", bad, "-- choices:", list(EXCHANGES))
        sys.exit(1)
    asyncio.run(main(targets))
