"""Read-only entry checks. They may veto a model, never approve one."""
from __future__ import annotations
import math

MAX_QUOTE_AGE_SECONDS = 10
MAX_ENTRY_DELAY_MS = 900_000
MAX_REFERENCE_DEPARTURE_ATR = 0.35
ASSUMED_COST = 0.0016


def entry_check(pred, quote, now_ms):
    reasons = []
    departure = net_reward = risk_reward = None
    try:
        price, stamp = float(quote['price']), float(quote['updated_at'])
        reference, atr_pct = float(pred['entry_reference']), float(pred['risk_atr_pct'])
        tp, sl = float(pred['tp_atr']), float(pred['sl_atr'])
        candle = float(pred['candle_time'])
        if not all(math.isfinite(v) for v in (price, stamp, reference, atr_pct, tp, sl, candle)):
            raise ValueError('non-finite')
        if not (price > 0 and reference > 0 and 0 < atr_pct < .30 and 0 < tp <= 6 and 0 < sl <= 6):
            raise ValueError('invalid bracket')
        age = now_ms/1000 - stamp
        if not 0 <= age < MAX_QUOTE_AGE_SECONDS:
            reasons.append('Canlı fiyat eski veya zaman damgası geçersiz')
        if not 0 <= now_ms - (candle + 900_000) <= MAX_ENTRY_DELAY_MS:
            reasons.append('Mum kapanışından sonra giriş penceresi doldu veya mum henüz kapanmadı')
        departure = abs(price/reference - 1)/atr_pct
        if departure > MAX_REFERENCE_DEPARTURE_ATR:
            reasons.append('Fiyat referans girişten 0,35 ATR uzaklaştı; yeni mum bekleniyor')
        net_reward = tp*atr_pct - ASSUMED_COST
        risk_reward = net_reward/(sl*atr_pct + ASSUMED_COST)
        if net_reward <= 0:
            reasons.append('TP mesafesi varsayılan işlem maliyetini karşılamıyor')
        if price*atr_pct*max(tp, sl) >= price:
            reasons.append('TP/SL seviyeleri geçersiz')
    except (TypeError, ValueError, KeyError, OverflowError):
        reasons.append('Giriş kontrolü için geçerli fiyat ve risk verisi yok')
    return {'approved': not reasons, 'reasons': reasons,
            'reference_departure_atr': departure,
            'assumed_net_reward': net_reward, 'assumed_net_risk_reward': risk_reward,
            'notice': 'Giriş uygunluğu kontrolüdür; test edilmiş ek strateji veya TP olasılığı değildir'}


def guarded_prediction(pred, quote, now_ms, permission, drift_blocked=False):
    """Do not mutate cached inference: quote failures can recover on later reads."""
    result = dict(pred)
    check = entry_check(pred, quote or {}, now_ms)
    side = pred.get('signal')
    if side in ('LONG', 'SHORT'):
        if not permission:
            check['reasons'].append('Güncel modelin bağımsız yön doğrulaması yok')
        if drift_blocked:
            check['reasons'].append('Son canlı sanal sonuçlar olumsuz; yön güvenlik kilidi')
        check['approved'] = not check['reasons']
    result['execution_quality'] = check
    if check['reasons']:
        result.update(signal='BEKLE', confidence=None,
                      candidate_signal=pred.get('candidate_signal') or (side if side in ('LONG', 'SHORT') else None),
                      blocked_reason=' · '.join(check['reasons']), execution_blocked=True)
    return result
