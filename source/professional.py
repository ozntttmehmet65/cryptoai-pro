"""Predeclared V7.2 evidence screens and causal market regimes.

These screens may veto. They do not tune thresholds on unseen outcomes and
do not establish profitability or independent market observations.
"""
import math
from statistics import NormalDist
import numpy as np
from quality_guard import wilson_lower

POLICY = 'professional_v72'
HISTORY_TARGET = 18000
MIN_HISTORY = 9000
FAMILY_TESTS = 1400  # Fixed 700 contracts * two directional tests per cycle.
FAMILY_ALPHA = .05
MIN_INDEPENDENT = 12
DAY_MS = 86400000


def market_regimes(frame):
    """Fixed rules use each row's already closed 15m and 1h/4h features."""
    atr=frame['atr_pct'].to_numpy(dtype=float)
    vol=frame['vol24'].to_numpy(dtype=float)
    adx=frame['adx14'].to_numpy(dtype=float)
    trend=frame['h4_trend'].to_numpy(dtype=float)
    return np.select([(atr>=.06)|(vol>=.03), (adx>=.25)&(trend>0),
                      (adx>=.25)&(trend<0)],
                     ['EXTREME','UPTREND','DOWNTREND'], default='RANGE')


def market_condition(frame):
    last=frame.iloc[-1]
    name=str(market_regimes(frame.iloc[-1:])[0])
    return {'name':name, 'label':{'EXTREME':'Aşırı oynaklık','UPTREND':'Yükseliş trendi',
             'DOWNTREND':'Düşüş trendi','RANGE':'Yatay / karma'}[name],
            'atr_pct':float(last['atr_pct']), 'volatility_15m':float(last['vol24']),
            'adx':float(last['adx14']), 'h4_trend':float(last['h4_trend']),
            'extreme':name=='EXTREME', 'rule_source':'fixed_closed_candle_rules_v72'}


def professional_audit(actions, outcomes, atrs, times, direction, tp, sl, regimes, history_count):
    selected=[];last=-10**20
    for i,a in enumerate(actions):
        if a==direction and int(times[i])-last>=DAY_MS:
            selected.append(i);last=int(times[i])
    resolved=[i for i in selected if outcomes[i] in (-1,1)]
    wins=sum(outcomes[i]==1 for i in resolved)
    n=len(resolved)
    returns=[(tp if outcomes[i]==1 else -sl)*float(atrs[i])-.004 for i in resolved]
    # Conservative common null: exceed the WORST stressed break-even across
    # sampled brackets. No choosing favourable events after observing outcomes.
    null=max([(sl*float(atrs[i])+.004)/((tp+sl)*float(atrs[i])) for i in resolved],default=1.)
    p=None
    if n and 0<null<1:
        p=sum(math.comb(n,k)*null**k*(1-null)**(n-k) for k in range(wins,n+1))
    adjusted=min(1., p*FAMILY_TESTS) if p is not None else None
    z=NormalDist().inv_cdf(1-FAMILY_ALPHA/FAMILY_TESTS)
    lower=wilson_lower(wins,n,z) if n else None
    thirds=[]
    for part in range(3):
        start=len(actions)*part//3;end=len(actions)*(part+1)//3
        indices=[i for i in resolved if start<=i<end]
        ns=[(tp if outcomes[i]==1 else -sl)*float(atrs[i])-.004 for i in indices]
        thirds.append({'segment':part+1,'start':int(times[start]) if end>start else None,
            'end':int(times[end-1]) if end>start else None,'independent_resolved':len(indices),
            'mean_stressed_net':sum(ns)/len(ns) if ns else None})
    regimes_report={}
    for regime in ('UPTREND','DOWNTREND','RANGE','EXTREME'):
        indices=[i for i in resolved if regimes[i]==regime]
        ns=[(tp if outcomes[i]==1 else -sl)*float(atrs[i])-.004 for i in indices]
        mean=sum(ns)/len(ns) if ns else None
        regimes_report[regime]={'independent_resolved':len(indices),'mean_stressed_net':mean,
            'allowed':regime!='EXTREME' and len(indices)>=4 and mean is not None and mean>0}
    checks={'history':history_count>=MIN_HISTORY,
            'independent_events':n>=MIN_INDEPENDENT,
            'resolution':bool(selected) and n/len(selected)>=.85,
            'family_screen':adjusted is not None and adjusted<=FAMILY_ALPHA,
            'stressed_lower_bound':lower is not None and lower>null,
            'stressed_mean':bool(returns) and sum(returns)/len(returns)>0,
            'period_consistency':all(s['independent_resolved']>=2 and
                s['mean_stressed_net'] is not None and s['mean_stressed_net']>0 for s in thirds)}
    return {'policy':POLICY,'passed':all(checks.values()),'checks':checks,
            'history_count':int(history_count),'history_target':HISTORY_TARGET,
            'independent_candidates':len(selected),'independent_resolved':n,'independent_tp':int(wins),
            'stressed_null_tp_rate':null,'one_sided_p':p,'family_adjusted_p':adjusted,
            'family_tests':FAMILY_TESTS,'family_alpha':FAMILY_ALPHA,
            'family_adjusted_wilson_lower':lower,'chronological_periods':thirds,
            'regimes':regimes_report,'threshold_source':'predeclared_not_holdout_tuned',
            'notice':'İhtiyatlı dönem ve çoklu seçim taraması; binom/Wilson bağımsızlık varsayar. 24s aralık kalan bağımlılığı gidermez, tekrarlanan eğitim turları için toplam hata garantisi değildir.'}


def correlation_groups(histories, sides, threshold=.85):
    """Same-direction exposures, matched closed timestamps, no missing-row fill."""
    returns={}
    for symbol,bars in histories.items():
        series={}
        for previous,current in zip(bars,bars[1:]):
            if current['time']-previous['time']!=900000:continue
            if previous['close']<=0 or current['close']<=0:continue
            series[current['time']]=math.log(current['close']/previous['close'])*(1 if sides[symbol]=='LONG' else -1)
        returns[symbol]=series
    parents={s:s for s in returns};pairs=[];unknown=0
    def root(s):
        while parents[s]!=s:s=parents[s]
        return s
    symbols=sorted(returns)
    for j,a in enumerate(symbols):
        for b in symbols[j+1:]:
            times=sorted(set(returns[a])&set(returns[b]))
            if len(times)<64:unknown+=1;continue
            x=np.array([returns[a][t] for t in times]);y=np.array([returns[b][t] for t in times])
            if x.std()<1e-12 or y.std()<1e-12:unknown+=1;continue
            corr=float(np.corrcoef(x,y)[0,1])
            if corr>=threshold:
                parents[root(b)]=root(a)
                pairs.append({'a':a,'b':b,'exposure_correlation':round(corr,4),'matched_returns':len(times)})
    groups={}
    for s in symbols:groups.setdefault(root(s),[]).append({'symbol':s,'direction':sides[s]})
    return {'groups':[members for members in groups.values() if len(members)>1],
            'pairs':sorted(pairs,key=lambda p:-p['exposure_correlation'])[:40],
            'unknown_pairs':unknown,'tested_symbols':len(symbols),'threshold':threshold,
            'notice':'Son kapanmış mumlarda ortak yön maruziyeti; kümeler bağımsız işlem fırsatları sayılmaz. Gruplama emir vermez ve sinyal onayı sağlamaz.'}
