"""V6.1 deterministic *display* synthesis of an existing AI model and causal technical scan.

The fused 0-100 confirmation score is NOT a TP probability and is NOT trained on
unseen holdout results. A technical setup can only reach TAKIP_ET, NEVER
ISLEM_SINYALI; live approval continues to come exclusively from the existing
per-symbol, purged/chronologically tested AI model decision.
"""
from __future__ import annotations
import math


def _bounded(value, low=0., high=1.):
    try:
        v = float(value)
        return max(low, min(high, v)) if math.isfinite(v) else 0.
    except (TypeError, ValueError, OverflowError):
        return 0.


def _matching_patterns(patterns, direction):
    """Do not reward reversal patterns pointing opposite to the candidate side."""
    bullish = ('yüksel', 'boğa', 'dip', 'çekiç', 'sabah', 'bull')
    bearish = ('düş', 'ayı piyasası', 'tepe', 'kayan', 'akşam', 'bear')
    out = []
    for name in patterns or []:
        label = str(name).lower()
        bull = any(v in label for v in bullish)
        bear = any(v in label for v in bearish)
        if (direction == 'LONG' and bull and not bear) or (direction == 'SHORT' and bear and not bull):
            out.append(str(name))
        elif not bull and not bear:  # direction-neutral compression/inside bar
            out.append(str(name))
    return out


def combined_assessment(pred):
    """One direction and one measured-context score per coin, with explicit status.

    Components maximum: AI 45, technical indicators 25, closed 1h/4h 15,
    matching candle/chart patterns 10, *separately* tested technical setup 5.
    Stress-cost failures reduce the WATCH score, never grant trade permission.
    """
    if not isinstance(pred, dict):
        return {'direction': None, 'status': 'BEKLE', 'score': None,
                'long_score': None, 'short_score': None, 'approved': False,
                'reason': 'Coin modeli veya kapanmış mum bekleniyor',
                'entry_reference': None, 'tp_estimate': None, 'sl_estimate': None}
    tech = pred.get('technical') or {}
    side = tech.get('direction')
    tf = tech.get('timeframes') or {}
    patterns = tech.get('patterns') or []
    candidates = {}
    for direction in ('LONG', 'SHORT'):
        p = _bounded(pred.get('p_long') if direction == 'LONG' else pred.get('p_short'))
        ai_part = 45*p
        tech_part = 25*_bounded(tech.get('score'), 0, 12.5)/12.5 if side == direction else 0.
        target = 'YUKARI' if direction == 'LONG' else 'ASAGI'
        higher_part = 7.5*sum(tf.get(t) == target for t in ('1h', '4h'))
        compatible = _matching_patterns(patterns, direction) if side == direction else []
        pattern_part = 5*min(2, len(compatible))
        audit = tech.get('holdout') or {} if side == direction else {}
        tested = side == direction and (tech.get('status') == 'DOĞRULANDI' and bool(audit.get('approved')))
        test_part = 5 if tested else 0
        stress = audit.get('mean_stressed_return') if side == direction else None
        # A direction with clearly bad separate historical outcomes is not a
        # recommendation just because its technical features look impressive.
        penalty = (min(20., abs(float(stress))*700.) if stress is not None
                   and math.isfinite(float(stress)) and float(stress) < 0 else 0.)
        raw = max(0, min(100, round(ai_part+tech_part+higher_part+pattern_part+test_part-penalty)))
        candidates[direction] = {
            'score': raw, 'parts': {'ai': round(ai_part,1), 'indicators':round(tech_part,1),
                                   'higher_timeframe':round(higher_part,1),
                                   'patterns':round(pattern_part,1),'tested_setup':round(test_part,1),
                                   'risk_penalty':round(penalty,1)},
            'matching_patterns':compatible, 'tested':tested,
            'holdout':audit if side == direction else None}
    approved = pred.get('signal') in ('LONG', 'SHORT')
    if approved:
        direction = pred['signal']  # independent AI's pre-existing approval always wins
        status = 'İŞLEM SİNYALİ'
    else:
        direction = max(('LONG','SHORT'), key=lambda k:candidates[k]['score'])
        best = candidates[direction]['score'];other=candidates['SHORT' if direction=='LONG' else 'LONG']['score']
        watch = (best >= 60 and best-other >= 10 and side == direction
                 and (_bounded(pred.get('p_long' if direction=='LONG' else 'p_short')) >= .20))
        status = 'TAKİP ET' if watch else 'BEKLE'
    winner = candidates[direction]
    if pred.get('execution_blocked'):
        status = 'BEKLE'
        approved = False
    if status == 'BEKLE':
        why = pred.get('blocked_reason') or 'Birleşik teyit yetersiz veya LONG/SHORT puanları birbirine yakın'
    elif status == 'TAKİP ET':
        why = 'AI + teknik teyit var; bağımsız işlem testi onayı yok, yalnızca takip'
    else:
        why = 'AI modelinin bağımsız TP/SL testi geçmiş; puan başarı olasılığı değil'
    entry = pred.get('entry_reference')
    if status == 'İŞLEM SİNYALİ':
        tp,sl = pred.get('target_estimate'),pred.get('stop_estimate')
    elif side == direction:
        entry = tech.get('entry_reference',entry)
        tp,sl = tech.get('target_estimate'),tech.get('stop_estimate')
    else:
        tp,sl = None,None
    # Do not display hypothetical trading levels when we are saying BEKLE.
    if status == 'BEKLE':
        tp=sl=None
    return {'direction': direction if status!='BEKLE' else None,
            'leaning':direction,'status':status,'score':winner['score'],
            'long_score':candidates['LONG']['score'], 'short_score':candidates['SHORT']['score'],
            'components':winner['parts'],'matching_patterns':winner['matching_patterns'],
            'execution_quality':pred.get('execution_quality'),
            'approved': approved,'historical_test_approved':approved or winner['tested'],
            'reason':why,'entry_reference':entry if status!='BEKLE' else None,
            'tp_estimate':tp,'sl_estimate':sl,
            'historical_tp_rate':(winner['holdout'] or {}).get('hit_rate'),
            'historical_resolved':(winner['holdout'] or {}).get('resolved'),
            'historical_stress_net':(winner['holdout'] or {}).get('mean_stressed_return'),
            'ai_model_score':pred.get('p_long') if direction=='LONG' else pred.get('p_short'),
            'score_notice':'0-100 teknik + AI teyit puanıdır; TP tutma olasılığı değildir'}
