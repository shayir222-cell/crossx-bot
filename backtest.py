"""Offline backtest of CrossX scoring strategy on Bitget historical candles.

WHAT IT DOES
============
1. Fetches N days of 5-minute candles for each symbol from Bitget public API
2. For each candle (post-warmup), computes the same score as the live bot
   using bot.score_signal logic (with fallback to bot's get_tf_bias)
3. When score >= MIN_SCORE: simulates entry at NEXT candle's open
4. Simulates exit using SL (ATR×SL_MULT) and TP (ATR×TP_R)
5. Reports: per-symbol win rate, profit factor, expectancy, max DD,
   equity curve

WHAT IT DOES NOT DO (limitations)
==================================
- No realistic slippage/fees modeling (uses fixed assumption)
- No session/cooldown/anti-dup gates (these depend on signal *order*)
- No MTF / news / correlation gates (offline data limitations)
- No partial fills, no trailing stop, no time stop (only simple SL/TP)
- ADX/funding NOT applied (test pure scoring first; add gates later)

So results are an OPTIMISTIC upper bound on the strategy's potential.
If backtest shows negative expectancy → live will be worse. If positive,
live still needs to overcome the gaps above.

USAGE
=====
    python backtest.py                          # 30 days, all 7 symbols
    python backtest.py --days 60 --symbol BTCUSDT
    python backtest.py --tp_r 5 --sl_atr 2.5    # custom exit rules

DEPENDENCIES
============
Requires bot.py to be importable (uses calc_atr, calc_rsi, score_signal,
get_tf_bias logic). Does NOT require Bitget API key — uses public endpoints.

OUTPUT
======
Per-symbol stats + overall summary printed to stdout.
Detailed trade log written to backtest_<timestamp>.csv.
"""
import argparse
import csv
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests


# ── Bitget public candle endpoint ─────────────────────────────────────
BASE_URL = 'https://api.bitget.com'

# Module-level default so backtest_symbol() works when imported (not only via
# main()'s argparse). main() overrides this via `global _LONGS_ONLY`.
_LONGS_ONLY = False

def fetch_candles_batch(symbol: str, granularity: str, end_ts_ms: int, limit: int = 1000, max_retries: int = 3):
    """Fetch up to `limit` candles ending at end_ts_ms with retry on SSL/network errors.
    Bitget returns ASC order. Each candle: [ts, o, h, l, c, vol, quote_vol]"""
    url = f'{BASE_URL}/api/v2/mix/market/history-candles'
    params = {
        'symbol': symbol,
        'productType': 'USDT-FUTURES',
        'granularity': granularity,
        'endTime': str(end_ts_ms),
        'limit': str(limit),
    }
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code != 200:
                return None
            d = r.json()
            if d.get('code') != '00000':
                return None
            return d.get('data') or []
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            if attempt == max_retries - 1:
                print(f'  [NET-FAIL] {symbol} {granularity}: {e}', file=sys.stderr)
                return None
            time.sleep(1.0 * (attempt + 1))  # 1s, 2s, 3s backoff
        except Exception:
            return None
    return None


def fetch_candles_range(symbol: str, granularity: str, days: int):
    """Fetch `days` of candles by paginating backwards. Returns oldest->newest list."""
    sec_per_candle = {'5m': 300, '15m': 900, '1H': 3600, '4H': 14400}.get(granularity, 300)
    needed = days * 24 * 3600 // sec_per_candle
    out = []
    end_ts_ms = int(time.time() * 1000)
    batch_limit = 200  # Bitget history-candles max
    empty_streak = 0   # consecutive failed batches at the same offset
    while len(out) < needed:
        batch = fetch_candles_batch(symbol, granularity, end_ts_ms, limit=batch_limit)
        if not batch:
            # Don't abandon the whole range on a transient timeout — retry the
            # same offset a few times with backoff before giving up.
            empty_streak += 1
            if empty_streak >= 4:
                print(f'  [PARTIAL] {symbol} {granularity}: stopped after '
                      f'{empty_streak} empty batches, have {len(out)} candles', file=sys.stderr)
                break
            time.sleep(2.0 * empty_streak)
            continue
        empty_streak = 0
        out = batch + out
        oldest = int(batch[0][0])
        if oldest >= end_ts_ms:
            break
        end_ts_ms = oldest - 1
        time.sleep(0.3)
    # Deduplicate by ts and sort ascending
    seen = set()
    cleaned = []
    for c in out:
        ts = int(c[0])
        if ts in seen:
            continue
        seen.add(ts)
        cleaned.append(c)
    cleaned.sort(key=lambda x: int(x[0]))
    return cleaned[-needed:] if len(cleaned) > needed else cleaned


