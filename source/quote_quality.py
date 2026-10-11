"""One freshness policy for execution, health and presentation. Never retimestamp."""
import math

MAX_QUOTE_AGE_SECONDS = 10
DISPLAY_QUOTE_AGE_SECONDS = 120


def quote_quality(quote, now):
    result = dict(status='missing', age_seconds=None, event_time=None,
                  max_age_seconds=MAX_QUOTE_AGE_SECONDS, fresh=False)
    if not quote:
        return result
    try:
        stamp, price = float(quote['updated_at']), float(quote['price'])
        if not all(math.isfinite(x) for x in (stamp, price, now)) or stamp <= 0 or price <= 0:
            raise ValueError('invalid quote')
    except (KeyError, TypeError, ValueError, OverflowError):
        result['status'] = 'invalid'
        return result
    age = now - stamp
    result.update(event_time=stamp, age_seconds=age,
                  status='future' if age < 0 else 'fresh' if age < MAX_QUOTE_AGE_SECONDS else 'stale',
                  fresh=0 <= age < MAX_QUOTE_AGE_SECONDS)
    return result


def quote_coverage(universe, quotes, now):
    states = {s: quote_quality(quotes.get(s), now) for s in universe}
    return dict(fresh=sum(q['fresh'] for q in states.values()),
                stale=sum(q['status'] == 'stale' for q in states.values()),
                missing=[s for s, q in states.items() if q['status'] == 'missing'],
                invalid=[s for s, q in states.items() if q['status'] in ('invalid', 'future')],
                max_age_seconds=MAX_QUOTE_AGE_SECONDS)
