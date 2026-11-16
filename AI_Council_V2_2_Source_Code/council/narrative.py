"""Narrative descriptors, NOT a mandatory entry gate and NOT measured fund flows.

Group score is an exploratory price/volume proxy using only closed 4h candles:
60% 7d relative return vs BTC; 25% change of relative 7d momentum;
15% recent 24h volume acceleration. Scores are hypotheses, not validated signals.
Unmapped coins get UNKNOWN and remain eligible for technical assessment.
"""
import json
import os

HERE = os.path.dirname(__file__)
MAP_FILE = os.path.join(HERE, 'narrative_map.json')
MIN_GROUP_MEMBERS = 2


def clamp(value, a=0., b=100.):
    return max(a, min(b, value))


def load_mapping(path=MAP_FILE):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if not data or not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
        raise ValueError('Narrative map is empty or invalid')
    return data


def coin_metrics(d, i, bars=42):
    if i < max(84, bars) or i >= len(d['c']):
        return None
    c, past, prev = (float(d['c'][j]) for j in (i, i-bars, i-2*bars))
    if min(c, past, prev) <= 0:
        return None
    current_volume = sum(float(v) for v in d['v'][i-5:i+1])
    previous_volume = sum(float(v) for v in d['v'][i-11:i-5])
    if previous_volume <= 0:
        volume_ratio = None
    else:
        volume_ratio = current_volume / previous_volume
    return {
        'return_7d_pct': (c / past - 1) * 100,
        'return_prev_7d_pct': (past / prev - 1) * 100,
        'volume_ratio': volume_ratio,
        'bar_ms': int(d['t'][i]),
    }


def calculate_scores(metrics, mapping, btc_metrics):
    if not btc_metrics:
        return {}  # UNKNOWN is propagated, not a hard stop.
    btc_return = btc_metrics['return_7d_pct']
    btc_prev = btc_metrics['return_prev_7d_pct']
    groups = {}
    for group, members in mapping.items():
        included = [(coin, metrics[coin]) for coin in members if coin in metrics and
                    metrics[coin]['bar_ms'] == btc_metrics['bar_ms']]
        if len(included) < MIN_GROUP_MEMBERS:
            continue
        rs = [m['return_7d_pct'] - btc_return for _, m in included]
        mom = [(m['return_7d_pct'] - m['return_prev_7d_pct']) - (btc_return - btc_prev)
               for _, m in included]
        vols = [min(m['volume_ratio'], 4.) for _, m in included if m['volume_ratio'] is not None]
        mean_rs = sum(rs) / len(rs)
        mean_mom = sum(mom) / len(mom)
        mean_vol = sum(vols) / len(vols) if vols else None
        rel_score = clamp(50 + 5 * mean_rs)
        momentum_score = clamp(50 + 3 * mean_mom)
        vol_score = clamp(50 + 25 * (mean_vol - 1)) if mean_vol is not None else 50
        score = round(.60 * rel_score + .25 * momentum_score + .15 * vol_score, 2)
        groups[group] = {
            'score': score, 'members': len(included),
            'relative_7d_pct': round(mean_rs, 3),
            'relative_momentum_pct': round(mean_mom, 3),
            'volume_ratio': round(mean_vol, 3) if mean_vol is not None else None,
            'status': 'STRONG' if mean_rs > 0 and score >= 60 else 'WEAK',
            'as_of_ms': btc_metrics['bar_ms'],
            'method': 'price_volume_proxy_v2_1',
        }
    return groups


def choose_for_coin(base, groups, metrics, mapping, btc_metrics):
    if not btc_metrics or base not in metrics or metrics[base]['bar_ms'] != btc_metrics['bar_ms']:
        return {'eligible': True, 'name': 'UNKNOWN', 'score': None,
                'status': 'UNKNOWN', 'reason': 'insufficient synchronized market data; informational only'}
    matched = []
    for group, tokens in mapping.items():
        if base in tokens and group in groups:
            g = groups[group]
            rel = metrics[base]['return_7d_pct'] - btc_metrics['return_7d_pct']
            matched.append((g['score'], group, g, rel))
    if not matched:
        return {'eligible': True, 'name': 'UNKNOWN', 'score': None,
                'status': 'UNKNOWN', 'reason': 'unmapped or too few constituents; not filtered'}
    matched.sort(reverse=True)
    score, name, g, rel = matched[0]
    return {
        'eligible': True, 'name': name, 'score': score, 'status': g['status'],
        'members': g['members'], 'relative_7d_pct': round(rel, 3),
        'relative_momentum_pct': g['relative_momentum_pct'],
        'reason': 'informational descriptor; not an entry gate',
        'as_of_ms': g['as_of_ms'], 'method': 'price_volume_proxy_v2_1',
    }
