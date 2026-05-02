"""
CrossX Pro Bot v2.0 — Elite Autonomous Trading System
BTCUSDT | Bitget Futures | 5m Timeframe
"""
import os, json, time, hmac, hashlib, base64, math, threading
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, date, timezone, timedelta

app = FastAPI(title="CrossX Pro Bot v2.0")

# ════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════
API_KEY       = os.environ['BITGET_API_KEY']
API_SECRET    = os.environ['BITGET_API_SECRET']
PASSPHRASE    = os.environ['BITGET_PASSPHRASE']
WEBHOOK_TOKEN = os.environ.get('WEBHOOK_TOKEN', 'change_me')
TG_TOKEN      = os.environ.get('TG_TOKEN', '')
TG_CHAT_ID    = os.environ.get('TG_CHAT_ID', '')
RENDER_URL    = os.environ.get('RENDER_URL', '')  # e.g. https://crossx-bot.onrender.com

BASE_URL = 'https://api.bitget.com'
SYMBOL   = 'BTCUSDT'

# ─── Risk ────────────────────────────────────────────────
BASE_RISK_PCT    = 0.01    # 1% base risk
HIGH_VOL_RISK    = 0.005   # 0.5% on high volatility
STREAK_RISK      = 0.0125  # 1.25% on 3+ win streak + score>90
LEVERAGE         = 10
DAILY_LOSS_LIMIT = 0.10

# ─── ATR-based levels ────────────────────────────────────
SL_ATR_MULT    = 1.5   # SL distance = ATR × 1.5
TP1_R          = 1.5   # First partial at +1.5R
TP2_R          = 3.0   # Second partial at +3R
TRAIL_ATR_MULT = 1.0   # Trailing stop = ATR × 1.0
MAX_GIVEBACK   = 0.35  # Close if floating PnL drops 35% from peak
TP1_SIZE_PCT   = 0.30  # Close 30% at TP1
TP2_SIZE_PCT   = 0.30  # Close 30% at TP2

# ─── Score ───────────────────────────────────────────────
MIN_SCORE = 75

# ─── Sessions (UTC hours) ────────────────────────────────
LONDON_OPEN  = 7
LONDON_CLOSE = 16
NY_OPEN      = 13
NY_CLOSE     = 21

# ─── Streak pauses ───────────────────────────────────────
PAUSE_2L = 30   # minutes
PAUSE_3L = 60   # minutes

# ─── Time stop ───────────────────────────────────────────
TIME_STOP_MIN  = 30    # minutes of no movement
TIME_STOP_MOVE = 0.003 # min 0.3% move required


# ════════════════════════════════════════════════════════════
# STATE
# ════════════════════════════════════════════════════════════
pos = {
    'active': False, 'side': None,
    'entry': 0.0, 'size': 0.0, 'remaining': 0.0,
    'sl': 0.0, 'atr': 0.0, 'r': 0.0,
    'tp1_hit': False, 'tp2_hit': False,
    'peak_pnl': 0.0, 'trail_sl': None,
    'entry_time': None,
    'ref_price': 0.0, 'ref_time': None,
    'score': 0, 'session': '',
}

daily = {
    'date': '', 'start_balance': None, 'halted': False,
    'trades': 0, 'wins': 0, 'losses': 0,
    'win_streak': 0, 'loss_streak': 0,
    'pause_until': None, 'pnl': 0.0,
}

trades_log = []  # in-memory trade history (resets on bot restart)


# ════════════════════════════════════════════════════════════
# TELEGRAM
# ════════════════════════════════════════════════════════════
def tg(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=5
        )
    except Exception as e:
        print(f'[TG] {e}')


# ════════════════════════════════════════════════════════════
# BITGET API
# ════════════════════════════════════════════════════════════
_time_offset = 0  # ms offset between local and Bitget server time

def _sync_time():
    global _time_offset
    try:
        r = requests.get('https://api.bitget.com/api/v2/public/time', timeout=5)
        server_ts = int(r.json()['data']['serverTime'])
        _time_offset = server_ts - int(time.time() * 1000)
    except Exception:
        _time_offset = 0

def _ts():
    return str(int(time.time() * 1000) + _time_offset)

