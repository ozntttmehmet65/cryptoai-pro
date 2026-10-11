"""Causal 15m technical market structure, candles and confirmed 1h/4h regimes.

Each bar uses only information closed at or before that bar's close. Higher
interval groups are merged only AFTER their last 15m member has closed.
These are numerical pattern *candidates*, not claimed infallible chart recognition.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

EXTRA_COLS = [
    'macd_hist_atr', 'macd_cross', 'bb_position', 'bb_width',
    'adx14', 'di_balance', 'stoch_k', 'stoch_momentum',
    'mfi14', 'cmf20', 'cci20', 'williams14', 'obv_slope',
    'vwap_distance', 'ema50_distance', 'ema200_distance',
    'support20_atr', 'resistance20_atr', 'support96_atr', 'resistance96_atr',
    'reversal_bull', 'reversal_bear', 'engulf_bull', 'engulf_bear',
    'doji', 'inside_bar', 'outside_bar', 'three_bull', 'three_bear',
    'double_bottom_candidate', 'double_top_candidate', 'triangle_candidate',
    'flag_bull_candidate', 'flag_bear_candidate', 'breakout_bull', 'breakout_bear',
    'h1_trend', 'h1_rsi', 'h1_macd', 'h4_trend', 'h4_rsi', 'h4_macd',
]


def _rsi(close, period=14):
    delta=close.diff()
    rise=delta.clip(lower=0).ewm(alpha=1/period,adjust=False).mean()
    fall=(-delta.clip(upper=0)).ewm(alpha=1/period,adjust=False).mean()
    return (rise/(rise+fall).replace(0,np.nan)).fillna(0.5).clip(0,1)


def _higher(df, periods, prefix):
    # The grouping key is the START of a UTC hour/4h. For example the row
    # 10:15 belongs to the 10-11am candle, which can be used at 11:00, not
    # 10:30. A missing member of a group invalidates it entirely.
    left=pd.DataFrame({'closed':pd.to_datetime(df['time'].to_numpy(dtype='int64')+900000,unit='ms',utc=True)})
    start=pd.to_datetime(df['time'],unit='ms',utc=True)
    seconds=periods*15*60
    bucket=start.dt.floor(f'{periods*15}min')
    bars=df[['open','high','low','close','volume']].copy()
    bars['bucket']=bucket.to_numpy()
    grouped=bars.groupby('bucket',sort=True)
    agg=grouped.agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
    count=grouped.size()
    agg=agg[count==periods].copy()
    if agg.empty:
        for name in (f'{prefix}_trend',f'{prefix}_rsi',f'{prefix}_macd'):
            df[name]=np.nan
        return
    # Mark only FULL periods. Never forward-fill a partially closed group.
    agg['closed']=agg.index+pd.Timedelta(seconds=seconds)
    ec=agg['close']
    ema_fast=ec.ewm(span=8,adjust=False).mean()
    ema_slow=ec.ewm(span=21,adjust=False).mean()
    macd=ec.ewm(span=12,adjust=False).mean()-ec.ewm(span=26,adjust=False).mean()
    agg[f'{prefix}_trend']=((ema_fast-ema_slow)/ec.replace(0,np.nan)).clip(-.5,.5)
    agg[f'{prefix}_rsi']=_rsi(ec)
    agg[f'{prefix}_macd']=((macd-macd.ewm(span=9,adjust=False).mean())/ec.replace(0,np.nan)).clip(-.5,.5)
    rhs=agg[['closed',f'{prefix}_trend',f'{prefix}_rsi',f'{prefix}_macd']].sort_values('closed')
    merged=pd.merge_asof(left.sort_values('closed'),rhs,on='closed',direction='backward')
    for name in (f'{prefix}_trend',f'{prefix}_rsi',f'{prefix}_macd'):
        df[name]=merged[name].to_numpy()


def enrich(df):
    """Mutate a time-sorted 15m candle DataFrame with causal features."""
    if df.empty:return df
    op,hi,lo,cl,vol=(df[k].astype(float) for k in ('open','high','low','close','volume'))
    eps=cl.replace(0,np.nan)
    span=(hi-lo).replace(0,np.nan)
    body=(cl-op)
    atr=df['atr_pct']*eps
    safe_atr=atr.replace(0,np.nan)
    macd=cl.ewm(span=12,adjust=False).mean()-cl.ewm(span=26,adjust=False).mean()
    signal=macd.ewm(span=9,adjust=False).mean()
    df['macd_hist_atr']=((macd-signal)/safe_atr).clip(-5,5)
    df['macd_cross']=np.select([(macd>signal)&(macd.shift(1)<=signal.shift(1)),(macd<signal)&(macd.shift(1)>=signal.shift(1))],[1,-1],0)
    m20=cl.rolling(20).mean();std=cl.rolling(20).std().replace(0,np.nan)
    df['bb_position']=((cl-m20)/(2*std)).clip(-4,4)
    df['bb_width']=(4*std/eps).clip(0,10)
    up=hi.diff();down=-lo.diff()
    plus=up.where((up>down)&(up>0),0.0)
    minus=down.where((down>up)&(down>0),0.0)
    tr=pd.concat([hi-lo,(hi-cl.shift()).abs(),(lo-cl.shift()).abs()],axis=1).max(axis=1)
    smooth_tr=tr.ewm(alpha=1/14,adjust=False).mean().replace(0,np.nan)
    pos_di=plus.ewm(alpha=1/14,adjust=False).mean()/smooth_tr
    neg_di=minus.ewm(alpha=1/14,adjust=False).mean()/smooth_tr
    dx=((pos_di-neg_di).abs()/(pos_di+neg_di).replace(0,np.nan))
    df['adx14']=dx.ewm(alpha=1/14,adjust=False).mean().clip(0,1)
    df['di_balance']=((pos_di-neg_di)/(pos_di+neg_di).replace(0,np.nan)).clip(-1,1)
    highest=hi.rolling(14).max();lowest=lo.rolling(14).min();denom=(highest-lowest).replace(0,np.nan)
    stoch=((cl-lowest)/denom).clip(0,1);smooth=stoch.rolling(3).mean()
    df['stoch_k']=stoch
    df['stoch_momentum']=(stoch-smooth).clip(-1,1)
    typical=(hi+lo+cl)/3
    flow=typical*vol
    positive=flow.where(typical.diff()>0,0).rolling(14).sum()
    negative=flow.where(typical.diff()<0,0).rolling(14).sum()
    df['mfi14']=(positive/(positive+negative).replace(0,np.nan)).clip(0,1)
    mf_multiplier=((cl-lo)-(hi-cl))/span
    df['cmf20']=(mf_multiplier.mul(vol).rolling(20).sum()/vol.rolling(20).sum().replace(0,np.nan)).clip(-1,1)
    mean_typ=typical.rolling(20).mean()
    md=(typical-mean_typ).abs().rolling(20).mean().replace(0,np.nan)
    df['cci20']=((typical-mean_typ)/(.015*md)).clip(-400,400)/400
    df['williams14']=((highest-cl)/denom).clip(0,1)
    obv=np.sign(cl.diff().fillna(0)).mul(vol).cumsum()
    df['obv_slope']=((obv-obv.shift(20))/(vol.rolling(20).sum().replace(0,np.nan))).clip(-2,2)
    vwap=(typical.mul(vol).rolling(24).sum()/vol.rolling(24).sum().replace(0,np.nan))
    df['vwap_distance']=(cl/vwap-1).clip(-.3,.3)
    df['ema50_distance']=(cl/cl.ewm(span=50,adjust=False).mean()-1).clip(-.5,.5)
    df['ema200_distance']=(cl/cl.ewm(span=200,adjust=False).mean()-1).clip(-.5,.5)
    for period in (20,96):
        df[f'support{period}_atr']=((cl-lo.shift(1).rolling(period).min())/safe_atr).clip(-10,20)
        df[f'resistance{period}_atr']=((hi.shift(1).rolling(period).max()-cl)/safe_atr).clip(-10,20)
    real=body.abs();upper=hi-np.maximum(op,cl);lower=np.minimum(op,cl)-lo
    bull=(body>0);bear=(body<0)
    df['engulf_bull']=((bull)&(body.shift(1)<0)&(op<=cl.shift(1))&(cl>=op.shift(1))).astype(float)
    df['engulf_bear']=((bear)&(body.shift(1)>0)&(op>=cl.shift(1))&(cl<=op.shift(1))).astype(float)
    hammer=(lower>2*real.clip(lower=0))&(upper<real.clip(lower=0)+span*.12)&(lower>span*.45)
    shooter=(upper>2*real.clip(lower=0))&(lower<real.clip(lower=0)+span*.12)&(upper>span*.45)
    morning=(bear.shift(2,fill_value=False)&(real.shift(1)<real.shift(2)*.65)&bull&(cl>(op.shift(2)+cl.shift(2))/2))
    evening=(bull.shift(2,fill_value=False)&(real.shift(1)<real.shift(2)*.65)&bear&(cl<(op.shift(2)+cl.shift(2))/2))
    df['reversal_bull']=(hammer|morning).astype(float)
    df['reversal_bear']=(shooter|evening).astype(float)
    df['doji']=(real/span<.12).astype(float)
    df['inside_bar']=((hi<hi.shift(1))&(lo>lo.shift(1))).astype(float)
    df['outside_bar']=((hi>hi.shift(1))&(lo<lo.shift(1))).astype(float)
    df['three_bull']=(bull&bull.shift(1,fill_value=False)&bull.shift(2,fill_value=False)&(cl>cl.shift(1))&(cl.shift(1)>cl.shift(2))).astype(float)
    df['three_bear']=(bear&bear.shift(1,fill_value=False)&bear.shift(2,fill_value=False)&(cl<cl.shift(1))&(cl.shift(1)<cl.shift(2))).astype(float)
    # Conservative candidate proxies for unconfirmed larger formations: two
    # trailing retests near the same swing, contracting range, impulse flag.
    prev_lo=lo.shift(6).rolling(12).min();prev_hi=hi.shift(6).rolling(12).max()
    df['double_bottom_candidate']=((lo.rolling(5).min()-prev_lo).abs()<safe_atr*.7).astype(float)
    df['double_top_candidate']=((hi.rolling(5).max()-prev_hi).abs()<safe_atr*.7).astype(float)
    prior_width=hi.shift(10).rolling(10).max()-lo.shift(10).rolling(10).min()
    newer_width=hi.rolling(10).max()-lo.rolling(10).min()
    df['triangle_candidate']=((newer_width<prior_width*.75)&(hi.rolling(10).max()<hi.shift(10).rolling(10).max())&(lo.rolling(10).min()>lo.shift(10).rolling(10).min())).astype(float)
    tight=newer_width < prior_width*.65
    df['flag_bull_candidate']=((cl.pct_change(16)>.025)&tight).astype(float)
    df['flag_bear_candidate']=((cl.pct_change(16)<-.025)&tight).astype(float)
    high20=hi.shift(1).rolling(20).max();low20=lo.shift(1).rolling(20).min()
    volratio=vol/(vol.shift(1).rolling(20).mean().replace(0,np.nan))
    df['breakout_bull']=((cl>high20)&(volratio>1.35)).astype(float)
    df['breakout_bear']=((cl<low20)&(volratio>1.35)).astype(float)
    _higher(df,4,'h1')
    _higher(df,16,'h4')
    return df


def setup_arrays(frame):
    """Independent deterministic technical confluence watchlist, NOT P(TP)."""
    h1=np.sign(frame['h1_trend'].to_numpy(dtype=float))
    h4=np.sign(frame['h4_trend'].to_numpy(dtype=float))
    ema=np.sign(frame['ema50_distance'].to_numpy(dtype=float))
    di=frame['di_balance'].to_numpy(dtype=float)
    adx=frame['adx14'].to_numpy(dtype=float)
    bb=frame['bb_position'].to_numpy(dtype=float)
    macd=frame['macd_hist_atr'].to_numpy(dtype=float)
    cmf=frame['cmf20'].to_numpy(dtype=float)
    volume=frame['volume_z'].to_numpy(dtype=float)
    rsi=frame['rsi14'].to_numpy(dtype=float)
    def col(name):return frame[name].to_numpy(dtype=float)
    long=(h1>0)*2+(h4>0)*2+(ema>0)+(di>.10)*(adx>.16)*1.5+(macd>0)+(cmf>0)*.5+(volume>.35)*.5+(
        col('engulf_bull')+col('reversal_bull')+col('breakout_bull')*1.5+col('three_bull')+
        col('double_bottom_candidate')*.5+col('flag_bull_candidate')*.5)+(rsi>.52)*.5+((bb>.05)&(bb<1.5))*.5
    short=(h1<0)*2+(h4<0)*2+(ema<0)+(di<-.10)*(adx>.16)*1.5+(macd<0)+(cmf<0)*.5+(volume>.35)*.5+(
        col('engulf_bear')+col('reversal_bear')+col('breakout_bear')*1.5+col('three_bear')+
        col('double_top_candidate')*.5+col('flag_bear_candidate')*.5)+(rsi<.48)*.5+((bb<-.05)&(bb> -1.5))*.5
    # Explicitly avoid weak/flat, opposing or overheated multi-timeframe setups.
    long=np.where((h1>0)&(h4>0)&(rsi<.79)&(adx>.12),long,0.)
    short=np.where((h1<0)&(h4<0)&(rsi>.21)&(adx>.12),short,0.)
    return np.nan_to_num(long,nan=0),np.nan_to_num(short,nan=0)


def technical_scan(frame):
    if frame is None or frame.empty:
        return {'direction':None,'score':0.,'reasons':[],'patterns':[], 'timeframes':{}}
    long,short=setup_arrays(frame.tail(1))
    x=frame.iloc[-1];l=float(long[-1]);s=float(short[-1])
    direction=('LONG' if l>=6.5 and l>=s+1.5 else 'SHORT' if s>=6.5 and s>=l+1.5 else None)
    names={
        'engulf_bull':'Yutan yükseliş mumu','engulf_bear':'Yutan düşüş mumu',
        'reversal_bull':'Çekiç / sabah yıldızı adayı','reversal_bear':'Kayan yıldız / akşam yıldızı adayı',
        'three_bull':'Üç yükselen mum','three_bear':'Üç düşen mum',
        'double_bottom_candidate':'İkili dip adayı','double_top_candidate':'İkili tepe adayı',
        'triangle_candidate':'Daralan üçgen adayı',
        'flag_bull_candidate':'Yükseliş bayrağı adayı','flag_bear_candidate':'Düşüş bayrağı adayı',
        'breakout_bull':'Hacimli direnç kırılımı','breakout_bear':'Hacimli destek kırılımı',
        'doji':'Doji','inside_bar':'İç mum','outside_bar':'Dış mum',
    }
    patterns=[v for k,v in names.items() if pd.notna(x.get(k)) and x[k]>.5]
    reasons=[]
    if direction:
        sign=1 if direction=='LONG' else -1
        reasons.append('1s ve 4s trend uyumlu')
        if sign*float(x['di_balance'])>.1:reasons.append('ADX / yönlü hareket teyidi')
        if sign*float(x['macd_hist_atr'])>0:reasons.append('MACD momentum teyidi')
        if sign*float(x['cmf20'])>0:reasons.append('Para akışı teyidi')
        if float(x['volume_z'])>.35:reasons.append('Göreli hacim artışı')
        reasons.extend(patterns[:4])
    return {'direction':direction, 'score':round(max(l,s),2),
            'reasons':reasons[:8], 'patterns':patterns[:8],
            'timeframes':{'1h':'YUKARI' if x['h1_trend']>0 else 'ASAGI' if x['h1_trend']<0 else 'YATAY',
                          '4h':'YUKARI' if x['h4_trend']>0 else 'ASAGI' if x['h4_trend']<0 else 'YATAY'},
            'indicators':{'rsi14':round(float(x['rsi14'])*100,1),
                          'adx14':round(float(x['adx14'])*100,1),
                          'mfi14':round(float(x['mfi14'])*100,1),
                          'bb_position':round(float(x['bb_position']),3)}}
