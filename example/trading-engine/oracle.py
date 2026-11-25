"""Independent acceptance projections for the public book and account statements."""

from collections import defaultdict
from decimal import Decimal
from fractions import Fraction


QUOTE_SCALE = 10**16
BASE_SCALE = 10**8
PRICE_SCALE = 100


def scaled(text, multiplier):
    amount = Fraction(Decimal(text)) * multiplier
    if amount.denominator != 1:
        raise ValueError(f"{text} is outside the declared precision")
    return amount.numerator


def book_checkpoint(snapshot):
    def levels(rows):
        book = defaultdict(int)
        identities = set()
        for price, size, order_id in rows:
            assert order_id not in identities
            identities.add(order_id)
            book[scaled(price, PRICE_SCALE)] += scaled(size, BASE_SCALE)
        return book

    bids = levels(snapshot["bids"])
    asks = levels(snapshot["asks"])
    bid = max(bids, default=0)
    ask = min(asks, default=0)
    return {
        "sequence": snapshot["sequence"],
        "bid": bid,
        "bid_size": bids.get(bid, 0),
        "ask": ask,
        "ask_size": asks.get(ask, 0),
        "orders": len(snapshot["bids"]) + len(snapshot["asks"]),
    }


def balances_from_statement(opening, fills, adjustments=()):
    """Calculate balances from the venue's settled statement and unique fills."""
    amounts = {currency: Fraction(Decimal(balance)) for currency, balance in opening.items()}
    seen = {}
    for fill in fills:
        venue_key = (fill["product_id"], fill["order_id"], fill["trade_id"])
        if not fill["settled"]:
            continue
        values = tuple(fill[field] for field in ("size", "price", "fee", "side"))
        if venue_key in seen:
            assert seen[venue_key] == values, f"contradictory execution {venue_key!r}"
            continue
        seen[venue_key] = values
        size, price, fee = (Fraction(Decimal(value)) for value in values[:3])
        base, quote = fill["product_id"].split("-")
        signed_size = size if fill["side"] == "buy" else -size
        amounts[base] += signed_size
        amounts[quote] -= price * signed_size + fee
    for currency, amount in adjustments:
        amounts[currency] += Fraction(Decimal(amount))

    balances = {}
    for currency, amount in amounts.items():
        scaled_amount = amount * (QUOTE_SCALE if currency == "USD" else BASE_SCALE)
        assert scaled_amount.denominator == 1
        balances[currency] = scaled_amount.numerator
    return balances