def _sign(ts, method, path, body=''):
    msg = ts + method.upper() + path + body
    return base64.b64encode(hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()

def _h(method, path, body=''):
    ts = _ts()
    return {
        'ACCESS-KEY': API_KEY, 'ACCESS-SIGN': _sign(ts, method, path, body),
        'ACCESS-TIMESTAMP': ts, 'ACCESS-PASSPHRASE': PASSPHRASE,
        'Content-Type': 'application/json', 'locale': 'en-US',
    }

def api_get(path):
    try:
        return requests.get(BASE_URL + path, headers=_h('GET', path), timeout=10).json()
    except Exception as e:
        print(f'[API GET ERROR] {path}: {e}')
        return {}

def api_post(path, body):
    try:
        b = json.dumps(body)
        return requests.post(BASE_URL + path, headers=_h('POST', path, b), data=b, timeout=10).json()
    except Exception as e:
        print(f'[API POST ERROR] {path}: {e}')
        return {}

def get_balance():
    d = api_get('/api/v2/mix/account/accounts?productType=USDT-FUTURES')
    if d.get('code') == '00000' and d.get('data'):
        return float(d['data'][0]['available'])
    return None

def get_price():
    d = api_get(f'/api/v2/mix/market/ticker?symbol={SYMBOL}&productType=USDT-FUTURES')
    if d.get('code') == '00000':
        return float(d['data'][0]['lastPr'])
    return None

def get_candles(gran='5m', limit=60):
    d = api_get(f'/api/v2/mix/market/candles?symbol={SYMBOL}&productType=USDT-FUTURES&granularity={gran}&limit={limit}')
    if d.get('code') == '00000':
        return d['data']  # [[ts,open,high,low,close,vol,...]]
    return None

def set_leverage():
    for side in ('long', 'short'):
        api_post('/api/v2/mix/account/set-leverage', {
            'symbol': SYMBOL, 'productType': 'USDT-FUTURES',
            'marginCoin': 'USDT', 'leverage': str(LEVERAGE), 'holdSide': side,
        })

def place_order(side, size, reduce_only=False):
    body = {
        'symbol': SYMBOL, 'productType': 'USDT-FUTURES',
        'marginMode': 'isolated', 'marginCoin': 'USDT',
        'size': str(round(size, 4)), 'side': side, 'orderType': 'market',
    }
    if reduce_only:
        body['reduceOnly'] = 'YES'
    return api_post('/api/v2/mix/order/place-order', body)

def close_all(hold_side):
    return api_post('/api/v2/mix/order/close-positions', {
        'symbol': SYMBOL, 'productType': 'USDT-FUTURES', 'holdSide': hold_side,
    })


# ════════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS
# ════════════════════════════════════════════════════════════
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

def get_tf_bias(gran):
    """EMA20/50 trend bias: 'bullish'|'bearish'|'neutral'"""
    candles = get_candles(gran, 55)
    if not candles or len(candles) < 25:
        return 'neutral'
    closes = [float(c[4]) for c in candles]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, min(50, len(closes)))
    price = closes[-1]
    if price > ema20 and ema20 > ema50:
        return 'bullish'
    elif price < ema20 and ema20 < ema50:
        return 'bearish'
    return 'neutral'

def get_volume_bias(candles):
    """Compare last 3 candle volumes vs 10-bar average"""
    if not candles or len(candles) < 13:
        return 'neutral'
    vols = [float(c[5]) for c in candles]
    avg_vol = sum(vols[-13:-3]) / 10
    recent_vol = sum(vols[-3:]) / 3
    if recent_vol > avg_vol * 1.3:
        return 'high'
    elif recent_vol < avg_vol * 0.7:
        return 'low'
    return 'normal'

def is_high_volatility(atr, price):
    return (atr / price) > 0.008  # >0.8% ATR on 5m


