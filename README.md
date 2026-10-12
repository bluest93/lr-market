# Lieb-Robinson Bounds for the Market Microstructure Graph
### Empirical measurement of cross-exchange price convergence in BTC/USDT

This project measures how fast mid-price dislocations between crypto exchanges
(Binance, Coinbase, Kraken, Bybit) decay, and asks whether the decay-time floor
is consistent with the geographic information-propagation delay between venues —
an empirical analog of the Lieb-Robinson velocity for a market graph whose
vertices are matching engines and whose edges are communication channels.

**Cost: $0.** All data comes from the exchanges' free public WebSocket feeds
(top-of-book only, no API key required). A VPS is optional; your own machine
works for a first pass.

## Quick start

Uses [uv](https://docs.astral.sh/uv/) — it creates the virtualenv and installs
the pinned dependencies (`uv.lock`) automatically on the first `uv run`.

```bash
uv sync

# 1. Validate the analysis pipeline on synthetic data with KNOWN half-lives
uv run python simulate.py --out data_sim --hours 2
uv run python analyze.py --data-dir data_sim --out-dir results_sim

# 2. Collect real data (run 24-48h minimum; sync your clock with chrony first)
uv run python collect.py --data-dir data

# 3. Analyze real data (two stages: 100 ms detection, then 1 ms refit)
uv run python analyze.py --data-dir data --out-dir results --fetch-latency
uv run python refine_events.py --data-dir data --events results/events.csv \
    --out results/refined_events.csv --grid-ms 1

# 4. Results notebook (figures, venue-effects model, robustness checks)
uv run jupyter lab notebooks/lr_market_results.ipynb
```

Outputs: `events.csv` (every detected dislocation with duration, peak size in
bps, fitted half-life, fit quality), `refined_events.csv` (the same events
re-measured at 1 ms, under two fitting conventions), `summary.csv` (per-pair
medians joined with great-circle light-time and measured network RTT between
venues), and `summary.png`.

## Methodology

The collector subscribes to best bid/ask on all four venues simultaneously and
records each update with a local nanosecond timestamp. Records are deduplicated
on price (quantity-only updates are dropped), which keeps disk usage to roughly
50–150 MB/day. A background probe measures TCP round-trip time to each
exchange's REST host every 60 seconds, giving you a record of your own network
position over the collection window.

The analysis merges all venues onto a common 100 ms grid, then for each
exchange pair computes the mid-price difference in basis points, removes the
slow-moving basis with a trailing rolling median (this absorbs persistent
offsets from fee structures and USDT transfer frictions), and normalizes by a
trailing robust noise scale. A dislocation event begins when the residual
exceeds both 4 robust sigmas and the sum of the two venues' half-spreads — the
second condition has a direct economic meaning: the mid-price gap is wider than
what a taker-taker round trip would pay in spread, i.e. the dislocation is
in principle tradeable before fees. The event ends with hysteresis, when the
residual falls back below 25% of the entry threshold. Convergence time is the
entry-to-exit duration; the half-life comes from fitting an exponential decay
from the event's peak. All rolling statistics are strictly trailing — there is
no look-ahead anywhere in the pipeline.

Finally, per-pair median half-lives are compared against the one-way
light-travel time between the venues' (approximate) matching-engine locations:

| Exchange | Assumed location | Pair light-times |
|---|---|---|
| Binance | Tokyo (AWS ap-northeast-1) | Tokyo–Singapore ~18 ms |
| Bybit | Singapore (AWS ap-southeast-1) | London–Virginia ~20 ms |
| Coinbase | N. Virginia (AWS us-east-1) | Tokyo–Virginia ~36 ms |
| Kraken | London (Equinix LD4 area) | Virginia–Singapore ~52 ms |

These locations are based on public reporting and **must be re-verified before
publishing** — exchanges migrate infrastructure.

## Validation

`simulate.py` generates synthetic quotes in the exact collector format, with
dislocations injected at known exponential decay constants (half-lives of
0.35 s, 0.55 s, 1.04 s, 1.73 s across the four venues). The pipeline recovers
per-pair medians within ~10–20% of the expected values of the mixed
distributions, detects >90% of injected events, and correctly shows bimodal
distributions for pairs mixing a fast and a slow venue. Run it yourself before
trusting any real-data numbers.

## Honest limitations (put these in any writeup)

You are observing from a single vantage point, so each feed reaches you with a
different, asymmetric network delay; the TCP-RTT log bounds but does not
eliminate this. WebSocket dissemination adds exchange-side batching and queuing
delay on top of matching-engine time, so everything you measure is an **upper
bound** on the true convergence time — the retail-observable lightcone, not
the co-located one. HFT-level convergence happens at microsecond scales you
cannot see from public feeds; what you can see is whether the *residual*
dislocation lifetime still carries a geographic signature. Crypto market
structure also differs from equities: no NBBO, no Reg NMS, inventory must be
pre-positioned on both venues (there is no instant settlement across
exchanges), so "arbitrage" here means statistical convergence trading, not
riskless arbitrage. Coinbase's BTC-USDT book is much thinner than BTC-USD; if
event counts are low, switch to BTC-USD and let the rolling-median baseline
absorb the USDT/USD basis. Finally, with four venues you only get six pair
points for the distance regression — the config is structured so adding OKX,
Bitstamp, or Gemini is a ~20-line parser each.

## Upgrade path

The natural extensions, roughly in order of value: run collectors on three
regional VPSs (Tokyo, Virginia, London — a few dollars/month each) so each
exchange is observed from a nearby vantage point, shrinking the observation
delay asymmetry; add more venues to densify the distance regression; replace
top-of-book with depth snapshots to measure *executable* dislocation size
after walking the book; condition half-lives on volatility regime and time of
day; and fit the LR-style functional form directly — testing whether influence
outside the effective lightcone decays exponentially, not just whether the
floor scales with distance.
