"""Bullish liquidity-sweep detector for *closed* 4h candles.

A pivot is eligible only when all its confirmation candles were already closed
before the sweep candle. No future candles are read.
"""


def bullish_sweep(d, i, *, pivot_confirm=5, pivot_lookback=120,
                  sweep_window=3, min_depth_atr=0.05,
                  max_depth_atr=1.0, min_close_position=0.60):
    if i < pivot_confirm * 2 + sweep_window or i >= len(d['c']):
        return None
    pivots = d.get('piv', [])
    if not pivots:
        return None
    for s in range(i, max(-1, i - sweep_window), -1):
        atr = d['atr'][s]
        if not atr or atr <= 0:
            continue
        o, h, l, c = (d[k][s] for k in ('o', 'h', 'l', 'c'))
        span = h - l
        if span <= 0 or c <= o or (c - l) / span < min_close_position:
            continue
        # Only pivots that were fully confirmed by the time bar s opened.
        historical = [j for j in pivots if max(0, s - pivot_lookback) <= j <= s - pivot_confirm - 1]
        for j in reversed(historical):
            level = d['l'][j]
            depth = (level - l) / atr
            if min_depth_atr <= depth <= max_depth_atr and c > level:
                # Confirmation candle must reclaim the same level too.
                if d['c'][i] <= level:
                    continue
                return {
                    'confirmed': True,
                    'pivot_index': j,
                    'sweep_index': s,
                    'level': round(level, 12),
                    'sweep_low': round(l, 12),
                    'depth_atr': round(depth, 3),
                    'close_position': round((c - l) / span, 3),
                    'bars_ago': i - s,
                }
    return None
