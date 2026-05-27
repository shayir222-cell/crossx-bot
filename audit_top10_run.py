"""One-shot audition runner: 10 symbols × 3 TP_R values on 30d history.

Writes results to audit_top10_20260527.csv. Does NOT touch production code.
"""
import csv
import time
import traceback
from datetime import datetime, timezone

import backtest

# Top-10 by market cap (May 2026), excluding stablecoins, LEO->ADA substitution
# (LEO not listed on Bitget perp).
SYMBOLS = [
    'BTCUSDT',  # 1 BTC
    'ETHUSDT',  # 2 ETH
    'BNBUSDT',  # 3 BNB  (incumbent, baseline reference)
    'XRPUSDT',  # 4 XRP
    'SOLUSDT',  # 5 SOL
    'TRXUSDT',  # 6 TRX
    'DOGEUSDT', # 7 DOGE
    'HYPEUSDT', # 8 HYPE (newer perp — may have less history)
    'ZECUSDT',  # 9 ZEC
    'ADAUSDT',  # 10 (LEO replacement)
]
TP_RS = [1.5, 2.0, 2.5]
DAYS = 30

# Match bot's live regime
ADX_MIN     = 20.0
MIN_SCORE   = 92
SL_ATR      = 2.5
FEE_PCT     = 0.06
LEVERAGE    = 7
CAPITAL     = 99.61
RISK_PCT    = 0.005

backtest._LONGS_ONLY = False  # we WANT both sides — that's the point of this audit


def per_trade_stats(trades):
    """Compute long/short split + side-specific stats."""
    if not trades:
        return {}
    longs  = [t for t in trades if t['side'] == 'long']
    shorts = [t for t in trades if t['side'] == 'short']
    out = {
        'longs_n': len(longs),
        'shorts_n': len(shorts),
        'long_pct': 100.0 * len(longs) / len(trades),
    }
    if longs:
        out['long_exp_r']  = sum(t['r_net'] for t in longs) / len(longs)
        out['long_wr']     = 100.0 * sum(1 for t in longs if t['r_net'] > 0) / len(longs)
    else:
        out['long_exp_r'] = 0.0
        out['long_wr']    = 0.0
    if shorts:
        out['short_exp_r'] = sum(t['r_net'] for t in shorts) / len(shorts)
        out['short_wr']    = 100.0 * sum(1 for t in shorts if t['r_net'] > 0) / len(shorts)
    else:
        out['short_exp_r'] = 0.0
        out['short_wr']    = 0.0
    return out


def run_cell(symbol, tp_r):
    trades = backtest.backtest_symbol(
        symbol, DAYS, MIN_SCORE, SL_ATR, tp_r, FEE_PCT, LEVERAGE,
        adx_min=ADX_MIN, strategy='v2',
    )
    if not trades:
        return None
    summary = backtest.report(symbol, trades, CAPITAL, RISK_PCT) or {}
    side_stats = per_trade_stats(trades)
    summary.update(side_stats)
    summary['tp_r'] = tp_r
    return summary


def main():
    out_path = 'audit_top10_20260527.csv'
    rows = []
    t_start = time.time()
    for i, sym in enumerate(SYMBOLS, 1):
        for tp_r in TP_RS:
            cell_t0 = time.time()
            print(f'\n[{i}/{len(SYMBOLS)}] {sym} TP_R={tp_r}')
            try:
                r = run_cell(sym, tp_r)
                if r is None:
                    print(f'  [SKIP] {sym} TP_R={tp_r} — no trades / no data')
                    rows.append({
                        'symbol': sym, 'tp_r': tp_r, 'trades': 0,
                        'win_rate': 0, 'expectancy_r': 0,
                        'profit_factor': 0, 'avg_win_r': 0, 'avg_loss_r': 0,
                        'final_equity': CAPITAL, 'max_dd_pct': 0,
                        'longs_n': 0, 'shorts_n': 0, 'long_pct': 0,
                        'long_exp_r': 0, 'long_wr': 0,
                        'short_exp_r': 0, 'short_wr': 0,
                        'status': 'no_data',
                    })
                else:
                    r['status'] = 'ok'
                    rows.append(r)
            except Exception as e:
                print(f'  [ERROR] {sym} TP_R={tp_r}: {e}')
                traceback.print_exc()
                rows.append({
                    'symbol': sym, 'tp_r': tp_r, 'trades': 0,
                    'win_rate': 0, 'expectancy_r': 0,
                    'profit_factor': 0, 'avg_win_r': 0, 'avg_loss_r': 0,
                    'final_equity': CAPITAL, 'max_dd_pct': 0,
                    'longs_n': 0, 'shorts_n': 0, 'long_pct': 0,
                    'long_exp_r': 0, 'long_wr': 0,
                    'short_exp_r': 0, 'short_wr': 0,
                    'status': f'error: {e}',
                })
            cell_dt = time.time() - cell_t0
            print(f'  Cell took {cell_dt:.1f}s, total elapsed {(time.time()-t_start)/60:.1f}m')

            # Incremental CSV write so we don't lose data on crash
            keys = ['symbol','tp_r','trades','win_rate','expectancy_r','profit_factor',
                    'avg_win_r','avg_loss_r','final_equity','max_dd_pct',
                    'longs_n','shorts_n','long_pct','long_exp_r','long_wr',
                    'short_exp_r','short_wr','status']
            with open(out_path, 'w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
                w.writeheader()
                w.writerows(rows)

    print(f'\nTotal elapsed: {(time.time()-t_start)/60:.1f} min')
    print(f'CSV -> {out_path}')


if __name__ == '__main__':
    main()
