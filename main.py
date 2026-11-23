#!/usr/bin/env python3
"""BTC trend-following bot for agent-fun/bars-replay/v1 (make.fun btc-race).

Strategy: sparse, multi-hour trend following on 1-minute cbBTC bars.
A round trip costs roughly 26 bps in fees, slippage and price impact, so
the bot trades rarely: it enters only when price sits above its slow EMA
by a hysteresis band AND the trailing multi-hour return confirms the
trend, sizes the position by realised volatility, holds for at least a
minimum period, and exits when the trend breaks. An equity drawdown
guard forces an exit and a long cooldown after a sharp loss.

Deterministic: standard library only, no network, no clock, no files,
no randomness. Position state is taken from the account object of each
batch, which the harness keeps authoritative.
"""

import json
import math
import sys
from collections import deque

# ---------------------------------------------------------------------------
# Parameters (all tunable values live here)
# ---------------------------------------------------------------------------
EMA_N = 360               # slow EMA length in bars (6 hours)
ENTRY_BAND = 0.002        # entry needs close > EMA * (1 + band)
EXIT_BAND = 0.010         # exit when close < EMA * (1 - band); wide on
                          # purpose: only a real trend break ends a hold
TRAIL_N = 240             # trailing return window in bars (4 hours)
ENTRY_TRAIL = 0.002       # entry needs trailing return above +0.2%
EXIT_TRAIL = -0.015       # exit when trailing return below -1.5%
VOL_N = 240               # volatility window in bars (log returns)
TARGET_DAILY_VOL = 0.020  # target daily volatility of the position
MIN_FRAC = 0.35           # smallest position fraction of equity
MAX_FRAC = 0.80           # largest position fraction of equity
WARMUP = 240              # bars to observe before the first trade
MIN_HOLD = 240            # minimum holding period in bars
COOLDOWN = 120            # bars to wait after a normal exit
DD_GUARD = 0.09           # equity drawdown from peak that forces exit
GUARD_COOLDOWN = 480      # bars to stay out after a drawdown exit
MAX_SLIPPAGE_BPS = 300    # max_slippage_bps sent on every order

DEFAULT_SYMBOL = "CBBTC"


