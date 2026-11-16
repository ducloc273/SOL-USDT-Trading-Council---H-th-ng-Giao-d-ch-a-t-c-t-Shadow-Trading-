"""Offline tests. All external API calls are mocked; no real trading occurs."""
import asyncio
import datetime as dt
import json
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


def _eventfile(tmp_path, line='2026-11-17 12:00 | Fed press conference\n'):
    f = tmp_path / 'events.txt'
    f.write_text(line, encoding='utf8')
    return f


def test_event_gate_before_after_inclusive_boundaries(tmp_path):
    f = _eventfile(tmp_path)
    when = dt.datetime(2026,11,17,12)
    assert event_gate.assess(f, when - dt.timedelta(hours=24))['status'] == 'EVENT_WINDOW'
    assert event_gate.assess(f, when - dt.timedelta(hours=24, minutes=1))['allowed']
    assert event_gate.assess(f, when + dt.timedelta(hours=1))['status'] == 'EVENT_WINDOW'
    assert event_gate.assess(f, when + dt.timedelta(hours=2))['status'] == 'EVENT_WINDOW'
    assert event_gate.assess(f, when + dt.timedelta(hours=2, minutes=1))['status'] == 'OUTDATED'


def test_event_gate_outdated_manual_ack_scope(tmp_path):
    f = _eventfile(tmp_path, '2026-01-01 00:00 | Old event\n')
    now = dt.datetime(2027, 1, 3)
    assert event_gate.assess(f, now)['status'] == 'OUTDATED'
    assert event_gate.assess(f, now, allow_stale_manual=True)['status'] == 'OUTDATED_MANUAL_ACK'
    assert event_gate.assess(f, now, allow_stale_manual=True)['allowed']
    f = _eventfile(tmp_path, 'bad event line\n')
    assert not event_gate.assess(f, now, allow_stale_manual=True)['allowed']
    assert not event_gate.assess(tmp_path/'missing.txt', now, allow_stale_manual=True)['allowed']
    f = _eventfile(tmp_path, '2027-01-03 01:00 | Actual active event\n')
    assert not event_gate.assess(f, now, allow_stale_manual=True)['allowed']


def test_event_gate_timezone_offset(tmp_path, monkeypatch):
    monkeypatch.setenv('COUNCIL_EVENT_TZ', 'Asia/Ho_Chi_Minh')
    f = _eventfile(tmp_path, '2026-11-17 12:00 | Event\n')
    # 12:00 UTC = 19:00 +07; i.e. 7 hours AFTER the local noon event.
    utc_now = dt.datetime(2026,11,17,12,tzinfo=dt.timezone.utc)
    assert event_gate.assess(f, utc_now)['status'] == 'OUTDATED'


def test_bullish_liquidity_sweep_requires_confirmed_pivots():
    n = 30
    d = {'o':[100.0]*n, 'h':[102.0]*n, 'l':[100.0]*n,
         'c':[101.0]*n, 'atr':[2.0]*n, 'piv':[10]}
    d['l'][20] = 99
    assert liquidity.bullish_sweep(d, 20)['depth_atr'] == .5
    d['piv'] = [18]
    assert liquidity.bullish_sweep(d, 20) is None


def test_narrative_unknown_does_not_disqualify():
    btc = {'return_7d_pct':2., 'return_prev_7d_pct':1., 'bar_ms':123}
    metrics = {'A':{'return_7d_pct':15., 'return_prev_7d_pct':2., 'volume_ratio':2, 'bar_ms':123},
               'B':{'return_7d_pct':13., 'return_prev_7d_pct':3., 'volume_ratio':2, 'bar_ms':123}}
    mapping={'AI':{'A':'alpha','B':'beta'}}
    groups = narrative.calculate_scores(metrics, mapping, btc)
    assert groups['AI']['status']=='STRONG'
    assert narrative.choose_for_coin('A',groups,metrics,mapping,btc)['eligible']
    x = narrative.choose_for_coin('X',groups,metrics,mapping,btc)
    assert x['eligible'] and x['status']=='UNKNOWN'
    assert 'breadth_pct' not in groups['AI']  # MA20/MA50 collinearity removed.
    stale={**metrics,'B':{**metrics['B'],'bar_ms':122}}
    assert narrative.calculate_scores(stale,mapping,btc)=={}


