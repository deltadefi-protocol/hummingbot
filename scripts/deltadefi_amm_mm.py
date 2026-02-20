import json
import logging
import os
import random
import time
from collections import deque
from decimal import Decimal
from typing import Dict, List, NamedTuple, Optional

from pydantic import Field

from hummingbot.client.config.config_data_types import BaseClientModel
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.connector.exchange.deltadefi import deltadefi_constants as CONSTANTS
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.event.events import OrderFilledEvent
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

D = Decimal
ZERO = D("0")


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
    trading_pair: str = Field("ADA-USDM")
    initial_price: Decimal = Field(D("0.2789"))
    pool_depth: Decimal = Field(D("5000"))
    base_spread_bps: Decimal = Field(D("20"))
    max_cumulative_loss: Decimal = Field(D("100"))
    min_base_balance: Decimal = Field(D("1000"))
    min_quote_balance: Decimal = Field(D("500"))
    # Optional (hardcode defaults, overridable in config)
    amplification: Decimal = Field(D("20"))
    num_levels: int = Field(3)
    size_decay: Decimal = Field(D("0.7"))
    order_amount_pct: Decimal = Field(D("0.05"))
    order_refresh_time: int = Field(5)
    floor_ratio: Decimal = Field(D("0.30"))
    balance_buffer_pct: Decimal = Field(D("0.90"))
    rebalance_threshold: Decimal = Field(D("0.005"))
    rebalance_cooldown: int = Field(30)
    max_book_spread_bps: Decimal = Field(D("150"))
    min_both_sides_pct: Decimal = Field(D("0.40"))
    # Enhancement flags
    enable_fill_velocity_detector: bool = Field(False)
    fill_velocity_window_sec: int = Field(10)
    fill_velocity_max_same_side: int = Field(3)
    enable_tapster_monitor: bool = Field(False)
    tapster_divergence_cancel_bps: Decimal = Field(D("30"))
    enable_asymmetric_spread: bool = Field(False)
    skew_sensitivity: Decimal = Field(D("0.5"))
    min_spread_bps: Decimal = Field(D("20"))
    enable_order_randomization: bool = Field(False)
    randomization_pct: Decimal = Field(D("0.15"))


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
    markets = {"deltadefi": {"ADA-USDM"}}
    _refresh_timestamp: float = 0
    _last_rebalance: float = 0
    _stopped: bool = False
    _needs_refresh: bool = False

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
            self.logger().info(f"Restored pool state from {self._state_file}")
        else:
            self.pool = VirtualPool(self.config.initial_price, self.config.pool_depth, self.config.amplification)
            self._base_flow = ZERO
            self._quote_flow = ZERO
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

        if self._needs_refresh:
            self._needs_refresh = False
            self._cancel_all_orders()
            mid = self.pool.get_mid_price()
            base_avail, quote_avail = self.pool.get_available_reserves(self.config.floor_ratio)
            orders = self._generate_orders(mid, base_avail, quote_avail)
            orders = self.balance_gate.scale_orders(orders)
            self._place_orders(orders)
            self._check_rebalance()
            self._refresh_timestamp = self.current_timestamp + self.config.order_refresh_time
            return

        if self.current_timestamp < self._refresh_timestamp:
            return

        self._cancel_all_orders()

        mid = self.pool.get_mid_price()
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

        for i in range(self.config.num_levels):
            base_spread = self.config.base_spread_bps * D(2 * i + 1) / D("10000")
            w = weights[i] / total_weight

            if self.config.enable_asymmetric_spread:
                bid_spread, ask_spread = self._asymmetric_spreads(base_spread)
            else:
                bid_spread, ask_spread = base_spread, base_spread

            ask_price = mid_price * (D(1) + ask_spread)
            bid_price = mid_price * (D(1) - bid_spread)

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
        await connector._api_request(
            path_url=CONSTANTS.CANCEL_ALL_PATH,
            method=RESTMethod.POST,
            is_auth_required=True,
            limit_id=CONSTANTS.CANCEL_ALL_PATH,
        )
        for order in self.get_active_orders(connector_name=self.config.exchange):
            connector.stop_tracking_order(order.client_order_id)

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

        # 3. Tapster gone — book spread blown out → pause
        spread_bps = self._get_book_spread_bps()
        if spread_bps is not None and spread_bps > self.config.max_book_spread_bps:
            self.logger().warning(f"Book spread {spread_bps:.0f} bps > limit {self.config.max_book_spread_bps}. Pausing.")
            self._cancel_all_orders()
            return True

        # 4. Tapster monitor (enhancement)
        if self.config.enable_tapster_monitor:
            book_mid = self._get_book_mid()
            if book_mid is not None:
                amm_mid = self.pool.get_mid_price()
                div_bps = abs(amm_mid - book_mid) / book_mid * D("10000")
                if div_bps > self.config.tapster_divergence_cancel_bps:
                    self.logger().warning(f"AMM/book divergence {div_bps:.0f} bps. Cancelling.")
                    self._cancel_all_orders()
                    return True

        # 5. Fill velocity (enhancement)
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
        else:
            self._base_flow += event.amount
            self._quote_flow -= event.price * event.amount

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

        # Defer requote to next tick (debounce fill cascades)
        self._needs_refresh = True

    def _refresh_orders(self):
        mid = self.pool.get_mid_price()
        base_avail, quote_avail = self.pool.get_available_reserves(self.config.floor_ratio)
        orders = self._generate_orders(mid, base_avail, quote_avail)
        orders = self.balance_gate.scale_orders(orders)
        self._place_orders(orders)

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

    def _get_book_spread_bps(self) -> Optional[Decimal]:
        try:
            connector = self.connectors[self.config.exchange]
            ask = connector.get_price(self.config.trading_pair, True)
            bid = connector.get_price(self.config.trading_pair, False)
            if ask > ZERO and bid > ZERO:
                mid = (ask + bid) / D(2)
                return (ask - bid) / mid * D("10000")
        except Exception:
            pass
        return None

    # ---- State persistence ------------------------------------------------

    def _save_state(self):
        state = self.pool.to_state()
        state["base_flow"] = str(self._base_flow)
        state["quote_flow"] = str(self._quote_flow)
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

        amm_mid = self.pool.get_mid_price()
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
            f"  [AMM {self.config.trading_pair}] Mid: {amm_mid:.6f} | Book: {book_str}",
            f"  VPool: {base_pct:.1f}%B / {quote_pct:.1f}%Q | Skew: {skew:+.3f}",
            f"  Real: {real_base:.2f} {base_token} / {real_quote:.2f} {quote_token}",
            f"  P&L: {pnl_str} {quote_token} (limit: -{self.config.max_cumulative_loss})",
        ]

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
