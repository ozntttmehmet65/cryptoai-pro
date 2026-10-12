"""V7.2 bid/ask hypothetical positions, explicit costs and observed funding.

No order is sent. Fee and slippage rates are assumptions, not account fees or
measured fills. Funding must be covered by a successful historical API response
before a net result is reported. Old last-trade observations remain untouched.
"""
import json
import math
from datetime import datetime, timezone
from quality_guard import wilson_lower
from paper_trade import _bracket
from execution_policy import POLICY_VERSION, PAPER_NOTIONAL_USDT

METHOD='bid_ask_funding_v72'
FEE_PER_LEG=.0006
SLIPPAGE_PER_LEG=.0002
NOTIONAL=PAPER_NOTIONAL_USDT
MAX_GAP_MS=180000


def setup(db):
    db.execute('''CREATE TABLE IF NOT EXISTS executable_paper (
      symbol TEXT NOT NULL,candle_time INTEGER NOT NULL,model_version TEXT NOT NULL,
      signal TEXT NOT NULL,confidence REAL NOT NULL,observed_at INTEGER NOT NULL,
      entry_price REAL NOT NULL,stop_price REAL NOT NULL,target_price REAL NOT NULL,
      shadow INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'OPEN',
      last_quote_ms INTEGER NOT NULL,result_at INTEGER,result_price REAL,exit_reason TEXT,
      gross_return REAL,fee_return REAL,funding_return REAL,net_return REAL,
      funding_covered_until INTEGER NOT NULL,funding_requested_at INTEGER NOT NULL DEFAULT 0,
      fee_rate REAL NOT NULL,slippage_rate REAL NOT NULL,regime TEXT,entry_audit TEXT,
      PRIMARY KEY(symbol,candle_time,model_version))''')
    db.execute('CREATE INDEX IF NOT EXISTS executable_open ON executable_paper(state,symbol)')
    db.execute('CREATE INDEX IF NOT EXISTS executable_result_at ON executable_paper(result_at)')
    db.execute('CREATE INDEX IF NOT EXISTS executable_observed_at ON executable_paper(observed_at)')
    db.execute('''CREATE TABLE IF NOT EXISTS paper_funding (
      symbol TEXT NOT NULL,event_ms INTEGER NOT NULL,rate_type TEXT NOT NULL,
      rate REAL NOT NULL,mark_price REAL NOT NULL,
      PRIMARY KEY(symbol,event_ms,rate_type))''')
    db.commit()


def valid_book(book, now_ms):
    try:
        bid,ask,bq,aq,stamp=[float(book[k]) for k in ('bid','ask','bid_qty','ask_qty','updated_at')]
        if not all(math.isfinite(v) for v in (bid,ask,bq,aq,stamp)):return None
        if not (0<bid<=ask and bq>0 and aq>0 and 0<=now_ms-stamp*1000<12000):return None
        return bid,ask,bq,aq,int(stamp*1000)
    except (TypeError,KeyError,ValueError,OverflowError):return None


