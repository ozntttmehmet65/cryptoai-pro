"""Shared entry sizing and bounded reference tolerance for paper execution."""
POLICY_VERSION = 'V7.3.2'
PAPER_NOTIONAL_USDT = 100.0
MAX_REFERENCE_DEPARTURE_ATR = 0.75
MAX_BRACKET_DEPARTURE_FRACTION = 0.5


def reference_departure_limit(tp_atr, sl_atr):
    # Never tolerate more than half of either frozen bracket distance.
    return min(MAX_REFERENCE_DEPARTURE_ATR,
               MAX_BRACKET_DEPARTURE_FRACTION * min(tp_atr, sl_atr))
