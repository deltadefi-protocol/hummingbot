import logging
import time
from enum import Enum
from typing import TYPE_CHECKING, Callable, List, Optional

from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.deltadefi.deltadefi_exchange import DeltadefiExchange


class ConnectorHealth(Enum):
    NORMAL = "normal"
    DEGRADED = "degraded"
    MAINTENANCE = "maintenance"
    STOPPED = "stopped"


class DeltaDefiHealthMonitor:

    _logger: Optional[HummingbotLogger] = None

    def __init__(self, connector: 'DeltadefiExchange', stale_threshold_minutes: float = 3.0):
        self._connector = connector
        self._stale_threshold_minutes = stale_threshold_minutes
        self._state: ConnectorHealth = ConnectorHealth.NORMAL
        self._listeners: List[Callable] = []
        self._last_ws_message_time: float = time.time()
        self._ws_connected: bool = False

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(HummingbotLogger.logger_name_for_class(cls))
        return cls._logger

    @property
    def state(self) -> ConnectorHealth:
        return self._state

    def update(self):
        candle_builder = self._connector.candle_builder
        minutes_stale = candle_builder.minutes_since_last_trade()

        if self._state == ConnectorHealth.MAINTENANCE:
            return  # Stay in maintenance until explicit notification

        if self._state == ConnectorHealth.STOPPED:
            return  # Stay stopped

        if minutes_stale > self._stale_threshold_minutes or not self._ws_connected:
            if self._state != ConnectorHealth.DEGRADED:
                self.transition_to(ConnectorHealth.DEGRADED)
        else:
            if self._state != ConnectorHealth.NORMAL:
                self.transition_to(ConnectorHealth.NORMAL)

    def transition_to(self, new_state: ConnectorHealth):
        old_state = self._state
        self._state = new_state
        self.logger().info(f"Connector health: {old_state.value} -> {new_state.value}")
        for listener in self._listeners:
            try:
                listener(old_state, new_state)
            except Exception:
                self.logger().exception("Error in health state listener")

    def on_ws_connected(self):
        self._ws_connected = True
        self._last_ws_message_time = time.time()

    def on_ws_disconnected(self):
        self._ws_connected = False
        if self._state == ConnectorHealth.NORMAL:
            self.transition_to(ConnectorHealth.DEGRADED)

    def on_ws_message_received(self):
        self._last_ws_message_time = time.time()

    def on_trade_received(self):
        self._last_ws_message_time = time.time()
        if self._state == ConnectorHealth.DEGRADED and self._ws_connected:
            self.transition_to(ConnectorHealth.NORMAL)

    def on_maintenance_notification(self):
        self.transition_to(ConnectorHealth.MAINTENANCE)

    def on_maintenance_end(self):
        if self._state == ConnectorHealth.MAINTENANCE:
            self.transition_to(ConnectorHealth.NORMAL)

    def add_listener(self, listener: Callable):
        self._listeners.append(listener)