# ════════════════════════════════════════════════════════════
# SIGNAL SCORER (0–100)
# Using Bitget data for objective scoring
# ════════════════════════════════════════════════════════════
def score_signal(side, candles_5m, alert_data=None):
    """
    Score: 0-100. Combines:
    - CrossX Pro dashboard values (if passed in alert)
    - Or calculates from Bitget data (fallback, always runs)
    Returns (score, breakdown_dict)
    """
    score = 0
    breakdown = {}

    # Helper: parse CrossX Pro value if present in alert
    def crossx(key):
        if alert_data:
            return alert_data.get(key, '').lower()
        return ''

    def points(val, bullish_pts, neutral_pts, side_is_long):
        if side_is_long:
            if val == 'bullish':   return bullish_pts
            if val == 'neutral':   return neutral_pts
            if val == 'bearish':   return 0
        else:
            if val == 'bearish':   return bullish_pts
            if val == 'neutral':   return neutral_pts
            if val == 'bullish':   return 0
        return 0

    is_long = (side == 'long')

    # ── Macro Trend (25 pts) ──────────────────
    mt = crossx('macro_trend') or get_tf_bias('4H')
    p = points(mt, 25, 12, is_long)
    score += p
    breakdown['macro_trend'] = f'{mt} ({p}/25)'

    # ── Momentum (20 pts) ─────────────────────
    mom = crossx('momentum') or get_tf_bias('1H')
    p = points(mom, 20, 10, is_long)
    score += p
    breakdown['momentum'] = f'{mom} ({p}/20)'

    # ── Market Structure (20 pts) ─────────────
    struct = crossx('structure') or get_tf_bias('15m')
    p = points(struct, 20, 10, is_long)
    score += p
    breakdown['structure'] = f'{struct} ({p}/20)'

    # ── Volume & Order Flow (15 pts) ──────────
    vol_raw = crossx('volume')
    if vol_raw:
        vol_val = vol_raw
    else:
        v = get_volume_bias(candles_5m)
        vol_val = 'bullish' if v == 'high' else ('bearish' if v == 'low' else 'neutral')
    p = points(vol_val, 15, 7, is_long)
    score += p
    breakdown['volume'] = f'{vol_val} ({p}/15)'

    # ── Current TF Signal (10 pts) ────────────
    ctf = crossx('current_tf') or get_tf_bias('5m')
    p = points(ctf, 10, 5, is_long)
    score += p
    breakdown['current_tf'] = f'{ctf} ({p}/10)'

    # ── OB/OS (10 pts) ────────────────────────
    ob_os_raw = crossx('ob_os')
    if ob_os_raw:
        ob_val = ob_os_raw
    else:
        rsi = calc_rsi(candles_5m)
        if rsi < 35:      ob_val = 'oversold'
        elif rsi > 65:    ob_val = 'overbought'
        else:              ob_val = 'neutral'

    if is_long:
        ob_pts = 10 if ob_val in ('oversold', 'neutral') else 0
    else:
        ob_pts = 10 if ob_val in ('overbought', 'neutral') else 0
    score += ob_pts
    breakdown['ob_os'] = f'{ob_val} ({ob_pts}/10)'

    return score, breakdown


# ════════════════════════════════════════════════════════════
# MULTI-TIMEFRAME GATE
# ════════════════════════════════════════════════════════════
def check_mtf(side):
    b4h  = get_tf_bias('4H')
    b1h  = get_tf_bias('1H')
    b15m = get_tf_bias('15m')
    info = f'4h:{b4h} 1h:{b1h} 15m:{b15m}'

    if side == 'long':
        if b4h == 'bearish':
            return False, f'BLOCKED: 4h bearish regime ({info})'
        if b1h == 'bearish':
            return False, f'BLOCKED: 1h bearish macro ({info})'
        if b15m == 'bearish':
            return False, f'BLOCKED: 15m bearish momentum ({info})'
    else:
        if b4h == 'bullish':
            return False, f'BLOCKED: 4h bullish regime ({info})'
        if b1h == 'bullish':
            return False, f'BLOCKED: 1h bullish macro ({info})'
        if b15m == 'bullish':
            return False, f'BLOCKED: 15m bullish momentum ({info})'

    return True, info


# ════════════════════════════════════════════════════════════
# SESSION FILTER
# ════════════════════════════════════════════════════════════
def get_session():
    h = datetime.now(timezone.utc).hour
    in_ld = LONDON_OPEN <= h < LONDON_CLOSE
    in_ny = NY_OPEN <= h < NY_CLOSE

    if in_ld and in_ny:
        return 'London/NY Overlap', 1.0
    elif in_ld:
        return 'London', 1.0
    elif in_ny:
        return 'New York', 1.0
    elif 0 <= h < 7:
        return 'Asian (low)', 0.6   # reduce size in Asian session
    else:
        return 'Off-session', 0.75


# ════════════════════════════════════════════════════════════
# NEWS FILTER
# ════════════════════════════════════════════════════════════
HIGH_IMPACT = ['cpi', 'fomc', 'nfp', 'non-farm', 'federal', 'interest rate', 'inflation', 'gdp', 'ppi']

def check_news():
    try:
        r = requests.get('https://nfs.faireconomy.media/ff_calendar_thisweek.json', timeout=5)
        now = datetime.now(timezone.utc)
        for ev in r.json():
            if ev.get('impact', '').lower() != 'high':
                continue
            if ev.get('country', '') not in ('USD', 'US'):
                continue
            title = ev.get('title', '').lower()
            if not any(kw in title for kw in HIGH_IMPACT):
                continue
            try:
                et = datetime.fromisoformat(ev['date'].replace('Z', '+00:00'))
                diff = (et - now).total_seconds() / 60
                if -15 <= diff <= 60:
                    return False, f'News block: {ev["title"]} @ {et.strftime("%H:%M")} UTC'
            except Exception:
                pass
        return True, 'No major news'
    except Exception:
        return True, 'News API error (allowed)'