def test_market_regime_only_closed_days():
    day = 86_400_000
    rows=[[i*day, '0','0','0',str(100+i)] for i in range(70)]
    assert market_regime.classify(rows, as_of_ms=70*day)['long_allowed']
    rows.append([70*day, '0','0','0','1'])
    assert market_regime.classify(rows,as_of_ms=70*day)['long_allowed']


def test_paper_manual_ack_does_not_override_active_event(monkeypatch):
    monkeypatch.setattr(event_gate, 'assess', lambda *_a, **kw: {
        'allowed': False, 'status': 'EVENT_WINDOW', 'reason': 'macro block', 'event': 'test'})
    ns = SimpleNamespace(ack_stale_events=True)
    result = asyncio.run(paper.open_position(ns))
    assert 'BLOCK' in result


def test_paper_auto_never_passes_manual_ack(monkeypatch):
    called=[]
    def fake_assess(*a, **kw):
        called.append(kw)
        return {'allowed':False, 'status':'OUTDATED', 'reason':'expired'}
    monkeypatch.setattr(event_gate, 'assess', fake_assess)
    ns=SimpleNamespace(ack_stale_events=True, signal_id='automatic:123')
    assert 'BLOCK' in asyncio.run(paper.open_position(ns))
    assert called[0]['allow_stale_manual'] is False


def test_autoshadow_macro_block_never_calls_ai(monkeypatch):
    s={'base':'AAVE','bar':12345,'entry':100.,'stop':97.,'target':106.,'rsi':48.,
       'sweep':{'confirmed':False}, 'eligible':True, 'gate_reason':'baseline pullback',
       'context':{'narrative':{'name':'UNKNOWN','score':None},'liquidity_sweep':{'confirmed':False}}}
    async def mock_scan():
        return [s],[],1,{'regime':{'regime':'RISK_OFF'},'groups':{}}
    async def must_not_run(*_args,**_kwargs):
        raise AssertionError('Council called during macro block')
    async def mock_notify(msg):
        notices.append(msg)
    notices=[]; logs=[]; states=[]
    monkeypatch.setattr(autoshadow,'scan_signals',mock_scan)
    monkeypatch.setattr(autoshadow,'load_state',lambda:{'processed':[],'last_boundary':None,'heartbeat_date':None})
    monkeypatch.setattr(autoshadow,'save_state',lambda st:states.append(st.copy()))
    monkeypatch.setattr(autoshadow,'log_research',lambda *a,**kw:logs.append(a))
    monkeypatch.setattr(autoshadow.fill,'log_shadow',lambda *a,**kw:None)
    monkeypatch.setattr(autoshadow,'run_council',must_not_run)
    monkeypatch.setattr(event_gate,'assess',lambda *_a,**_kw:{'allowed':False,'status':'EVENT_WINDOW','reason':'event 24h'})
    asyncio.run(autoshadow.process_signals(mock_notify))
    assert logs[0][1]=='EVENT_BLOCK'
    assert 'no AI, no paper' in notices[0]
    assert states[-1]['processed']


