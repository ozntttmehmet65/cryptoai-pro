"""Safe Binance contract identifiers and backwards-compatible local file names.

Exchange-listed Unicode contracts are legitimate. Only file paths need ASCII
keys; preserve all previously persisted ASCII model/cache names unchanged.
"""
from __future__ import annotations
import hashlib
import re
import unicodedata

ASCII_CONTRACT = re.compile(r"^[A-Z0-9]{2,32}$")


def symbol_valid(symbol: str) -> bool:
    if not isinstance(symbol, str) or not 2 <= len(symbol) <= 32:
        return False
    if unicodedata.normalize("NFC", symbol) != symbol or symbol.upper() != symbol:
        return False
    # Only Unicode letters or decimal digits; no path characters, whitespace,
    # invisible marks, combining controls, punctuation, or separators.
    return all(unicodedata.category(ch).startswith("L") or
               unicodedata.category(ch) == "Nd" for ch in symbol)


def symbol_file_key(symbol: str) -> str:
    """Legacy ASCII filenames do not move; Unicode symbols get stable safe keys."""
    if not symbol_valid(symbol):
        raise ValueError("Invalid Futures symbol")
    if ASCII_CONTRACT.fullmatch(symbol):
        return symbol
    return "u-" + hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:40]