# ════════════════════════════════════════════════════════════
# DAILY STATS & RISK ENGINE
# ════════════════════════════════════════════════════════════
def reset_daily():
    today = str(date.today())
    if daily['date'] != today:
        daily.update(date=today, start_balance=None, halted=False,
                     trades=0, wins=0, losses=0, win_streak=0,
                     loss_streak=0, pause_until=None, pnl=0.0)
        print(f'[DAY] New day: {today}')

def check_daily_loss(balance):
    if daily['start_balance'] is None:
        daily['start_balance'] = balance
        return False
    lost = (daily['start_balance'] - balance) / daily['start_balance']
    if lost >= DAILY_LOSS_LIMIT:
        daily['halted'] = True
        tg(f'🛑 <b>DAILY STOP</b>\nLoss: <b>-{lost*100:.1f}%</b>\nBalance: ${balance:.2f}\nStopped until tomorrow.')
        return True
    return False

def is_paused():
    if not daily['pause_until']:
        return False, ''
    now = datetime.now(timezone.utc)
    if now < daily['pause_until']:
        mins = int((daily['pause_until'] - now).total_seconds() / 60)
        return True, f'Pause {mins}m remaining (loss streak)'
    daily['pause_until'] = None
    return False, ''

def get_risk_pct(score, atr, price):
    if is_high_volatility(atr, price):
        return HIGH_VOL_RISK
    if daily['win_streak'] >= 3 and score > 90:
        return STREAK_RISK
    return BASE_RISK_PCT

def log_trade(exit_price: float, exit_reason: str, won: bool, pnl_pct: float):
    trade = {
        'id': len(trades_log) + 1,
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'time': datetime.now(timezone.utc).strftime('%H:%M'),
        'side': pos['side'],
        'entry': pos['entry'],
        'exit': round(exit_price, 2),
        'size': pos['size'],
        'score': pos.get('score', 0),
        'session': pos.get('session', ''),
        'atr': round(pos['atr'], 2),
        'sl': pos['sl'],
        'tp1_hit': pos['tp1_hit'],
        'tp2_hit': pos['tp2_hit'],
        'peak_pnl': round(pos['peak_pnl'], 3),
        'pnl_pct': round(pnl_pct, 3),
        'won': won,
        'exit_reason': exit_reason,
        'duration_min': int((datetime.now(timezone.utc) - pos['entry_time']).total_seconds() / 60) if pos['entry_time'] else 0,
    }
    trades_log.append(trade)
    if len(trades_log) > 1000:
        trades_log.pop(0)
    print(f'[TRADE #{trade["id"]}] {trade["side"].upper()} | {exit_reason} | PnL:{pnl_pct:+.3f}% | Score:{trade["score"]}')


def record_result(won, pnl_pct):
    daily['trades'] += 1
    daily['pnl'] += pnl_pct
    if won:
        daily['wins'] += 1
        daily['win_streak'] += 1
        daily['loss_streak'] = 0
    else:
        daily['losses'] += 1
        daily['loss_streak'] += 1
        daily['win_streak'] = 0
        if daily['loss_streak'] == 2:
            daily['pause_until'] = datetime.now(timezone.utc) + timedelta(minutes=PAUSE_2L)
            tg(f'⏸ <b>2 losses in a row</b> — pausing {PAUSE_2L}min')
        elif daily['loss_streak'] >= 3:
            daily['pause_until'] = datetime.now(timezone.utc) + timedelta(minutes=PAUSE_3L)
            tg(f'⏸ <b>3+ losses in a row</b> — pausing {PAUSE_3L}min')


# ════════════════════════════════════════════════════════════
# POSITION RESET
# ════════════════════════════════════════════════════════════
def reset_pos():
    pos.update(active=False, side=None, entry=0.0, size=0.0, remaining=0.0,
               sl=0.0, atr=0.0, r=0.0, tp1_hit=False, tp2_hit=False,
               peak_pnl=0.0, trail_sl=None, entry_time=None,
               ref_price=0.0, ref_time=None, score=0, session='')


