"""Shared candle fetch + log-return helpers.

Used by robustness.py and correlation_matrix.py. Centralizes the
fetch / inner-join / log-return logic so the two tools cannot drift.

Functions:
    fetch_closes_map(symbol, granularity, days, fetch_fn)
        -> ({ts_ms: close_price}, error_string_or_None)

    pair_log_returns_from_maps(map_a, map_b, granularity_seconds=3600)
        -> (ra, rb, diag_dict)
        where diag_dict has n_common_candles, n_gap_dropped, n_price_invalid.
        If maps don't overlap, returns empty lists with diag.

The pair function expects consecutive-bar adjacency (e.g. exactly 3600s for
1H candles). Non-adjacent bars are dropped (counted in n_gap_dropped) so
log returns are not computed across gaps.
"""
import math


def fetch_closes_map(symbol, granularity, days, fetch_fn):
    """Fetch candles for one symbol, return ({ts_ms: close}, error_or_None).

    granularity: '1H', '5m', '15m', '4H' (passed through to fetch_fn).
    fetch_fn: backtest.fetch_candles_range or compatible. Returns oldest->newest
        list of [ts_ms, open, high, low, close, volume, ...].
    """
    if not fetch_fn:
        return None, 'no fetch function available'
    try:
        candles = fetch_fn(symbol, granularity, days)
    except Exception as e:
        return None, f'fetch failed: {type(e).__name__}: {e}'
    if not candles:
        return None, 'empty candles returned'
    try:
        close_map = {int(c[0]): float(c[4]) for c in candles}
    except (TypeError, ValueError, IndexError) as e:
        return None, f'candle parse failed: {e}'
    if not close_map:
        return None, 'no usable candles after parse'
    return close_map, None


def pair_log_returns_from_maps(map_a, map_b, granularity_seconds=3600):
    """Inner-join two close maps on ts, return paired log returns + diag.

    Drops bars that aren't adjacent in time (gap_dropped) and bars with
    non-positive prices (price_invalid). All counters are exposed so
    callers can surface them in their result schema.
    """
    diag = {'n_common_candles': 0, 'n_gap_dropped': 0, 'n_price_invalid': 0}
    if not map_a or not map_b:
        return [], [], diag
    common = sorted(set(map_a.keys()) & set(map_b.keys()))
    diag['n_common_candles'] = len(common)
    granularity_ms = granularity_seconds * 1000
    ra = []
    rb = []
    for i in range(1, len(common)):
        ts_prev, ts_curr = common[i - 1], common[i]
        if ts_curr - ts_prev != granularity_ms:
            diag['n_gap_dropped'] += 1
            continue
        pa_prev = map_a[ts_prev]
        pa_curr = map_a[ts_curr]
        pb_prev = map_b[ts_prev]
        pb_curr = map_b[ts_curr]
        if pa_prev <= 0 or pa_curr <= 0 or pb_prev <= 0 or pb_curr <= 0:
            diag['n_price_invalid'] += 1
            continue
        ra.append(math.log(pa_curr / pa_prev))
        rb.append(math.log(pb_curr / pb_prev))
    return ra, rb, diag
