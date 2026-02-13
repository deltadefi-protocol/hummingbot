import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from hummingbot.connector.exchange.deltadefi import deltadefi_constants as CONSTANTS, deltadefi_web_utils as web_utils
from hummingbot.connector.exchange.deltadefi.deltadefi_auth import DeltaDefiAuth
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import WSPlainTextRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.deltadefi.deltadefi_exchange import DeltadefiExchange


class DeltaDefiAPIUserStreamDataSource(UserStreamTrackerDataSource):

    _logger: Optional[HummingbotLogger] = None

    def __init__(
            self,
            auth: DeltaDefiAuth,
            connector: 'DeltadefiExchange',
            api_factory: WebAssistantsFactory):
        super().__init__()
        self._auth: DeltaDefiAuth = auth
        self._connector = connector
        self._api_factory = api_factory

    async def _connected_websocket_assistant(self) -> WSAssistant:
        ws: WSAssistant = await self._get_ws_assistant()
        ws_url = web_utils.private_ws_url(
            api_key=self._auth.api_key,
            domain=self._connector.domain,
        )
        async with self._api_factory.throttler.execute_task(limit_id=CONSTANTS.WS_CONNECTION_LIMIT_ID):
            await ws.connect(
                ws_url=ws_url,
                message_timeout=CONSTANTS.SECONDS_TO_WAIT_TO_RECEIVE_MESSAGE)
        return ws

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        # No subscription messages needed - connecting to the account stream
        # endpoint implicitly subscribes to all account events
        self.logger().info("Connected to private account stream (auto-subscribed)")

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant, queue: asyncio.Queue):
        while True:
            try:
                await super()._process_websocket_messages(
                    websocket_assistant=websocket_assistant,
                    queue=queue)
            except asyncio.TimeoutError:
                ping_request = WSPlainTextRequest(payload="ping")
                await websocket_assistant.send(request=ping_request)

    async def _process_event_message(self, event_message: Dict[str, Any], queue: asyncio.Queue):
        # DeltaDeFi account stream messages have format:
        # {type: "Account", sub_type: "balance"|"order_info"|"dlta_points", ...}
        if not event_message or not isinstance(event_message, dict):
            return
        msg_type = event_message.get("type", "")
        sub_type = event_message.get("sub_type", "")
        if msg_type == "Account" and sub_type in ("balance", "order_info"):
            queue.put_nowait(event_message)

    async def _get_ws_assistant(self) -> WSAssistant:
        if self._ws_assistant is None:
            self._ws_assistant = await self._api_factory.get_ws_assistant()
        return self._ws_assistant
