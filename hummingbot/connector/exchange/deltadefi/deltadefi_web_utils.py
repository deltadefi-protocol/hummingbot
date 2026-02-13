import time
from typing import Optional

import hummingbot.connector.exchange.deltadefi.deltadefi_constants as CONSTANTS
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


def public_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    base_url = CONSTANTS.REST_URLS.get(domain, CONSTANTS.REST_URLS[CONSTANTS.DEFAULT_DOMAIN])
    return f"{base_url}{path_url}"


def private_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    return public_rest_url(path_url, domain)


def wss_url(domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    return CONSTANTS.WSS_URLS.get(domain, CONSTANTS.WSS_URLS[CONSTANTS.DEFAULT_DOMAIN])


def public_ws_url(path: str, symbol: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    base = wss_url(domain)
    return f"{base}{path}/{symbol}"


def private_ws_url(api_key: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    base = wss_url(domain)
    return f"{base}{CONSTANTS.WS_ACCOUNT_STREAM}?api_key={api_key}"


def build_api_factory(
        throttler: Optional[AsyncThrottler] = None,
        auth: Optional[AuthBase] = None,
        domain: str = CONSTANTS.DEFAULT_DOMAIN) -> WebAssistantsFactory:
    throttler = throttler or create_throttler()
    api_factory = WebAssistantsFactory(
        throttler=throttler,
        auth=auth)
    return api_factory


def create_throttler() -> AsyncThrottler:
    return AsyncThrottler(CONSTANTS.RATE_LIMITS)


async def get_current_server_time(
        throttler: Optional[AsyncThrottler] = None,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
) -> float:
    # DeltaDeFi has no server time endpoint; use local time.
    return time.time()
