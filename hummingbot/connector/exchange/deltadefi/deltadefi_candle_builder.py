import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional


@dataclass
class Candle:
    open_time: float
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")
    trade_count: int = 0
    is_synthetic: bool = False


class DeltaDefiCandleBuilder:
    CANDLE_INTERVAL_S = 60  # 1-minute candles

    def __init__(self, max_window: int = 200):
        self._max_window = max_window
        self._completed_candles: deque = deque(maxlen=max_window)
        self._current_candle: Optional[Candle] = None
        self._last_trade_timestamp: float = 0.0

    @property
    def candle_count(self) -> int:
        """Total number of candles available (completed + current in-progress)."""
        total = len(self._completed_candles)
        if self._current_candle is not None:
            total += 1
        return total

    def warmup_progress(self, periods: int) -> float:
        """Return warmup progress as a float from 0.0 to 1.0.

        Args:
            periods: The number of candles required for the indicator.

        Returns:
            A float between 0.0 (no data) and 1.0 (fully warmed up).
        """
        if periods <= 0:
            return 1.0
        return min(self.candle_count / periods, 1.0)

    def process_trade(self, price: Decimal, quantity: Decimal, side: str, timestamp: float):
        self._last_trade_timestamp = timestamp

        if self._current_candle is None:
            self._start_new_candle(price, quantity, timestamp)
            return

        candle_end = self._current_candle.open_time + self.CANDLE_INTERVAL_S
        if timestamp >= candle_end:
            self._finalize_current_candle(timestamp)
            self._start_new_candle(price, quantity, timestamp)
        else:
            self._current_candle.high = max(self._current_candle.high, price)
            self._current_candle.low = min(self._current_candle.low, price)
            self._current_candle.close = price
            self._current_candle.volume += quantity
            self._current_candle.trade_count += 1

    def _finalize_current_candle(self, current_timestamp: float):
        if self._current_candle is None:
            return

        self._completed_candles.append(self._current_candle)

        # Generate synthetic gap candles for missed minutes
        last_close = self._current_candle.close
        gap_start = self._current_candle.open_time + self.CANDLE_INTERVAL_S
        while gap_start + self.CANDLE_INTERVAL_S <= current_timestamp:
            synthetic = Candle(
                open_time=gap_start,
                open=last_close,
                high=last_close,
                low=last_close,
                close=last_close,
                volume=Decimal("0"),
                trade_count=0,
                is_synthetic=True,
            )
            self._completed_candles.append(synthetic)
            gap_start += self.CANDLE_INTERVAL_S

    def _start_new_candle(self, price: Decimal, quantity: Decimal, timestamp: float):
        candle_open_time = timestamp - (timestamp % self.CANDLE_INTERVAL_S)
        self._current_candle = Candle(
            open_time=candle_open_time,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=quantity,
            trade_count=1,
        )

    def get_candles(self, count: int) -> List[Candle]:
        candles = list(self._completed_candles)
        if self._current_candle is not None:
            candles.append(self._current_candle)
        return candles[-count:]

    def get_closes(self, count: int) -> List[Decimal]:
        candles = self.get_candles(count)
        return [c.close for c in candles]

    def minutes_since_last_trade(self) -> float:
        if self._last_trade_timestamp == 0.0:
            return float("inf")
        return (time.time() - self._last_trade_timestamp) / 60.0

    def has_enough_data(self, periods: int) -> bool:
        total = len(self._completed_candles)
        if self._current_candle is not None:
            total += 1
        return total >= periods
