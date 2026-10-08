import os, json, time, logging
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
CHAT_IDS=[x.strip() for x in os.getenv('TELEGRAM_CHAT_ID','').split(',') if x.strip()]
SYMBOL_RAW=os.getenv('MEXC_SYMBOL','ETH_USDT').strip().upper()
# This engine processes one symbol. If Railway contains a comma-separated list
# from an older multi-symbol setup, use the first symbol instead of sending the
# whole list as one invalid MEXC symbol.
SYMBOL=next((x.strip() for x in SYMBOL_RAW.split(',') if x.strip()), 'ETH_USDT')
POLL=int(os.getenv('POLL_SECONDS','15'))
STATE_FILE=os.getenv('STATE_FILE','state.json')
FUTURES_URL=f'https://api.mexc.com/api/v1/contract/kline/{SYMBOL}'
SPOT_SYMBOL=SYMBOL.replace('_','')
SPOT_URL='https://api.mexc.com/api/v3/klines'
DATA_SOURCE='FUTURES'

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log=logging.getLogger('eth-bot')

def utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

def kyiv(ts):
    return datetime.fromtimestamp(ts, tz=ZoneInfo('Europe/Kyiv')).strftime('%Y-%m-%d %H:%M')

def color(c):
    return 'GREEN' if c['close'] > c['open'] else 'RED' if c['close'] < c['open'] else 'DOJI'

def load_state():
    try:
        with open(STATE_FILE, encoding='utf8') as f:
            return json.load(f)
    except Exception:
        return {'last_processed_5m': None, 'pending': []}

def save_state(s):
    with open(STATE_FILE + '.tmp', 'w', encoding='utf8') as f:
        json.dump(s, f, indent=2)
    os.replace(STATE_FILE + '.tmp', STATE_FILE)

def tg(text, group_text=None):
    if not TOKEN or not CHAT_IDS:
        log.error('TELEGRAM NOT CONFIGURED')
        return False
    sent=0
    for chat_id in CHAT_IDS:
        send_text = group_text if (group_text is not None and chat_id.startswith('-')) else text
        delivered=False
        for attempt in range(2):
            try:
                r=requests.post(
                    f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                    json={'chat_id': chat_id, 'text': send_text},
                    timeout=15
                )
                if r.ok and r.json().get('ok'):
                    sent += 1
                    delivered=True
                    log.info('TELEGRAM SENT OK | chat=%s', chat_id)
                    break
                if r.status_code == 429 and attempt == 0:
                    try:
                        retry_after=max(1, int(r.json().get('parameters', {}).get('retry_after', 10)))
                    except Exception:
                        retry_after=10
                    log.warning('TELEGRAM RATE LIMIT | chat=%s | retry_after=%ss', chat_id, retry_after)
                    time.sleep(retry_after)
                    continue
                log.error('TELEGRAM ERROR | chat=%s | status=%s body=%s', chat_id, r.status_code, r.text[:300])
                break
            except Exception as e:
                log.exception('TELEGRAM EXCEPTION | chat=%s: %s', chat_id, e)
                break
        if not delivered:
            continue
        time.sleep(1.0)
    return sent == len(CHAT_IDS)

