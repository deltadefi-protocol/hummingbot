import sys

from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.common import OrderType
from hummingbot.core.data_type.in_flight_order import OrderState

CLIENT_ID_PREFIX = "hbot-deltadefi"
MAX_ORDER_ID_LEN = 64
WS_HEARTBEAT_TIME_INTERVAL = 30
SECONDS_TO_WAIT_TO_RECEIVE_MESSAGE = WS_HEARTBEAT_TIME_INTERVAL * 0.8

DEFAULT_DOMAIN = "mainnet"

# Domain URLs
REST_URLS = {
    "mainnet": "https://api.deltadefi.io",
    "testnet": "https://api-staging.deltadefi.io",
}

WSS_URLS = {
    "mainnet": "wss://stream.deltadefi.io",
    "testnet": "wss://stream-staging.deltadefi.io",
}

# REST API endpoints
BALANCE_PATH = "/accounts/balance"
OPEN_ORDERS_PATH = "/accounts/open-orders"
TRADE_ORDERS_PATH = "/accounts/trade-orders"
ORDER_STATUS_PATH = "/accounts/order"  # GET /accounts/order/{id}
OPERATION_KEY_PATH = "/accounts/operation-key"
TRADES_PATH = "/accounts/trades"
ORDER_BUILD_PATH = "/order/build"
ORDER_SUBMIT_PATH = "/order/submit"
CANCEL_ORDER_PATH = "/order"  # POST /order/{id}/cancel
CANCEL_ALL_PATH = "/order/cancel-all"
MARKET_PRICE_PATH = "/market/market-price"
TRADING_PAIRS_PATH = "/app/market-config"
MARKET_DEPTH_PATH = "/market/depth"

# WS endpoints (separate connections, not channels)
WS_ACCOUNT_STREAM = "/accounts/stream"  # ?api_key={key}
WS_MARKET_DEPTH = "/market/depth"  # /{symbol}
WS_MARKET_PRICE = "/market/market-price"  # /{symbol}
WS_RECENT_TRADES = "/market/recent-trades"  # /{symbol}

# Order state mapping: DeltaDeFi lowercase states -> Hummingbot OrderState
ORDER_STATE = {
    "building": OrderState.OPEN,
    "processing": OrderState.OPEN,
    "open": OrderState.OPEN,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "fully_filled": OrderState.FILLED,
    "cancelled": OrderState.CANCELED,
    "partially_cancelled": OrderState.CANCELED,
    "closed": OrderState.FILLED,
    "failed": OrderState.FAILED,
}

# Order type mapping: Hummingbot OrderType -> DeltaDeFi strings (lowercase)
ORDER_TYPE_MAP = {
    OrderType.LIMIT: "limit",
    OrderType.MARKET: "market",
}

# Order sides (lowercase)
SIDE_BUY = "buy"
SIDE_SELL = "sell"

# Rate limiting
WS_CONNECTION_LIMIT_ID = "WSConnection"
WS_REQUEST_LIMIT_ID = "WSRequest"
WS_SUBSCRIPTION_LIMIT_ID = "WSSubscription"

NO_LIMIT = sys.maxsize

RATE_LIMITS = [
    RateLimit(WS_CONNECTION_LIMIT_ID, limit=3, time_interval=1),
    RateLimit(WS_REQUEST_LIMIT_ID, limit=100, time_interval=10),
    RateLimit(WS_SUBSCRIPTION_LIMIT_ID, limit=240, time_interval=60 * 60),
    RateLimit(limit_id=TRADING_PAIRS_PATH, limit=20, time_interval=2),
    RateLimit(limit_id=BALANCE_PATH, limit=10, time_interval=2),
    RateLimit(limit_id=ORDER_BUILD_PATH, limit=20, time_interval=2),
    RateLimit(limit_id=ORDER_SUBMIT_PATH, limit=20, time_interval=2),
    RateLimit(limit_id=CANCEL_ORDER_PATH, limit=20, time_interval=2),
    RateLimit(limit_id=CANCEL_ALL_PATH, limit=10, time_interval=2),
    RateLimit(limit_id=OPEN_ORDERS_PATH, limit=20, time_interval=2),
    RateLimit(limit_id=ORDER_STATUS_PATH, limit=20, time_interval=2),
    RateLimit(limit_id=MARKET_PRICE_PATH, limit=20, time_interval=2),
    RateLimit(limit_id=MARKET_DEPTH_PATH, limit=20, time_interval=2),
    RateLimit(limit_id=OPERATION_KEY_PATH, limit=5, time_interval=10),
    RateLimit(limit_id=TRADE_ORDERS_PATH, limit=20, time_interval=2),
    RateLimit(limit_id=TRADES_PATH, limit=20, time_interval=2),
]
