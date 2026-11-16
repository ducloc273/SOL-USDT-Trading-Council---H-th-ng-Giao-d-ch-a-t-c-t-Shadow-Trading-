"""Transparent price/volume *proxy* for crypto narrative strength.

Mapping is a manually curated research hypothesis, not proof of capital flow.
7-day relative returns, breadth, and volume use closed 4h candles only.
"""
import json
import os

HERE = os.path.dirname(__file__)
MAP_FILE = os.path.join(HERE, 'narrative_map.json')
MIN_GROUP_MEMBERS = 2
MIN_SCORE = 60.0
MIN_COIN_REL_PCT = 0.0


def clamp(value, a=0., b=100.):
    return max(a, min(b, value))


def load_mapping(path=MAP_FILE):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if not data or not isinstance(data, dict):
        raise ValueError('Narrative map is empty or invalid')
    return data


def coin_metrics(d, i, bars=42):
    if i < max(50, bars) or d['c'][i-bars] <= 0:
        return None
    c = float(d['c'][i]); old = float(d['c'][i-bars])
    ma20, ma50, vol20 = d['sma20'][i], d['sma50'][i], d['vol20'][i]
    if c <= 0 or not ma20 or not ma50 or not vol20 or vol20 <= 0:
        return None
    return {
        'return_7d_pct': (c / old - 1) * 100,
        'uptrend': bool(c > ma50 and ma20 > ma50),
        'volume_ratio': float(d['v'][i]) / vol20,
        'bar_ms': int(d['t'][i]),
    }


def calculate_scores(metrics, mapping, btc_metrics):
    if not btc_metrics:
        raise ValueError('BTC 4h reference missing')
    btc_return = btc_metrics['return_7d_pct']
    groups = {}
    for group, members in mapping.items():
        included = [(coin, metrics[coin]) for coin in members if coin in metrics and
                    metrics[coin]['bar_ms'] == btc_metrics['bar_ms']]
        if len(included) < MIN_GROUP_MEMBERS:
            continue  # Avoid claiming group strength from just one listed token.
        relative = [x['return_7d_pct'] - btc_return for _, x in included]
        average_rel = sum(relative) / len(relative)
        breadth = sum(1 for _, x in included if x['uptrend']) / len(included)
        vol_ratio = sum(min(x['volume_ratio'], 4.) for _, x in included) / len(included)
        rel_score = clamp(50 + 5 * average_rel)
        vol_score = clamp(50 + 25 * (vol_ratio - 1))
        score = round(.45 * rel_score + .35 * breadth * 100 + .20 * vol_score, 2)
        groups[group] = {
            'score': score, 'members': len(included),
            'relative_7d_pct': round(average_rel, 3),
            'breadth_pct': round(100 * breadth, 1),
            'volume_ratio': round(vol_ratio, 2),
            'status': 'STRONG' if score >= MIN_SCORE and average_rel > 0 else 'WEAK',
            'as_of_ms': btc_metrics['bar_ms'],
        }
    return groups


def choose_for_coin(base, groups, metrics, mapping, btc_metrics):
    if base not in metrics or not btc_metrics:
        return {'eligible': False, 'reason': 'no coin metrics'}
    matched = []
    for name, tokens in mapping.items():
        if base in tokens and name in groups:
            g = groups[name]
            rel = metrics[base]['return_7d_pct'] - btc_metrics['return_7d_pct']
            matched.append((g['score'], name, g, rel))
    if not matched:
        return {'eligible': False, 'reason': 'coin is unmapped or group has insufficient constituents'}
    matched.sort(reverse=True)
    score, name, g, rel = matched[0]
    eligible = g['status'] == 'STRONG' and rel >= MIN_COIN_REL_PCT
    return {
        'eligible': eligible, 'name': name, 'score': score,
        'status': g['status'], 'members': g['members'],
        'relative_7d_pct': round(rel, 3),
        'reason': 'narrative and coin outperform BTC' if eligible else 'weak narrative or coin underperforms BTC',
        'as_of_ms': g['as_of_ms'],
    }
