"""Funding rate fetcher for Bitget USDT-FUTURES.

When funding is extreme (one side overpays), the market often mean-reverts
because the costly side closes positions. This module exposes a cached
fetcher used as an optional gate in bot.py.

Public API:
    get_funding_rate(symbol) -> float | None   # current rate as decimal (0.0001 = 0.01%)
    is_funding_unfavorable(symbol, side, threshold_pct) -> bool

Failure mode: fetch error → returns None / False. Trading never blocked
on infra failure.

Caching: ~5 minutes per symbol to avoid hammering the API.
"""
import time
import requests
import threading


_BASE_URL = 'https://api.bitget.com'
_CACHE_TTL_SEC = 300  # 5 min
_cache = {}           # symbol -> (timestamp, rate)
_lock = threading.Lock()


def _fetch_funding_rate(symbol: str) -> float | None:
    """Single API call. Returns funding rate as decimal (e.g. 0.0001 = 0.01%)."""
    try:
        url = f'{_BASE_URL}/api/v2/mix/market/current-fund-rate'
        params = {'symbol': symbol, 'productType': 'USDT-FUTURES'}
        r = requests.get(url, params=params, timeout=5)
        if r.status_code != 200:
            return None
        d = r.json()
        if d.get('code') != '00000':
            return None
        data = d.get('data')
        if not data:
            return None
        # API returns list of dicts or single dict depending on version
        rec = data[0] if isinstance(data, list) else data
        rate_str = rec.get('fundingRate') or rec.get('rate')
        if rate_str is None:
            return None
        return float(rate_str)
    except Exception:
        return None


def get_funding_rate(symbol: str) -> float | None:
    """Cached fetch. Returns funding rate decimal or None on failure."""
    now = time.time()
    with _lock:
        cached = _cache.get(symbol)
        if cached and (now - cached[0]) < _CACHE_TTL_SEC:
            return cached[1]
    rate = _fetch_funding_rate(symbol)
    if rate is not None:
        with _lock:
            _cache[symbol] = (now, rate)
    return rate


def is_funding_unfavorable(symbol: str, side: str, threshold_pct: float = 0.10) -> tuple:
    """Returns (blocked: bool, reason: str, rate: float | None).

    blocked = True if funding strongly favors taking the opposite side.
      - For LONG entry: funding > +threshold% (longs overpay → reversal likely)
      - For SHORT entry: funding < -threshold% (shorts overpay → reversal likely)

    Fail-open: if rate fetch fails → returns (False, '', None). Trade not blocked.
    """
    rate = get_funding_rate(symbol)
    if rate is None:
        return (False, '', None)
    rate_pct = rate * 100.0
    threshold = abs(threshold_pct)

    side = (side or '').lower()
    if side == 'long' and rate_pct > threshold:
        return (True, f'funding +{rate_pct:.4f}% > +{threshold}% (longs overpaying)', rate)
    if side == 'short' and rate_pct < -threshold:
        return (True, f'funding {rate_pct:.4f}% < -{threshold}% (shorts overpaying)', rate)
    return (False, '', rate)


def reset_cache():
    """For tests."""
    with _lock:
        _cache.clear()
