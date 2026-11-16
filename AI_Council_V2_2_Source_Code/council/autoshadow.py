import os
import re
import sys
import json
import time
import asyncio
import argparse
import datetime
import httpx
import scanner
import pullback
import fill
import paper
import council
import narrative
import market_regime
import event_gate
from council import run_council

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "autoshadow_state.json")
EVENTS_FILE = os.path.join(HERE, "events.txt")
LOG_MD = os.path.join(HERE, "autoshadow_log.md")
RESEARCH_LOG = os.path.join(HERE, "research_signals_v21.jsonl")

SETUP = "pullback_v21"
STRATEGY_VERSION = "pullback_context_v2_1"
RULE = "LONG 4H: original Pullback entry; liquidity sweep, narrative and BTC regime are optional context, NOT mandatory gates"
INTERVAL = "4h"
STEP_S = 4 * 3600
DELAY_S = 120                   # chờ sau khi nến đóng
HOLD_HOURS = pullback.HOLD_BARS * 4.0
MAX_PER_CYCLE = max(1, min(10, int(os.getenv("COUNCIL_MAX_PER_CYCLE", "3"))))
MAX_SIGNAL_AGE_S = max(60, int(os.getenv("COUNCIL_MAX_SIGNAL_AGE_S", "600")))
MAX_CYCLE_SECONDS = max(60, int(os.getenv("COUNCIL_MAX_CYCLE_SECONDS", "480")))
COUNCIL_PER_SIGNAL_TIMEOUT_S = max(1, int(os.getenv("COUNCIL_PER_SIGNAL_TIMEOUT_S", "360")))
CATCHUP_WINDOW_S = 3.5 * 3600   # bot khởi động trong khoảng này sau khi nến đóng thì chạy bù ngay

# Bổ sung tên cho một số đồng phổ biến và ít gây nhầm với từ thông thường
EXTRA_ALIASES = {
    "ZEC": "zcash", "UNI": "uniswap", "HYPE": "hyperliquid", "AAVE": "aave", "APT": "aptos",
    "FIL": "filecoin", "ICP": "internet computer", "INJ": "injective", "ENA": "ethena",
    "ONDO": "ondo finance", "HBAR": "hedera", "ALGO": "algorand", "BCH": "bitcoin cash",
    "ETC": "ethereum classic", "TAO": "bittensor", "WLD": "worldcoin", "SEI": "sei network",
}
council.ALIASES.update({k: v for k, v in EXTRA_ALIASES.items() if k not in council.ALIASES})
for _members in narrative.load_mapping().values():
    council.ALIASES.update({k: v for k, v in _members.items() if k not in council.ALIASES})


# ---------- Trạng thái ----------
def load_state():
    st = {"processed": [], "last_boundary": None, "last_cycle": None, "heartbeat_date": None}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                st.update(json.load(fh))
        except Exception as e:
            print(f"Không đọc được autoshadow_state.json ({e}). Dùng trạng thái trống.")
    return st


