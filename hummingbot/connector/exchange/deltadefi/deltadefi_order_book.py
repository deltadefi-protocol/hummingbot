from typing import Dict, Optional

from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType


class DeltaDefiOrderBook(OrderBook):

    @classmethod
    def snapshot_message_from_exchange(cls,
                                       msg: Dict[str, any],
                                       timestamp: float,
                                       metadata: Optional[Dict] = None) -> OrderBookMessage:
        if metadata:
            msg.update(metadata)

        bids = msg.get("bids", [])
        asks = msg.get("asks", [])

        # DeltaDeFi sends bids/asks as [{price: F, quantity: F}, ...]
        formatted_bids = [
            (float(b["price"]), float(b["quantity"])) if isinstance(b, dict) else (b[0], b[1])
            for b in bids
        ]
        formatted_asks = [
            (float(a["price"]), float(a["quantity"])) if isinstance(a, dict) else (a[0], a[1])
            for a in asks
        ]

        return OrderBookMessage(OrderBookMessageType.SNAPSHOT, {
            "trading_pair": msg["trading_pair"],
            "update_id": msg.get("update_id", int(timestamp)),
            "bids": formatted_bids,
            "asks": formatted_asks,
        }, timestamp=timestamp)

    @classmethod
    def diff_message_from_exchange(cls,
                                   msg: Dict[str, any],
                                   timestamp: Optional[float] = None,
                                   metadata: Optional[Dict] = None) -> OrderBookMessage:
        if metadata:
            msg.update(metadata)

        bids = msg.get("bids", [])
        asks = msg.get("asks", [])

        formatted_bids = [
            (float(b["price"]), float(b["quantity"])) if isinstance(b, dict) else (b[0], b[1])
            for b in bids
        ]
        formatted_asks = [
            (float(a["price"]), float(a["quantity"])) if isinstance(a, dict) else (a[0], a[1])
            for a in asks
        ]

        return OrderBookMessage(OrderBookMessageType.DIFF, {
            "trading_pair": msg["trading_pair"],
            "update_id": msg.get("update_id", int(timestamp)),
            "bids": formatted_bids,
            "asks": formatted_asks,
        }, timestamp=timestamp)

    @classmethod
    def trade_message_from_exchange(cls, msg: Dict[str, any], metadata: Optional[Dict] = None):
        if metadata:
            msg.update(metadata)
        ts = msg.get("timestamp", 0)
        # DeltaDeFi uses lowercase side: "buy" / "sell"
        side = msg.get("side", "").lower()
        return OrderBookMessage(OrderBookMessageType.TRADE, {
            "trading_pair": msg["trading_pair"],
            "trade_type": float(TradeType.BUY.value) if side == "buy" else float(TradeType.SELL.value),
            "trade_id": msg.get("trade_id", ts),
            "update_id": ts,
            "price": msg["price"],
            "amount": msg.get("amount", msg.get("quantity", "0")),
        }, timestamp=ts * 1e-3 if ts > 1e12 else ts)
