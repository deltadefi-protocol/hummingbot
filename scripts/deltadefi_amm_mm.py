import json
import logging
import os
import random
import time
from collections import deque
from decimal import Decimal
from typing import Dict, List, NamedTuple, Optional

from pydantic import Field, model_validator

from hummingbot.client.config.config_data_types import BaseClientModel
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.event.events import OrderFilledEvent
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

D = Decimal
ZERO = D("0")

PAIR_PRESETS = {
    "ADA-USDM": {"initial_price": D("0.2762"), "pool_depth": D("17000")},
    "IAG-USDM": {"initial_price": D("0.26"), "pool_depth": D("10000")},
    "NIGHT-USDM": {"initial_price": D("0.0001"), "pool_depth": D("5000")},
}


class OrderProposal(NamedTuple):
    side: TradeType
    price: Decimal
    size: Decimal


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class DeltaDefiAMMConfig(BaseClientModel):
    script_file_name: str = os.path.basename(__file__)
    # Required (must set per pair)
    exchange: str = Field("deltadefi")
    trading_pair: str = Field(default="IAG-USDM")
    initial_price: Optional[Decimal] = Field(default=None)
    pool_depth: Optional[Decimal] = Field(default=None)
    base_spread_bps: Decimal = Field(D("100"))
    max_cumulative_loss: Decimal = Field(D("100"))
    min_base_balance: Decimal = Field(D("1000"))
    min_quote_balance: Decimal = Field(D("500"))
    # Optional (hardcode defaults, overridable in config)
    amplification: Decimal = Field(D("20"))
    num_levels: int = Field(3)
    size_decay: Decimal = Field(D("0.7"))
    spread_multiplier: Decimal = Field(D("1.5"))
    order_amount_pct: Decimal = Field(D("0.015"))
    order_refresh_time: int = Field(5)
    refresh_on_fill_only: bool = Field(True)
    floor_ratio: Decimal = Field(D("0.30"))
    balance_buffer_pct: Decimal = Field(D("0.90"))
    rebalance_threshold: Decimal = Field(D("0.02"))
    rebalance_cooldown: int = Field(60)
    min_both_sides_pct: Decimal = Field(D("0.40"))
    # Enhancement flags
    enable_fill_velocity_detector: bool = Field(False)
    fill_velocity_window_sec: int = Field(10)
    fill_velocity_max_same_side: int = Field(3)
    enable_asymmetric_spread: bool = Field(False)
    skew_sensitivity: Decimal = Field(D("0.5"))
    min_spread_bps: Decimal = Field(D("20"))
    enable_order_randomization: bool = Field(False)
    randomization_pct: Decimal = Field(D("0.15"))
    breakeven_margin_bps: Decimal = Field(D("10"))

    @model_validator(mode="after")
    def apply_pair_presets(self):
        preset = PAIR_PRESETS.get(self.trading_pair, {})
        if self.initial_price is None:
            self.initial_price = preset.get("initial_price", D("0.01"))
        if self.pool_depth is None:
            self.pool_depth = preset.get("pool_depth", D("5000"))
        return self


# ---------------------------------------------------------------------------
# VirtualPool — pricing via amplified x*y=k
# ---------------------------------------------------------------------------


