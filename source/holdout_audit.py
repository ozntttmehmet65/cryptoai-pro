"""Frozen holdout diagnostics, not parameters selected on test data."""
from quality_guard import wilson_lower


def audit(actions, outcomes, atrs, times, direction, tp, sl, probabilities):
    selected=[]; last=-10**20
    for i,action in enumerate(actions):
        if action==direction and int(times[i])-last>=86_400_000:
            selected.append(i);last=int(times[i])
    # Space ALL candidates before resolving labels: unresolved trials cannot be
    # silently skipped to select a more favourable next observation.
    resolved=[i for i in selected if outcomes[i] in (-1,1)]
    nets=[(tp if outcomes[i]==1 else -sl)*float(atrs[i])-.004 for i in resolved]
    wins=sum(outcomes[i]==1 for i in resolved)
    bins=[]
    for low in (0.,.2,.4,.6,.8):
        rows=[i for i,y in enumerate(outcomes) if y in (-1,1)
              and low<=float(probabilities[i])<(low+.2 if low<.8 else 1.000001)]
        if rows:
            bins.append({'from':low,'to':round(low+.2,1),'samples':len(rows),
                         'mean_score':sum(float(probabilities[i]) for i in rows)/len(rows),
                         'tp_rate':sum(outcomes[i]==1 for i in rows)/len(rows)})
    ece=sum(b['samples']*abs(b['mean_score']-b['tp_rate']) for b in bins)/sum(b['samples'] for b in bins) if bins else None
    segment=[]
    for part in range(3):
        lo=len(actions)*part//3;hi=len(actions)*(part+1)//3
        rows=[i for i in range(lo,hi) if actions[i]==direction]
        rs=[i for i in rows if outcomes[i] in (-1,1)]
        returns=[(tp if outcomes[i]==1 else -sl)*float(atrs[i])-.004 for i in rs]
        segment.append({'segment':part+1,'signals':len(rows),'resolved':len(rs),
                        'mean_stressed_net':sum(returns)/len(returns) if returns else None})
    return {'independent_24h':{'signals':len(selected),'resolved':len(resolved),'tp':int(wins),
            'tp_rate':wins/len(resolved) if resolved else None,
            'wilson_lower_95':wilson_lower(wins,len(resolved)) if resolved else None,
            'mean_stressed_net':sum(nets)/len(nets) if nets else None},
            'frozen_thirds':segment,'reliability_bins':bins,'calibration_ece':ece,
            'used_for_tuning':False,
            'notice':'Tanısal ve az örnekli olabilir. 24 saat ayrılmış kayıtlar kalan bağımlılığı ortadan kaldırmaz. Dilim sonuçları yeni model/strateji seçmek için kullanılmaz.'}
