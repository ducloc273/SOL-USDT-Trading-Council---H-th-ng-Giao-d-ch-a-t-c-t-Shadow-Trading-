"""Offline unit tests; no live orders, Ollama, Binance or SearXNG requests."""
import asyncio
import datetime as dt
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import event_gate
import liquidity
import narrative
import market_regime
import paper
import autoshadow


def test_event_gate_24h_boundary(tmp_path):
    f = tmp_path / 'events.txt'
    now = dt.datetime(2026, 11, 16, 12)
    f.write_text('2026-11-17 12:00 | Fed press conference\n', encoding='utf8')
    assert not event_gate.assess(f, now)['allowed']
    assert event_gate.assess(f, now - dt.timedelta(minutes=1))['allowed']
    assert not event_gate.assess(tmp_path/'missing.txt', now)['allowed']
    f.write_text('bad event line\n', encoding='utf8')
    assert not event_gate.assess(f, now)['allowed']
    f.write_text('2026-11-15 12:00 | expired\n', encoding='utf8')
    assert not event_gate.assess(f, now)['allowed']


def test_bullish_liquidity_sweep_uses_confirmed_pivots():
    n = 30
    d = {'o':[100.0]*n, 'h':[102.0]*n, 'l':[100.0]*n,
         'c':[101.0]*n, 'atr':[2.0]*n, 'piv':[10]}
    d['l'][10] = 100
    d['l'][20] = 99
    assert liquidity.bullish_sweep(d, 20)['depth_atr'] == .5
    d['piv'] = [18]  # pivot has not yet had five RIGHT candles
    assert liquidity.bullish_sweep(d, 20) is None


def test_narrative_scoring_and_coin_gate():
    bt = {'return_7d_pct': 2., 'bar_ms': 123}
    m = {'A':{'return_7d_pct':15., 'uptrend': True, 'volume_ratio':2, 'bar_ms':123},
         'B':{'return_7d_pct':13., 'uptrend': True, 'volume_ratio':2, 'bar_ms':123}}
    mapping={'AI':{'A':'alpha','B':'beta'}}
    score = narrative.calculate_scores(m, mapping, bt)
    assert score['AI']['status']=='STRONG'
    assert narrative.choose_for_coin('A',score,m,mapping,bt)['eligible']
    assert not narrative.choose_for_coin('X',score,m,mapping,bt)['eligible']
    stale={**m,'B':{**m['B'],'bar_ms':122}}
    assert narrative.calculate_scores(stale,mapping,bt)=={}


def test_market_regime_uses_only_closed_days():
    day=86_400_000
    rows=[[i*day, '0','0','0',str(100+i)] for i in range(70)]
    result=market_regime.classify(rows, as_of_ms=70*day)
    assert result['long_allowed']
    # Huge unclosed future candle must not influence the decision.
    rows.append([70*day,'0','0','0','1'])
    assert market_regime.classify(rows,as_of_ms=70*day)['long_allowed']


def test_manual_paper_open_is_macro_blocked(monkeypatch):
    monkeypatch.setattr(event_gate,'assess', lambda *_a, **_kw:
                        {'allowed':False,'reason':'24h macro','event':None})
    result=asyncio.run(paper.open_position(SimpleNamespace()))
    assert 'BLOCK' in result


def test_autoshadow_macro_block_bypasses_ai(monkeypatch):
    s={'base':'AAVE','bar':12345,'entry':100.,'stop':97.,'target':106.,'rsi':48.,
       'sweep':{'depth_atr':.4},'eligible':True,'gate_reason':'eligible',
       'context':{'narrative':{'name':'DeFi','score':80},'liquidity_sweep':{'confirmed':True}}}
    async def mock_scan():
        return [s],[],1,{'regime':{'regime':'RISK_ON'},'groups':{'DeFi':{'score':80,'members':3}}}
    async def must_not_run(*_args,**_kwargs):
        raise AssertionError('Council called during event block')
    async def mock_notify(msg):
        notices.append(msg)
    notices=[]; logs=[]; states=[]
    monkeypatch.setattr(autoshadow,'scan_signals',mock_scan)
    monkeypatch.setattr(autoshadow,'load_state',lambda:{'processed':[],'last_boundary':None,'heartbeat_date':None})
    monkeypatch.setattr(autoshadow,'save_state',lambda st:states.append(st.copy()))
    monkeypatch.setattr(autoshadow,'log_research',lambda *a,**kw:logs.append(a))
    monkeypatch.setattr(autoshadow.fill,'log_shadow',lambda *a,**kw:None)
    monkeypatch.setattr(autoshadow,'run_council',must_not_run)
    monkeypatch.setattr(event_gate,'assess',lambda *_a,**_kw:{'allowed':False,'reason':'event under 24h'})
    asyncio.run(autoshadow.process_signals(mock_notify))
    assert logs[0][1] == 'EVENT_BLOCK'
    assert 'no AI, no paper' in notices[0]
    assert states[-1]['processed']