def observe(db,symbol,signal,book,now_ms,*,shadow=False):
    side=signal.get('signal');b=valid_book(book,now_ms)
    if side not in ('LONG','SHORT') or not b:return False
    if not shadow and not (signal.get('execution_quality') or {}).get('approved'):return False
    try:
        confidence=float(signal['p_long' if side=='LONG' else 'p_short'])
        candle=int(signal['candle_time']);version=str(signal['model_version'])
        atr=float(signal['risk_atr_pct']);tp=float(signal['tp_atr']);sl=float(signal['sl_atr'])
    except (ValueError,TypeError,KeyError,OverflowError):return False
    if not math.isfinite(confidence) or not 0<=confidence<=1 or not version:return False
    if not 0<=now_ms-(candle+900000)<=900000:return False
    bid,ask,bq,aq,event=b
    if min(bid*bq,ask*aq)<NOTIONAL:return False
    entry=(ask*(1+SLIPPAGE_PER_LEG) if side=='LONG' else bid*(1-SLIPPAGE_PER_LEG))
    bracket=_bracket(side,entry,atr,tp,sl)
    if bracket is None:return False
    if db.execute("SELECT 1 FROM executable_paper WHERE symbol=? AND shadow=? AND state IN ('OPEN','AWAITING_FUNDING') LIMIT 1",(symbol,int(shadow))).fetchone():return False
    if shadow:version+='-shadow-v72'
    before=db.total_changes
    db.execute('''INSERT OR IGNORE INTO executable_paper
      (symbol,candle_time,model_version,signal,confidence,observed_at,entry_price,stop_price,target_price,
       shadow,last_quote_ms,funding_covered_until,fee_rate,slippage_rate,regime,entry_audit)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
      (symbol,candle,version,side,confidence,now_ms,entry,*bracket,int(shadow),now_ms,now_ms,
       FEE_PER_LEG,SLIPPAGE_PER_LEG,(signal.get('market_condition') or {}).get('name'),
       json.dumps(signal.get('execution_quality') or {},ensure_ascii=False,allow_nan=False)))
    db.commit();return db.total_changes>before


def reconcile_books(db,books,now_ms):
    rows=db.execute("SELECT symbol,candle_time,model_version,signal,observed_at,entry_price,stop_price,target_price,last_quote_ms,slippage_rate,fee_rate FROM executable_paper WHERE state='OPEN'").fetchall()
    changed=0
    for symbol,candle,version,side,observed,entry,stop,target,last,slip,fee in rows:
        book=valid_book(books.get(symbol),now_ms)
        if not book:continue
        bid,ask,bq,aq,event=book
        if event<=last:continue
        key=(symbol,candle,version)
        if event-last>MAX_GAP_MS:
            db.execute("UPDATE executable_paper SET state='VERI_EKSIK',exit_reason='BOOK_GAP',last_quote_ms=? WHERE symbol=? AND candle_time=? AND model_version=? AND state='OPEN'",(event,*key));changed+=1;continue
        price=bid if side=='LONG' else ask
        tp=price>=target if side=='LONG' else price<=target
        sl=price<=stop if side=='LONG' else price>=stop
        if tp or sl:
            exit_price=price*(1-slip if side=='LONG' else 1+slip)
            if tp:exit_price=min(target,exit_price) if side=='LONG' else max(target,exit_price)
            # If sufficient displayed size is missing, execution is unknown;
            # do not award a touch as a filled TP.
            quantity=NOTIONAL/entry
            if (bq if side=='LONG' else aq)<quantity:
                db.execute("UPDATE executable_paper SET state='VERI_EKSIK',exit_reason='EXIT_SIZE_UNKNOWN',last_quote_ms=? WHERE symbol=? AND candle_time=? AND model_version=?",(event,*key));changed+=1;continue
            gross=(1 if side=='LONG' else -1)*(exit_price/entry-1)
            fees=fee*(1+exit_price/entry)
            db.execute("""UPDATE executable_paper SET state='AWAITING_FUNDING',result_at=?,result_price=?,exit_reason=?,gross_return=?,fee_return=?,last_quote_ms=?
              WHERE symbol=? AND candle_time=? AND model_version=? AND state='OPEN'""",
              (event,exit_price,'TP' if tp else 'SL',gross,fees,event,*key));changed+=1
        else:
            db.execute('UPDATE executable_paper SET last_quote_ms=? WHERE symbol=? AND candle_time=? AND model_version=?',(event,*key))
    db.commit();return changed


def funding_jobs(db,now_ms,limit=2):
    rows=db.execute("""SELECT symbol,MIN(funding_covered_until),MAX(COALESCE(result_at,?)),MIN(funding_requested_at)
      FROM executable_paper WHERE state IN ('OPEN','AWAITING_FUNDING')
      GROUP BY symbol ORDER BY MIN(funding_requested_at) LIMIT ?""",(now_ms,limit)).fetchall()
    return [(s,start+1,min(end,now_ms-120000,start+7*86400000)) for s,start,end,last in rows
            if min(end,now_ms-120000)>start and now_ms-last>=300000]


def apply_funding(db,symbol,start_ms,end_ms,events,now_ms):
    """Only complete, symbol-specific, ascending API pages establish coverage."""
    if not isinstance(events,list) or len(events)>=1000 or not 0<start_ms<=end_ms<=now_ms:return False
    parsed=[]
    try:
        for e in events:
            at=int(e['fundingTime']);rate=float(e['fundingRate']);mark=float(e['markPrice'])
            kind=e.get('rateType','Regular')
            if e.get('symbol')!=symbol or not start_ms<=at<=end_ms or kind not in ('Regular','Special'):return False
            if not math.isfinite(rate) or not math.isfinite(mark) or abs(rate)>1 or mark<=0:return False
            parsed.append((symbol,at,kind,rate,mark))
    except (TypeError,ValueError,KeyError,OverflowError):return False
    if any(b[1]<a[1] for a,b in zip(parsed,parsed[1:])):return False
    for event in parsed:db.execute('INSERT OR IGNORE INTO paper_funding VALUES (?,?,?,?,?)',event)
    # Require overlap with prior certified coverage; don't bridge a missed interval.
    db.execute("""UPDATE executable_paper SET funding_covered_until=MAX(funding_covered_until,?),funding_requested_at=?
      WHERE symbol=? AND state IN ('OPEN','AWAITING_FUNDING') AND funding_covered_until>=?-1""",(end_ms,now_ms,symbol,start_ms))
    rows=db.execute("""SELECT candle_time,model_version,signal,entry_price,observed_at,result_at,gross_return,fee_return
      FROM executable_paper WHERE symbol=? AND state='AWAITING_FUNDING' AND funding_covered_until>=result_at""",(symbol,)).fetchall()
    for candle,version,side,entry,observed,exit_at,gross,fees in rows:
        amount=db.execute('SELECT COALESCE(SUM(rate*mark_price),0) FROM paper_funding WHERE symbol=? AND event_ms>? AND event_ms<=?',(symbol,observed,exit_at)).fetchone()[0]
        funding=-(1 if side=='LONG' else -1)*amount/entry
        net=gross-fees+funding
        db.execute("UPDATE executable_paper SET state='CLOSED',funding_return=?,net_return=? WHERE symbol=? AND candle_time=? AND model_version=? AND state='AWAITING_FUNDING'",(funding,net,symbol,candle,version))
    db.commit();return True


def report(db,limit=100,historical=None):
    db.row_factory=None
    keys=['symbol','signal','confidence','observed_at','entry_price','result_at','result_price','net_return','state','stop_price','target_price','exit_reason','last_quote_ms','shadow','gross_return','fee_return','funding_return','model_version','regime']
    data=db.execute('SELECT '+','.join(keys)+' FROM executable_paper ORDER BY observed_at DESC LIMIT ?',(limit,)).fetchall()
    rows=[]
    for values in data:
        r=dict(zip(keys,values));r['settlement_method']=METHOD;r['legacy']=False
        for k in ('observed_at','result_at'):r[k]=datetime.fromtimestamp(r[k]/1000,timezone.utc).isoformat() if r[k] else None
        r['last_quote_at']=datetime.fromtimestamp(r.pop('last_quote_ms')/1000,timezone.utc).isoformat();rows.append(r)
    all_rows=db.execute('SELECT signal,state,exit_reason,net_return,shadow,entry_audit FROM executable_paper').fetchall()
    approved=[r for r in all_rows if not r[4]];shadow=[r for r in all_rows if r[4]]
    closed=[r for r in approved if r[1]=='CLOSED'];tp=sum(r[2]=='TP' for r in closed);sl=sum(r[2]=='SL' for r in closed)
    states={}
    for r in approved:states[r[1]]=states.get(r[1],0)+1
    directional={}
    for side in ('LONG','SHORT'):
        ss=[r for r in approved if r[0]==side];cs=[r for r in ss if r[1]=='CLOSED']
        hits=sum(r[2]=='TP' for r in cs);gain=sum(max(0,r[3]) for r in cs);loss=sum(max(0,-r[3]) for r in cs)
        directional[side]={'observed':len(ss),'settled':len(cs),'unverified':sum(r[1]=='VERI_EKSIK' for r in ss),
            'tp_first_rate':hits/len(cs) if cs else None,'tp_wilson_lower_95':wilson_lower(hits,len(cs)) if cs else None,
            'mean_net_return':sum(r[3] for r in cs)/len(cs) if cs else None,'profit_factor':gain/loss if loss else None,
            'profit_factor_status':'measured' if loss else 'no_losses' if gain else 'no_results',
            'evidence_status':'YETERLİ ÖRNEK' if len(cs)>=40 else 'ÖRNEK AZ'}
    shadow_closed=[r for r in shadow if r[1]=='CLOSED']
    h=historical or {}; old=h.get('summary') or {}
    summary={'observed':len(approved),'settled':len(closed),'winning':sum(r[3]>0 for r in closed),
        'tp_count':tp,'sl_count':sl,'win_rate':sum(r[3]>0 for r in closed)/len(closed) if closed else None,
        'mean_net_return':sum(r[3] for r in closed)/len(closed) if closed else None,'by_state':states,
        'legacy_observations_excluded':old.get('observed',0)+old.get('legacy_observations_excluded',0)+old.get('shadow_observations',0),
        'shadow_observations':len(shadow),'shadow_settled':len(shadow_closed),
        'shadow_tp_count':sum(r[2]=='TP' for r in shadow_closed),'shadow_sl_count':sum(r[2]=='SL' for r in shadow_closed),
        'shadow_unverified':sum(r[1]=='VERI_EKSIK' for r in shadow)}
    policies={}
    for row in approved:
        try:
            audit=json.loads(row[5] or '{}')
            policy=audit.get('policy_version','V7.2') if isinstance(audit,dict) else 'V7.2'
            if not isinstance(policy,str) or not policy:policy='V7.2'
        except (ValueError,TypeError):policy='V7.2'
        group=policies.setdefault(policy,{'observed':0,'closed':0,'tp':0,'unverified':0})
        group['observed']+=1
        group['closed']+=int(row[1]=='CLOSED')
        group['tp']+=int(row[1]=='CLOSED' and row[2]=='TP')
        group['unverified']+=int(row[1]=='VERI_EKSIK')
    return {'method':METHOD,'summary':summary,'directional':directional,'rows':rows,
        'current_entry_policy':POLICY_VERSION,'entry_policy_summary':policies,
        'historical_last_trade_summary':old,'historical_directional':h.get('directional',{}),
        'cost_assumptions':{'fee_per_leg':FEE_PER_LEG,'adverse_slippage_per_leg':SLIPPAGE_PER_LEG,'hypothetical_notional':NOTIONAL,
            'funding':'Settled historical rates and their mark prices; entry < funding event <= exit'},
        'disclaimer':'Alış/satış örneklerinden sanal takip. Spread fiyatlarda; komisyon ve kayma varsayım, funding gerçekleşmiş resmî veriden hesaplanır. Funding kapsamı doğrulanmadan net sonuç yok. Gerçek emir dolumu veya kâr kanıtı değildir.'}
