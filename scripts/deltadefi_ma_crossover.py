import logging
import os
import time
from decimal import Decimal
from typing import Dict, List

from pydantic import Field

from hummingbot.client.config.config_data_types import BaseClientModel
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.connector.exchange.deltadefi.deltadefi_health import ConnectorHealth
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.event.events import OrderFilledEvent
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class DeltaDefiMACrossoverConfig(BaseClientModel):
    script_file_name: str = os.path.basename(__file__)
    exchange: str = Field("deltadefi")
    trading_pair: str = Field("ADA-USDM")
    fast_ma_period: int = Field(7)
    slow_ma_period: int = Field(25)
    order_size: Decimal = Field(Decimal("10"))
    cooldown_after_trade: int = Field(60)
    max_position: Decimal = Field(Decimal("100"))
    stop_loss_pct: Decimal = Field(Decimal("0.05"))


class DeltaDefiMACrossover(ScriptStrategyBase):

    create_timestamp = 0
    _last_trade_time: float = 0.0
    _last_fast_above_slow: bool = False
    _entry_price: Decimal = Decimal("0")

    @classmethod
    def init_markets(cls, config: DeltaDefiMACrossoverConfig):
        cls.markets = {config.exchange: {config.trading_pair}}

    def __init__(self, connectors: Dict[str, ConnectorBase], config: DeltaDefiMACrossoverConfig):
        super().__init__(connectors)
        self.config = config

    def on_tick(self):
        connector = self.connectors[self.config.exchange]

        # Check connector health
        if hasattr(connector, "health_monitor"):
            health = connector.health_monitor.state
            if health != ConnectorHealth.NORMAL:
                if health == ConnectorHealth.DEGRADED:
                    self._cancel_all()
                elif health == ConnectorHealth.MAINTENANCE:
                    self._cancel_all()
                return

        # Check stop loss
        self._check_stop_loss()

        # Check candle builder data availability
        if not hasattr(connector, "candle_builder"):
            return

        candle_builder = connector.candle_builder

        if not candle_builder.has_enough_data(self.config.slow_ma_period):
            return

        # Compute MAs
        closes = candle_builder.get_closes(self.config.slow_ma_period)
        fast_ma = self._compute_ma(closes, self.config.fast_ma_period)
        slow_ma = self._compute_ma(closes, self.config.slow_ma_period)

        if fast_ma is None or slow_ma is None:
            return

        fast_above_slow = fast_ma > slow_ma

        # Detect crossover
        if fast_above_slow and not self._last_fast_above_slow:
            # Bullish crossover - buy signal
            if self._can_trade():
                self._place_market_order(TradeType.BUY)
        elif not fast_above_slow and self._last_fast_above_slow:
            # Bearish crossover - sell signal
            if self._can_trade():
                self._place_market_order(TradeType.SELL)

        self._last_fast_above_slow = fast_above_slow

    def _compute_ma(self, closes: List[Decimal], period: int) -> Decimal:
        if len(closes) < period:
            return None
        relevant = closes[-period:]
        return sum(relevant) / Decimal(str(period))

    def _can_trade(self) -> bool:
        # Check cooldown
        now = time.time()
        if now - self._last_trade_time < self.config.cooldown_after_trade:
            return False

        # Check position size
        connector = self.connectors[self.config.exchange]
        base, quote = self.config.trading_pair.split("-")
        base_balance = connector.get_balance(base)
        if base_balance >= self.config.max_position:
            return False

        return True

    def _place_market_order(self, side: TradeType):
        if side == TradeType.BUY:
            self.buy(
                connector_name=self.config.exchange,
                trading_pair=self.config.trading_pair,
                amount=self.config.order_size,
                order_type=OrderType.MARKET,
            )
        else:
            self.sell(
                connector_name=self.config.exchange,
                trading_pair=self.config.trading_pair,
                amount=self.config.order_size,
                order_type=OrderType.MARKET,
            )

    def _check_stop_loss(self):
        if self._entry_price <= Decimal("0"):
            return

        connector = self.connectors[self.config.exchange]
        try:
            mid_price = connector.get_mid_price(self.config.trading_pair)
        except Exception:
            return

        if mid_price is None or mid_price <= Decimal("0"):
            return

        loss_pct = (self._entry_price - mid_price) / self._entry_price
        if loss_pct >= self.config.stop_loss_pct:
            self.logger().warning(
                f"Stop loss triggered: loss {loss_pct:.2%} >= threshold {self.config.stop_loss_pct:.2%}"
            )
            # Flatten position
            base, quote = self.config.trading_pair.split("-")
            base_balance = connector.get_balance(base)
            if base_balance > Decimal("0"):
                self.sell(
                    connector_name=self.config.exchange,
                    trading_pair=self.config.trading_pair,
                    amount=base_balance,
                    order_type=OrderType.MARKET,
                )
            self._entry_price = Decimal("0")

    def _cancel_all(self):
        for order in self.get_active_orders(connector_name=self.config.exchange):
            self.cancel(self.config.exchange, order.trading_pair, order.client_order_id)

    def did_fill_order(self, event: OrderFilledEvent):
        msg = (
            f"{event.trade_type.name} {round(event.amount, 2)} "
            f"{event.trading_pair} {self.config.exchange} at {round(event.price, 2)}"
        )
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)
        self._last_trade_time = time.time()

        if event.trade_type == TradeType.BUY:
            self._entry_price = event.price