class Bot:
    def __init__(self):
        self.symbol = DEFAULT_SYMBOL
        self.qty_scale = 8
        self.min_notional = 1_000_000  # micro-USDC, from hello fees
        self.hello_warmup = 0

        self.bars_seen = 0             # batches that carried our bar
        self.closes = deque(maxlen=TRAIL_N + 1)
        self.logrets = deque(maxlen=VOL_N)
        self.prev_close = None
        self.ema = None

        self.peak_equity = 0
        self.holding = False
        self.position_since = -1
        self.cooldown_until = 0
        self.guard_until = 0
        self.pending_buy_bar = -1
        self.pending_sell_bar = -1
        self.order_seq = 0

    # -- hello ---------------------------------------------------------------
    def on_hello(self, msg):
        symbols = msg.get("symbols") or []
        if symbols:
            first = symbols[0]
            self.symbol = first.get("symbol", self.symbol)
            self.qty_scale = int(first.get("qty_scale", self.qty_scale))
        fees = msg.get("fees") or {}
        if "min_notional_quote_units" in fees:
            try:
                self.min_notional = int(fees["min_notional_quote_units"])
            except (TypeError, ValueError):
                pass
        if "warmup_bars" in msg:
            try:
                self.hello_warmup = int(msg["warmup_bars"])
            except (TypeError, ValueError):
                pass

    # -- helpers ---------------------------------------------------------------
    def _next_client_id(self, side):
        self.order_seq += 1
        return "%s%d" % (side, self.order_seq)

    def _position_qty(self, account):
        for pos in account.get("positions") or []:
            if pos.get("symbol") == self.symbol:
                try:
                    return int(pos.get("qty", "0"))
                except (TypeError, ValueError):
                    return 0
        return 0

    def _trailing_return(self):
        if len(self.closes) < TRAIL_N + 1:
            return None
        old = self.closes[0]
        if old <= 0:
            return None
        return self.closes[-1] / old - 1.0

    def _exposure_fraction(self):
        """Volatility-scaled fraction of equity to hold, in [MIN, MAX]."""
        n = len(self.logrets)
        if n < 30:
            return MAX_FRAC
        mean = sum(self.logrets) / n
        var = sum((r - mean) ** 2 for r in self.logrets) / n
        daily_vol = math.sqrt(var) * math.sqrt(1440.0)
        if daily_vol <= 1e-12:
            return MAX_FRAC
        frac = TARGET_DAILY_VOL / daily_vol
        if frac < MIN_FRAC:
            return MIN_FRAC
        if frac > MAX_FRAC:
            return MAX_FRAC
        return frac

    # -- batch -----------------------------------------------------------------
    def on_batch(self, msg):
        orders = []
        account = msg.get("account") or {}
        try:
            cash = int(account.get("cash_quote_units", "0"))
            equity = int(account.get("equity_quote_units", "0"))
        except (TypeError, ValueError):
            cash, equity = 0, 0
        qty = self._position_qty(account)

        if equity > self.peak_equity:
            self.peak_equity = equity

        # Find our bar; a batch without it changes no indicators.
        bar = None
        for b in msg.get("bars") or []:
            if b.get("symbol") == self.symbol:
                bar = b
                break
        if bar is None:
            return orders
        try:
            close = float(bar["c"])
        except (TypeError, ValueError, KeyError):
            return orders
        if close <= 0:
            return orders

        self.bars_seen += 1
        self.closes.append(close)
        if self.prev_close is not None and self.prev_close > 0:
            self.logrets.append(math.log(close / self.prev_close))
        self.prev_close = close
        if self.ema is None:
            self.ema = close
        else:
            alpha = 2.0 / (EMA_N + 1)
            self.ema += alpha * (close - self.ema)

        # Reconcile position state with the authoritative account.
        now_holding = qty > 0
        if now_holding and not self.holding:
            self.position_since = self.bars_seen
            self.pending_buy_bar = -1
        elif not now_holding and self.holding:
            self.pending_sell_bar = -1
            if self.bars_seen > self.cooldown_until:
                self.cooldown_until = self.bars_seen + COOLDOWN
        self.holding = now_holding

        # Clear stale pending flags (order must have been rejected).
        if self.pending_buy_bar >= 0 and not now_holding \
                and self.bars_seen > self.pending_buy_bar:
            self.pending_buy_bar = -1
        if self.pending_sell_bar >= 0 and now_holding \
                and self.bars_seen > self.pending_sell_bar:
            self.pending_sell_bar = -1

        warmup = max(WARMUP, self.hello_warmup)
        trail = self._trailing_return()

        if self.holding and self.pending_sell_bar < 0:
            forced = self.peak_equity > 0 and \
                equity <= self.peak_equity * (1.0 - DD_GUARD)
            trend_exit = False
            if self.bars_seen - self.position_since >= MIN_HOLD:
                if close < self.ema * (1.0 - EXIT_BAND):
                    trend_exit = True
                elif trail is not None and trail < EXIT_TRAIL:
                    trend_exit = True
            if forced or trend_exit:
                orders.append({
                    "side": "sell",
                    "client_id": self._next_client_id("s"),
                    "symbol": self.symbol,
                    "qty": str(qty),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                })
                self.pending_sell_bar = self.bars_seen
                if forced:
                    self.guard_until = self.bars_seen + GUARD_COOLDOWN
        elif not self.holding and self.pending_buy_bar < 0:
            if self.bars_seen >= warmup \
                    and self.bars_seen >= self.cooldown_until \
                    and self.bars_seen >= self.guard_until \
                    and trail is not None \
                    and close > self.ema * (1.0 + ENTRY_BAND) \
                    and trail > ENTRY_TRAIL:
                frac = self._exposure_fraction()
                notional = int(equity * frac)
                if notional > cash:
                    notional = cash
                if notional >= self.min_notional:
                    orders.append({
                        "side": "buy",
                        "client_id": self._next_client_id("b"),
                        "symbol": self.symbol,
                        "notional_quote_units": str(notional),
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    })
                    self.pending_buy_bar = self.bars_seen
        return orders


def main():
    bot = Bot()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mtype = msg.get("type")
        if mtype == "hello":
            bot.on_hello(msg)
        elif mtype == "batch":
            try:
                orders = bot.on_batch(msg)
            except Exception:
                orders = []
            reply = {"type": "reply", "seq": msg.get("seq"), "orders": orders}
            out.write(json.dumps(reply) + "\n")
            out.flush()
        elif mtype == "done":
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