def test_scan_keeps_pullback_without_sweep_or_known_narrative(monkeypatch):
    step=14_400_000
    now=step*1000+120_000
    last_closed=(now//step-1)*step
    times=[last_closed-j*step for j in range(199,-1,-1)]
    rows=[[t,'100','102','98','101','10000'] for t in times]
    rows.append([last_closed+step,'100','1000','1','999','9000'])  # still OPEN

    async def universe(_): return [('XYZ','XYZUSDT', 100)], 1, 1
    async def load(_client, _symbol): return rows
    async def regime(_): return {'regime':'RISK_OFF','long_allowed':False}
    monkeypatch.setattr(autoshadow.time,'time',lambda:now/1000.)
    monkeypatch.setattr(autoshadow,'configure_scanner',lambda:None)
    monkeypatch.setattr(autoshadow.scanner,'universe',universe)
    monkeypatch.setattr(autoshadow.scanner,'load_klines',load)
    monkeypatch.setattr(autoshadow.market_regime,'fetch_regime',regime)
    monkeypatch.setattr(autoshadow.pullback,'prepare_full',lambda r:{'t':[x[0] for x in r], 'atr':[2]*len(r)})
    monkeypatch.setattr(autoshadow.pullback,'pullback_signal',lambda d,i:{'entry':101,'stop':98,'target':107,'rsi':45})
    monkeypatch.setattr(autoshadow.narrative,'coin_metrics',lambda d,i:{'return_7d_pct':1,'return_prev_7d_pct':0,'bar_ms':d['t'][i], 'volume_ratio':1})
    monkeypatch.setattr(autoshadow.narrative,'calculate_scores',lambda *args:{})
    monkeypatch.setattr(liquidity,'bullish_sweep',lambda *_a,**_kw:None)
    signals, errors, count, context=asyncio.run(autoshadow.scan_signals())
    assert not errors and any(s['base']=='XYZ' for s in signals)
    signals = [s for s in signals if s['base']=='XYZ']
    assert signals[0]['bar']==last_closed  # unclosed candle excluded
    assert signals[0]['eligible'] is True
    assert signals[0]['sweep']['confirmed'] is False
    assert signals[0]['context']['narrative']['status']=='UNKNOWN'
    assert signals[0]['context']['market_regime']['regime']=='RISK_OFF'


# --- V2.2 regression tests: replay key, settings, latency, paper guard ---

def test_replay_processed_key_v21():
    import diagnose
    from autoshadow import STRATEGY_VERSION, signal_key
    bar = 1731744000000
    state = {'processed': [f'SOL:{bar}:{STRATEGY_VERSION}']}
    assert signal_key('SOL', bar) in set(state['processed'])
    assert f'SOL:{bar}' not in set(state['processed'])
    assert diagnose.signal_key('SOL', bar) == signal_key('SOL', bar)


def test_old_macro_helpers_and_guards_removed():
    assert not hasattr(autoshadow, 'load_events')
    assert not hasattr(autoshadow, 'event_text')
    assert not hasattr(autoshadow, 'STALE_MS')
    assert not hasattr(autoshadow, 'GUARD_NEWS')


def test_comparison_uses_settings_and_optional_cli_fee(tmp_path, monkeypatch):
    import research_compare_v21 as cmp
    import scanner
    monkeypatch.setattr(cmp, 'CACHE', tmp_path)  # no external access
    monkeypatch.setattr(cmp.pullback, 'load_settings', lambda: {'fee_pct':0.19, 'slip_pct':0.06})
    paths, trades, fee, slip = cmp.collect()
    assert not paths and not trades
    assert (fee, slip) == (0.19, 0.06)
    assert abs(scanner.COST_PER_SIDE - 0.0025) < 1e-12
    _, _, fee2, slip2 = cmp.collect(fee=0.13, slip=0.03)
    assert (fee2, slip2) == (0.13, 0.03)
    assert abs(scanner.COST_PER_SIDE - 0.0016) < 1e-12


def test_lateness_is_measured_from_candle_close():
    signal={'base':'SOL', 'bar':1_000_000}
    close = signal['bar'] / 1000 + autoshadow.STEP_S
    assert autoshadow.signal_age_s(signal, now=close + 120) == 120
    assert autoshadow.signal_age_s(signal, now=close + 601) > 600


def test_paper_rechecks_age_and_price_drift():
    now_ms = 1_000_000 + 14_400_000 + 120_000
    ctx={'signal_bar_ms':1_000_000}
    assert paper.automatic_entry_guard('SOL:1:v21',ctx,now_ms,100.,100.1,2.) is None
    assert 'drift' in paper.automatic_entry_guard('SOL:1:v21',ctx,now_ms,100.,101.,2.)
    assert 'age' in paper.automatic_entry_guard('SOL:1:v21',ctx,now_ms+600_000,100.,100.,2.)
    assert 'missing' in paper.automatic_entry_guard('SOL:1:v21',{},now_ms,100.,100.,2.)
    assert paper.automatic_entry_guard(None,{},now_ms,100.,200.,2.) is None


def test_auto_signal_too_old_does_not_call_council(monkeypatch):
    now=1_000_000.0
    bar=int((now-autoshadow.STEP_S-660)*1000)
    sig={'base':'SOL','bar':bar,'entry':100.,'stop':96.,'target':108.,'rsi':48.,
         'sweep':{'confirmed':False},'eligible':True,'gate_reason':'baseline',
         'context':{'narrative':{'name':'UNKNOWN','score':None},
                    'liquidity_sweep':{'confirmed':False}}}
    async def scan(): return [sig],[],1,{'regime':{'regime':'UNKNOWN'},'groups':{}}
    async def must_not_run(_): raise AssertionError('Late signal reached AI')
    msgs=[]; statuses=[]
    async def notify(x): msgs.append(x)
    monkeypatch.setattr(autoshadow.time,'time',lambda: now)
    monkeypatch.setattr(autoshadow,'scan_signals',scan)
    monkeypatch.setattr(autoshadow,'load_state',lambda:{'processed':[],'last_boundary':None,'heartbeat_date':None})
    monkeypatch.setattr(autoshadow,'save_state',lambda *_:None)
    monkeypatch.setattr(autoshadow,'log_research',lambda _s,status,*_a: statuses.append(status))
    monkeypatch.setattr(autoshadow,'run_council',must_not_run)
    monkeypatch.setattr(event_gate,'assess',lambda *_a,**_kw:{'allowed':True,'status':'CLEAR','reason':'ok'})
    asyncio.run(autoshadow.process_signals(notify))
    assert 'LATE_SIGNAL' in statuses
    assert 'too old' in msgs[0]


def test_model_profiles_are_configured_with_single_gpu_request_limit():
    import council
    assert council.COUNCIL_MODEL_PROFILE in ('compact','lite','original')
    assert council.OLLAMA_CONCURRENCY >= 1
    if council.COUNCIL_MODEL_PROFILE in ('compact','lite'):
        assert len(set(council.MODELS.values())) == 1


def test_council_timeout_skips_paper(monkeypatch):
    now = 1_000_000.0
    bar = int((now - autoshadow.STEP_S - 120) * 1000)
    sig = {'base': 'SOL', 'bar': bar, 'entry': 100., 'stop': 96.,
           'target': 108., 'rsi': 46., 'eligible': True,
           'context': {'signal_bar_ms': bar, 'narrative': {'name':'UNKNOWN'}},
           'sweep': {'confirmed': False}}
    async def scan(): return [sig], [], 1, {'regime': {'regime': 'UNKNOWN'}, 'groups': {}}
    async def slow_council(_):
        await asyncio.sleep(0.1)
        yield 'unreachable'
    async def request(_): return {'question': 'question', 'plan': {}}
    async def must_not_open(*_args, **_kwargs):
        raise AssertionError('Timed-out AI opened paper')
    notices, statuses = [], []
    async def notify(text): notices.append(text)
    monkeypatch.setattr(autoshadow.time, 'time', lambda: now)
    monkeypatch.setattr(autoshadow, 'scan_signals', scan)
    monkeypatch.setattr(autoshadow, 'load_state', lambda: {'processed': [], 'last_boundary': None, 'heartbeat_date': None})
    monkeypatch.setattr(autoshadow, 'save_state', lambda *_: None)
    monkeypatch.setattr(autoshadow, 'log_research', lambda _s, status, *details: statuses.append((status, details)))
    monkeypatch.setattr(autoshadow, 'run_council', slow_council)
    monkeypatch.setattr(autoshadow, 'COUNCIL_PER_SIGNAL_TIMEOUT_S', 0.01)
    monkeypatch.setattr(autoshadow.fill, 'build_request', request)
    monkeypatch.setattr(autoshadow.fill, 'shadow_after', must_not_open)
    monkeypatch.setattr(autoshadow.paper, 'load_db', lambda: {'positions': []})
    monkeypatch.setattr(event_gate, 'assess', lambda *_a, **_kw: {'allowed': True, 'status': 'CLEAR', 'reason':'ok'})
    asyncio.run(autoshadow.process_signals(notify))
    assert any(x[0] == 'COUNCIL_TIMEOUT' for x in statuses), statuses
    assert 'timeout' in notices[0]
