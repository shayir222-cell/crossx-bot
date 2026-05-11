"""Live statistics analyzer for CrossX Bot.

Reads analytics_trades + analytics_signals from the SQLite DB and produces
a human-readable performance breakdown:
  - Per-symbol win rate, expectancy, profit factor
  - Per-session (Asian / London / Overlap / NY / Off-session)
  - Per-side (long / short)
  - Per-exit-reason (TP1, TP2, SL, BE, trail, manual close)
  - Overall portfolio expectancy + equity curve estimate

Usage (CLI):
    python analyze.py                              # default: /var/data/crossx.db
    python analyze.py --db ./crossx.db
    python analyze.py --since 7d                   # last 7 days
    python analyze.py --since 24h --format json

Also importable as a module:
    from analyze import build_report
    md_text = build_report(db_path, since='30d')

Output format: markdown by default, JSON optional.
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def _parse_since(spec: str) -> str | None:
    """'24h' / '7d' / '30d' -> ISO timestamp; '' or None -> None (all-time)."""
    if not spec:
        return None
    s = spec.strip().lower()
    try:
        if s.endswith('h'):
            hours = int(s[:-1])
            dt = datetime.now(timezone.utc) - timedelta(hours=hours)
        elif s.endswith('d'):
            days = int(s[:-1])
            dt = datetime.now(timezone.utc) - timedelta(days=days)
        elif s.endswith('w'):
            weeks = int(s[:-1])
            dt = datetime.now(timezone.utc) - timedelta(weeks=weeks)
        else:
            return None
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def _safe_div(num, den, default=0.0):
    return num / den if den else default


def _fetch_trades(conn, since_ts: str | None):
    sql = ('SELECT symbol, side, session, score, pnl_pct, won, exit_reason, '
           '       duration_min, ts_close_utc, sl, entry, exit, peak_pnl_pct '
           '  FROM analytics_trades '
           ' WHERE ts_close_utc IS NOT NULL')
    params = []
    if since_ts:
        sql += ' AND ts_close_utc >= ?'
        params.append(since_ts)
    sql += ' ORDER BY ts_close_utc'
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_signals(conn, since_ts: str | None):
    sql = ('SELECT symbol, side, action, status, reason, session, score '
           '  FROM analytics_signals')
    params = []
    if since_ts:
        sql += ' WHERE ts_utc >= ?'
        params.append(since_ts)
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _stats_from_trades(trades):
    """Compute aggregate stats for a list of trade dicts.
    Returns dict with: trades, wins, losses, wr, exp_pct, avg_win, avg_loss, pf, total_pnl_pct."""
    if not trades:
        return {'trades': 0}
    pnls = [float(t.get('pnl_pct') or 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_loss = abs(sum(losses))
    # PF: ∞ when no losses (cap at 99.99 for display); 0 when no wins.
    if total_loss == 0:
        pf_val = 99.99 if wins else 0.0
    else:
        pf_val = round(sum(wins) / total_loss, 2)
    return {
        'trades':       len(trades),
        'wins':         len(wins),
        'losses':       len(losses),
        'wr':           round(_safe_div(len(wins), len(trades)) * 100, 1),
        'exp_pct':      round(_safe_div(sum(pnls), len(trades)), 3),
        'avg_win':      round(_safe_div(sum(wins), len(wins)), 3),
        'avg_loss':     round(_safe_div(sum(losses), len(losses)), 3),
        'pf':           pf_val,
        'total_pnl_pct': round(sum(pnls), 2),
    }


def _group_by(items, key):
    out = defaultdict(list)
    for item in items:
        out[item.get(key) or '(none)'].append(item)
    return dict(out)


def build_report(db_path: str, since: str = '', fmt: str = 'markdown') -> str:
    """Build a performance report. fmt='markdown' or 'json'."""
    if not os.path.exists(db_path):
        return f'DB not found: {db_path}'
    since_ts = _parse_since(since)
    try:
        conn = sqlite3.connect(db_path)
        trades = _fetch_trades(conn, since_ts)
        signals = _fetch_signals(conn, since_ts)
        conn.close()
    except Exception as e:
        return f'DB read error: {e}'

    period_label = f'since {since}' if since else 'all-time'

    # Overall
    overall = _stats_from_trades(trades)

    # Per-symbol
    by_symbol = {sym: _stats_from_trades(ts) for sym, ts in _group_by(trades, 'symbol').items()}

    # Per-side
    by_side = {side: _stats_from_trades(ts) for side, ts in _group_by(trades, 'side').items()}

    # Per-session
    by_session = {ses: _stats_from_trades(ts) for ses, ts in _group_by(trades, 'session').items()}

    # Per-exit-reason
    by_exit = {reason: _stats_from_trades(ts) for reason, ts in _group_by(trades, 'exit_reason').items()}

    # Signal funnel
    sig_total = len(signals)
    sig_taken = sum(1 for s in signals if (s.get('status') or '').lower() == 'taken')
    sig_filtered = sig_total - sig_taken
    by_status = defaultdict(int)
    for s in signals:
        by_status[(s.get('status') or '(none)').lower()] += 1
    by_reason = defaultdict(int)
    for s in signals:
        if (s.get('status') or '').lower() == 'filtered':
            r = (s.get('reason') or '(unknown)')
            # Bucket score-rejections together
            if r.startswith('Score'):
                by_reason['Score < threshold'] += 1
            else:
                by_reason[r[:60]] += 1

    if fmt == 'json':
        return json.dumps({
            'period': period_label,
            'overall': overall,
            'by_symbol': by_symbol,
            'by_side': by_side,
            'by_session': by_session,
            'by_exit_reason': by_exit,
            'signal_funnel': {
                'total': sig_total, 'taken': sig_taken, 'filtered': sig_filtered,
                'by_status': dict(by_status),
                'top_filter_reasons': dict(sorted(by_reason.items(), key=lambda x: -x[1])[:10]),
            },
        }, indent=2)

    # Markdown rendering
    out = []
    out.append(f'# CrossX Performance Report — {period_label}\n')
    out.append(f'_Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")} UTC_\n')

    out.append('\n## Overall\n')
    if overall.get('trades', 0) == 0:
        out.append('  No closed trades in this period.\n')
    else:
        out.append(f'  Trades: **{overall["trades"]}** | Win rate: **{overall["wr"]}%** | '
                   f'Expectancy: **{overall["exp_pct"]:+.3f}%/trade**\n')
        out.append(f'  Avg win: {overall["avg_win"]:+.2f}%  Avg loss: {overall["avg_loss"]:+.2f}%  '
                   f'PF: {overall["pf"]}  Total PnL: **{overall["total_pnl_pct"]:+.2f}%**\n')

    out.append('\n## Per-symbol\n')
    out.append('| Symbol | Trades | WR% | Exp% | AvgWin | AvgLoss | PF | TotalPnL% |')
    out.append('|--------|--------|-----|------|--------|---------|-----|----------|')
    for sym in sorted(by_symbol.keys()):
        s = by_symbol[sym]
        if s.get('trades', 0) == 0:
            continue
        out.append(f'| {sym} | {s["trades"]} | {s["wr"]} | {s["exp_pct"]:+.3f} | '
                   f'{s["avg_win"]:+.2f} | {s["avg_loss"]:+.2f} | {s["pf"]} | {s["total_pnl_pct"]:+.2f} |')

    out.append('\n## Per-side\n')
    out.append('| Side | Trades | WR% | Exp% | PF |')
    out.append('|------|--------|-----|------|-----|')
    for side in ('long', 'short'):
        s = by_side.get(side, {})
        if s.get('trades', 0) == 0:
            continue
        out.append(f'| {side} | {s["trades"]} | {s["wr"]} | {s["exp_pct"]:+.3f} | {s["pf"]} |')

    out.append('\n## Per-session\n')
    out.append('| Session | Trades | WR% | Exp% | PF |')
    out.append('|---------|--------|-----|------|-----|')
    for ses in ('Asian', 'London', 'London/NY Overlap', 'New York', 'Off-session'):
        s = by_session.get(ses, {})
        if s.get('trades', 0) == 0:
            continue
        out.append(f'| {ses} | {s["trades"]} | {s["wr"]} | {s["exp_pct"]:+.3f} | {s["pf"]} |')

    out.append('\n## Per-exit-reason\n')
    out.append('| Reason | Trades | WR% | Avg PnL% |')
    out.append('|--------|--------|-----|----------|')
    for reason in sorted(by_exit.keys(), key=lambda r: -by_exit[r].get('trades', 0)):
        s = by_exit[reason]
        if s.get('trades', 0) == 0:
            continue
        out.append(f'| {reason} | {s["trades"]} | {s["wr"]} | {s["exp_pct"]:+.3f} |')

    out.append('\n## Signal funnel\n')
    out.append(f'  Total signals: **{sig_total}** | Taken: **{sig_taken}** | '
               f'Filtered: **{sig_filtered}** | Take rate: **{_safe_div(sig_taken, sig_total)*100:.1f}%**')
    if by_status:
        out.append('\n  By status:')
        for st, cnt in sorted(by_status.items(), key=lambda x: -x[1]):
            out.append(f'    - {st}: {cnt}')
    if by_reason:
        out.append('\n  Top filter reasons:')
        for r, cnt in sorted(by_reason.items(), key=lambda x: -x[1])[:10]:
            out.append(f'    - {r}: {cnt}')

    return '\n'.join(out)


def build_telegram_perf_report(db_path: str,
                                 windows=('24h', '3d', '7d', '30d')) -> str:
    """Compact multi-window performance summary for Telegram (HTML).
    Stays under 4096 chars. Default windows: 24h, 3d, 7d, 30d."""
    if not os.path.exists(db_path):
        return f'⚠️ DB not found: <code>{db_path}</code>'

    out = []
    out.append('📊 <b>CrossX Performance Report</b>')
    out.append('━━━━━━━━━━━━━━━━━━━')

    labels = {'24h': '24 hours', '3d': '3 days', '7d': '7 days',
              '30d': '30 days', '2w': '2 weeks'}

    longest_window_data = None
    for w in windows:
        try:
            conn = sqlite3.connect(db_path)
            since_ts = _parse_since(w)
            trades = _fetch_trades(conn, since_ts)
            signals = _fetch_signals(conn, since_ts)
            conn.close()
        except Exception as e:
            out.append(f'\n⏰ <b>{labels.get(w, w)}</b>\n  ⚠️ read error: {e}')
            continue

        s = _stats_from_trades(trades)
        out.append(f'\n⏰ <b>{labels.get(w, w)}</b>')
        if s.get('trades', 0) == 0:
            out.append('  <i>no closed trades</i>')
            continue

        out.append(
            f'  Trades: <b>{s["trades"]}</b> | '
            f'WR: <b>{s["wr"]:.1f}%</b> | '
            f'Exp: <b>{s["exp_pct"]:+.2f}%</b>'
        )
        out.append(
            f'  PnL: <b>{s["total_pnl_pct"]:+.2f}%</b> | '
            f'PF: <b>{s["pf"]:.2f}</b> | '
            f'AvgW: {s["avg_win"]:+.2f}% AvgL: {s["avg_loss"]:+.2f}%'
        )

        # Top/worst symbol
        by_sym = _group_by(trades, 'symbol')
        sym_pnls = [(sym, sum(float(t.get('pnl_pct') or 0) for t in ts))
                    for sym, ts in by_sym.items()]
        sym_pnls.sort(key=lambda x: x[1])
        if len(sym_pnls) >= 1:
            best = sym_pnls[-1]
            out.append(f'  🏆 {best[0]}: <b>{best[1]:+.2f}%</b>')
        if len(sym_pnls) >= 2:
            worst = sym_pnls[0]
            out.append(f'  💀 {worst[0]}: <b>{worst[1]:+.2f}%</b>')

        # Funnel for 24h
        if w == '24h':
            sig_total = len(signals)
            sig_taken = sum(1 for x in signals if (x.get('status') or '').lower() == 'taken')
            take_rate = _safe_div(sig_taken, sig_total) * 100
            out.append(
                f'  Signals: {sig_total} | Taken: {sig_taken} | '
                f'Rate: {take_rate:.1f}%'
            )

        # Keep last window data for the per-symbol detail block
        longest_window_data = (w, trades, by_sym)

    # Per-symbol breakdown for the longest window
    if longest_window_data:
        w, trades, by_sym = longest_window_data
        out.append(f'\n📌 <b>Per-symbol ({labels.get(w, w)})</b>')
        out.append('<pre>')
        out.append(f'{"Pair":9}{"N":>4} {"WR%":>5} {"Exp%":>7} {"PF":>6}')
        for sym in sorted(by_sym.keys()):
            ts = by_sym[sym]
            sym_stats = _stats_from_trades(ts)
            if sym_stats.get('trades', 0) == 0:
                continue
            out.append(
                f'{sym:9}{sym_stats["trades"]:>4} '
                f'{sym_stats["wr"]:>5.1f} '
                f'{sym_stats["exp_pct"]:>+7.2f} '
                f'{sym_stats["pf"]:>6.2f}'
            )
        out.append('</pre>')

    text = '\n'.join(out)
    if len(text) > 4000:
        text = text[:3990] + '\n...(truncated)'
    return text


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--db', default=os.environ.get('DB_PATH', '/var/data/crossx.db'),
                   help='Path to SQLite DB file')
    p.add_argument('--since', default='', help='Period filter: 24h, 7d, 30d, 2w, or empty for all-time')
    p.add_argument('--format', default='markdown', choices=['markdown', 'json'])
    args = p.parse_args()

    print(build_report(args.db, since=args.since, fmt=args.format))


if __name__ == '__main__':
    main()