# ════════════════════════════════════════════════════════════
# POSITION MONITOR — background thread (every 15s)
# ════════════════════════════════════════════════════════════
def monitor():
    print('[MONITOR] Started')
    while True:
        time.sleep(15)
        if not pos['active']:
            continue
        try:
            price = get_price()
            if not price:
                continue

            side  = pos['side']
            entry = pos['entry']
            atr   = pos['atr']
            r     = pos['r']
            now   = datetime.now(timezone.utc)

            # Floating metrics
            if side == 'long':
                pnl_pct = (price - entry) / entry * 100
                float_r = (price - entry) / r if r else 0
            else:
                pnl_pct = (entry - price) / entry * 100
                float_r = (entry - price) / r if r else 0

            if pnl_pct > pos['peak_pnl']:
                pos['peak_pnl'] = pnl_pct

            # ── Hard SL ─────────────────────────────────
            sl_hit = (side == 'long' and price <= pos['sl']) or \
                     (side == 'short' and price >= pos['sl'])
            if sl_hit:
                close_all(side)
                tg(f'🔴 <b>STOP LOSS</b>\n${price:,.2f} | SL was ${pos["sl"]:,.2f}\nPnL: {pnl_pct:+.2f}%')
                log_trade(price, 'SL', False, pnl_pct)
                record_result(False, pnl_pct)
                reset_pos()
                continue

            # ── Max Giveback ─────────────────────────────
            if pos['peak_pnl'] > 0.5:
                giveback = (pos['peak_pnl'] - pnl_pct) / pos['peak_pnl']
                if giveback >= MAX_GIVEBACK:
                    close_all(side)
                    tg(f'💰 <b>MAX GIVEBACK</b>\nPeak: {pos["peak_pnl"]:.2f}% → Now: {pnl_pct:.2f}%\nClosed at ${price:,.2f}')
                    log_trade(price, 'Max Giveback', pnl_pct > 0, pnl_pct)
                    record_result(pnl_pct > 0, pnl_pct)
                    reset_pos()
                    continue

            # ── Time Stop ────────────────────────────────
            if pos['ref_time'] is None:
                pos['ref_time'] = now
                pos['ref_price'] = price
            elif (now - pos['ref_time']).seconds >= TIME_STOP_MIN * 60:
                move = abs(price - pos['ref_price']) / pos['ref_price']
                if move < TIME_STOP_MOVE:
                    close_all(side)
                    tg(f'⏱ <b>TIME STOP</b>\n{TIME_STOP_MIN}min, move {move*100:.2f}%\nClosed at ${price:,.2f}')
                    log_trade(price, 'Time Stop', pnl_pct > 0, pnl_pct)
                    record_result(pnl_pct > 0, pnl_pct)
                    reset_pos()
                    continue
                else:
                    pos['ref_time'] = now
                    pos['ref_price'] = price

            # ── TP1: +1.5R → close 30%, SL to BE ────────
            if not pos['tp1_hit'] and float_r >= TP1_R:
                tp1_size = round(pos['size'] * TP1_SIZE_PCT, 4)
                if tp1_size >= 0.001:
                    close_side = 'sell' if side == 'long' else 'buy'
                    place_order(close_side, tp1_size, reduce_only=True)
                    pos['remaining'] -= tp1_size
                    pos['tp1_hit'] = True
                    pos['sl'] = entry  # move SL to breakeven
                    tg(f'✂️ <b>TP1 +{TP1_R}R</b> → closed 30% at ${price:,.2f}\nSL → breakeven ${entry:,.2f}')

            # ── TP2: +3R → close 30% ─────────────────────
            if pos['tp1_hit'] and not pos['tp2_hit'] and float_r >= TP2_R:
                tp2_size = round(pos['size'] * TP2_SIZE_PCT, 4)
                if tp2_size >= 0.001:
                    close_side = 'sell' if side == 'long' else 'buy'
                    place_order(close_side, tp2_size, reduce_only=True)
                    pos['remaining'] -= tp2_size
                    pos['tp2_hit'] = True
                    tg(f'✂️ <b>TP2 +{TP2_R}R</b> → closed 30% at ${price:,.2f}\n40% trailing...')

            # ── Trailing Stop (after TP1) ─────────────────
            if pos['tp1_hit']:
                trail_dist = atr * TRAIL_ATR_MULT
                if side == 'long':
                    new_trail = price - trail_dist
                    if pos['trail_sl'] is None or new_trail > pos['trail_sl']:
                        pos['trail_sl'] = new_trail
                    if price <= pos['trail_sl']:
                        close_all('long')
                        tg(f'📉 <b>TRAILING STOP</b>\nClosed at ${price:,.2f} | Trail was ${pos["trail_sl"]:,.2f}\nPnL: {pnl_pct:+.2f}%')
                        log_trade(price, 'Trailing Stop', pnl_pct > 0, pnl_pct)
                        record_result(pnl_pct > 0, pnl_pct)
                        reset_pos()
                        continue
                else:
                    new_trail = price + trail_dist
                    if pos['trail_sl'] is None or new_trail < pos['trail_sl']:
                        pos['trail_sl'] = new_trail
                    if price >= pos['trail_sl']:
                        close_all('short')
                        tg(f'📉 <b>TRAILING STOP</b>\nClosed at ${price:,.2f}\nPnL: {pnl_pct:+.2f}%')
                        log_trade(price, 'Trailing Stop', pnl_pct > 0, pnl_pct)
                        record_result(pnl_pct > 0, pnl_pct)
                        reset_pos()

        except Exception as e:
            print(f'[MONITOR ERROR] {e}')