class VirtualPool:
    """Virtual AMM reserves. Updates only on fills. Ignores deposits,
    withdrawals, and other bots on the same account."""

    def __init__(self, initial_price: Decimal, pool_depth: Decimal, amplification: Decimal):
        self.initial_quote = D(str(pool_depth))
        self.initial_base = self.initial_quote / D(str(initial_price))
        self.base = self.initial_base
        self.quote = self.initial_quote
        self.k = self.base * self.quote
        self.amplification = D(str(amplification))

    @classmethod
    def from_state(cls, state: dict) -> "VirtualPool":
        pool = cls.__new__(cls)
        pool.initial_base = D(str(state["initial_base"]))
        pool.initial_quote = D(str(state["initial_quote"]))
        pool.base = D(str(state["base"]))
        pool.quote = D(str(state["quote"]))
        pool.k = D(str(state["k"]))
        pool.amplification = D(str(state["amplification"]))
        return pool

    def to_state(self) -> dict:
        return {
            "initial_base": str(self.initial_base),
            "initial_quote": str(self.initial_quote),
            "base": str(self.base),
            "quote": str(self.quote),
            "k": str(self.k),
            "amplification": str(self.amplification),
        }

    def get_mid_price(self) -> Decimal:
        if self.base <= ZERO:
            return self.quote
        inventory_ratio = self.base / self.initial_base
        raw_adj = D(1) / inventory_ratio
        dampened_adj = D(1) + (raw_adj - D(1)) / self.amplification
        return (self.quote / self.base) * dampened_adj

    def update_on_fill(self, side: TradeType, amount: Decimal):
        filled = D(str(amount))
        if side == TradeType.SELL:      # we sold base → reserves shrink
            self.base -= filled
        elif side == TradeType.BUY:     # we bought base → reserves grow
            self.base += filled
        if self.base > ZERO:
            self.quote = self.k / self.base

    def get_inventory_skew(self) -> float:
        base_ratio = float(self.base / self.initial_base)
        quote_ratio = float(self.quote / self.initial_quote)
        denom = base_ratio + quote_ratio
        if denom == 0:
            return 0.0
        return (base_ratio - quote_ratio) / denom

    def get_available_reserves(self, floor_ratio: Decimal):
        floor = D(str(floor_ratio))
        base_avail = max(ZERO, self.base - self.initial_base * floor)
        quote_avail = max(ZERO, self.quote - self.initial_quote * floor)
        return base_avail, quote_avail


# ---------------------------------------------------------------------------
# BalanceGate — scales orders to real account balance
# ---------------------------------------------------------------------------


