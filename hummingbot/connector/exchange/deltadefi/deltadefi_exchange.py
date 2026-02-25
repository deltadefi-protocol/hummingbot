import asyncio
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from bidict import bidict

try:
    from sidan_gin import Wallet, decrypt_with_cipher
    _HAS_SIDAN_GIN = True
except ImportError:
    _HAS_SIDAN_GIN = False

from hummingbot.connector.exchange.deltadefi import (
    deltadefi_constants as CONSTANTS,
    deltadefi_utils,
    deltadefi_web_utils as web_utils,
)
from hummingbot.connector.exchange.deltadefi.deltadefi_api_order_book_data_source import DeltaDefiAPIOrderBookDataSource
from hummingbot.connector.exchange.deltadefi.deltadefi_api_user_stream_data_source import (
    DeltaDefiAPIUserStreamDataSource,
)
from hummingbot.connector.exchange.deltadefi.deltadefi_auth import DeltaDefiAuth
from hummingbot.connector.exchange.deltadefi.deltadefi_candle_builder import DeltaDefiCandleBuilder
from hummingbot.connector.exchange.deltadefi.deltadefi_health import DeltaDefiHealthMonitor
from hummingbot.connector.exchange.deltadefi.deltadefi_risk import DeltaDefiRiskGuard, RiskConfig
from hummingbot.connector.exchange_base import s_decimal_NaN
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class DeltadefiExchange(ExchangePyBase):

    web_utils = web_utils

    def __init__(self,
                 deltadefi_api_key: str,
                 deltadefi_password: str,
                 deltadefi_network: str = CONSTANTS.DEFAULT_DOMAIN,
                 deltadefi_custom_rest_url: str = "",
                 deltadefi_custom_wss_url: str = "",
                 trading_pairs: Optional[List[str]] = None,
                 trading_required: bool = True,
                 balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
                 rate_limits_share_pct: Decimal = Decimal("100")):
        self.deltadefi_api_key = deltadefi_api_key
        self.deltadefi_password = deltadefi_password
        self.deltadefi_network = deltadefi_network or CONSTANTS.DEFAULT_DOMAIN

        if deltadefi_custom_rest_url:
            CONSTANTS.REST_URLS[self.deltadefi_network] = deltadefi_custom_rest_url.rstrip("/")
        if deltadefi_custom_wss_url:
            CONSTANTS.WSS_URLS[self.deltadefi_network] = deltadefi_custom_wss_url.rstrip("/")
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._candle_builder: Optional[DeltaDefiCandleBuilder] = None
        self._health_monitor: Optional[DeltaDefiHealthMonitor] = None
        self._risk_guard: Optional[DeltaDefiRiskGuard] = None
        self._reconciliation_task: Optional[asyncio.Task] = None
        self._operation_wallet = None  # Initialized on start_network
        self._asset_id_to_symbol: Dict[str, str] = {"lovelace": "ADA"}
        super().__init__(balance_asset_limit, rate_limits_share_pct)

    # --- Properties required by ExchangePyBase ---

    @property
    def authenticator(self):
        return DeltaDefiAuth(api_key=self.deltadefi_api_key)

    @property
    def name(self) -> str:
        return "deltadefi"

    @property
    def rate_limits_rules(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self):
        return self.deltadefi_network

    @property
    def client_order_id_max_length(self):
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self):
        return CONSTANTS.CLIENT_ID_PREFIX

    @property
    def trading_rules_request_path(self):
        return CONSTANTS.TRADING_PAIRS_PATH

    @property
    def trading_pairs_request_path(self):
        return CONSTANTS.TRADING_PAIRS_PATH

    @property
    def check_network_request_path(self):
        return CONSTANTS.TRADING_PAIRS_PATH

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    @property
    def candle_builder(self) -> DeltaDefiCandleBuilder:
        if self._candle_builder is None:
            self._candle_builder = DeltaDefiCandleBuilder()
        return self._candle_builder

    @property
    def health_monitor(self) -> DeltaDefiHealthMonitor:
        if self._health_monitor is None:
            self._health_monitor = DeltaDefiHealthMonitor(connector=self)
        return self._health_monitor

    @property
    def risk_guard(self) -> Optional[DeltaDefiRiskGuard]:
        return self._risk_guard

    def set_risk_config(self, config: RiskConfig):
        self._risk_guard = DeltaDefiRiskGuard(config=config, connector=self)

    def supported_order_types(self):
        return [OrderType.LIMIT, OrderType.MARKET]

    # --- Abstract method implementations ---

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return "not found" in str(status_update_exception).lower() or "ORDER_NOT_FOUND" in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return "not found" in str(cancelation_exception).lower() or "ORDER_NOT_FOUND" in str(cancelation_exception)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            auth=self._auth,
            domain=self.domain)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return DeltaDefiAPIOrderBookDataSource(
            trading_pairs=self.trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            candle_builder=self.candle_builder)

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return DeltaDefiAPIUserStreamDataSource(
            auth=self._auth,
            connector=self,
            api_factory=self._web_assistants_factory)

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 amount: Decimal,
                 price: Decimal = s_decimal_NaN,
                 is_maker: Optional[bool] = None) -> TradeFeeBase:
        is_maker = is_maker or (order_type is OrderType.LIMIT)
        fee = build_trade_fee(
            self.name,
            is_maker,
            base_currency=base_currency,
            quote_currency=quote_currency,
            order_type=order_type,
            order_side=order_side,
            amount=amount,
            price=price,
        )
        return fee

    # --- Trading pair symbol mapping ---
    # Exchange format: "ADAUSDM" (no separator)
    # Hummingbot format: "ADA-USDM"

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        # GetMarketConfigResponse: {"trading_pairs": [...], "assets": [...]}
        if isinstance(exchange_info, dict):
            pairs_data = exchange_info.get("trading_pairs", exchange_info.get("data", []))
        else:
            pairs_data = exchange_info
        if isinstance(pairs_data, dict):
            pairs_data = pairs_data.get("trading_pairs", [])
        for symbol_data in filter(deltadefi_utils.is_exchange_information_valid, pairs_data):
            exchange_symbol = symbol_data["symbol"]
            base_token = symbol_data["base_token"]
            quote_token = symbol_data["quote_token"]
            base = base_token["symbol"]
            quote = quote_token["symbol"]
            hb_pair = combine_to_hb_trading_pair(base=base, quote=quote)
            mapping[exchange_symbol] = hb_pair
            # Map Cardano policy IDs / unit strings to friendly symbols
            for token_data, symbol in [(base_token, base), (quote_token, quote)]:
                for key in ("unit", "policy_id", "asset_id"):
                    asset_id = token_data.get(key, "")
                    if asset_id and asset_id != symbol:
                        self._asset_id_to_symbol[asset_id] = symbol
        self._set_trading_pair_symbol_map(mapping)

    # --- Balance updates ---

    async def _update_balances(self):
        msg = await self._api_request(
            path_url=CONSTANTS.BALANCE_PATH,
            is_auth_required=True)

        # DeltaDeFi returns balance data - may be list or dict
        balances = msg if isinstance(msg, list) else msg.get("data", msg.get("balances", []))

        self._account_available_balances.clear()
        self._account_balances.clear()

        if isinstance(balances, list):
            for balance in balances:
                asset = balance.get("asset", balance.get("token", ""))
                available = Decimal(str(balance.get("free", balance.get("available", "0"))))
                locked = Decimal(str(balance.get("locked", "0")))
                total = available + locked
                self._account_balances[asset] = total
                self._account_available_balances[asset] = available
        elif isinstance(balances, dict):
            for asset, bal_data in balances.items():
                if isinstance(bal_data, dict):
                    available = Decimal(str(bal_data.get("free", "0")))
                    locked = Decimal(str(bal_data.get("locked", "0")))
                else:
                    available = Decimal(str(bal_data))
                    locked = Decimal("0")
                self._account_balances[asset] = available + locked
                self._account_available_balances[asset] = available

    # --- Trading rules ---

    async def _format_trading_rules(self, raw_trading_pair_info: List[Dict[str, Any]]) -> List[TradingRule]:
        trading_rules = []
        pairs_data = raw_trading_pair_info
        if isinstance(raw_trading_pair_info, dict):
            pairs_data = raw_trading_pair_info.get("trading_pairs", raw_trading_pair_info.get("data", []))
        for info in pairs_data:
            try:
                if deltadefi_utils.is_exchange_information_valid(exchange_info=info):
                    exchange_symbol = info["symbol"]
                    base_token = info["base_token"]
                    # Derive precision from max_qty_dp and price_max_dp
                    price_max_dp = info.get("price_max_dp", 4)
                    base_max_qty_dp = base_token.get("max_qty_dp", 2)
                    trading_rules.append(
                        TradingRule(
                            trading_pair=await self.trading_pair_associated_to_exchange_symbol(symbol=exchange_symbol),
                            min_order_size=Decimal(str(info.get("min_order_size", "0.01"))),
                            min_price_increment=Decimal(10) ** -price_max_dp,
                            min_base_amount_increment=Decimal(10) ** -base_max_qty_dp,
                        )
                    )
            except Exception:
                self.logger().exception(f"Error parsing the trading pair rule {info}. Skipping.")
        return trading_rules

    async def _update_trading_fees(self):
        pass

    # --- Last traded prices ---

    async def get_last_traded_prices(self, trading_pairs: List[str] = None) -> Dict[str, float]:
        if trading_pairs and len(trading_pairs) == 1:
            return {trading_pairs[0]: await self._get_last_traded_price(trading_pairs[0])}

        resp_json = await self._api_get(
            path_url=CONSTANTS.MARKET_PRICE_PATH,
        )
        last_traded_prices = {}
        prices_data = resp_json if isinstance(resp_json, list) else resp_json.get("data", [])
        for ticker in prices_data:
            symbol = ticker.get("symbol", ticker.get("pair", ""))
            price = ticker.get("price")
            if price and symbol:
                try:
                    trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=symbol)
                    last_traded_prices[trading_pair] = float(price)
                except KeyError:
                    continue
        return last_traded_prices

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        resp_json = await self._api_request(
            path_url=f"{CONSTANTS.MARKET_PRICE_PATH}/{exchange_symbol}",
        )
        if isinstance(resp_json, dict):
            data = resp_json.get("data", resp_json)
            if isinstance(data, list) and len(data) > 0:
                return float(data[0].get("price", 0))
            return float(data.get("price", 0))
        return 0.0

    # --- Order placement (2-step: build -> sign -> submit) ---

    async def _place_order(self,
                           order_id: str,
                           trading_pair: str,
                           amount: Decimal,
                           trade_type: TradeType,
                           order_type: OrderType,
                           price: Decimal,
                           **kwargs) -> Tuple[str, float]:

        if self._risk_guard is not None:
            allowed, reason = self._risk_guard.check_order(
                trading_pair=trading_pair,
                amount=amount,
                side=trade_type,
                price=price,
            )
            if not allowed:
                raise ValueError(f"Risk guard rejected order: {reason}")

        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        side = CONSTANTS.SIDE_BUY if trade_type == TradeType.BUY else CONSTANTS.SIDE_SELL

        # Step 1: Build the order (get unsigned tx)
        build_data = {
            "symbol": exchange_symbol,
            "side": side,
            "type": CONSTANTS.ORDER_TYPE_MAP[order_type],
            "base_quantity": str(amount),
        }

        if order_type.is_limit_type():
            build_data["price"] = f"{price:f}"

        post_only = kwargs.get("post_only", False)
        if post_only:
            build_data["post_only"] = True

        build_response = await self._api_request(
            path_url=CONSTANTS.ORDER_BUILD_PATH,
            method=RESTMethod.POST,
            data=build_data,
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_BUILD_PATH,
        )

        build_result = build_response.get("data", build_response) if isinstance(build_response, dict) else build_response
        order_id_from_exchange = str(build_result.get("order_id", ""))
        tx_hex = build_result.get("tx_hex", "")

        if not order_id_from_exchange or not tx_hex:
            raise IOError(f"Error building order {order_id}: {build_response}")

        # Step 2: Sign the transaction locally
        signed_tx = self._sign_transaction(tx_hex)

        # Step 3: Submit the signed transaction
        submit_data = {
            "order_id": order_id_from_exchange,
            "signed_tx": signed_tx,
        }

        submit_response = await self._api_request(
            path_url=CONSTANTS.ORDER_SUBMIT_PATH,
            method=RESTMethod.POST,
            data=submit_data,
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_SUBMIT_PATH,
        )

        submit_result = submit_response.get("data", submit_response) if isinstance(submit_response, dict) else submit_response
        exchange_order_id = str(submit_result.get("order_id", order_id_from_exchange))

        if not exchange_order_id:
            raise IOError(f"Error submitting order {order_id}: {submit_response}")

        return exchange_order_id, self.current_timestamp

    def _sign_transaction(self, tx_hex: str) -> str:
        if self._operation_wallet is not None:
            try:
                return self._operation_wallet.sign_tx(tx_hex)
            except Exception as e:
                raise IOError(f"Failed to sign transaction: {e}")
        raise IOError("Operation wallet not initialized. Cannot sign transactions.")

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        exchange_order_id = await tracked_order.get_exchange_order_id()
        cancel_url = f"{CONSTANTS.CANCEL_ORDER_PATH}/{exchange_order_id}/cancel"

        try:
            cancel_result = await self._api_request(
                path_url=cancel_url,
                method=RESTMethod.POST,
                is_auth_required=True,
                limit_id=CONSTANTS.CANCEL_ORDER_PATH,
            )
        except IOError as e:
            # Treat HTTP 404 or similar as "order already gone"
            err_str = str(e).lower()
            if "404" in err_str or "not found" in err_str:
                self.logger().info(f"Order {order_id} already gone on exchange (HTTP error). Treating as cancelled.")
                return True
            raise

        if isinstance(cancel_result, dict):
            status = cancel_result.get("status", "").lower()
            if status in ("success", "cancelled", "canceled"):
                return True
            error_code = cancel_result.get("code", "").upper()
            error_msg = cancel_result.get("message", cancel_result.get("msg", "")).lower()
            # Order no longer exists on exchange — treat as successfully cancelled
            if error_code in ("ORDER_NOT_FOUND", "ORDER_ALREADY_CANCELLED", "ORDER_ALREADY_FILLED"):
                self.logger().info(f"Order {order_id} already gone on exchange ({error_code}). Treating as cancelled.")
                return True
            if "not found" in error_msg or "already" in error_msg:
                self.logger().info(f"Order {order_id} already gone on exchange ({error_msg}). Treating as cancelled.")
                return True
        raise IOError(f"Error cancelling order {order_id}: {cancel_result}")

    # --- Order status and trade updates ---

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates = []

        if order.exchange_order_id is not None:
            exchange_order_id = await order.get_exchange_order_id()
            order_url = f"{CONSTANTS.ORDER_STATUS_PATH}/{exchange_order_id}"
            response = await self._api_request(
                method=RESTMethod.GET,
                path_url=order_url,
                is_auth_required=True,
                limit_id=CONSTANTS.ORDER_STATUS_PATH,
            )

            order_data = response.get("data", response) if isinstance(response, dict) else response

            # DeltaDeFi uses order_execution_records instead of fills
            fills = order_data.get("order_execution_records", order_data.get("fills", []))

            for fill_data in fills:
                fee_amount = Decimal(str(fill_data.get("commission", fill_data.get("fee", "0"))))
                fee_asset = fill_data.get("commission_unit", fill_data.get("fee_asset", order.quote_asset))
                # Normalize Cardano asset IDs (policy IDs, lovelace) to friendly symbols
                if fee_asset == "lovelace":
                    fee_amount = fee_amount / Decimal("1000000")
                fee_asset = self._asset_id_to_symbol.get(fee_asset, fee_asset)
                fee = TradeFeeBase.new_spot_fee(
                    fee_schema=self.trade_fee_schema(),
                    trade_type=order.trade_type,
                    percent_token=fee_asset,
                    flat_fees=[TokenAmount(amount=abs(fee_amount), token=fee_asset)]
                )
                fill_price = Decimal(str(fill_data.get("execution_price", fill_data.get("price", "0"))))
                fill_size = Decimal(str(fill_data.get("filled_base_qty", fill_data.get("quantity", "0"))))
                fill_quote = Decimal(str(fill_data.get("filled_quote_qty", str(fill_size * fill_price))))
                fill_ts_raw = fill_data.get("created_at", fill_data.get("timestamp", 0))
                if isinstance(fill_ts_raw, str) and fill_ts_raw:
                    from dateutil.parser import parse as parse_dt
                    fill_ts = parse_dt(fill_ts_raw).timestamp()
                else:
                    fill_ts = int(fill_ts_raw or 0) * 1e-3
                trade_update = TradeUpdate(
                    trade_id=str(fill_data.get("id", fill_data.get("trade_id", ""))),
                    client_order_id=order.client_order_id,
                    exchange_order_id=exchange_order_id,
                    trading_pair=order.trading_pair,
                    fee=fee,
                    fill_base_amount=fill_size,
                    fill_quote_amount=fill_quote,
                    fill_price=fill_price,
                    fill_timestamp=fill_ts,
                )
                trade_updates.append(trade_update)

        return trade_updates

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        exchange_order_id = await tracked_order.get_exchange_order_id()
        order_url = f"{CONSTANTS.ORDER_STATUS_PATH}/{exchange_order_id}"

        response = await self._api_request(
            method=RESTMethod.GET,
            path_url=order_url,
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_STATUS_PATH,
        )

        order_data = response.get("data", response) if isinstance(response, dict) else response
        status_str = order_data.get("status", "")
        new_state = CONSTANTS.ORDER_STATE.get(status_str, OrderState.OPEN)

        order_update = OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id,
            trading_pair=tracked_order.trading_pair,
            update_timestamp=self._parse_order_timestamp(order_data),
            new_state=new_state,
        )
        return order_update

    @staticmethod
    def _parse_order_timestamp(data: dict) -> float:
        ts_raw = data.get("updated_at", data.get("timestamp", 0))
        if isinstance(ts_raw, str) and ts_raw:
            from dateutil.parser import parse as parse_dt
            return parse_dt(ts_raw).timestamp()
        ts_int = int(ts_raw or 0)
        return ts_int * 1e-3 if ts_int > 1e12 else float(ts_int)

    # --- User stream event processing ---

    async def _user_stream_event_listener(self):
        async for stream_message in self._iter_user_event_queue():
            try:
                msg_type = stream_message.get("type", "")
                sub_type = stream_message.get("sub_type", "")

                if msg_type == "Account" and sub_type == "order_info":
                    # WS payload: {"type":"Account","sub_type":"order_info","order":{...}}
                    data = stream_message.get("order", stream_message.get("data", stream_message))
                    order_status = CONSTANTS.ORDER_STATE.get(data.get("status", ""), OrderState.OPEN)
                    exchange_order_id = str(data.get("id", ""))

                    # DeltaDeFi WS doesn't include client_order_id — match by exchange_order_id
                    fillable_order = None
                    updatable_order = None
                    for o in self._order_tracker.all_fillable_orders.values():
                        if o.exchange_order_id == exchange_order_id:
                            fillable_order = o
                            break
                    for o in self._order_tracker.all_updatable_orders.values():
                        if o.exchange_order_id == exchange_order_id:
                            updatable_order = o
                            break

                    if (fillable_order is not None
                            and order_status in [OrderState.PARTIALLY_FILLED, OrderState.FILLED]):
                        try:
                            trade_updates = await self._all_trade_updates_for_order(fillable_order)
                            for trade_update in trade_updates:
                                self._order_tracker.process_trade_update(trade_update)
                                if self._risk_guard is not None:
                                    self._risk_guard.on_fill(
                                        amount=trade_update.fill_base_amount,
                                        price=trade_update.fill_price,
                                    )
                        except Exception:
                            self.logger().debug(
                                f"Failed to fetch fill details for {exchange_order_id} via REST"
                            )

                    if updatable_order is not None:
                        order_update = OrderUpdate(
                            trading_pair=updatable_order.trading_pair,
                            update_timestamp=self._parse_order_timestamp(data),
                            new_state=order_status,
                            client_order_id=updatable_order.client_order_id,
                            exchange_order_id=exchange_order_id,
                        )
                        self._order_tracker.process_order_update(order_update=order_update)

                elif msg_type == "Account" and sub_type == "balance":
                    data = stream_message.get("data", stream_message)
                    if isinstance(data, list):
                        for bal in data:
                            asset = bal.get("asset", bal.get("token", ""))
                            if asset:
                                available = Decimal(str(bal.get("free", bal.get("available", "0"))))
                                locked = Decimal(str(bal.get("locked", "0")))
                                self._account_balances[asset] = available + locked
                                self._account_available_balances[asset] = available
                    elif isinstance(data, dict):
                        asset = data.get("asset", data.get("token", ""))
                        if asset:
                            available = Decimal(str(data.get("free", data.get("available", "0"))))
                            locked = Decimal(str(data.get("locked", "0")))
                            self._account_balances[asset] = available + locked
                            self._account_available_balances[asset] = available

                # Update health monitor on any successful message
                if self._health_monitor is not None:
                    self._health_monitor.on_ws_message_received()

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception("Unexpected error in user stream listener loop.")
                await self._sleep(5.0)

    # --- Tick override for health monitor ---

    def tick(self, timestamp: float):
        super().tick(timestamp)
        if self._health_monitor is not None:
            self._health_monitor.update()

    # --- Network lifecycle with reconciliation and operation key ---

    async def start_network(self):
        await super().start_network()
        # Fetch and decrypt operation key for tx signing
        await self._initialize_operation_wallet()
        # Cancel any orphan orders left on exchange from previous session
        await self._cancel_all_on_exchange()
        self._reconciliation_task = safe_ensure_future(self._reconciliation_loop())

    async def stop_network(self):
        if self._reconciliation_task is not None:
            self._reconciliation_task.cancel()
            self._reconciliation_task = None
        await super().stop_network()

    async def _initialize_operation_wallet(self):
        try:
            response = await self._api_request(
                path_url=CONSTANTS.OPERATION_KEY_PATH,
                method=RESTMethod.GET,
                is_auth_required=True,
                limit_id=CONSTANTS.OPERATION_KEY_PATH,
            )

            op_key_data = response.get("data", response) if isinstance(response, dict) else response
            encrypted_key = op_key_data.get("encrypted_operation_key", op_key_data.get("encrypted_key", ""))

            if not encrypted_key:
                self.logger().warning("No operation key returned from API. Transaction signing will not be available.")
                return

            if _HAS_SIDAN_GIN:
                try:
                    operation_key = decrypt_with_cipher(encrypted_key, self.deltadefi_password)
                    self._operation_wallet = Wallet.new_root_key(operation_key)
                    self.logger().info("Operation wallet initialized successfully for transaction signing")
                except Exception as e:
                    self.logger().error(f"Failed to decrypt operation key: {e}")
            else:
                self.logger().warning(
                    "sidan-gin package not available. Transaction signing will not be available. "
                    "Install with: pip install sidan-gin"
                )

        except Exception:
            self.logger().exception("Error fetching operation key. Transaction signing may not work.")

    async def cancel_all(self, timeout_seconds: float = 10.0):
        """Cancel all open orders using the batch cancel-all endpoint.
        Overrides the base class one-by-one implementation.
        Retries with exponential backoff on 429 rate-limit errors."""
        incomplete_orders = [o for o in self.in_flight_orders.values() if not o.is_done]
        if not incomplete_orders:
            return []

        from hummingbot.core.data_type.cancellation_result import CancellationResult

        max_retries = 3
        for attempt in range(max_retries):
            try:
                for trading_pair in (self._trading_pairs or []):
                    exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)
                    await self._api_request(
                        path_url=CONSTANTS.CANCEL_ALL_PATH,
                        method=RESTMethod.POST,
                        data={"symbol": exchange_symbol},
                        is_auth_required=True,
                        limit_id=CONSTANTS.CANCEL_ALL_PATH,
                    )
                # Fire proper OrderUpdate so the strategy-level tracker also removes the orders
                for o in incomplete_orders:
                    self._order_tracker.process_order_update(OrderUpdate(
                        client_order_id=o.client_order_id,
                        exchange_order_id=o.exchange_order_id or "",
                        trading_pair=o.trading_pair,
                        update_timestamp=self.current_timestamp,
                        new_state=OrderState.CANCELED,
                    ))
                return [CancellationResult(o.client_order_id, True) for o in incomplete_orders]
            except IOError as e:
                if "429" in str(e) and attempt < max_retries - 1:
                    wait = 0.5 * (2 ** attempt)
                    self.logger().warning(
                        f"cancel_all rate-limited (429), retrying in {wait}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(wait)
                    continue
                self.logger().network("Error in batch cancel-all.", exc_info=True)
                return [CancellationResult(o.client_order_id, False) for o in incomplete_orders]
            except Exception:
                self.logger().network("Error in batch cancel-all.", exc_info=True)
                return [CancellationResult(o.client_order_id, False) for o in incomplete_orders]

    async def _cancel_all_on_exchange(self):
        """Cancel all open orders on the exchange for configured trading pairs.
        Called on startup to clean up orphans from previous sessions."""
        for trading_pair in (self._trading_pairs or []):
            try:
                exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)
                await self._api_request(
                    path_url=CONSTANTS.CANCEL_ALL_PATH,
                    method=RESTMethod.POST,
                    data={"symbol": exchange_symbol},
                    is_auth_required=True,
                    limit_id=CONSTANTS.CANCEL_ALL_PATH,
                )
                self.logger().info(f"Startup cancel-all sent for {trading_pair}")
            except Exception:
                self.logger().debug(f"Startup cancel-all failed for {trading_pair} (may have no open orders)")

    # --- Reconciliation ---

    async def _reconciliation_loop(self):
        while True:
            try:
                await self._sleep(10.0)
                await self._reconcile_orders()
                await self._reconcile_balances()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception("Error in reconciliation loop.")
                await self._sleep(5.0)

    async def _reconcile_orders(self):
        try:
            # Fetch open orders for all configured trading pairs
            all_exchange_orders: Dict[str, Any] = {}
            for trading_pair in (self._trading_pairs or []):
                try:
                    exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)
                    response = await self._api_request(
                        path_url=CONSTANTS.OPEN_ORDERS_PATH,
                        method=RESTMethod.GET,
                        params={"symbol": exchange_symbol},
                        is_auth_required=True,
                        limit_id=CONSTANTS.OPEN_ORDERS_PATH,
                    )
                    orders_data = response if isinstance(response, list) else response.get("data", [])
                    for o in orders_data:
                        cid = str(o.get("client_order_id", ""))
                        if cid:
                            all_exchange_orders[cid] = o
                except Exception:
                    self.logger().debug(f"Could not fetch open orders for {trading_pair}")
            exchange_orders = all_exchange_orders

            for client_order_id, tracked_order in list(self._order_tracker.active_orders.items()):
                if client_order_id not in exchange_orders:
                    if tracked_order.is_open:
                        exchange_order_id = tracked_order.exchange_order_id or ""
                        order_url = f"{CONSTANTS.ORDER_STATUS_PATH}/{exchange_order_id}"
                        try:
                            status_response = await self._api_request(
                                method=RESTMethod.GET,
                                path_url=order_url,
                                is_auth_required=True,
                                limit_id=CONSTANTS.ORDER_STATUS_PATH,
                            )
                            order_data = status_response.get("data", status_response) if isinstance(status_response, dict) else status_response
                            new_state = CONSTANTS.ORDER_STATE.get(
                                order_data.get("status", ""), OrderState.OPEN
                            )
                            if new_state != tracked_order.current_state:
                                # Fetch fill details before updating state so the
                                # order tracker receives TradeUpdates before the
                                # state transition fires CompletedEvent.
                                if new_state in (OrderState.PARTIALLY_FILLED, OrderState.FILLED):
                                    try:
                                        trade_updates = await self._all_trade_updates_for_order(tracked_order)
                                        for tu in trade_updates:
                                            self._order_tracker.process_trade_update(tu)
                                    except Exception:
                                        self.logger().debug(
                                            f"Reconciliation: could not fetch fills for {client_order_id}"
                                        )
                                order_update = OrderUpdate(
                                    client_order_id=client_order_id,
                                    exchange_order_id=exchange_order_id,
                                    trading_pair=tracked_order.trading_pair,
                                    update_timestamp=self.current_timestamp,
                                    new_state=new_state,
                                )
                                self._order_tracker.process_order_update(order_update=order_update)
                                self.logger().warning(
                                    f"Reconciliation: order {client_order_id} state corrected to {new_state}"
                                )
                        except Exception:
                            self.logger().debug(
                                f"Reconciliation: could not fetch status for {client_order_id}"
                            )
        except Exception:
            self.logger().exception("Error during order reconciliation.")

    async def _reconcile_balances(self):
        try:
            await self._update_balances()
        except Exception:
            self.logger().exception("Error during balance reconciliation.")