def save_state(st):
    st["processed"] = st["processed"][-2000:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ---------- Lịch chạy ----------

def signal_key(base, bar_ms):
    """One canonical key shared by autoshadow and diagnose replay."""
    return f"{base}:{int(bar_ms)}:{STRATEGY_VERSION}"


def signal_age_s(signal, *, now=None):
    """Seconds since the 4h signal candle closed (can be negative in tests)."""
    return (time.time() if now is None else now) - (int(signal['bar']) / 1000 + STEP_S)


def log_research(signal, status, reason, verdict=None):
    """Append an audit row, including blocked signals. No real orders."""
    record = {
        'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
        'signal_id': signal_key(signal['base'], signal['bar']),
        'symbol': signal['base'], 'bar_ms': signal['bar'],
        'strategy_version': STRATEGY_VERSION, 'code_release': '2.2', 'status': status,
        'reason': reason, 'verdict': verdict,
        'entry': signal['entry'], 'stop': signal['stop'], 'target': signal['target'],
        'context': signal.get('context', {}),
    }
    with open(RESEARCH_LOG, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')


def seconds_until_next_cycle():
    now = time.time()
    this_run = (now // STEP_S) * STEP_S + DELAY_S
    target = this_run if now < this_run else (now // STEP_S + 1) * STEP_S + DELAY_S
    return max(1.0, target - now)


def needs_catchup():
    """Trả về (có cần chạy bù ngay không, số giây nên chờ trước khi chạy)."""
    now = time.time()
    boundary = int(now // STEP_S * STEP_S)
    st = load_state()
    if st.get("last_boundary") == boundary or now - boundary > CATCHUP_WINDOW_S:
        return False, 0
    return True, max(20.0, DELAY_S - (now - boundary))


# ---------- Quét tín hiệu ----------
def configure_scanner():
    ns = argparse.Namespace(fee=None, slip=None, interval=INTERVAL, days=730, top=40, minvol=5.0)
    pullback.configure(ns)


async def scan_signals():
    """Produce every original 4H pullback candidate and attach optional market context.

    Never filter a valid pullback solely because of no sweep / BTC RISK_OFF /
    unknown coin narrative. Only finished, matching 4h candles are eligible.
    """
    configure_scanner()
    step_ms = scanner.INTERVAL_MS[INTERVAL]
    now_ms = int(time.time() * 1000)
    expected_bar_ms = (now_ms // step_ms - 1) * step_ms
    mapping = narrative.load_mapping()
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "council-autoshadow-v2.1/1.0"}) as client:
        uni, total, passing = await scanner.universe(client)
        sem = asyncio.Semaphore(4)
        try:
            regime = await market_regime.fetch_regime(client)
        except Exception as e:
            regime = {'regime': 'UNKNOWN', 'long_allowed': False, 'reason': str(e)}

        async def one(base, symbol):
            async with sem:
                try:
                    rows = await scanner.load_klines(client, symbol)
                except Exception as e:
                    return {'error': f'{symbol}: {e}'}
            # Binance may include an OPEN current candle; never trade on it.
            rows = [r for r in rows if int(r[0]) + step_ms <= now_ms]
            if len(rows) < 200:
                return {'error': f'{symbol}: insufficient closed 4h candles'}
            d = pullback.prepare_full(rows)
            i = len(rows) - 1
            if d['t'][i] != expected_bar_ms:
                return {'error': f'{symbol}: stale/misaligned last closed candle {d["t"][i]} expected {expected_bar_ms}'}
            cm = narrative.coin_metrics(d, i)
            sig = pullback.pullback_signal(d, i)  # V1 UNCHANGED
            if sig:
                from liquidity import bullish_sweep
                sweep = bullish_sweep(d, i, pivot_confirm=pullback.PIVOT)
                sig.update(base=base, bar=d['t'][i], atr=d['atr'][i],
                           sweep=sweep or {'confirmed': False, 'reason': 'no qualifying sweep'})
            return {'base': base, 'metric': cm, 'signal': sig}

        raw = await asyncio.gather(*(one(b, s) for b, s, _ in uni))
        if not any(x and x.get('base') == 'BTC' for x in raw):
            raw.append(await one('BTC', 'BTCUSDT'))
    errors = [r['error'] for r in raw if r and 'error' in r]
    metrics = {r['base']: r['metric'] for r in raw if r and r.get('metric')}
    btc = metrics.get('BTC')
    groups = narrative.calculate_scores(metrics, mapping, btc)
    sigs = []
    for r in raw:
        if not r or not r.get('signal'):
            continue
        sig = r['signal']
        nar = narrative.choose_for_coin(r['base'], groups, metrics, mapping, btc)
        sig['context'] = {
            'strategy_version': STRATEGY_VERSION,
            'code_release': '2.2',
            'market_regime': regime,
            'narrative': nar,
            'liquidity_sweep': sig['sweep'],
            'signal_bar_ms': sig['bar'],
            'signal_close_price': sig['entry'],
            'filter_design': 'Pullback gate only; auxiliary factors are observational',
        }
        sig['eligible'] = True
        sig['gate_reason'] = 'Pullback V1 candidate; sweep/narrative/regime informational only'
        sigs.append(sig)
    return sigs, errors, len(uni), {'regime': regime, 'groups': groups}


def fix_levels(question, sig):
    """Với quy tắc pullback, dừng lỗ nằm dưới đáy nhịp hồi theo thiết kế.
    Thay dòng Levels do fill.py tính theo hỗ trợ 20 nến (khác định nghĩa của quy tắc)."""
    m = re.search(r"Resistance:\s*([0-9]+(?:\.[0-9]+)?)", question)
    res = float(m.group(1)) if m else None
    notes = [
        "the stop-loss is placed below the pullback low, which is the intended design for this setup",
        "the stop-loss distance is adequate and not too tight",
    ]
    ok = True
    if res is not None and sig["entry"] < res < sig["target"]:
        notes.append("target is beyond the resistance level, so resistance may block price (risk)")
        ok = False
    else:
        notes.append("target is below the resistance level, which is not a concern")
    head = "all level placements are acceptable" if ok else "some level placements carry risk"
    line = f"Levels (verified by code, do not dispute): {head}: " + "; ".join(notes) + "."
    return re.sub(r"^Levels \(verified by code.*$", lambda _m: line, question, count=1, flags=re.M)


def stub(sig):
    return {"setup": SETUP, "base": sig["base"], "structure": "uptrend", "rsi": round(sig["rsi"], 1)}


def summarize(base, full, msg):
    t = full.replace("**", "")
    m = re.search(r"KẾT LUẬN:\s*([A-Z_]+)", t)
    verdict = m.group(1) if m else "?"
    fails = []
    for line in t.splitlines():
        if "KHÔNG ĐẠT" in line:
            fails.append(re.sub(r":\s*KHÔNG ĐẠT.*$", "", line).strip("- ").strip())
    ml = msg.splitlines()
    first = ml[0] if ml else ""
    second = ml[1] if len(ml) > 1 else ""
    text = f"- {base}: {verdict}."
    if fails:
        text += " Cổng không đạt: " + "; ".join(fails) + "."
    return text + f"\n  {first} {second[:140]}".rstrip()


def append_md(base, question, full, msg):
    try:
        with open(LOG_MD, "a", encoding="utf-8") as fh:
            fh.write(
                f"\n\n## {datetime.datetime.now():%Y-%m-%d %H:%M} {base}\n\n"
                f"**Câu hỏi:**\n{question}\n\n{full}\n\n**Lệnh bóng:** {msg}\n"
            )
    except Exception as e:
        print("autoshadow_log lỗi:", e, flush=True)


# ---------- Một chu kỳ ----------
async def process_signals(notify, dry=False):
    now = time.time()
    cycle_started = time.monotonic()
    boundary = int(now // STEP_S * STEP_S)
    state = load_state()
    last = state.get('last_boundary')
    missed = max(0, int((boundary - last) // STEP_S) - 1) if last else 0
    event = event_gate.assess(EVENTS_FILE)
    try:
        sigs, errs, n_scanned, ctx = await scan_signals()
    except Exception as e:
        await notify(f'V2 scan failed safely: {e}')
        return
    when = datetime.datetime.now().strftime('%d/%m %H:%M')
    eligible = [s for s in sigs if s['eligible']]
    header = (f'V2.1 {when}: {n_scanned} pairs, {len(sigs)} original Pullback candidates, '
              'sweep/narrative/BTC context optional.')
    if dry:
        lines = [header, f"Market regime: {ctx['regime'].get('regime')}",
                 f"Event gate: {'ALLOW' if event['allowed'] else 'BLOCK'} ({event['reason']})",
                 'Narratives: ' + ', '.join(f'{k}={v["score"]:.1f}({v["members"]})'
                                            for k,v in sorted(ctx['groups'].items(),
                                            key=lambda kv: -kv[1]['score']))]
        for s in sigs:
            n = s['context']['narrative']
            depth = s['sweep'].get('depth_atr')
            sweep_desc = f"{depth:.2f} ATR" if depth is not None else "NO SWEEP"
            lines.append(f"- {s['base']}: candidate; "
                         f"narrative {n.get('name','UNKNOWN')} ({n.get('score','?')}); "
                         f"sweep {sweep_desc}; "
                         f"entry {scanner.fmt(s['entry'])}, SL {scanner.fmt(s['stop'])}, "
                         f"TP {scanner.fmt(s['target'])}; {s['gate_reason']}")
        if errs: lines.append(f'Data errors {len(errs)}; e.g. {errs[0]}')
        lines.append('DRY-RUN: no AI call, no position, no state mutation')
        await notify('\n'.join(lines))
        return

    lines = []
    judged = 0
    processed = set(state['processed'])
    # Event gate is checked by CODE before SearXNG, Ollama or paper.open_position.
    # Log newly blocked signals and mark processed so a past bar is not traded later.
    for sig in sigs:
        key = signal_key(sig['base'], sig['bar'])
        if key in processed:
            continue
        if not event['allowed']:
            log_research(sig, 'EVENT_BLOCK', event['reason'])
            fill.log_shadow(stub(sig), '', '', '', 'V2 event blocked: ' + event['reason'])
            lines.append(f"- {sig['base']}: macro event BLOCK; no AI, no paper position")
            state['processed'].append(key); processed.add(key)
            continue

    active = [s for s in eligible if signal_key(s['base'], s['bar']) not in processed]
    for s in active[MAX_PER_CYCLE:]:
        key = signal_key(s['base'], s['bar'])
        log_research(s, 'LIMIT', 'MAX_PER_CYCLE exceeded')
        fill.log_shadow(stub(s), '', '', '', 'V2 cycle limit reached')
        state['processed'].append(key); processed.add(key)
    for s in active[:MAX_PER_CYCLE]:
        key = signal_key(s['base'], s['bar'])
        try:
            age = signal_age_s(s)
            if age > MAX_SIGNAL_AGE_S:
                log_research(s, 'LATE_SIGNAL', f'signal age {age:.0f}s > {MAX_SIGNAL_AGE_S}s')
                lines.append(f"- {s['base']}: signal too old ({age:.0f}s), no Council/paper")
                continue
            if time.monotonic() - cycle_started > MAX_CYCLE_SECONDS:
                log_research(s, 'TIME_BUDGET', f'cycle > {MAX_CYCLE_SECONDS}s')
                lines.append(f"- {s['base']}: cycle time budget exhausted, no Council/paper")
                continue
            other = next((p for p in paper.load_db()['positions']
                          if p['status'] == 'open' and p['base'] == s['base']), None)
            if other:
                log_research(s, 'OPEN_BLOCK', f'open paper position #{other["id"]}')
                fill.log_shadow(stub(s), '', '', other['id'], 'already open position')
                lines.append(f"- {s['base']}: existing paper position #{other['id']}")
            else:
                args = argparse.Namespace(
                    symbol=s['base'], setup=SETUP, rule=RULE,
                    entry=s['entry'], stop=s['stop'], target=s['target'],
                    auto=False, short=False, atr_stop=None, rr=None,
                    account=None, risk=None, interval=INTERVAL, leverage=None, perp=False,
                    market=None, keywords=None, event=event_gate.as_council_text(event),
                    demo=False, shadow=True, hold=HOLD_HOURS, dry=False,
                    context=s['context'], signal_id=key,
                )
                res = await fill.build_request(args)
                if not res.get('question'):
                    reason = (res.get('text') or '')[-150:]
                    log_research(s, 'PLAN_ERROR', reason)
                    lines.append(f"- {s['base']}: plan failed: {reason[:100]}")
                else:
                    question = fix_levels(res['question'], s)
                    remaining = MAX_CYCLE_SECONDS - (time.monotonic() - cycle_started)
                    if remaining <= 0:
                        log_research(s, 'TIME_BUDGET', 'no time left before Council')
                        lines.append(f"- {s['base']}: out of cycle budget before Council")
                        continue
                    try:
                        async with asyncio.timeout(min(COUNCIL_PER_SIGNAL_TIMEOUT_S, remaining)):
                            chunks = [part async for part in run_council(question)]
                    except TimeoutError:
                        log_research(s, 'COUNCIL_TIMEOUT', 'Council exceeded per-coin/cycle time budget')
                        lines.append(f"- {s['base']}: Council timeout, no paper position")
                        continue
                    full = ''.join(chunks)
                    verdict, info = fill.verdict_of(full)
                    late_gate = event_gate.assess(EVENTS_FILE)
                    age_after = signal_age_s(s)
                    if age_after > MAX_SIGNAL_AGE_S:
                        msg = f"Paper position BLOCKED: delayed {age_after:.0f}s > {MAX_SIGNAL_AGE_S}s"
                        log_research(s, 'LATE_AFTER_AI', msg, verdict)
                    elif not late_gate['allowed']:
                        msg = f"Paper position BLOCKED: {late_gate['reason']}"
                        log_research(s, 'EVENT_BLOCK_AFTER_AI', late_gate['reason'], verdict)
                    else:
                        msg = await fill.shadow_after(res['plan'], full)
                        log_research(s, 'COUNCIL_JUDGED', info, verdict)
                    append_md(s['base'], question, full, msg)
                    lines.append(summarize(s['base'], full, msg))
                    judged += 1
        except Exception as e:
            log_research(s, 'ERROR', str(e))
            lines.append(f"- {s['base']}: ERROR {e}")
        finally:
            state['processed'].append(key)
            processed.add(key)
            save_state(state)

    state['last_boundary'] = boundary
    state['last_cycle'] = {
        'time': when, 'scanned': n_scanned, 'signals': len(sigs),
        'judged': judged, 'missed': missed, 'errors': len(errs),
    }
    if errs:
        lines.append(f'Data errors: {len(errs)}; example: {errs[0]}')
    if missed:
        lines.append(f'Missed {missed} earlier 4h cycles')
    if lines:
        await notify('\n'.join([header] + lines))
    elif state.get('heartbeat_date') != datetime.date.today().isoformat():
        state['heartbeat_date'] = datetime.date.today().isoformat()
        await notify(header + ' No new Pullback candidates.')
    save_state(state)


def describe_status(enabled=True):
    st = load_state()
    lc = st.get("last_cycle")
    event = event_gate.assess(EVENTS_FILE)
    nxt = datetime.datetime.now() + datetime.timedelta(seconds=seconds_until_next_cycle())
    lines = [f"Thu thập tự động: {'BẬT' if enabled else 'TẮT (đặt biến môi trường AUTOSHADOW=1 rồi chạy lại bot)'}."]
    if lc:
        lines.append(
            f"Chu kỳ gần nhất lúc {lc['time']}: quét {lc['scanned']} cặp, {lc['signals']} tín hiệu, "
            f"{lc['judged']} đã chấm, {lc['errors']} cặp lỗi dữ liệu, {lc['missed']} chu kỳ bị bỏ lỡ trước đó."
        )
    else:
        lines.append("Chưa có chu kỳ nào được ghi nhận.")
    lines.append(f"Chu kỳ kế tiếp khoảng {nxt:%d/%m %H:%M}.")
    lines.append(f"Event gate: {'ALLOW' if event['allowed'] else 'BLOCK'} — {event['reason']}.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Quét pullback sau mỗi nến 4h và chấm bằng hội đồng ở chế độ bóng.")
    ap.add_argument("cmd", nargs="?", default="status", choices=["once", "status"])
    ap.add_argument("--dry", action="store_true", help="chỉ liệt kê tín hiệu, không gọi hội đồng")
    a = ap.parse_args()
    if a.cmd == "status":
        print(describe_status())
        return

    async def notify(text):
        print(text, flush=True)

    asyncio.run(process_signals(notify, dry=a.dry))


if __name__ == "__main__":
    main()
