"""Persistent Istanbul-day decision reports and forward 24h score diagnostics."""
import json
import math
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from quality_guard import wilson_lower

TZ=ZoneInfo('Europe/Istanbul')


def setup(db):
    db.execute('''CREATE TABLE IF NOT EXISTS decision_audits (
      symbol TEXT NOT NULL,candle_time INTEGER NOT NULL,model_version TEXT NOT NULL,
      observed_at INTEGER NOT NULL,signal TEXT,regime TEXT,status TEXT NOT NULL,reasons TEXT NOT NULL,
      consensus INTEGER NOT NULL,PRIMARY KEY(symbol,candle_time,model_version))''')
    db.execute('CREATE INDEX IF NOT EXISTS audit_time ON decision_audits(observed_at)')
    db.execute('CREATE TABLE IF NOT EXISTS daily_quality (day TEXT PRIMARY KEY,updated_at INTEGER NOT NULL,report TEXT NOT NULL)')
    db.commit()


def record(db,symbol,pred,now_ms):
    side=pred.get('candidate_signal') or pred.get('signal')
    if side not in ('LONG','SHORT'):side=None
    check=pred.get('execution_quality') or {}
    reasons=list(dict.fromkeys(check.get('reasons') or ([pred['blocked_reason']] if pred.get('blocked_reason') else [])))
    votes=((pred.get('ensemble') or {}).get(side) or {})
    status='APPROVED' if pred.get('signal') in ('LONG','SHORT') and check.get('approved') else 'BLOCKED' if side else 'WAIT'
    db.execute('''INSERT INTO decision_audits VALUES (?,?,?,?,?,?,?,?,?)
      ON CONFLICT(symbol,candle_time,model_version) DO UPDATE SET
       signal=excluded.signal,regime=excluded.regime,status=excluded.status,reasons=excluded.reasons,consensus=excluded.consensus''',
       (symbol,int(pred['candle_time']),str(pred['model_version']),now_ms,side,
        (pred.get('market_condition') or {}).get('name'),status,json.dumps(reasons,ensure_ascii=False),int(votes.get('agreement') is True)))
    db.commit()


def calibration(db,now_ms):
    # A no-expiry position closing AFTER 24h does not validate a conditional
    # 24h model score. Open mature windows stay censored, never become invented SL.
    rows=db.execute('SELECT confidence,observed_at,result_at,exit_reason,state FROM executable_paper WHERE shadow=0').fetchall()
    resolved=[];censored=unverified=immature=0
    for score,entry,exit_at,reason,state in rows:
        if state=='VERI_EKSIK':unverified+=1;continue
        if (exit_at and 0<exit_at-entry<=86400000 and reason in ('TP','SL')
            and state in ('CLOSED','AWAITING_FUNDING')):
            resolved.append((float(score),int(reason=='TP')))
        elif now_ms-entry>=86400000:censored+=1
        else:immature+=1
    bins=[]
    for low in (0.,.2,.4,.6,.8):
        group=[(p,y) for p,y in resolved if low<=p<(low+.2 if low<.8 else 1.000001)]
        if group:
            wins=sum(y for _,y in group);n=len(group)
            bins.append({'from':low,'to':round(low+.2,1),'samples':n,'mean_score':sum(p for p,_ in group)/n,
                         'tp_rate':wins/n,'wilson_lower_95':wilson_lower(wins,n),
                         'status':'AZ ÖRNEK' if n<10 else 'BETİMSEL ÖLÇÜM'})
    n=len(resolved)
    return {'resolved_within_24h':n,'censored_24h':censored,'unverified':unverified,'immature':immature,
        'brier':sum((p-y)**2 for p,y in resolved)/n if n else None,
        'ece':sum(b['samples']*abs(b['mean_score']-b['tp_rate']) for b in bins)/n if n else None,
        'bins':bins,'status':'ÖRNEK AZ' if n<40 else 'BETİMSEL ÖLÇÜM',
        'probability_meaning':'TP first conditional on observable TP/SL resolution within 24h',
        'used_for_retraining':False,
        'notice':'Onaylı ileri gözlemler; gölge adayları hariç. ECE/Brier ve Wilson bağımlılık/örnek azlığına duyarlıdır; canlı başarı garantisi veya otomatik eşik ayarı değildir.'}