# ════════════════════════════════════════════════════════════
# WEEKLY TELEGRAM REPORT
# ════════════════════════════════════════════════════════════
_last_weekly_report = {'week': ''}

def send_weekly_report():
    week_key = datetime.now(timezone.utc).strftime('%Y-W%U')
    if _last_weekly_report['week'] == week_key:
        return
    _last_weekly_report['week'] = week_key

    if not trades_log:
        tg('📊 <b>Недельный отчёт</b>\nСделок на этой неделе не было.')
        return

    total = len(trades_log)
    wins  = sum(1 for t in trades_log if t['won'])
    wr    = wins / total * 100
    pnl   = sum(t['pnl_pct'] for t in trades_log)

    by_reason = {}
    for t in trades_log:
        r = t['exit_reason']
        by_reason[r] = by_reason.get(r, 0) + 1

    reasons_str = ' | '.join(f'{r}: {c}' for r, c in sorted(by_reason.items(), key=lambda x: -x[1]))
    balance = get_balance()
    bal_str = f'${balance:.2f}' if balance else '—'

    tg(
        f'📊 <b>Недельный отчёт CrossX Pro</b>\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'Баланс: <b>{bal_str}</b>\n'
        f'Сделок: <b>{total}</b> (побед: {wins}, потерь: {total-wins})\n'
        f'Win Rate: <b>{wr:.1f}%</b>\n'
        f'PnL итого: <b>{pnl:+.3f}%</b>\n'
        f'Avg PnL: {pnl/total:+.3f}%\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'Выходы: {reasons_str}\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'Obsidian обновится в 04:00'
    )


# ════════════════════════════════════════════════════════════
# KEEP-ALIVE (prevents Render free tier spindown)
# ════════════════════════════════════════════════════════════
def keep_alive():
    while True:
        time.sleep(540)  # ping every 9 min
        if RENDER_URL:
            try:
                requests.get(f'{RENDER_URL}/ping', timeout=5)
            except Exception:
                pass
        # Weekly report — every Sunday at 20:00 UTC
        now = datetime.now(timezone.utc)
        if now.weekday() == 6 and now.hour == 20:
            try:
                send_weekly_report()
            except Exception as e:
                print(f'[WEEKLY] {e}')

@app.get('/ping')
async def ping():
    return {'status': 'ok'}