def fetch():
    global DATA_SOURCE
    now=int(time.time())
    start=now-(300*5*60)
    headers={'Cache-Control':'no-cache', 'Pragma':'no-cache', 'User-Agent':'Mozilla/5.0 (compatible; ETHUSDT-5m-Signal-Bot/5.2)'}

    # First try the native MEXC Futures 5m endpoint. If MEXC returns
    # code 1001 for the futures contract, fall back to MEXC Spot 5m data.
    # The signal engine itself is unchanged.
    r=requests.get(
        FUTURES_URL,
        params={'interval':'Min5', 'start':start, 'end':now},
        headers=headers,
        timeout=15
    )
    r.raise_for_status()
    p=r.json()
    data=p.get('data')
    if not data and p.get('code') == 1001:
        if DATA_SOURCE != 'SPOT':
            log.warning('MEXC Futures unavailable for %s (code 1001); switching to MEXC Spot 5m candles for %s', SYMBOL, SPOT_SYMBOL)
        DATA_SOURCE='SPOT'
        r=requests.get(
            SPOT_URL,
            params={'symbol':SPOT_SYMBOL, 'interval':'5m', 'limit':300},
            headers=headers,
            timeout=15
        )
        r.raise_for_status()
        p=r.json()
        data=p
    elif data:
        DATA_SOURCE='FUTURES'

    if not data:
        raise RuntimeError(f'MEXC empty response: {p}')

    out=[]
    if DATA_SOURCE == 'FUTURES':
        if isinstance(data, dict) and isinstance(data.get('time'), list):
            times=data['time']; opens=data.get('open',[]); closes=data.get('close',[])
            for i,t in enumerate(times):
                out.append({'ts':int(t), 'open':float(opens[i]), 'close':float(closes[i])})
        elif isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    out.append({'ts':int(row.get('time',row.get('t'))), 'open':float(row.get('open',row.get('o'))), 'close':float(row.get('close',row.get('c')))})
                else:
                    out.append({'ts':int(row[0]), 'open':float(row[1]), 'close':float(row[4] if len(row) > 4 else row[2])})
    else:
        # Spot V3 kline rows: [open_time, open, high, low, close, volume, ...]
        if not isinstance(data, list):
            raise RuntimeError(f'Unknown MEXC Spot data format: {type(data).__name__}')
        for row in data:
            out.append({'ts':int(row[0])//1000, 'open':float(row[1]), 'close':float(row[4])})

    out.sort(key=lambda x:x['ts'])
    return out

def current_5m(candles):
    """Return the currently forming native 5m MEXC candle."""
    if not candles:
        return None
    return candles[-1]

def agg(candles):
    now=int(time.time())
    return [c for c in candles if c['ts'] + 300 <= now]

class Engine:
    def __init__(self, state):
        self.state=state
        self.c=OrderedDict()
        self.pending=[]
        self.initialized=False
        self.pre_alerted=set()
        self.startup_ts=None
        self.sent_signals=set(self.state.get('sent_signals', []))

    def seed(self, closed):
        """Load current history without generating historical signals/results."""
        self.c=OrderedDict((x['ts'], x) for x in closed[-150:])
        self.c=OrderedDict(sorted(self.c.items()))
        self.pending=[]
        self.state['pending']=[]
        self.state['last_processed_5m']=next(reversed(self.c)) if self.c else None
        self.startup_ts=next(reversed(self.c)) if self.c else None
        # Keep sent-signal deduplication across restarts.
        self.sent_signals=set(self.state.get('sent_signals', []))
        save_state(self.state)
        self.initialized=True
        if self.c:
            latest=next(reversed(self.c.values()))
            log.info('INITIALIZED | history=%d | latest=%s %s | waiting for NEW 5m candle', len(self.c), utc(latest['ts']), color(latest))

    def maybe_pre_alert(self, live10):
        """At ~2 minutes before trigger close, warn when the live trigger is opposite the start."""
        if not self.initialized or not live10:
            return
        # Only send during the final ~2 minutes of the current 5m candle.
        now=time.time()
        elapsed=now-live10['ts']
        if elapsed < 480 or elapsed >= 600:
            return

        keys=list(self.c)
        # Need the five prior closed candles plus the start candle; trigger is live.
        # Current live candle is candle #6 after the candidate start.
        target_ts=live10['ts']
        if target_ts in self.c:
            return
        s_ts=target_ts-6*300
        if s_ts not in self.c:
            return
        sidx=keys.index(s_ts)
        start=self.c[s_ts]
        sc=color(start)
        if sc not in ('GREEN','RED'):
            return
        # Start must be the last candle of its same-color run.
        if sidx+1 < len(keys) and color(self.c[keys[sidx+1]]) == sc:
            return

        trig=color(live10)
        if trig not in ('GREEN','RED') or trig == sc:
            return
        if target_ts in self.pre_alerted:
            return

        log.info('PRE-SIGNAL | start=%s %s | live trigger=%s %s | ~2m left', utc(start['ts']), sc, utc(target_ts), trig)
        group_text=('**Всі готові?**\n'
                     '**Скоро дам СИГНАЛ!**\n\n'
                     f'{SPOT_SYMBOL} Futures\n'
                     'Timeframe: 5m\n\n'
                     '⚠️ Сигнал буде тільки після закриття свічки.')
        # Pre-signal announcement is intended for the Telegram group only.
        tg('', group_text)
        self.pre_alerted.add(target_ts)

    def ingest_new(self, closed):
        if not self.initialized:
            self.seed(closed)
            return 0
        # MEXC returns a rolling history on every poll. The engine keeps only
        # the latest 150 candles, so comparing against all returned timestamps
        # would re-add ~149 old candles every 15 seconds. Only ingest candles
        # strictly newer than the newest candle already stored.
        last_ts=next(reversed(self.c)) if self.c else None
        new=[x for x in closed if last_ts is None or x['ts'] > last_ts]
        for x in new:
            self.c[x['ts']]=x
        self.c=OrderedDict(sorted(self.c.items()))
        # Evaluate newly arrived candles before trimming history.
        # This prevents a batch of >150 unseen candles from removing an
        # earlier new candle and causing keys.index(ts) to fail.
        for x in new:
            # Never evaluate candles that belong to the startup history window.
            # Only candles that arrived after the initial seed may create signals.
            if self.startup_ts is None or x['ts'] > self.startup_ts:
                self.evaluate(x['ts'])
        while len(self.c)>150:
            self.c.popitem(last=False)
        if new:
            self.state['pending']=self.pending
            self.state['last_processed_5m']=new[-1]['ts']
            save_state(self.state)
        return len(new)

    def evaluate(self, ts):
        keys=list(self.c)
        idx=keys.index(ts)
        cur=self.c[ts]

        # Existing pending signals: control candles 7..13 correspond to rel 1..7.
        keep=[]
        for p in self.pending:
            rel=idx-p['trigger_idx']
            if rel<1:
                keep.append(p)
                continue
            want='GREEN' if p['direction']=='LONG' else 'RED'
            if color(cur)==want:
                log.info('RESULT WIN | %s | start=%s | control=%d', p['direction'], utc(p['start_ts']), rel)
                tg(f'WIN\n{SPOT_SYMBOL} Futures\nDirection: {p["direction"]}\nControl candle: {rel}/7\n\nТрейдер Василь Павлів\n@vasylpavliv\nhttps://t.me/vasylpavliv')
            elif rel>=7:
                log.info('RESULT LOSS | %s | start=%s', p['direction'], utc(p['start_ts']))
                tg(f'LOSS\n{SPOT_SYMBOL} Futures\nDirection: {p["direction"]}\nNo confirmation in candles 7-13\n\nТрейдер Василь Павлів\n@vasylpavliv\nhttps://t.me/vasylpavliv')
            else:
                keep.append(p)
        self.pending=keep

        # Start can be either:
        #   1) an isolated GREEN/RED candle, or
        #   2) the LAST candle of a consecutive run of the same color.
        #
        # Trigger is exactly the 6th subsequent candle. The first five
        # subsequent candles may be ANY color (except that a DOJI is not
        # GREEN/RED). Only candle #6 determines the signal direction.
        if idx<6:
            return
        sidx=idx-6
        start=self.c[keys[sidx]]
        sc=color(start)
        if sc not in ('GREEN','RED'):
            return

        # If the next candle has the same color, this is NOT the last candle
        # of the run, so this candidate start is ignored. If the next candle
        # is opposite color or DOJI, this candle IS the last one of its run.
        if sidx+1 < len(keys) and color(self.c[keys[sidx+1]]) == sc:
            return

        trig=color(self.c[keys[idx]])
        direction='LONG' if sc=='GREEN' and trig=='RED' else 'SHORT' if sc=='RED' and trig=='GREEN' else None
        if not direction:
            return
        if any(p['start_ts']==start['ts'] for p in self.pending):
            return
        # Hard deduplication: one signal per trigger candle, even after
        # pending resolves or the process restarts.
        if ts in self.sent_signals:
            return
        self.sent_signals.add(ts)
        self.state['sent_signals']=sorted(self.sent_signals)[-300:]
        save_state(self.state)
        log.info('SIGNAL %s | start=%s %s | trigger=%s %s', direction, utc(start['ts']), sc, utc(ts), trig)
        signal_text=(f'SIGNAL {direction}\n\n{SPOT_SYMBOL} Futures\nTimeframe: 5m\nStart: {kyiv(start["ts"])} Kyiv time\nTrigger: candle 6\n\nSignal only - no automatic trading.\n\nТрейдер Василь Павлів\n@vasylpavliv\nhttps://t.me/vasylpavliv')
        signal_group_text=(f'SIGNAL {direction}\n\n{SPOT_SYMBOL} Futures\nTimeframe: 5m\nStart: {kyiv(start["ts"])} Kyiv time\n\nSignal only - no automatic trading.\n\nТрейдер Василь Павлів\n@vasylpavliv\nhttps://t.me/vasylpavliv')
        tg(signal_text, signal_group_text)
        self.pending.append({'start_ts':start['ts'], 'trigger_idx':idx, 'direction':direction})

state=load_state()
engine=Engine(state)

def main():
    log.info('Started %s 5m signal bot v4-fixed (MEXC REST)', SYMBOL)
    log.info('Config: poll=%ss, chats=%d, token_configured=%s', POLL, len(CHAT_IDS), bool(TOKEN))
    if TOKEN and CHAT_IDS:
        tg(f'BOT ONLINE\n{SPOT_SYMBOL} Futures\nSignal bot is active.\nThis test confirms Telegram delivery to all configured chats.')
    last_log=0
    while True:
        try:
            mins=fetch()
            live5=current_5m(mins)
            if live5:
                engine.maybe_pre_alert(live5)
            closed=agg(mins)
            if not closed:
                log.warning('MEXC OK but no closed 5m candles yet')
            else:
                latest=closed[-1]
                n=engine.ingest_new(closed)
                if n:
                    log.info('NEW DATA | MEXC %s 5m=%d | closed_5m=%d | added=%d | latest=%s %s | O=%.4f C=%.4f | pending=%d', DATA_SOURCE, len(mins), len(closed), n, utc(latest['ts']), color(latest), latest['open'], latest['close'], len(engine.pending))
                elif time.time()-last_log>=60:
                    age=int(time.time()-(latest['ts']+300))
                    log.info('HEARTBEAT OK | MEXC %s 5m=%d | closed_5m=%d | latest=%s %s | age=%ss | pending=%d', DATA_SOURCE, len(mins), len(closed), utc(latest['ts']), color(latest), max(age,0), len(engine.pending))
                    last_log=time.time()
        except Exception as e:
            log.exception('LOOP ERROR: %s', e)
        time.sleep(POLL)

if __name__=='__main__':
    main()