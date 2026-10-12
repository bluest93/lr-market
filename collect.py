"""Collect top-of-book BTC/USDT quotes from Binance, Coinbase, Kraken, Bybit
over their FREE public WebSocket feeds (no API key, no cost), plus a periodic
TCP round-trip latency probe to each exchange's REST host.

Output (CSV, one file per exchange per UTC day):
    data/quotes_{exchange}_{YYYYMMDD}.csv
        exchange,symbol,bid,ask,bid_qty,ask_qty,exch_ts_ms,local_ts_ns
    data/latency_{YYYYMMDD}.csv
        local_ts_ns,exchange,host,rtt_ms

Usage:
    python collect.py --data-dir data
Run for at least 24-48 h. Sync your clock first (chrony/NTP) -- local
timestamps are the backbone of the whole analysis.
"""

import argparse
import asyncio
import json
import sys
import time

import websockets

from config import EXCHANGES

FLUSH_EVERY_ROWS = 200
FLUSH_EVERY_S = 5.0
HEARTBEAT_S = 5.0          # write a row at least this often even if unchanged
LATENCY_PERIOD_S = 60.0


def log(*args):
    print(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), *args,
          file=sys.stderr, flush=True)


class QuoteWriter:
    """Buffered CSV writer, one file per (exchange, UTC day).
    Skips records where neither bid nor ask price changed (dedupe)."""

    HEADER = "exchange,symbol,bid,ask,bid_qty,ask_qty,exch_ts_ms,local_ts_ns\n"

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.handles = {}          # (exchange, day) -> file handle
        self.last_px = {}          # exchange -> (bid, ask)
        self.last_write_ns = {}    # exchange -> local ns of last row
        self.rows_since_flush = 0
        self.last_flush = time.time()

    def _handle(self, exchange, day):
        key = (exchange, day)
        if key not in self.handles:
            # close stale handles for this exchange (previous day)
            for k in [k for k in self.handles if k[0] == exchange and k != key]:
                self.handles.pop(k).close()
            path = f"{self.data_dir}/quotes_{exchange}_{day}.csv"
            new = False
            try:
                f = open(path, "x")
                new = True
            except FileExistsError:
                f = open(path, "a")
            if new:
                f.write(self.HEADER)
            self.handles[key] = f
        return self.handles[key]

    def write(self, exchange, symbol, bid, ask, bid_qty, ask_qty, exch_ts_ms):
        if bid is None or ask is None:
            return
        local_ns = time.time_ns()
        if (self.last_px.get(exchange) == (bid, ask)
                and local_ns - self.last_write_ns.get(exchange, 0)
                    < HEARTBEAT_S * 1e9):
            return                      # unchanged and recently recorded; skip
        self.last_px[exchange] = (bid, ask)
        self.last_write_ns[exchange] = local_ns
        day = time.strftime("%Y%m%d", time.gmtime())
        f = self._handle(exchange, day)
        f.write(f"{exchange},{symbol},{bid},{ask},{bid_qty},{ask_qty},"
                f"{exch_ts_ms},{local_ns}\n")
        self.rows_since_flush += 1
        now = time.time()
        if (self.rows_since_flush >= FLUSH_EVERY_ROWS
                or now - self.last_flush >= FLUSH_EVERY_S):
            for h in self.handles.values():
                h.flush()
            self.rows_since_flush = 0
            self.last_flush = now

    def close(self):
        for h in self.handles.values():
            h.close()


# ---------------- per-exchange session handlers ----------------
# Each connects, subscribes, and yields normalized quotes to the writer.

async def session_binance(cfg, writer):
    async with websockets.connect(cfg["ws_url"], ping_interval=20) as ws:
        log("binance connected")
        async for raw in ws:
            m = json.loads(raw)
            if m.get("s") and m.get("b") and m.get("a"):
                writer.write("binance", m["s"], m["b"], m["a"],
                             m.get("B", ""), m.get("A", ""), "")


async def session_coinbase(cfg, writer):
    async with websockets.connect(cfg["ws_url"], ping_interval=20) as ws:
        await ws.send(json.dumps({
            "type": "subscribe",
            "product_ids": [cfg["symbol"]],
            "channels": ["ticker"],
        }))
        log("coinbase connected")
        async for raw in ws:
            m = json.loads(raw)
            if m.get("type") == "ticker" and m.get("best_bid") and m.get("best_ask"):
                writer.write("coinbase", m.get("product_id", cfg["symbol"]),
                             m["best_bid"], m["best_ask"],
                             m.get("best_bid_size", ""),
                             m.get("best_ask_size", ""),
                             m.get("time", ""))