# ════════════════════════════════════════════════════════════
# WEBHOOK
# ════════════════════════════════════════════════════════════
@app.post('/webhook')
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({'error': 'invalid JSON'}, status_code=400)

    if data.get('token') != WEBHOOK_TOKEN:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    reset_daily()

    action = data.get('action', '').lower()

    # Manual close
    if action in ('close_long', 'close_short'):
        if pos['active']:
            side = 'long' if action == 'close_long' else 'short'
            close_all(side)
            price = get_price() or pos['entry']
            pnl_pct = 0.0
            if price and pos['entry']:
                pnl_pct = ((price - pos['entry']) / pos['entry'] * 100) if side == 'long' \
                          else ((pos['entry'] - price) / pos['entry'] * 100)
            tg(f'⚪ <b>Manual Close {side.upper()}</b> at ${price:,.2f}\nPnL: {pnl_pct:+.2f}%')
            log_trade(price, 'Manual Close', pnl_pct > 0, pnl_pct)
            record_result(pnl_pct > 0, pnl_pct)
            reset_pos()
        return JSONResponse({'status': 'closed'})

    if action not in ('buy', 'sell'):
        return JSONResponse({'error': f'unknown action: {action}'})

    # Guards
    if daily['halted']:
        return JSONResponse({'status': 'halted', 'reason': 'Daily loss limit'})

    paused, pause_reason = is_paused()
    if paused:
        return JSONResponse({'status': 'paused', 'reason': pause_reason})

    if pos['active']:
        return JSONResponse({'status': 'skipped', 'reason': 'Position already open'})

    side = 'long' if action == 'buy' else 'short'

    # Balance
    balance = get_balance()
    if not balance:
        return JSONResponse({'error': 'balance error'}, status_code=500)

    if check_daily_loss(balance):
        return JSONResponse({'status': 'halted'})

    # Get 5m candles (needed for ATR and scoring)
    candles_5m = get_candles('5m', 60)
    if not candles_5m:
        return JSONResponse({'error': 'candle error'}, status_code=500)

    price = float(candles_5m[-1][4])
    atr   = calc_atr(candles_5m)
    if atr == 0:
        atr = price * 0.005

    # Score
    score, breakdown = score_signal(side, candles_5m, data)
    if score < MIN_SCORE:
        print(f'[FILTER] Score {score}/100 < {MIN_SCORE}')
        return JSONResponse({'status': 'filtered', 'score': score, 'reason': f'Score {score}/100 < {MIN_SCORE}', 'breakdown': breakdown})

    # MTF gate
    mtf_ok, mtf_info = check_mtf(side)
    if not mtf_ok:
        return JSONResponse({'status': 'filtered', 'reason': mtf_info})

    # News filter
    news_ok, news_reason = check_news()
    if not news_ok:
        tg(f'📰 <b>News Filter Active</b>\n{news_reason}')
        return JSONResponse({'status': 'filtered', 'reason': news_reason})

    # Session
    session_name, risk_mult = get_session()
    high_vol = is_high_volatility(atr, price)

    # Risk + sizing
    risk_pct = get_risk_pct(score, atr, price) * risk_mult
    risk_usd  = balance * risk_pct
    sl_dist   = atr * SL_ATR_MULT
    size      = round(risk_usd / sl_dist, 4)
    if size < 0.001:
        size = 0.001

    # Levels
    if side == 'long':
        sl_price  = round(price - sl_dist, 2)
        tp1_price = round(price + sl_dist * TP1_R, 2)
        tp2_price = round(price + sl_dist * TP2_R, 2)
    else:
        sl_price  = round(price + sl_dist, 2)
        tp1_price = round(price - sl_dist * TP1_R, 2)
        tp2_price = round(price - sl_dist * TP2_R, 2)

    # Place order
    set_leverage()
    result = place_order('buy' if side == 'long' else 'sell', size)

    if result.get('code') != '00000':
        tg(f'❌ Order failed: {result}')
        return JSONResponse({'error': result}, status_code=500)

    # Update state
    now = datetime.now(timezone.utc)
    pos.update(
        active=True, side=side, entry=price, size=size, remaining=size,
        sl=sl_price, atr=atr, r=sl_dist,
        tp1_hit=False, tp2_hit=False, peak_pnl=0.0, trail_sl=None,
        entry_time=now, ref_price=price, ref_time=now,
        score=score, session=session_name,
    )

    emoji = '🟢' if side == 'long' else '🔴'
    breakdown_str = '\n'.join([f'  {k}: {v}' for k, v in breakdown.items()])
    msg = (
        f'{emoji} <b>{"LONG" if side == "long" else "SHORT"} OPENED</b>\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'Score: <b>{score}/100</b>\n'
        f'{breakdown_str}\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'Entry: <b>${price:,.2f}</b>\n'
        f'Size:  {size} BTC (${size*price:,.0f})\n'
        f'ATR:   ${atr:,.2f} {"⚠️ HIGH VOL" if high_vol else ""}\n'
        f'SL:    ${sl_price:,.2f} (ATR×{SL_ATR_MULT})\n'
        f'TP1:   ${tp1_price:,.2f} (+{TP1_R}R → close 30%)\n'
        f'TP2:   ${tp2_price:,.2f} (+{TP2_R}R → close 30%)\n'
        f'Trail: ATR×{TRAIL_ATR_MULT} on remaining 40%\n'
        f'Risk:  ${risk_usd:.2f} ({risk_pct*100:.1f}%)\n'
        f'Session: {session_name}\n'
        f'MTF: {mtf_info}'
    )
    tg(msg)
    print(f'[ORDER] {side.upper()} | score={score} | price={price} | sl={sl_price}')

    return JSONResponse({
        'status': f'{side} opened',
        'score': score, 'price': price,
        'sl': sl_price, 'tp1': tp1_price, 'tp2': tp2_price,
        'atr': round(atr, 2), 'risk_usd': round(risk_usd, 2),
        'session': session_name,
    })


# ════════════════════════════════════════════════════════════
# TRADES & REPORT
# ════════════════════════════════════════════════════════════
@app.get('/trades')
async def get_trades():
    return {'count': len(trades_log), 'trades': trades_log}