def daily(db,now_ms):
    now=datetime.fromtimestamp(now_ms/1000,TZ);day=now.date().isoformat()
    start=int(now.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()*1000)
    rows=db.execute('SELECT symbol,status,regime,reasons,consensus FROM decision_audits WHERE observed_at>=? AND observed_at<=?',(start,now_ms)).fetchall()
    reasons=Counter();regimes=Counter();statuses=Counter();symbols=set();disagreement=0
    for symbol,status,regime,reasons_json,consensus in rows:
        symbols.add(symbol);statuses[status]+=1
        if regime:regimes[regime]+=1
        for reason in json.loads(reasons_json):reasons[reason]+=1
        if status=='BLOCKED' and not consensus:disagreement+=1
    exits=db.execute("SELECT exit_reason,net_return,state FROM executable_paper WHERE shadow=0 AND result_at>=? AND result_at<=? ORDER BY result_at,symbol,candle_time,model_version",(start,now_ms)).fetchall()
    closed=[r for r in exits if r[2]=='CLOSED'];loss_run=max_loss_run=0
    for reason,net,state in closed:
        loss_run=loss_run+1 if net<0 else 0;max_loss_run=max(max_loss_run,loss_run)
    entered=db.execute('SELECT COUNT(*) FROM executable_paper WHERE shadow=0 AND observed_at>=? AND observed_at<=?',(start,now_ms)).fetchone()[0]
    return {'day':day,'timezone':'Europe/Istanbul','as_of':now.isoformat(),
        'decisions':len(rows),'symbols':len(symbols),'approved_decisions':statuses['APPROVED'],
        'blocked_candidates':statuses['BLOCKED'],'wait_decisions':statuses['WAIT'],
        'disagreement_candidates':disagreement,'regimes':dict(regimes),
        'veto_reasons':[{'reason':r,'count':n} for r,n in reasons.most_common(10)],
        'new_approved_observations':entered,'closed_today':len(closed),
        'tp_today':sum(r[0]=='TP' for r in closed),'sl_today':sum(r[0]=='SL' for r in closed),
        'funding_pending_exits_today':sum(r[2]=='AWAITING_FUNDING' for r in exits),
        'mean_net_closed_today':sum(r[1] for r in closed)/len(closed) if closed else None,
        'max_negative_sequence_today':max_loss_run,
        'notice':'Kararlar coin/mum/model kimliğiyle tekilleştirilir; onay sonradan oluşursa kayıt güncellenir. Bugün kapananlar önceki gün açılmış olabilir. Sinyal sayısı bağımsız kanıt veya portföy getirisi değildir.'}


def snapshot(db,now_ms):
    report=daily(db,now_ms)
    current=datetime.fromtimestamp(now_ms/1000,TZ)
    day_start=int(current.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()*1000)
    # Rebuild six completed calendar days: a midnight boundary or funding
    # received later must not permanently leave yesterday's snapshot incomplete.
    reports=[report]+[daily(db,day_start-(i-1)*86400000-1) for i in range(1,7)]
    for item in reports:
        item['generated_at']=current.isoformat()
        db.execute('INSERT INTO daily_quality VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET updated_at=excluded.updated_at,report=excluded.report',
                   (item['day'],now_ms,json.dumps(item,ensure_ascii=False,allow_nan=False)))
    db.execute('DELETE FROM decision_audits WHERE observed_at<?',(now_ms-14*86400000,))
    db.execute('DELETE FROM daily_quality WHERE updated_at<?',(now_ms-90*86400000,))
    db.commit();return report


def report(db,now_ms):
    previous=db.execute('SELECT report FROM daily_quality ORDER BY day DESC LIMIT 7').fetchall()
    return {'daily':daily(db,now_ms),'calibration':calibration(db,now_ms),
            'daily_history':[json.loads(r[0]) for r in previous],
            'report_policy':'Istanbul calendar day; last seven days refreshed for late funding; 14-day decision retention; 90-day daily snapshots'}
