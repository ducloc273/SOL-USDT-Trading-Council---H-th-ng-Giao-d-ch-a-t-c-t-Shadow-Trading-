"""Offline V1 Pullback vs optional Liquidity Sweep factor analysis.

Run from project directory: py research_compare_v21.py
Uses ONLY local klines_cache/*_4h.json; never sends any orders or requests.
Not a validation of AI, Narrative or BTC regime; no future data is used for sweep.
"""
import csv
import json
import argparse
from pathlib import Path

import pullback
import scanner
from liquidity import bullish_sweep

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'klines_cache'
OUT = ROOT / 'research_compare_v21.csv'


def outcome(d, i, sig):
    entry, stop, target = sig['entry'], sig['stop'], sig['target']
    horizon = i + pullback.HOLD_BARS
    risk_u = entry - stop
    cost_r = 2 * scanner.COST_PER_SIDE * entry / risk_u
    for j in range(i + 1, horizon + 1):
        if d['l'][j] <= stop:  # pessimistic assumption if both hit
            return j, -1 - cost_r
        if d['h'][j] >= target:
            return j, pullback.RR - cost_r
    return horizon, (d['c'][horizon] - entry) / risk_u - cost_r


def collect(*, fee=None, slip=None):
    # Use the same settings.json-based configuration as the production pullback
    # scanner; CLI overrides remain optional.
    ns = argparse.Namespace(fee=fee, slip=slip, interval='4h',
                            days=730, top=40, minvol=5.0)
    loaded_fee, loaded_slip = pullback.configure(ns)
    records = []
    paths = sorted(CACHE.glob('*USDT_4h.json'))
    for path in paths:
        try:
            with path.open(encoding='utf8') as f:
                blob = json.load(f)
            rows = blob.get('rows', []) if isinstance(blob, dict) else blob
            if len(rows) < 200:
                continue
            d = pullback.prepare_full(rows)
            i = 130
            while i < len(rows) - pullback.HOLD_BARS - 1:
                sig = pullback.pullback_signal(d, i)
                if not sig:
                    i += 1
                    continue
                # This simulates non-overlapping positions for this specific coin.
                sweep = bullish_sweep(d, i, pivot_confirm=pullback.PIVOT)
                exit_i, r = outcome(d, i, sig)
                records.append({'base': path.name.split('USDT_')[0], 'bar_ms': int(d['t'][i]),
                                'entry': sig['entry'], 'stop': sig['stop'], 'target': sig['target'],
                                'sweep': int(bool(sweep)), 'r_net': round(r, 6),
                                'exit_bar_ms': int(d['t'][exit_i])})
                i = exit_i + 1
        except (OSError, ValueError, KeyError, ZeroDivisionError) as e:
            print(f'SKIP {path.name}: {e}')
    return paths, records, loaded_fee, loaded_slip


def main():
    parser = argparse.ArgumentParser(description='Offline Pullback / Liquidity Sweep comparison')
    parser.add_argument('--fee', type=float, default=None, help='override fee_pct from settings.json')
    parser.add_argument('--slip', type=float, default=None, help='override slip_pct from settings.json')
    args = parser.parse_args()
    paths, rows, fee, slip = collect(fee=args.fee, slip=args.slip)
    print(f'Cost settings: fee={fee:.4f}%/side, slip={slip:.4f}%/side; '
          f'round-trip={2*(fee+slip):.4f}%; source=settings.json or defaults/CLI')
    if not paths:
        print(f'No local 4h Binance cache in {CACHE}. Nothing downloaded.')
        return
    with OUT.open('w', newline='', encoding='utf8') as fh:
        writer=csv.DictWriter(fh, fieldnames=['base','bar_ms','entry','stop','target','sweep','r_net','exit_bar_ms'])
        writer.writeheader()
        writer.writerows(rows)
    print(f'Cached pair files: {len(paths)}; non-overlapping Pullback trades: {len(rows)}')
    for name, subset in [('All Pullback',rows),('Sweep detected',[r for r in rows if r['sweep']]),
                         ('No Sweep',[r for r in rows if not r['sweep']])]:
        n=len(subset)
        avg=sum(x['r_net'] for x in subset)/n if n else float('nan')
        print(f'{name}: n={n}, mean net R={avg:+.4f}')
    print(f'Detail CSV: {OUT}')
    print('LIMIT: no AI, event gate, narrative or BTC regime in this offline ablation; historical universe may be biased.')


if __name__ == '__main__':
    main()