@app.get('/report')
async def get_report():
    if not trades_log:
        return {'total_trades': 0, 'message': 'No trades yet (resets on restart). Use /status for today stats.'}

    total = len(trades_log)
    wins  = sum(1 for t in trades_log if t['won'])
    total_pnl = sum(t['pnl_pct'] for t in trades_log)

    by_score = {}
    for t in trades_log:
        s = t['score']
        b = '95+' if s >= 95 else ('90-94' if s >= 90 else ('85-89' if s >= 85 else ('80-84' if s >= 80 else '75-79')))
        if b not in by_score:
            by_score[b] = {'trades': 0, 'wins': 0}
        by_score[b]['trades'] += 1
        if t['won']:
            by_score[b]['wins'] += 1

    by_session = {}
    for t in trades_log:
        k = t.get('session') or 'Unknown'
        if k not in by_session:
            by_session[k] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
        by_session[k]['trades'] += 1
        if t['won']:
            by_session[k]['wins'] += 1
        by_session[k]['pnl'] = round(by_session[k]['pnl'] + t['pnl_pct'], 3)

    by_reason = {}
    for t in trades_log:
        r = t['exit_reason']
        if r not in by_reason:
            by_reason[r] = {'trades': 0, 'wins': 0}
        by_reason[r]['trades'] += 1
        if t['won']:
            by_reason[r]['wins'] += 1

    return {
        'total_trades': total,
        'wins': wins, 'losses': total - wins,
        'win_rate': round(wins / total * 100, 1),
        'total_pnl_pct': round(total_pnl, 3),
        'avg_pnl_pct': round(total_pnl / total, 3),
        'by_score': by_score,
        'by_session': by_session,
        'by_exit_reason': by_reason,
        'recent_5': trades_log[-5:],
    }


# ════════════════════════════════════════════════════════════
# STATUS
# ════════════════════════════════════════════════════════════
@app.get('/status')
async def status():
    reset_daily()
    balance = get_balance()
    price   = get_price()
    loss_pct = 0.0
    if daily['start_balance'] and balance:
        loss_pct = (daily['start_balance'] - balance) / daily['start_balance'] * 100

    paused, pause_reason = is_paused()
    session_name, _ = get_session()

    pos_info = None
    if pos['active'] and price and pos['r']:
        float_r = ((price - pos['entry']) / pos['r'] if pos['side'] == 'long'
                   else (pos['entry'] - price) / pos['r'])
        pos_info = {
            'side': pos['side'], 'entry': pos['entry'], 'current_price': price,
            'float_r': round(float_r, 2), 'sl': pos['sl'],
            'trail_sl': pos['trail_sl'], 'tp1_hit': pos['tp1_hit'],
            'tp2_hit': pos['tp2_hit'], 'peak_pnl': f'{pos["peak_pnl"]:.2f}%',
            'remaining_size': pos['remaining'],
        }

    return {
        'bot': 'CrossX Pro Bot v2.0',
        'balance': f'${balance:.2f}' if balance else 'error',
        'daily_loss': f'{loss_pct:.2f}%',
        'daily_pnl': f'${daily["pnl"]:.2f}',
        'halted': daily['halted'],
        'paused': paused, 'pause_reason': pause_reason,
        'session': session_name,
        'trades': daily['trades'], 'wins': daily['wins'], 'losses': daily['losses'],
        'win_streak': daily['win_streak'], 'loss_streak': daily['loss_streak'],
        'position': pos_info,
        'settings': {
            'timeframe': '5m', 'symbol': SYMBOL,
            'base_risk': '1%', 'high_vol_risk': '0.5%', 'streak_risk': '1.25%',
            'leverage': f'{LEVERAGE}x', 'min_score': MIN_SCORE,
            'sl': f'ATR×{SL_ATR_MULT}', 'tp1': f'+{TP1_R}R (30%)', 'tp2': f'+{TP2_R}R (30%)',
            'trail': f'ATR×{TRAIL_ATR_MULT}', 'daily_stop': f'-{DAILY_LOSS_LIMIT*100}%',
        }
    }

@app.get('/')
async def home():
    return {'status': 'running', 'bot': 'CrossX Pro Bot v2.0', 'symbol': SYMBOL}


# ════════════════════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════════════════════
@app.on_event('startup')
async def startup():
    _sync_time()
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    print('[BOT] CrossX Pro Bot v2.0 started — BTCUSDT 5m')
    tg(
        '🚀 <b>CrossX Pro Bot v2.0 Online</b>\n'
        '━━━━━━━━━━━━━━━━━━━\n'
        f'Symbol:  BTCUSDT Perpetual\n'
        f'TF:      5m entry | 15m/1h/4h confirm\n'
        f'Risk:    1% base | 0.5% high-vol\n'
        f'SL:      ATR×1.5 (dynamic)\n'
        f'TP1:     +1.5R → 30%\n'
        f'TP2:     +3R → 30%\n'
        f'Trail:   ATR×1.0 on remaining\n'
        f'Filter:  Score ≥75/100\n'
        f'Daily stop: -10%'
    )


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 8080))
    uvicorn.run(app, host='0.0.0.0', port=port)
