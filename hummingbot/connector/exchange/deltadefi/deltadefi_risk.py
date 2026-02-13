import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Tuple

from hummingbot.core.data_type.common import TradeType

if TYPE_CHECKING:
    from hummingbot.connector.exchange.deltadefi.deltadefi_exchange import DeltadefiExchange


@dataclass
class RiskConfig:
    max_position_size: Decimal = Decimal("1000")
    max_order_size: Decimal = Decimal("500")
    max_open_orders: int = 10
    min_order_interval_s: float = 1.0
    max_daily_volume: Decimal = Decimal("10000")
    stop_loss_pct: Decimal = Decimal("0.05")


class DeltaDefiRiskGuard:

    def __init__(self, config: RiskConfig, connector: 'DeltadefiExchange'):
        self._config = config
        self._connector = connector
        self._daily_volume: Decimal = Decimal("0")
        self._daily_reset_date: str = ""
        self._last_order_timestamp: float = 0.0

    def check_order(
        self,
        trading_pair: str,
        amount: Decimal,
        side: TradeType,
        price: Decimal,
    ) -> Tuple[bool, str]:
        self._check_daily_reset()

        # Check order size
        if amount > self._config.max_order_size:
            return False, f"Order size {amount} exceeds max {self._config.max_order_size}"

        # Check daily volume
        order_value = amount * price
        if self._daily_volume + order_value > self._config.max_daily_volume:
            return False, f"Daily volume limit would be exceeded: {self._daily_volume + order_value} > {self._config.max_daily_volume}"

        # Check open orders count
        active_orders = self._connector._order_tracker.active_orders
        if len(active_orders) >= self._config.max_open_orders:
            return False, f"Max open orders reached: {len(active_orders)} >= {self._config.max_open_orders}"

        # Check order interval
        now = time.time()
        if now - self._last_order_timestamp < self._config.min_order_interval_s:
            return False, f"Order too soon after last order (min interval: {self._config.min_order_interval_s}s)"

        # Check position size
        if not self.check_position(trading_pair):
            return False, f"Position limit would be exceeded for {trading_pair}"

        self._last_order_timestamp = now
        return True, ""

    def on_fill(self, amount: Decimal, price: Decimal):
        self._check_daily_reset()
        self._daily_volume += amount * price

    def check_position(self, trading_pair: str) -> bool:
        base, quote = trading_pair.split("-")
        current_balance = self._connector._account_balances.get(base, Decimal("0"))
        return current_balance <= self._config.max_position_size

    def reset_daily(self):
        self._daily_volume = Decimal("0")

    def _check_daily_reset(self):
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._daily_reset_date:
            self._daily_reset_date = today
            self.reset_daily()
