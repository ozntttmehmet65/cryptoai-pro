"""Read-only entry checks. They may veto a model, never approve one."""
from __future__ import annotations
import math
from quote_quality import quote_quality, MAX_QUOTE_AGE_SECONDS

MAX_ENTRY_DELAY_MS = 900_000
MAX_REFERENCE_DEPARTURE_ATR = 0.35
ASSUMED_COST = 0.0016
STRESS_COST = 0.0040
MAX_SPREAD_BPS = 8.0
MAX_SPREAD_ATR_FRACTION = .10
MIN_TOP_NOTIONAL = 1000.0
POLICY_VERSION = "V7.2"


def entry_check(pred, quote, now_ms, book=None):
    reasons = []
    departure = net_reward = risk_reward = spread_bps = top_notional = break_even = None
    checks = []
    def verify(key, label, passed):
        checks.append({"id":key, "label":label, "passed":bool(passed)})
        if not passed:reasons.append(label)
    quality = quote_quality(quote, now_ms/1000)
    quote_labels = {'missing':'Canlı fiyat henüz alınmadı',
                    'invalid':'Canlı fiyat veya zaman damgası geçersiz',
                    'future':'Fiyat zaman damgası sunucu saatinden ileride',
                    'stale':'Son fiyat 10 saniye veya daha eski; yeni fiyat bekleniyor',
                    'fresh':'Fiyat güncel (10 saniyeden yeni)'}
    verify('quote', quote_labels[quality['status']], quality['fresh'])
    try:
        price, stamp = float(quote['price']), float(quote['updated_at'])
        reference, atr_pct = float(pred['entry_reference']), float(pred['risk_atr_pct'])
        tp, sl = float(pred['tp_atr']), float(pred['sl_atr'])
        candle = float(pred['candle_time'])
        if not all(math.isfinite(v) for v in (price, stamp, reference, atr_pct, tp, sl, candle)):
            raise ValueError('non-finite')
        if not (price > 0 and reference > 0 and 0 < atr_pct < .30 and 0 < tp <= 6 and 0 < sl <= 6):
            raise ValueError('invalid bracket')
        verify('candle', 'Mum kapanışından sonra giriş penceresi doldu veya mum henüz kapanmadı', 0 <= now_ms - (candle + 900_000) <= MAX_ENTRY_DELAY_MS)
        departure = abs(price/reference - 1)/atr_pct
        verify('departure', 'Fiyat referans girişten 0,35 ATR uzaklaştı; yeni mum bekleniyor', departure <= MAX_REFERENCE_DEPARTURE_ATR)
        net_reward = tp*atr_pct - ASSUMED_COST
        risk_reward = net_reward/(sl*atr_pct + ASSUMED_COST)
        verify('reward', 'TP mesafesi varsayılan işlem maliyetini karşılamıyor', net_reward > 0)
        verify('bracket', 'TP/SL seviyeleri geçersiz', price*atr_pct*max(tp, sl) < price)
    except (TypeError, ValueError, KeyError, OverflowError):
        reasons.append('Giriş kontrolü için geçerli fiyat ve risk verisi yok')
    try:
        bid, ask, bq, aq, stamp = (float(book[k]) for k in ('bid','ask','bid_qty','ask_qty','updated_at'))
        if not all(math.isfinite(v) for v in (bid,ask,bq,aq,stamp)) or not (0<bid<=ask and bq>0 and aq>0):
            raise ValueError('invalid book')
        verify('book', 'Güncel alış-satış verisi yok; giriş bekletiliyor', 0 <= now_ms/1000-stamp < 12)
        spread = (ask-bid)/((ask+bid)/2)
        spread_bps = spread*10000
        top_notional = min(bid*bq, ask*aq)
        verify('spread', 'Alış-satış makası fazla; giriş bekletiliyor',
               spread_bps <= MAX_SPREAD_BPS and spread <= atr_pct*MAX_SPREAD_ATR_FRACTION)
        verify('liquidity', 'En iyi alış/satış kademesinde görünür tutar 1.000 USDT altında', top_notional >= MIN_TOP_NOTIONAL)
        executable = ask if (pred.get('candidate_signal') if pred.get('signal')=='BEKLE' else pred.get('signal'))=='LONG' else bid
        verify('executable', 'Alış/satış fiyatı referanstan 0,35 ATR uzaklaştı', abs(executable/reference-1)/atr_pct <= MAX_REFERENCE_DEPARTURE_ATR)
        side=pred.get('candidate_signal') if pred.get('signal')=='BEKLE' else pred.get('signal')
        if side in ('LONG','SHORT'):
            probability=float(pred['p_long' if side=='LONG' else 'p_short'])
            break_even=(sl*atr_pct+STRESS_COST+spread)/((tp+sl)*atr_pct)
            verify('stress', 'Kalibre model puanı stres maliyetinin başabaş eşiğini karşılamıyor',
                   math.isfinite(probability) and 0<=probability<=1 and probability>=break_even+.05)
    except (TypeError,ValueError,KeyError,OverflowError,ZeroDivisionError,UnboundLocalError):
        verify('book', 'Geçerli alış-satış ve stres kontrolü verisi yok', False)
    def finite(value):
        return value if value is None or math.isfinite(value) else None
    departure,net_reward,risk_reward,spread_bps,top_notional,break_even=map(finite,(departure,net_reward,risk_reward,spread_bps,top_notional,break_even))
    return {'approved': not reasons, 'reasons': reasons, 'checks':checks, 'policy_version':POLICY_VERSION,
            'quote_quality':quality,
            'spread_bps':spread_bps, 'min_top_notional_usdt':top_notional,
            'conditional_stress_break_even':break_even,
            'assumptions':{'max_spread_bps':MAX_SPREAD_BPS,'min_top_notional_usdt':MIN_TOP_NOTIONAL,'stress_total_cost':STRESS_COST},
            'reference_departure_atr': departure,
            'assumed_net_reward': net_reward, 'assumed_net_risk_reward': risk_reward,
            'notice': 'İhtiyatlı giriş politikasıdır; üst kademe tüm emir defteri değildir. Stres hesabı 24 saatte çözülen TP/SL koşuluna bağlı model puanını kullanır; canlı kâr beklentisi veya doğrulanmış başarı olasılığı değildir'}