# ── Indicator + scoring (mirrors bot.py) ──────────────────────────────
def calc_atr(candles, period=14):
    if not candles or len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = float(candles[i][2]), float(candles[i][3]), float(candles[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period

def calc_ema(values, period):
    if len(values) < period:
        return sum(values) / len(values)
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema

def calc_rsi(candles, period=14):
    if not candles or len(candles) < period + 1:
        return 50.0
    closes = [float(c[4]) for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))


def calc_adx(candles, period=14):
    """Wilder ADX (0-100). Mirrors bot.calc_adx logic."""
    if not candles or len(candles) < (period * 2 + 1):
        return 0.0
    try:
        highs  = [float(c[2]) for c in candles]
        lows   = [float(c[3]) for c in candles]
        closes = [float(c[4]) for c in candles]
        tr, pdm, mdm = [], [], []
        for i in range(1, len(candles)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr.append(max(h-l, abs(h-pc), abs(l-pc)))
            up   = highs[i] - highs[i-1]
            down = lows[i-1] - lows[i]
            pdm.append(up   if (up   > down and up   > 0) else 0.0)
            mdm.append(down if (down > up   and down > 0) else 0.0)
        def w(values, p):
            if len(values) < p: return []
            out = [sum(values[:p])]
            for v in values[p:]:
                out.append(out[-1] - out[-1]/p + v)
            return out
        tr_s, pdm_s, mdm_s = w(tr, period), w(pdm, period), w(mdm, period)
        if not tr_s: return 0.0
        dx = []
        for tr_v, pdm_v, mdm_v in zip(tr_s, pdm_s, mdm_s):
            if tr_v == 0: dx.append(0.0); continue
            pdi  = 100.0 * pdm_v / tr_v
            mdi  = 100.0 * mdm_v / tr_v
            denom = pdi + mdi
            dx.append(0.0 if denom == 0 else 100.0 * abs(pdi - mdi) / denom)
        if len(dx) < period:
            return sum(dx) / max(len(dx), 1)
        adx = sum(dx[:period]) / period
        for v in dx[period:]:
            adx = (adx * (period-1) + v) / period
        return max(0.0, min(100.0, adx))
    except Exception:
        return 0.0

def tf_bias_from_candles(candles):
    """Simplified bias inference from candle set: bullish if last close > EMA50, else bearish."""
    if not candles or len(candles) < 50:
        return 'neutral'
    closes = [float(c[4]) for c in candles]
    last = closes[-1]
    ema50 = calc_ema(closes, 50)
    if last > ema50 * 1.001:
        return 'bullish'
    if last < ema50 * 0.999:
        return 'bearish'
    return 'neutral'


def calc_ema_series(closes, period):
    """Returns full EMA series, not just last value."""
    if len(closes) < period:
        return [sum(closes) / len(closes)] * len(closes)
    out = []
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for i in range(len(closes)):
        if i < period:
            out.append(ema)
        else:
            ema = closes[i] * k + ema * (1 - k)
            out.append(ema)
    return out


def calc_dmi_di(candles, period=14):
    """Returns (di_plus, di_minus) — current values."""
    if not candles or len(candles) < period * 2 + 1:
        return (0.0, 0.0)
    try:
        highs  = [float(c[2]) for c in candles]
        lows   = [float(c[3]) for c in candles]
        closes = [float(c[4]) for c in candles]
        tr, pdm, mdm = [], [], []
        for i in range(1, len(candles)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr.append(max(h-l, abs(h-pc), abs(l-pc)))
            up   = highs[i] - highs[i-1]
            down = lows[i-1] - lows[i]
            pdm.append(up   if (up   > down and up   > 0) else 0.0)
            mdm.append(down if (down > up   and down > 0) else 0.0)
        def w(values, p):
            if len(values) < p: return [0.0]
            out = [sum(values[:p])]
            for v in values[p:]:
                out.append(out[-1] - out[-1]/p + v)
            return out
        tr_s, pdm_s, mdm_s = w(tr, period), w(pdm, period), w(mdm, period)
        tr_v = tr_s[-1]
        if tr_v == 0:
            return (0.0, 0.0)
        return (100.0 * pdm_s[-1] / tr_v, 100.0 * mdm_s[-1] / tr_v)
    except Exception:
        return (0.0, 0.0)


def score_signal_v3(side, candles_5m, candles_1h,
                     rsi_long=(45, 70), rsi_short=(30, 55),
                     adx_min=25, vol_mult=1.3, ema_short=20, ema_long=50, ema_macro=200):
    """v3 Triple Confirmation strategy: returns (100, breakdown) if all conditions
    met, (0, breakdown) otherwise. Binary trigger, not gradient."""
    breakdown = {}
    if not candles_5m or len(candles_5m) < 60 or not candles_1h or len(candles_1h) < ema_macro + 5:
        return (0, {'reason': 'insufficient_data'})

    closes_5m = [float(c[4]) for c in candles_5m]
    closes_1h = [float(c[4]) for c in candles_1h]
    vols_5m   = [float(c[5]) for c in candles_5m]
    last_5m   = candles_5m[-1]
    open_p    = float(last_5m[1])
    close_p   = float(last_5m[4])

    # EMAs
    ema_s_series = calc_ema_series(closes_5m, ema_short)
    ema_l_series = calc_ema_series(closes_5m, ema_long)
    ema_m_series = calc_ema_series(closes_1h, ema_macro)
    es, el, em = ema_s_series[-1], ema_l_series[-1], ema_m_series[-1]

    # RSI & ADX & DI on 5m
    rsi = calc_rsi(candles_5m, 14)
    adx = calc_adx(candles_5m, 14)
    di_plus, di_minus = calc_dmi_di(candles_5m, 14)

    # Volume confirmation
    vol_ma = sum(vols_5m[-20:]) / 20 if len(vols_5m) >= 20 else 0
    vol_strong = (vols_5m[-1] > vol_ma * vol_mult) if vol_ma > 0 else False

    is_long = (side == 'long')
    body_ok = (close_p > open_p) if is_long else (close_p < open_p)
    macro_ok = (close_p > em) if is_long else (close_p < em)
    trend_ok = (close_p > es and es > el) if is_long else (close_p < es and es < el)
    rsi_lo, rsi_hi = rsi_long if is_long else rsi_short
    rsi_ok = (rsi_lo <= rsi <= rsi_hi)
    adx_ok = (adx >= adx_min)
    di_ok  = (di_plus > di_minus) if is_long else (di_minus > di_plus)

    breakdown.update(macro=macro_ok, trend=trend_ok, rsi=rsi_ok, rsi_val=round(rsi,1),
                     adx=adx_ok, adx_val=round(adx,1), di=di_ok, vol=vol_strong, body=body_ok)
    all_pass = macro_ok and trend_ok and rsi_ok and adx_ok and di_ok and vol_strong and body_ok
    return (100 if all_pass else 0, breakdown)


def score_signal(side, candles_5m, candles_15m, candles_1h, candles_4h):
    """Mirror of bot.score_signal — bot uses payload values OR get_tf_bias fallback.
    In backtest we always use the fallback (offline-derived bias)."""
    score = 0
    breakdown = {}
    is_long = (side == 'long')

    def pts(val, bull_pts, neutral_pts):
        if is_long:
            if val == 'bullish': return bull_pts
            if val == 'neutral': return neutral_pts
            return 0
        else:
            if val == 'bearish': return bull_pts
            if val == 'neutral': return neutral_pts
            return 0

    mt = tf_bias_from_candles(candles_4h)
    p = pts(mt, 25, 12); score += p; breakdown['macro_trend'] = (mt, p)

    mom = tf_bias_from_candles(candles_1h)
    p = pts(mom, 20, 10); score += p; breakdown['momentum'] = (mom, p)

    struct = tf_bias_from_candles(candles_15m)
    p = pts(struct, 20, 10); score += p; breakdown['structure'] = (struct, p)

    # Volume bias: last 5 vs prior 20
    if candles_5m and len(candles_5m) >= 25:
        vols = [float(c[5]) for c in candles_5m]
        recent_avg = sum(vols[-5:]) / 5
        baseline = sum(vols[-25:-5]) / 20 if sum(vols[-25:-5]) > 0 else 1
        if recent_avg > baseline * 1.2:
            v = 'bullish'
        elif recent_avg < baseline * 0.8:
            v = 'bearish'
        else:
            v = 'neutral'
    else:
        v = 'neutral'
    p = pts(v, 15, 7); score += p; breakdown['volume'] = (v, p)

    ctf = tf_bias_from_candles(candles_5m)
    p = pts(ctf, 10, 5); score += p; breakdown['current_tf'] = (ctf, p)

    rsi = calc_rsi(candles_5m, 14)
    if rsi < 35:    ob = 'oversold'
    elif rsi > 65:  ob = 'overbought'
    else:           ob = 'neutral'

    ob_pts = 10 if (is_long and ob in ('oversold', 'neutral')) or \
                   (not is_long and ob in ('overbought', 'neutral')) else 0
    score += ob_pts
    breakdown['ob_os'] = (ob, ob_pts, rsi)

    # RSI penalty
    pen = 0
    if is_long:
        if rsi >= 78: pen = -10
        elif rsi >= 70: pen = -5
    else:
        if rsi <= 22: pen = -10
        elif rsi <= 30: pen = -5
    score += pen

    return score, breakdown


def calc_stdev(values):
    """Population stdev of a list (returns 0.0 for <2 values)."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return var ** 0.5


def calc_var_ratio(candles, short_w=10, long_w=40):
    """Variance ratio of log returns: stdev(short) / stdev(long).
    >1.0 → recent vol elevated (trend persistence); ~1.0 random walk;
    <1.0 mean-reverting. Mirrors Pine v7 `var_ratio`. Returns 1.0 if
    insufficient data so the gate is a no-op rather than a false block."""
    closes = [float(c[4]) for c in candles]
    if len(closes) < long_w + 1:
        return 1.0
    rets = []
    for i in range(1, len(closes)):
        prev = closes[i - 1] if closes[i - 1] != 0 else closes[i]
        rets.append(math.log(closes[i] / prev) if prev > 0 else 0.0)
    short_std = calc_stdev(rets[-short_w:])
    long_std = calc_stdev(rets[-long_w:])
    if long_std <= 0:
        return 1.0
    return short_std / long_std


# ── Trade simulator ───────────────────────────────────────────────────
def simulate_trade(side, entry_price, sl_price, tp_price, future_candles, max_bars=60,
                   be_at_r=0.0, be_buffer_pct=0.0, sl_dist=0.0):
    """Simulate fill outcome over future candles.
    Returns (outcome, exit_price, bars_held):
      outcome: 'tp' | 'sl' | 'be' | 'time' | 'noop'

    Break-even option (mirrors live bot + Pine v7 BE-with-fee-buffer):
      If be_at_r > 0, once price moves be_at_r * sl_dist in favour, the stop
      is ratcheted to entry ± (be_buffer_pct% of entry). buffer>0 leaves room
      for round-trip fees so a BE exit nets ~0 instead of -fees. A subsequent
      adverse touch of that stop exits as 'be' (distinct from the original 'sl').
    """
    if not future_candles:
        return ('noop', entry_price, 0)
    be_armed = False
    cur_sl = sl_price
    use_be = be_at_r > 0.0 and sl_dist > 0.0
    for idx, c in enumerate(future_candles[:max_bars]):
        h, l = float(c[2]), float(c[3])
        if side == 'long':
            # Hit either SL or TP this bar — pessimistic: SL first
            if l <= cur_sl:
                return ('be' if be_armed else 'sl', cur_sl, idx + 1)
            if h >= tp_price:
                return ('tp', tp_price, idx + 1)
            # Arm BE after the TP/SL check (can't move stop on the entry bar's
            # own spike below; ratchet applies from next evaluation onward)
            if use_be and not be_armed and h >= entry_price + be_at_r * sl_dist:
                be_armed = True
                cur_sl = entry_price * (1.0 + be_buffer_pct / 100.0)
        else:  # short
            if h >= cur_sl:
                return ('be' if be_armed else 'sl', cur_sl, idx + 1)
            if l <= tp_price:
                return ('tp', tp_price, idx + 1)
            if use_be and not be_armed and l <= entry_price - be_at_r * sl_dist:
                be_armed = True
                cur_sl = entry_price * (1.0 - be_buffer_pct / 100.0)
    # Time stop at last bar
    last_close = float(future_candles[min(max_bars, len(future_candles)) - 1][4])
    return ('time', last_close, min(max_bars, len(future_candles)))


# ── Main backtest loop ────────────────────────────────────────────────
def backtest_symbol(symbol, days, min_score, sl_atr_mult, tp_r, fee_pct, leverage,
                    adx_min=0.0, strategy='v2', block_hours=None, min_atr_pct=0.0,
                    climax_mult=0.0, var_ratio_min=0.0, wick_reject=False,
                    fake_break=False, be_at_r=0.0, be_buffer_pct=0.0, prefetched=None):
    print(f'\n=== {symbol} ===')
    if prefetched is not None:
        # Reuse candles fetched once by a caller (e.g. multi-config runner) to
        # avoid re-downloading the same history for every parameter variant.
        c5, c15, c1h, c4h = prefetched
        if not c5 or len(c5) < 100:
            print(f'  [SKIP] not enough 5m data ({len(c5) if c5 else 0} candles)')
            return None
    else:
        print(f'  Fetching 5m candles ({days}d)...')
        c5  = fetch_candles_range(symbol, '5m',  days)
        if not c5 or len(c5) < 100:
            print(f'  [SKIP] not enough 5m data ({len(c5) if c5 else 0} candles)')
            return None
        print(f'  Fetching 15m / 1H / 4H...')
        c15 = fetch_candles_range(symbol, '15m', days)
        c1h = fetch_candles_range(symbol, '1H',  days)
        c4h = fetch_candles_range(symbol, '4H',  days)

    # Build timestamp→index maps for 15m/1h/4h (lookup higher-TF context per 5m bar)
    def map_by_ts(candles):
        return {int(c[0]): i for i, c in enumerate(candles)}
    map_15 = map_by_ts(c15)
    map_1h = map_by_ts(c1h)
    map_4h = map_by_ts(c4h)

    def floor_ts(ts_ms, sec):
        ms = sec * 1000
        return (ts_ms // ms) * ms

    trades = []
    print(f'  Replay {len(c5)} bars, MIN_SCORE={min_score}...')
    for i in range(80, len(c5) - 60):
        bar = c5[i]
        ts = int(bar[0])
        close = float(bar[4])

        # Get higher-TF context (use last completed TF candle ≤ this 5m bar)
        ts_15 = floor_ts(ts, 900)
        ts_1h = floor_ts(ts, 3600)
        ts_4h = floor_ts(ts, 14400)
        idx_15 = map_15.get(ts_15)
        idx_1h = map_1h.get(ts_1h)
        idx_4h = map_4h.get(ts_4h)
        if idx_15 is None or idx_1h is None or idx_4h is None:
            continue

        # Window sizes: v2 uses 80-bar lookback; v3 needs 210+ on 1H for EMA200.
        win5  = c5[max(0, i-79):i+1]
        win15 = c15[max(0, idx_15-79):idx_15+1]
        win1h_size = 210 if strategy == 'v3' else 79
        win1h = c1h[max(0, idx_1h-win1h_size):idx_1h+1]
        win4h = c4h[max(0, idx_4h-79):idx_4h+1]

        atr = calc_atr(win5, 14)
        if atr == 0:
            continue

        # Time-of-day blackout (UTC hour of decision bar -- not fill bar -- to
        # match what a live signal generator sees at decision time).
        if block_hours:
            hour = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).hour
            if hour in block_hours:
                continue

        # Volatility-band gate: skip low-vol entries where SL is too tight
        # to absorb noise. Uses close (decision-time) to match what a live
        # signal generator sees. Note: the trade-log atr_pct column is
        # computed as atr/entry*100 (entry = next-bar open) so it differs
        # from this gate by ~one bar of price drift -- typically <0.1%,
        # well below the gate's precision.
        if min_atr_pct > 0.0 and (atr / close) * 100.0 < min_atr_pct:
            continue

        # ADX regime gate
        if adx_min > 0:
            adx_val = calc_adx(win5, 14)
            if adx_val < adx_min:
                continue

        # Climax-bar filter (Pine v7 `no_climax`): skip FOMO entries on bars
        # whose range exceeds climax_mult * ATR — SL would sit at a poor level.
        if climax_mult > 0.0:
            bar_range = float(bar[2]) - float(bar[3])
            if bar_range > atr * climax_mult:
                continue

        # Variance-ratio regime gate (Pine v7 `trending_regime`): only trade
        # when recent vol is elevated vs baseline (trend persistence).
        if var_ratio_min > 0.0:
            if calc_var_ratio(win5) < var_ratio_min:
                continue

        # Pre-compute rolling extremes of the prior 5 bars for fake-break test
        prev5 = c5[max(0, i - 5):i]  # bars before decision bar
        prev5_high = max((float(c[2]) for c in prev5), default=float(bar[2]))
        prev5_low = min((float(c[3]) for c in prev5), default=float(bar[3]))
        bar_high, bar_low = float(bar[2]), float(bar[3])
        bar_open, bar_close = float(bar[1]), float(bar[4])

        sides_to_test = ('long',) if (_LONGS_ONLY) else ('long', 'short')
        for side in sides_to_test:
            if strategy == 'v3':
                score, br = score_signal_v3(side, win5, win1h)
            else:
                score, br = score_signal(side, win5, win15, win1h, win4h)
            if score < min_score:
                continue

            # Wick-rejection entry guard (Pine v7): require close in the
            # favourable half of the bar AND body colour confirming direction.
            if wick_reject:
                upper_half = (bar_close - bar_low) > (bar_high - bar_close)
                lower_half = (bar_high - bar_close) > (bar_close - bar_low)
                if side == 'long' and not (upper_half and bar_close > bar_open):
                    continue
                if side == 'short' and not (lower_half and bar_close < bar_open):
                    continue

            # Fake-break filter (Pine v7): skip entries where the bar pierced
            # the prior-5 extreme but closed back against the break direction.
            if fake_break:
                if side == 'long' and bar_high > prev5_high and bar_close < bar_open:
                    continue
                if side == 'short' and bar_low < prev5_low and bar_close > bar_open:
                    continue

            # Entry next bar open
            if i + 1 >= len(c5):
                continue
            entry = float(c5[i+1][1])
            sl_dist = atr * sl_atr_mult
            if side == 'long':
                sl_price = entry - sl_dist
                tp_price = entry + sl_dist * tp_r
            else:
                sl_price = entry + sl_dist
                tp_price = entry - sl_dist * tp_r

            future = c5[i+1:i+61]
            outcome, exit_price, bars = simulate_trade(
                side, entry, sl_price, tp_price, future,
                be_at_r=be_at_r, be_buffer_pct=be_buffer_pct, sl_dist=sl_dist)
            if outcome == 'noop':
                continue

            # P&L in R units (1R = sl_dist)
            if side == 'long':
                r_value = (exit_price - entry) / sl_dist
            else:
                r_value = (entry - exit_price) / sl_dist
            # Fee in R: position_notional = risk_usd / sl_dist_pct, so
            # round_trip_fee_R = 2 * fee_pct/100 / sl_dist_pct
            sl_dist_pct = sl_dist / entry
            fee_r_cost = (2 * fee_pct / 100) / max(sl_dist_pct, 1e-9)
            r_net = r_value - fee_r_cost

            trades.append({
                'ts': ts, 'side': side, 'entry': round(entry, 6),
                'sl': round(sl_price, 6), 'tp': round(tp_price, 6),
                'exit': round(exit_price, 6), 'outcome': outcome,
                'bars_held': bars, 'r_value': round(r_value, 3),
                'r_net': round(r_net, 3), 'score': score, 'atr_pct': round(atr/entry*100, 3),
            })

    return trades


def report(symbol, trades, capital, risk_pct):
    if not trades:
        print(f'  {symbol}: no trades')
        return None
    wins = [t for t in trades if t['r_net'] > 0]
    losses = [t for t in trades if t['r_net'] <= 0]
    win_rate = len(wins) / len(trades) * 100
    avg_win = sum(t['r_net'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['r_net'] for t in losses) / len(losses) if losses else 0
    expectancy = sum(t['r_net'] for t in trades) / len(trades)
    profit_factor = (sum(t['r_net'] for t in wins) / abs(sum(t['r_net'] for t in losses))) if losses and sum(t['r_net'] for t in losses) != 0 else float('inf')

    # Equity curve
    equity = capital
    peak = capital
    max_dd = 0.0
    for t in trades:
        equity += capital * risk_pct * t['r_net']
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)

    print(f'  {symbol}: {len(trades)} trades  win_rate={win_rate:.1f}%  '
          f'expectancy={expectancy:+.3f}R  PF={profit_factor:.2f}  '
          f'avg_win={avg_win:+.2f}R  avg_loss={avg_loss:+.2f}R  '
          f'final_eq=${equity:.2f}  max_DD={max_dd:.1f}%')

    return {
        'symbol': symbol, 'trades': len(trades), 'win_rate': win_rate,
        'expectancy_r': expectancy, 'profit_factor': profit_factor,
        'avg_win_r': avg_win, 'avg_loss_r': avg_loss,
        'final_equity': equity, 'max_dd_pct': max_dd,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--days', type=int, default=30)
    p.add_argument('--symbol', type=str, default='', help='one symbol or "" for all 7')
    p.add_argument('--min_score', type=int, default=92)
    p.add_argument('--sl_atr', type=float, default=2.5)
    p.add_argument('--tp_r', type=float, default=2.5)
    p.add_argument('--fee_pct', type=float, default=0.06, help='per-side fee %% (Bitget taker ~0.06%%)')
    p.add_argument('--leverage', type=int, default=7)
    p.add_argument('--capital', type=float, default=152.60)
    p.add_argument('--risk_pct', type=float, default=0.005)
    p.add_argument('--adx_min', type=float, default=0.0,
                   help='block bars where ADX < this (0 = no filter)')
    p.add_argument('--longs_only', action='store_true',
                   help='only take long entries (backtest shows shorts unprofitable)')
    p.add_argument('--strategy', type=str, default='v2', choices=['v2', 'v3'],
                   help='v2 = score-based (current), v3 = triple confirmation Pine')
    p.add_argument('--block_hours', type=str, default='',
                   help='comma-separated UTC hours 0-23 to skip entries on (e.g. "2,3,4,5,6,7,8")')
    p.add_argument('--min_atr_pct', type=float, default=0.0,
                   help='skip entries where atr/close*100 < this percent (0 = no filter)')
    # ── v7-derived candidate filters (default off = no behaviour change) ──
    p.add_argument('--climax_mult', type=float, default=0.0,
                   help='skip entries whose decision-bar range > this * ATR (0 = off)')
    p.add_argument('--var_ratio_min', type=float, default=0.0,
                   help='require stdev(10)/stdev(40) of log-returns >= this (0 = off; v7 uses 1.10)')
    p.add_argument('--wick_reject', action='store_true',
                   help='require close in favourable half of bar + confirming body colour')
    p.add_argument('--fake_break', action='store_true',
                   help='skip bars that pierced prior-5 extreme but closed against the break')
    p.add_argument('--be_at_r', type=float, default=0.0,
                   help='ratchet stop to break-even once price reaches this R (0 = off)')
    p.add_argument('--be_buffer_pct', type=float, default=0.0,
                   help='offset BE stop by this %% of entry to cover round-trip fees (v7: 0.05)')
    args = p.parse_args()

    # Parse + validate --block_hours; drop out-of-range values with a warning
    block_hours = set()
    if args.block_hours:
        for tok in args.block_hours.split(','):
            tok = tok.strip()
            if not tok:
                continue
            try:
                h = int(tok)
            except ValueError:
                print(f'  [WARN] --block_hours: skipping non-int {tok!r}')
                continue
            if 0 <= h <= 23:
                block_hours.add(h)
            else:
                print(f'  [WARN] --block_hours: dropping out-of-range {h}')

    symbols = [args.symbol] if args.symbol else \
              ['BTCUSDT','ETHUSDT','BNBUSDT','XRPUSDT','TONUSDT','LINKUSDT','AVAXUSDT']

    global _LONGS_ONLY
    _LONGS_ONLY = args.longs_only

    print(f'Backtest config: {args.days}d  MIN_SCORE={args.min_score}  '
          f'SL=ATRx{args.sl_atr}  TP={args.tp_r}R  fee={args.fee_pct}%/side  '
          f'leverage={args.leverage}x')

    all_trades = []
    all_summary = []
    print(f'  strategy={args.strategy}  adx_min={args.adx_min}  longs_only={args.longs_only}  '
          f'block_hours={sorted(block_hours) if block_hours else "(none)"}  '
          f'min_atr_pct={args.min_atr_pct}')
    print(f'  [v7-filters] climax_mult={args.climax_mult}  var_ratio_min={args.var_ratio_min}  '
          f'wick_reject={args.wick_reject}  fake_break={args.fake_break}  '
          f'be_at_r={args.be_at_r}  be_buffer_pct={args.be_buffer_pct}')
    for sym in symbols:
        trades = backtest_symbol(sym, args.days, args.min_score, args.sl_atr, args.tp_r,
                                  args.fee_pct, args.leverage, adx_min=args.adx_min,
                                  strategy=args.strategy,
                                  block_hours=block_hours,
                                  min_atr_pct=args.min_atr_pct,
                                  climax_mult=args.climax_mult,
                                  var_ratio_min=args.var_ratio_min,
                                  wick_reject=args.wick_reject,
                                  fake_break=args.fake_break,
                                  be_at_r=args.be_at_r,
                                  be_buffer_pct=args.be_buffer_pct) or []
        summary = report(sym, trades, args.capital, args.risk_pct)
        if summary:
            all_summary.append(summary)
        all_trades.extend([(sym, t) for t in trades])

    # Aggregate
    if all_summary:
        total_trades = sum(s['trades'] for s in all_summary)
        avg_wr = sum(s['win_rate'] * s['trades'] for s in all_summary) / max(total_trades, 1)
        avg_exp = sum(s['expectancy_r'] * s['trades'] for s in all_summary) / max(total_trades, 1)
        print(f'\n=== OVERALL ===')
        print(f'  total trades: {total_trades}')
        print(f'  weighted win rate: {avg_wr:.1f}%')
        print(f'  weighted expectancy: {avg_exp:+.3f}R')

    # CSV log
    ts_tag = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    out_path = f'backtest_{ts_tag}.csv'
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['symbol','ts','side','entry','sl','tp','exit','outcome','bars_held','r_value','r_net','score','atr_pct'])
        for sym, t in all_trades:
            w.writerow([sym, t['ts'], t['side'], t['entry'], t['sl'], t['tp'], t['exit'],
                        t['outcome'], t['bars_held'], t['r_value'], t['r_net'], t['score'], t['atr_pct']])
    print(f'\nTrade log -> {out_path}')


if __name__ == '__main__':
    main()
