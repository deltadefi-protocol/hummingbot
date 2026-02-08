from decimal import Decimal
from typing import Any, Dict

from pydantic import Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

CENTRALIZED = True

EXAMPLE_PAIR = "ADA-USDM"

DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.001"),
    taker_percent_fee_decimal=Decimal("0.001"),
)


class DeltaDefiConfigMap(BaseConnectorConfigMap):
    connector: str = "deltadefi"
    deltadefi_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your DeltaDeFi API key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    deltadefi_password: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your DeltaDeFi wallet password (for signing transactions)",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    deltadefi_network: str = Field(
        default="mainnet",
        json_schema_extra={
            "prompt": "Which DeltaDeFi network? (mainnet/testnet)",
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )


KEYS = DeltaDefiConfigMap.model_construct()


def is_exchange_information_valid(exchange_info: Dict[str, Any]) -> bool:
    return (
        exchange_info.get("status", None) in ("active", None)
        and exchange_info.get("base", "") != ""
        and exchange_info.get("quote", "") != ""
    )