class BalanceGate:
    """Reads real balance from exchange connector each cycle. Scales or
    removes orders that exceed what the account can support."""

    def __init__(self, connector: ConnectorBase, config: DeltaDefiAMMConfig):
        self.connector = connector
        self.config = config

    def get_real_balances(self):
        base_token, quote_token = self.config.trading_pair.split("-")
        return (
            self.connector.get_available_balance(base_token),
            self.connector.get_available_balance(quote_token),
        )

    def scale_orders(self, orders: List[OrderProposal]) -> List[OrderProposal]:
        real_base, real_quote = self.get_real_balances()
        usable_base = real_base * self.config.balance_buffer_pct
        usable_quote = real_quote * self.config.balance_buffer_pct

        # Remove sides below minimum
        if real_base < self.config.min_base_balance:
            orders = [o for o in orders if o.side != TradeType.SELL]
        if real_quote < self.config.min_quote_balance:
            orders = [o for o in orders if o.side != TradeType.BUY]

        if not orders:
            return orders

        # Scale asks to fit real base
        total_ask = sum(o.size for o in orders if o.side == TradeType.SELL)
        if total_ask > ZERO and total_ask > usable_base:
            scale = usable_base / total_ask
            orders = [
                OrderProposal(o.side, o.price, o.size * scale) if o.side == TradeType.SELL else o
                for o in orders
            ]

        # Scale bids to fit real quote
        total_bid_quote = sum(o.size * o.price for o in orders if o.side == TradeType.BUY)
        if total_bid_quote > ZERO and total_bid_quote > usable_quote:
            scale = usable_quote / total_bid_quote
            orders = [
                OrderProposal(o.side, o.price, o.size * scale) if o.side == TradeType.BUY else o
                for o in orders
            ]

        return orders


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class DeltaDefiAMM(ScriptStrategyBase):
    _default_config = DeltaDefiAMMConfig()
    markets = {_default_config.exchange: {_default_config.trading_pair}}
    _refresh_timestamp: float = 0
    _last_rebalance: float = 0
    _stopped: bool = False

    @classmethod
    def init_markets(cls, config: DeltaDefiAMMConfig):
        cls.markets = {config.exchange: {config.trading_pair}}

    def __init__(self, connectors: Dict[str, ConnectorBase], config: Optional[DeltaDefiAMMConfig] = None):
        super().__init__(connectors, config)
        if self.config is None:
            self.config = DeltaDefiAMMConfig()
        self._state_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
        os.makedirs(self._state_dir, exist_ok=True)
        self._state_file = os.path.join(self._state_dir, f"{self.config.trading_pair}_pool_state.json")

        state = self._load_state()
        if state:
            self.pool = VirtualPool.from_state(state)
            self._base_flow = D(str(state.get("base_flow", "0")))
            self._quote_flow = D(str(state.get("quote_flow", "0")))
            self._total_bought = D(str(state.get("total_bought", "0")))
            self._total_buy_cost = D(str(state.get("total_buy_cost", "0")))
            self._total_sold = D(str(state.get("total_sold", "0")))
            self._total_sell_revenue = D(str(state.get("total_sell_revenue", "0")))
            self.logger().info(f"Restored pool state from {self._state_file}")
        else:
            self.pool = VirtualPool(self.config.initial_price, self.config.pool_depth, self.config.amplification)
            self._base_flow = ZERO
            self._quote_flow = ZERO
            self._total_bought = ZERO
            self._total_buy_cost = ZERO
            self._total_sold = ZERO
            self._total_sell_revenue = ZERO
            self.logger().info(
                f"Initialized new pool: {self.config.trading_pair} @ {self.config.initial_price} "
                f"depth={self.config.pool_depth}"
            )

        self.balance_gate = BalanceGate(connectors[self.config.exchange], self.config)
        self._recent_fills: deque = deque()

    # ---- Main loop --------------------------------------------------------

    def on_tick(self):
        if self._stopped:
            return

        # Wait for trading rules to be loaded before generating orders
        connector = self.connectors[self.config.exchange]
        if self.config.trading_pair not in connector.trading_rules:
            return

        if self._check_circuit_breakers():
            return

        has_active = bool(self.get_active_orders(connector_name=self.config.exchange))

        # In fill-only mode, skip if orders are already on the book
        if self.config.refresh_on_fill_only and has_active:
            return

        if self.current_timestamp < self._refresh_timestamp:
            return

        self._cancel_all_orders()

        # Don't place new orders until previous cancels are confirmed
        if self.get_active_orders(connector_name=self.config.exchange):
            return

        mid = self._get_book_mid() or self.pool.get_mid_price()
        base_avail, quote_avail = self.pool.get_available_reserves(self.config.floor_ratio)

        orders = self._generate_orders(mid, base_avail, quote_avail)
        orders = self.balance_gate.scale_orders(orders)
        self._place_orders(orders)

        self._check_rebalance()
        self._refresh_timestamp = self.current_timestamp + self.config.order_refresh_time

    # ---- Order generation -------------------------------------------------

    def _generate_orders(
        self, mid_price: Decimal, avail_base: Decimal, avail_quote: Decimal
    ) -> List[OrderProposal]:
        orders: List[OrderProposal] = []
        total_ask_budget = avail_base * self.config.order_amount_pct
        total_bid_budget = avail_quote * self.config.order_amount_pct

        weights = [self.config.size_decay ** i for i in range(self.config.num_levels)]
        total_weight = sum(weights)
        if total_weight <= ZERO:
            return orders

        connector = self.connectors[self.config.exchange]
        pair = self.config.trading_pair

        margin = self.config.breakeven_margin_bps / D("10000")
        min_ask_floor = (self._total_buy_cost / self._total_bought) * (D(1) + margin) if self._total_bought > ZERO else None
        max_bid_ceil = (self._total_sell_revenue / self._total_sold) * (D(1) - margin) if self._total_sold > ZERO else None

        for i in range(self.config.num_levels):
            base_spread = self.config.base_spread_bps * (self.config.spread_multiplier ** i) / D("10000")
            w = weights[i] / total_weight

            if self.config.enable_asymmetric_spread:
                bid_spread, ask_spread = self._asymmetric_spreads(base_spread)
            else:
                bid_spread, ask_spread = base_spread, base_spread

            ask_price = mid_price * (D(1) + ask_spread)
            bid_price = mid_price * (D(1) - bid_spread)

            if min_ask_floor is not None:
                ask_price = max(ask_price, min_ask_floor)
            if max_bid_ceil is not None:
                bid_price = min(bid_price, max_bid_ceil)

            ask_size = total_ask_budget * w
            bid_size = (total_bid_budget * w) / bid_price if bid_price > ZERO else ZERO

            if self.config.enable_order_randomization:
                ask_size = self._randomize(ask_size)
                bid_size = self._randomize(bid_size)

            ask_price = connector.quantize_order_price(pair, ask_price)
            bid_price = connector.quantize_order_price(pair, bid_price)
            ask_size = connector.quantize_order_amount(pair, ask_size)
            bid_size = connector.quantize_order_amount(pair, bid_size)

            if ask_size > ZERO:
                orders.append(OrderProposal(TradeType.SELL, ask_price, ask_size))
            if bid_size > ZERO:
                orders.append(OrderProposal(TradeType.BUY, bid_price, bid_size))

        return orders

    def _asymmetric_spreads(self, base_spread: Decimal):
        skew = D(str(self.pool.get_inventory_skew()))
        sens = self.config.skew_sensitivity
        floor = self.config.min_spread_bps / D("10000")
        # Positive skew (excess base) → widen bid, tighten ask
        bid_spread = max(base_spread * (D(1) + skew * sens), floor)
        ask_spread = max(base_spread * (D(1) - skew * sens), floor)
        return bid_spread, ask_spread

    def _randomize(self, size: Decimal) -> Decimal:
        pct = float(self.config.randomization_pct)
        jitter = D(str(1.0 + random.uniform(-pct, pct)))
        return max(ZERO, size * jitter)

    # ---- Order execution --------------------------------------------------

    def _place_orders(self, orders: List[OrderProposal]):
        for o in orders:
            if o.side == TradeType.SELL:
                self.sell(self.config.exchange, self.config.trading_pair, o.size, OrderType.LIMIT, o.price)
            else:
                self.buy(self.config.exchange, self.config.trading_pair, o.size, OrderType.LIMIT, o.price)

    def _cancel_all_orders(self):
        safe_ensure_future(self._cancel_all_orders_async())

    async def _cancel_all_orders_async(self):
        connector = self.connectors[self.config.exchange]
        await connector.cancel_all(timeout_seconds=10.0)
        remaining = self.get_active_orders(connector_name=self.config.exchange)
        if remaining:
            self.logger().info(f"Retry cancel: {len(remaining)} orders still active after cancel-all")
            await connector.cancel_all(timeout_seconds=10.0)

    # ---- Circuit breakers -------------------------------------------------

    def _check_circuit_breakers(self) -> bool:
        # 1. Loss limit exceeded → full stop
        pnl = self._get_pnl()
        if pnl is not None and pnl < -self.config.max_cumulative_loss:
            self.logger().error(f"Loss limit exceeded: P&L {pnl:.2f}. Shutting down.")
            self._cancel_all_orders()
            self._stopped = True
            return True

        # 2. Both virtual sides depleted → pause
        base_pct = self.pool.base / self.pool.initial_base
        quote_pct = self.pool.quote / self.pool.initial_quote
        if base_pct < self.config.min_both_sides_pct and quote_pct < self.config.min_both_sides_pct:
            self.logger().warning("Both virtual sides depleted. Pausing.")
            self._cancel_all_orders()
            return True

        # 3. Fill velocity (enhancement)
        if self.config.enable_fill_velocity_detector:
            now = time.time()
            window = self.config.fill_velocity_window_sec
            while self._recent_fills and now - self._recent_fills[0][0] > window:
                self._recent_fills.popleft()
            buy_count = sum(1 for _, t in self._recent_fills if t == TradeType.BUY)
            sell_count = sum(1 for _, t in self._recent_fills if t == TradeType.SELL)
            if max(buy_count, sell_count) >= self.config.fill_velocity_max_same_side:
                self.logger().warning(f"Fill velocity: {buy_count}B/{sell_count}S in {window}s. Pausing.")
                self._cancel_all_orders()
                return True

        return False

    # ---- Rebalance --------------------------------------------------------

    def _check_rebalance(self):
        if time.time() - self._last_rebalance < self.config.rebalance_cooldown:
            return

        book_mid = self._get_book_mid()
        if book_mid is None or book_mid <= ZERO:
            return

        amm_mid = self.pool.get_mid_price()
        divergence = abs(amm_mid - book_mid) / book_mid

        if divergence <= self.config.rebalance_threshold:
            return

        target_base = (self.pool.k / book_mid).sqrt()
        rebalance_amount = abs(target_base - self.pool.base)
        side = TradeType.BUY if self.pool.base < target_base else TradeType.SELL

        if side == TradeType.SELL and self._total_bought > ZERO:
            avg_buy = self._total_buy_cost / self._total_bought
            if book_mid < avg_buy:
                return  # don't sell at a loss via market order

        if side == TradeType.BUY and self._total_sold > ZERO:
            avg_sell = self._total_sell_revenue / self._total_sold
            if book_mid > avg_sell:
                return  # don't buy above avg sell via market order

        # Cap to real balance
        real_base, real_quote = self.balance_gate.get_real_balances()
        if side == TradeType.BUY:
            max_amount = real_quote * self.config.balance_buffer_pct / book_mid
        else:
            max_amount = real_base * self.config.balance_buffer_pct
        rebalance_amount = min(rebalance_amount, max_amount)

        connector = self.connectors[self.config.exchange]
        rebalance_amount = connector.quantize_order_amount(self.config.trading_pair, rebalance_amount)
        if rebalance_amount <= ZERO:
            return

        self.logger().info(f"Rebalance: {side.name} {rebalance_amount} (divergence: {divergence:.4f})")
        if side == TradeType.BUY:
            self.buy(self.config.exchange, self.config.trading_pair, rebalance_amount, OrderType.MARKET)
        else:
            self.sell(self.config.exchange, self.config.trading_pair, rebalance_amount, OrderType.MARKET)
        self._last_rebalance = time.time()

    # ---- Fill handling ----------------------------------------------------

    def did_fill_order(self, event: OrderFilledEvent):
        # Track real flows
        if event.trade_type == TradeType.SELL:
            self._base_flow -= event.amount
            self._quote_flow += event.price * event.amount
            self._total_sold += event.amount
            self._total_sell_revenue += event.amount * event.price
        else:
            self._base_flow += event.amount
            self._quote_flow -= event.price * event.amount
            self._total_bought += event.amount
            self._total_buy_cost += event.amount * event.price

        # Update virtual pool
        self.pool.update_on_fill(event.trade_type, event.amount)

        # Track fill velocity
        if self.config.enable_fill_velocity_detector:
            self._recent_fills.append((time.time(), event.trade_type))

        # Persist state
        self._save_state()

        # Log
        pnl = self._get_pnl()
        pnl_str = f"{pnl:+.4f}" if pnl is not None else "n/a"
        msg = (
            f"AMM {event.trade_type.name} {event.amount:.4f} @ {event.price:.6f} | "
            f"P&L: {pnl_str}"
        )
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

        # Cancel stale orders — next on_tick will requote once cancels confirm
        self._cancel_all_orders()

    # ---- P&L --------------------------------------------------------------

    def _get_pnl(self) -> Optional[Decimal]:
        """Mark-to-market P&L: value of all base exchanged at book mid + net quote flow."""
        book_mid = self._get_book_mid()
        if book_mid is None or book_mid <= ZERO:
            return None
        return self._base_flow * book_mid + self._quote_flow

    # ---- Market data helpers ----------------------------------------------

    def _get_book_mid(self) -> Optional[Decimal]:
        try:
            mid = self.connectors[self.config.exchange].get_mid_price(self.config.trading_pair)
            return mid if mid is not None and mid > ZERO else None
        except Exception:
            return None

    # ---- State persistence ------------------------------------------------

    def _save_state(self):
        state = self.pool.to_state()
        state["base_flow"] = str(self._base_flow)
        state["quote_flow"] = str(self._quote_flow)
        state["total_bought"] = str(self._total_bought)
        state["total_buy_cost"] = str(self._total_buy_cost)
        state["total_sold"] = str(self._total_sold)
        state["total_sell_revenue"] = str(self._total_sell_revenue)
        with open(self._state_file, "w") as f:
            json.dump(state, f, indent=2)

    def _load_state(self) -> dict:
        if not os.path.exists(self._state_file):
            return {}
        try:
            with open(self._state_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError, IOError) as e:
            self.logger().warning(f"Failed to load state from {self._state_file}: {e}")
            return {}

    # ---- Status display ---------------------------------------------------

    def format_status(self) -> str:
        if not self.ready_to_trade:
            return "Market connectors are not ready."

        pool_mid = self.pool.get_mid_price()
        book_mid = self._get_book_mid()
        book_str = f"{book_mid:.6f}" if book_mid else "n/a"

        base_pct = self.pool.base / self.pool.initial_base * D(100)
        quote_pct = self.pool.quote / self.pool.initial_quote * D(100)
        skew = self.pool.get_inventory_skew()

        real_base, real_quote = self.balance_gate.get_real_balances()
        base_token, quote_token = self.config.trading_pair.split("-")

        pnl = self._get_pnl()
        pnl_str = f"{pnl:+.4f}" if pnl is not None else "n/a"

        lines = [
            f"  [AMM {self.config.trading_pair}] Anchor: {book_str} | Pool: {pool_mid:.6f}",
            f"  VPool: {base_pct:.1f}%B / {quote_pct:.1f}%Q | Skew: {skew:+.3f}",
            f"  Real: {real_base:.2f} {base_token} / {real_quote:.2f} {quote_token}",
            f"  P&L: {pnl_str} {quote_token} (limit: -{self.config.max_cumulative_loss})",
        ]

        # Break-even tracking
        if self._total_bought > ZERO:
            avg_buy = self._total_buy_cost / self._total_bought
            lines.append(f"  Avg Buy: {avg_buy:.6f} ({self._total_bought:.2f} {base_token})")
            if book_mid is not None and book_mid < avg_buy:
                lines.append(f"  ** ASK CLAMPED: market {book_mid:.6f} < avg_buy {avg_buy:.6f} **")
        if self._total_sold > ZERO:
            avg_sell = self._total_sell_revenue / self._total_sold
            lines.append(f"  Avg Sell: {avg_sell:.6f} ({self._total_sold:.2f} {base_token})")
            if book_mid is not None and book_mid > avg_sell:
                lines.append(f"  ** BID CLAMPED: market {book_mid:.6f} > avg_sell {avg_sell:.6f} **")

        if self._stopped:
            lines.append("  *** STOPPED - loss limit exceeded ***")

        active = self.get_active_orders(connector_name=self.config.exchange)
        if active:
            lines.append(f"  Orders ({len(active)}):")
            for o in sorted(active, key=lambda x: x.price, reverse=True):
                side = "ASK" if not o.is_buy else "BID"
                lines.append(f"    {side} {o.quantity:.4f} @ {o.price:.6f}")
        else:
            lines.append("  Orders: none")

        return "\n".join(lines)
