"""Long-only BTC market-regime gate, based on closed Binance 1d candles."""
import time
import scanner


def classify(rows, as_of_ms=None):
    now_ms = int(time.time() * 1000) if as_of_ms is None else int(as_of_ms)
    closed = [r for r in rows if int(r[0]) + scanner.INTERVAL_MS['1d'] <= now_ms]
    if len(closed) < 51:
        return {'regime': 'UNKNOWN', 'long_allowed': False, 'reason': 'BTC 1d has fewer than 51 closed candles'}
    prices = [float(r[4]) for r in closed]
    m20 = sum(prices[-20:]) / 20
    m50 = sum(prices[-50:]) / 50
    old50 = sum(prices[-51:-1]) / 50
    allowed = prices[-1] > m50 and m20 > m50 and m50 >= old50
    return {
        'regime': 'RISK_ON' if allowed else 'RISK_OFF',
        'long_allowed': allowed,
        'btc_close': round(prices[-1], 8),
        'btc_ma20': round(m20, 8),
        'btc_ma50': round(m50, 8),
        'as_of_ms': int(closed[-1][0]),
        'reason': 'BTC close > MA50; MA20 > MA50; MA50 not falling' if allowed else 'BTC daily LONG trend filter not met',
    }


async def fetch_regime(client):
    rows = await scanner.get_json(client, '/api/v3/klines',
                                  {'symbol': 'BTCUSDT', 'interval': '1d', 'limit': 80})
    return classify(rows)