def guarded_prediction(pred, quote, now_ms, permission, drift_blocked=False, *, book=None):
    """Do not mutate cached inference: quote failures can recover on later reads."""
    result = dict(pred)
    check = entry_check(pred, quote or {}, now_ms, book)
    side = pred.get('candidate_signal') if pred.get('signal')=='BEKLE' else pred.get('signal')
    if side in ('LONG', 'SHORT'):
        if 'ensemble' in pred:
            agreement = ((pred.get('ensemble') or {}).get(side) or {}).get('agreement') is True
            check['checks'].append({'id':'consensus','passed':agreement,'label':'Çoklu model oy uzlaşması'})
            if not agreement:
                check['reasons'].append('Model oyları yeterince uzlaşmıyor; BEKLE')
        if 'market_condition' in pred:
            condition=pred.get('market_condition') or {}
            regime_ok=condition.get('name') in ('UPTREND','DOWNTREND','RANGE') and (pred.get('regime_evidence') or {}).get('allowed') is True
            check['checks'].append({'id':'regime','passed':regime_ok,'label':'Piyasa koşulunda bağımsız dönem kanıtı'})
            if not regime_ok:check['reasons'].append('Bu piyasa koşulunda dönem kanıtı yok veya aşırı oynaklık var')
        check['checks'].append({'id':'model','passed':bool(permission),'label':'Bağımsız model yön testi'})
        check['checks'].append({'id':'drift','passed':not drift_blocked,'label':'Canlı yön ve coin/model sonuç kilidi'})
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
