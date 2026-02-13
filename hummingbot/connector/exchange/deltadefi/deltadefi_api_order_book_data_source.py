import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from hummingbot.connector.exchange.deltadefi import deltadefi_constants as CONSTANTS, deltadefi_web_utils as web_utils
from hummingbot.connector.exchange.deltadefi.deltadefi_candle_builder import DeltaDefiCandleBuilder
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import WSPlainTextRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.deltadefi.deltadefi_exchange import DeltadefiExchange


class DeltaDefiAPIOrderBookDataSource(OrderBookTrackerDataSource):

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 trading_pairs: List[str],
                 connector: 'DeltadefiExchange',
                 api_factory: WebAssistantsFactory,
                 candle_builder: Optional[DeltaDefiCandleBuilder] = None):
        super().__init__(trading_pairs)
        self._connector = connector
        self._api_factory = api_factory
        self._candle_builder = candle_builder
        self._trades_ws_assistant: Optional[WSAssistant] = None

    async def get_last_traded_prices(self,
                                     trading_pairs: List[str],
                                     domain: Optional[str] = None) -> Dict[str, float]:
        return await self._connector.get_last_traded_prices(trading_pairs=trading_pairs)

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        # Return an empty snapshot; the authenticated WS depth stream will populate the order book.
        snapshot_timestamp = time.time()
        order_book_message_content = {
            "trading_pair": trading_pair,
            "update_id": int(snapshot_timestamp),
            "bids": [],
            "asks": [],
        }
        snapshot_msg = OrderBookMessage(
            OrderBookMessageType.SNAPSHOT,
            order_book_message_content,
            snapshot_timestamp)
        return snapshot_msg

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        # DeltaDeFi has no REST endpoint for order book depth.
        # The authenticated WS depth stream sends full snapshots (handled as SNAPSHOT
        # messages in _parse_order_book_diff_message), so no REST fallback is needed.
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception("Unexpected error in order book snapshot listener.")
                await asyncio.sleep(5.0)

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        # DeltaDeFi WS may send plain string messages (ping/pong) - skip them
        if not raw_message or isinstance(raw_message, str):
            return
        # DeltaDeFi recent-trades WS sends: [{timestamp, symbol, side, price, amount}, ...]
        trade_updates = raw_message if isinstance(raw_message, list) else [raw_message]

        for trade_data in trade_updates:
            symbol = trade_data.get("symbol", "")
            try:
                trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=symbol)
            except KeyError:
                continue

            side = trade_data.get("side", "").lower()
            price = trade_data.get("price", "0")
            amount = trade_data.get("amount", "0")
            ts_raw = trade_data.get("timestamp", 0)
            if isinstance(ts_raw, str) and ts_raw:
                from dateutil.parser import parse as parse_dt
                timestamp = int(parse_dt(ts_raw).timestamp())
            else:
                timestamp = int(ts_raw) if ts_raw else 0

            message_content = {
                "trade_id": trade_data.get("trade_id", str(timestamp)),
                "trading_pair": trading_pair,
                "trade_type": float(TradeType.BUY.value) if side == "buy" else float(TradeType.SELL.value),
                "amount": amount,
                "price": price,
            }
            trade_message: OrderBookMessage = OrderBookMessage(
                message_type=OrderBookMessageType.TRADE,
                content=message_content,
                timestamp=timestamp * 1e-3 if timestamp > 1e12 else float(timestamp),
            )
            message_queue.put_nowait(trade_message)

            if self._candle_builder is not None:
                self._candle_builder.process_trade(
                    price=Decimal(str(price)),
                    quantity=Decimal(str(amount)),
                    side=side,
                    timestamp=float(timestamp) * 1e-3 if timestamp > 1e12 else float(timestamp),
                )

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        if not raw_message or not isinstance(raw_message, dict):
            return
        # DeltaDeFi depth WS sends full snapshots each time:
        # {timestamp: N, bids: [{price: F, quantity: F}], asks: [{price: F, quantity: F}]}
        timestamp = float(raw_message.get("timestamp", 0))
        if timestamp > 1e12:
            timestamp = timestamp * 1e-3

        # Determine trading pair from the first configured pair (depth WS is per-symbol)
        # The trading pair is embedded in the connection URL, not in the message
        trading_pair = self._trading_pairs[0] if self._trading_pairs else ""

        bids = raw_message.get("bids", [])
        asks = raw_message.get("asks", [])

        formatted_bids = [
            (float(b["price"]), float(b["quantity"])) if isinstance(b, dict) else (b[0], b[1])
            for b in bids
        ]
        formatted_asks = [
            (float(a["price"]), float(a["quantity"])) if isinstance(a, dict) else (a[0], a[1])
            for a in asks
        ]

        # Treat each depth message as a full snapshot
        order_book_message_content = {
            "trading_pair": trading_pair,
            "update_id": int(timestamp),
            "bids": formatted_bids,
            "asks": formatted_asks,
        }
        snapshot_message: OrderBookMessage = OrderBookMessage(
            OrderBookMessageType.SNAPSHOT,
            order_book_message_content,
            timestamp)

        message_queue.put_nowait(snapshot_message)

    async def _subscribe_channels(self, ws: WSAssistant):
        # No subscription messages needed - connection to the depth WS endpoint
        # is the subscription itself. We also start a separate trades WS.
        self.logger().info("Connected to market depth stream (auto-subscribed)")

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        if not isinstance(event_message, dict):
            return ""
        # All messages from the depth WS go to the diff (snapshot) queue
        if "bids" in event_message or "asks" in event_message:
            return self._diff_messages_queue_key
        return ""

    async def _connected_websocket_assistant(self) -> WSAssistant:
        # Connect to depth WS for the first trading pair
        # DeltaDeFi depth WS requires api_key query parameter for authentication
        if not self._trading_pairs:
            raise ValueError("No trading pairs configured for order book data source")

        symbol = await self._connector.exchange_symbol_associated_to_pair(self._trading_pairs[0])
        ws_url = web_utils.public_ws_url(
            path=CONSTANTS.WS_MARKET_DEPTH,
            symbol=symbol,
            domain=self._connector.domain,
        )
        ws_url = f"{ws_url}?api_key={self._connector.deltadefi_api_key}"
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        async with self._api_factory.throttler.execute_task(limit_id=CONSTANTS.WS_CONNECTION_LIMIT_ID):
            await ws.connect(
                ws_url=ws_url,
                message_timeout=CONSTANTS.SECONDS_TO_WAIT_TO_RECEIVE_MESSAGE)
        return ws

    async def listen_for_trades(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        while True:
            try:
                if not self._trading_pairs:
                    await asyncio.sleep(1)
                    continue

                symbol = await self._connector.exchange_symbol_associated_to_pair(self._trading_pairs[0])
                ws_url = web_utils.public_ws_url(
                    path=CONSTANTS.WS_RECENT_TRADES,
                    symbol=symbol,
                    domain=self._connector.domain,
                )
                ws_url = f"{ws_url}?api_key={self._connector.deltadefi_api_key}"
                trades_ws = await self._api_factory.get_ws_assistant()
                async with self._api_factory.throttler.execute_task(limit_id=CONSTANTS.WS_CONNECTION_LIMIT_ID):
                    await trades_ws.connect(
                        ws_url=ws_url,
                        message_timeout=CONSTANTS.SECONDS_TO_WAIT_TO_RECEIVE_MESSAGE)

                self.logger().info("Connected to market trades stream")

                while True:
                    try:
                        async for ws_response in trades_ws.iter_messages():
                            msg = ws_response.data
                            if msg is not None:
                                await self._parse_trade_message(msg, output)
                    except asyncio.TimeoutError:
                        ping_request = WSPlainTextRequest(payload="ping")
                        await trades_ws.send(request=ping_request)

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception("Unexpected error in trades listener. Reconnecting...")
                await asyncio.sleep(5.0)

    async def subscribe_to_trading_pair(self, trading_pair: str) -> bool:
        # DeltaDeFi WS is per-symbol: connecting to the endpoint is the subscription.
        return True

    async def unsubscribe_from_trading_pair(self, trading_pair: str) -> bool:
        # DeltaDeFi WS is per-symbol: disconnecting is the unsubscription.
        return True

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant):
        while True:
            try:
                await super()._process_websocket_messages(websocket_assistant=websocket_assistant)
            except asyncio.TimeoutError:
                ping_request = WSPlainTextRequest(payload="ping")
                await websocket_assistant.send(request=ping_request)