async def session_kraken(cfg, writer):
    async with websockets.connect(cfg["ws_url"], ping_interval=20) as ws:
        await ws.send(json.dumps({
            "method": "subscribe",
            "params": {"channel": "ticker", "symbol": [cfg["symbol"]],
                       "event_trigger": "bbo"},   # push on BBO change, not only trades
        }))
        log("kraken connected")
        async for raw in ws:
            m = json.loads(raw)
            if m.get("channel") == "ticker" and m.get("data"):
                for d in m["data"]:
                    if d.get("bid") is not None and d.get("ask") is not None:
                        writer.write("kraken", d.get("symbol", cfg["symbol"]),
                                     d["bid"], d["ask"],
                                     d.get("bid_qty", ""), d.get("ask_qty", ""),
                                     "")


async def session_bybit(cfg, writer):
    async with websockets.connect(cfg["ws_url"], ping_interval=None) as ws:
        # NOTE: the spot "tickers" stream carries 24h statistics only (no
        # bid/ask) -- top-of-book lives on the level-1 orderbook stream.
        await ws.send(json.dumps({"op": "subscribe",
                                  "args": [f"orderbook.1.{cfg['symbol']}"]}))
        log("bybit connected")

        async def keepalive():
            while True:
                await asyncio.sleep(20)
                await ws.send(json.dumps({"op": "ping"}))

        ka = asyncio.create_task(keepalive())
        last = {"bid": None, "ask": None, "bq": "", "aq": ""}
        try:
            async for raw in ws:
                m = json.loads(raw)
                if not str(m.get("topic", "")).startswith("orderbook"):
                    continue
                d = m.get("data", {})
                b, a = d.get("b") or [], d.get("a") or []
                # snapshot carries both sides; deltas carry only the changed
                # side. A level with size "0" is a removal -- keep the last
                # known price until the replacement level arrives.
                if b and len(b[0]) >= 2 and b[0][1] != "0":
                    last["bid"], last["bq"] = b[0][0], b[0][1]
                if a and len(a[0]) >= 2 and a[0][1] != "0":
                    last["ask"], last["aq"] = a[0][0], a[0][1]
                if last["bid"] and last["ask"]:
                    writer.write("bybit", d.get("s", cfg["symbol"]),
                                 last["bid"], last["ask"],
                                 last["bq"], last["aq"], m.get("ts", ""))
        finally:
            ka.cancel()


SESSIONS = {
    "binance": session_binance,
    "coinbase": session_coinbase,
    "kraken": session_kraken,
    "bybit": session_bybit,
}


async def run_exchange(name, cfg, writer):
    """Reconnect loop with exponential backoff."""
    backoff = 1.0
    while True:
        started = time.time()
        try:
            await SESSIONS[name](cfg, writer)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"{name} error: {type(e).__name__}: {e}")
        if time.time() - started > 300:
            backoff = 1.0                 # stable for 5 min -> reset backoff
        log(f"{name} reconnecting in {backoff:.0f}s")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


# ---------------- latency probe ----------------

async def probe_rtt(host, port=443, samples=3, timeout=5.0):
    """Best-of-N TCP connect time (~1 network round trip), in ms."""
    best = None
    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            reader, wr = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout)
            dt = (time.perf_counter() - t0) * 1000.0
            wr.close()
            try:
                await wr.wait_closed()
            except Exception:
                pass
            best = dt if best is None else min(best, dt)
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return best


async def latency_loop(data_dir):
    while True:
        day = time.strftime("%Y%m%d", time.gmtime())
        path = f"{data_dir}/latency_{day}.csv"
        rows = []
        for name, cfg in EXCHANGES.items():
            rtt = await probe_rtt(cfg["rest_host"])
            if rtt is not None:
                rows.append(f"{time.time_ns()},{name},{cfg['rest_host']},{rtt:.3f}\n")
        try:
            with open(path, "x") as f:
                f.write("local_ts_ns,exchange,host,rtt_ms\n")
        except FileExistsError:
            pass
        with open(path, "a") as f:
            f.writelines(rows)
        await asyncio.sleep(LATENCY_PERIOD_S)


async def main(data_dir):
    writer = QuoteWriter(data_dir)
    tasks = [asyncio.create_task(run_exchange(n, c, writer))
             for n, c in EXCHANGES.items()]
    tasks.append(asyncio.create_task(latency_loop(data_dir)))
    try:
        await asyncio.gather(*tasks)
    finally:
        writer.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()
    import os
    os.makedirs(args.data_dir, exist_ok=True)
    try:
        asyncio.run(main(args.data_dir))
    except KeyboardInterrupt:
        log("stopped by user")
