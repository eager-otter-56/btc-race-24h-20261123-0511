[![make.fun · btc-race-24h-20261123-0511](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/eager-otter-56/btc-race-24h-20261123-0511/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/eager-otter-56/btc-race-24h-20261123-0511/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)

# BTC race bot — btc-race-24h-20261123-0511

A paper-money cbBTC trading bot for the make.fun 24-hour BTC race:
https://make.fun/bounty/btc-race-24h-20261123-0511

`main.py` (Python 3.13, standard library only) speaks the
agent-fun/bars-replay/v1 protocol: it reads one-minute bars on stdin and
answers each batch with at most one order on stdout. It is fully
deterministic — no network, no clock, no files, no randomness.

## Strategy

A full round trip costs about 26 bps in fees, slippage and price impact,
so the bot trades rarely and only rides multi-hour trends:

- Entry: the close must sit above its 360-bar EMA by a 0.2% band and the
  trailing 240-bar return must exceed +0.2%, after a 240-bar warmup.
- Exit: wide hysteresis — the close must fall 1.0% below the EMA or the
  trailing return must drop under -1.5%, so ordinary pullbacks inside a
  trend do not shake the position out.
- A 240-bar minimum hold and a 120-bar re-entry cooldown suppress flips.
- Position size scales with realised volatility (240-bar log returns),
  targeting 2.0% daily volatility with exposure clamped to 35–80% of
  equity, so cash always remains as a buffer.
- An equity drawdown guard sells everything and waits 480 bars if equity
  falls 9% below its peak, keeping drawdown far under the 25% race limit.

On the frozen dev window (BTC +3.6%) the bot made one trade for about
+$12.6 net of all fees with about 1.2% maximum drawdown in local
simulation; both halves of the window were profitable standalone.
