"""
Suggested recurring: charges from the same place, for the same amount, on a
steady cadence, that no recurring category accounts for yet.

Pure - dicts in, dicts out, no database and no app import - so the rules can
be tested on their own. The page is /recurring-suggested in app.py, which
gathers the bank transactions (the stored feed plus the months before
the link that bank_history.py keeps aside), leaves out what a recurring category already
covers, and passes the rest here.

    detect(transactions, today, dismissed=())

    transactions  [{'kind', 'card_id', 'merchant_name', 'description',
                    'amount', 'date', 'transaction_id'}]
                  kind: income | expense | ca (a card's charges; card_id is
                  the Blankee credit account)
    dismissed     {group key} - answered "Not recurring"

A group is one kind (and card), one place and one amount to the cent - see
group_key. The place is the merchant name the bank gives, lower-cased with
reference codes taken out, so "SPOTIFY P1A2B3" and "SPOTIFY P9Z8Y7" are one.

The tests, in order:
  1. Occurrences: one per day, and a charge within two days of the one
     before it is the same occurrence (a retry, a late post).
  2. Cadence: the median gap picks weekly, fortnightly, monthly, every 2, 3
     or 6 months, or yearly; an 11-17 day gap is also tried as twice a
     month on two days.
  3. Slots: the dates that cadence would have produced, counted back from
     the latest occurrence over the last WINDOW slots. At least MIN_HITS
     hits, at least 80% of the slots hit (within a tolerance), and few
     extras - an occurrence the cadence does not explain.
  4. Still alive: the latest hit no older than one and a half periods - a
     cancelled subscription is not suggested.
"""
from datetime import date, datetime, timedelta
import calendar
import re
import statistics
from collections import Counter

MERGE_DAYS = 2
WINDOW = 12
HIT_RATIO = 0.8
MIN_HITS = 3

WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

# (low gap, high gap, interval, unit, tolerance days, minimum hits, period days)
CADENCES = [
    (5, 9, 1, 'weeks', 1, MIN_HITS, 7),
    (11, 17, 2, 'weeks', 2, MIN_HITS, 14),
    (26, 35, 1, 'months', 3, MIN_HITS, 30.44),
    (55, 66, 2, 'months', 4, MIN_HITS, 60.88),
    (84, 97, 3, 'months', 6, MIN_HITS, 91.31),
    (174, 192, 6, 'months', 7, MIN_HITS, 182.62),
    (350, 380, 1, 'years', 7, MIN_HITS, 365.25),
]
SEMI_MONTHLY = (None, None, 1, 'months', 2, MIN_HITS, 15.22)

KINDS = ('income', 'expense', 'ca')


# ── small helpers ───────────────────────────────────────────────

def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None


def _clamp_day(year, month, day):
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _add_months(d, months, day):
    total = d.year * 12 + (d.month - 1) + months
    return _clamp_day(total // 12, total % 12 + 1, day)


def _is_last_day(d):
    return d.day == calendar.monthrange(d.year, d.month)[1]


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def _mode(values, prefer):
    """The most common value; a tie goes to `prefer` if it is among the tied, else the latest."""
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    best = max(counts.values())
    tied = [v for v in values if counts[v] == best]
    return prefer if prefer in tied else tied[-1]


def _money(amount, symbol='$'):
    return f'{symbol}{amount:,.2f}'


# ── groups ──────────────────────────────────────────────────────

_NOT_WORD = re.compile(r'[^0-9a-z&]+')


def place(name):
    """
    The merchant as a grouping key: lower case, punctuation out, and every
    word with a digit in it dropped - those are the reference codes and store
    numbers that change from one charge to the next ("P8A2B3", "#1043").
    """
    words = _NOT_WORD.sub(' ', (name or '').lower()).split()
    return ' '.join(w for w in words if not any(c.isdigit() for c in w))


def _cents(amount):
    return int(round(abs(float(amount)) * 100))


def group_key(kind, card_id, merchant, amount):
    """'bank|expense|0|spotify|1199' - what "Not recurring" stores, at most 255 characters."""
    return f'bank|{kind}|{int(card_id or 0)}|{place(merchant)[:200]}|{_cents(amount)}'


def groups(transactions, today):
    """{key: [txn, ...]} for the posted, positive charges up to today."""
    out = {}
    for t in transactions or []:
        kind = t.get('kind')
        d = _as_date(t.get('date'))
        merchant = t.get('merchant_name') or t.get('description')
        try:
            amount = float(t.get('amount') or 0)
        except (TypeError, ValueError):
            continue
        if kind not in KINDS or d is None or d > today or not amount or not place(merchant):
            continue
        key = group_key(kind, t.get('card_id'), merchant, amount)
        out.setdefault(key, []).append(dict(t, date=d))
    return out


def occurrences(txns):
    """[(date, amount)] oldest first: one per day, a charge within MERGE_DAYS of the one before folded in."""
    out = []
    for d in sorted({t['date'] for t in txns}):
        if out and (d - out[-1][0]).days <= MERGE_DAYS:
            continue
        out.append((d, abs(float(txns[0]['amount']))))
    return out


# ── slots ───────────────────────────────────────────────────────

def _slots_back(cadence, anchor, days, start):
    """The cadence's dates from `anchor` back to `start`, newest first, at most WINDOW."""
    _, _, interval, unit, _, _, _ = cadence
    out = []
    k = 0
    while len(out) < WINDOW:
        if unit == 'weeks':
            d = anchor - timedelta(weeks=interval * k)
            ds = [d]
        elif unit == 'years':
            ds = [_clamp_day(anchor.year - k, anchor.month, anchor.day)]
        else:
            month = _add_months(anchor, -interval * k, 1)
            ds = sorted((_month_day(month, day) for day in days), reverse=True)
        k += 1
        stop = False
        for d in ds:
            if d > anchor:
                continue
            if d < start:
                stop = True
                break
            out.append(d)
            if len(out) >= WINDOW:
                break
        if stop or k > 600:
            break
    return out


def _month_day(month_start, day):
    if day == 'Last Day':
        return _clamp_day(month_start.year, month_start.month, 31)
    return _clamp_day(month_start.year, month_start.month, day)


def _next_after(cadence, anchor, days, today):
    """The first date on the cadence strictly after today."""
    _, _, interval, unit, _, _, _ = cadence
    if unit == 'weeks':
        step = 7 * interval
        n = max(1, (today - anchor).days // step + 1)
        d = anchor + timedelta(days=step * n)
        while d <= today:
            d += timedelta(days=step)
        return d
    if unit == 'years':
        y = today.year
        while True:
            d = _clamp_day(y, anchor.month, anchor.day)
            if d > today:
                return d
            y += 1
    k = 0
    while True:
        month = _add_months(anchor, interval * k, 1)
        for d in sorted(_month_day(month, day) for day in days):
            if d > today:
                return d
        k += 1


def _match(slots, occ, tolerance):
    """Each slot paired with the nearest unused occurrence within tolerance."""
    used = set()
    hits = []
    for s in slots:
        best = None
        for i, (d, _a) in enumerate(occ):
            if i in used:
                continue
            gap = abs((d - s).days)
            if gap <= tolerance and (best is None or gap < best[0]):
                best = (gap, i)
        if best is not None:
            used.add(best[1])
            hits.append(occ[best[1]])
    return hits, used


def _fit(cadence, occ, today):
    """The cadence laid over the occurrences: a result dict, or None if it does not fit."""
    _, _, interval, unit, tolerance, min_hits, period = cadence
    latest = occ[-1][0]
    recent = [d for d, _ in occ[-6:]]

    if unit == 'weeks':
        weekday = _mode([d.weekday() for d in recent], latest.weekday())
        shift = weekday - latest.weekday()
        if shift > 3:
            shift -= 7
        elif shift < -3:
            shift += 7
        anchor = latest + timedelta(days=shift)
        days = [weekday]
    elif unit == 'years':
        anchor = latest
        days = [latest.day]
    elif cadence is SEMI_MONTHLY:
        # Two days of the month: the recent days split at their widest gap.
        doms = sorted({d.day for d, _ in occ[-8:]})
        if len(doms) < 2:
            return None
        cut = max(range(1, len(doms)), key=lambda i: doms[i] - doms[i - 1])
        lower = [d.day for d, _ in occ[-8:] if d.day < doms[cut]]
        upper = [d.day for d, _ in occ[-8:] if d.day >= doms[cut]]
        if max(lower) - min(lower) > 4 or max(upper) - min(upper) > 4:
            return None
        days = [_mode(lower, None), _mode(upper, None)]
        candidates = [_month_day(_add_months(latest, m, 1), day) for m in (-1, 0, 1) for day in days]
        anchor = min(candidates, key=lambda c: abs((c - latest).days))
    else:
        if sum(1 for d in recent if _is_last_day(d)) >= max(2, int(len(recent) * 0.6 + 0.5)):
            day = 'Last Day'
        else:
            day = _mode([d.day for d in recent], latest.day)
        days = [day]
        # The latest slot is the one nearest the latest occurrence.
        candidates = [_month_day(_add_months(latest, m, 1), day) for m in (-1, 0, 1)]
        anchor = min(candidates, key=lambda c: abs((c - latest).days))

    start = occ[0][0] - timedelta(days=tolerance)
    slots = _slots_back(cadence, anchor, days, start)
    if not slots:
        return None
    window_start = slots[-1] - timedelta(days=tolerance)
    in_window = [o for o in occ if o[0] >= window_start]
    hits, used = _match(slots, in_window, tolerance)
    extras = len(in_window) - len(used)

    if len(hits) < min_hits:
        return None
    if len(hits) < HIT_RATIO * len(slots):
        return None
    if extras > max(1, int(0.25 * len(hits))):
        return None
    last_hit = max(d for d, _ in hits)
    if (today - last_hit).days > max(1.5 * period, period + tolerance):
        return None

    hits.sort()
    return {'cadence': cadence, 'hits': hits, 'slots': len(slots), 'extras': extras,
            'anchor': anchor, 'days': days, 'ratio': len(hits) / len(slots)}


def _cadence_candidates(occ):
    gaps = [(b[0] - a[0]).days for a, b in zip(occ, occ[1:])]
    if not gaps:
        return []
    median = statistics.median(gaps)
    out = [c for c in CADENCES if c[0] <= median <= c[1]]
    if 11 <= median <= 17:
        out.append(SEMI_MONTHLY)
    return out


# ── the sentence ────────────────────────────────────────────────

def _day_word(day):
    return 'the last day' if day == 'Last Day' else f'the {_ordinal(int(day))}'


def sentence(s, symbol='$'):
    """'$15.99 every month on the 4th' - 'about' in front when the amount varies."""
    amount = _money(s['amount'], symbol)
    if not s['fixed']:
        amount = 'about ' + amount
    unit, interval = s['cadence_unit'], s['cadence_interval']
    if unit == 'weeks':
        day = s['weekdays'][0].capitalize() + 's'
        every = 'every week' if interval == 1 else f'every {interval} weeks'
        return f'{amount} {every} on {day}'
    if unit == 'years':
        on = date(2000, s['yearly_month'], 1).strftime('%b')
        return f'{amount} every year on {s["yearly_day"]} {on}'
    days = s['monthly_days']
    if len(days) == 2:
        return f'{amount} twice a month, on {_day_word(days[0])} and {_day_word(days[1])}'
    every = 'every month' if interval == 1 else f'every {interval} months'
    return f'{amount} {every} on {_day_word(days[0])}'


# ── the whole pass ──────────────────────────────────────────────

def detect_group(key, txns, today):
    """One group's suggestion, or None."""
    occ = occurrences(txns)
    if len(occ) < MIN_HITS:
        return None
    best = None
    for cadence in _cadence_candidates(occ):
        fit = _fit(cadence, occ, today)
        if fit and (best is None or fit['ratio'] > best['ratio']
                    or (fit['ratio'] == best['ratio'] and fit['extras'] < best['extras'])):
            best = fit
    if not best:
        return None
    _, _, interval, unit, _, _, _ = best['cadence']
    days = best['days']
    first = txns[0]
    names = Counter((t.get('merchant_name') or t.get('description') or '').strip() for t in txns)
    s = {
        'key': key,
        'kind': first['kind'],
        'card_id': int(first['card_id']) if first.get('card_id') not in (None, '') else None,
        'merchant': names.most_common(1)[0][0],
        # Every name the bank used, for the merchant memory: the import looks
        # a transaction up by its description and by its merchant name.
        'names': sorted({(t.get(f) or '').strip() for t in txns for f in ('description', 'merchant_name')} - {''}),
        'amount': round(abs(float(first['amount'])), 2),
        'fixed': True,
        'wage_bill': 1,
        'cadence_interval': interval,
        'cadence_unit': unit,
        'weekdays': [WEEKDAYS[days[0]]] if unit == 'weeks' else [],
        'monthly_days': [str(d) for d in days] if unit == 'months' else [],
        'yearly_day': best['anchor'].day if unit == 'years' else None,
        'yearly_month': best['anchor'].month if unit == 'years' else None,
        'next_date': _next_after(best['cadence'], best['anchor'], days, today),
        'recent': list(reversed(best['hits'][-4:])),
        'hits': len(best['hits']),
        'extras': best['extras'],
    }
    s['sentence'] = sentence(s)
    return s


def detect(transactions, today, dismissed=()):
    """Every suggestion, by merchant."""
    dismissed = set(dismissed or ())
    out = []
    for key, txns in groups(transactions, today).items():
        if key in dismissed:
            continue
        s = detect_group(key, txns, today)
        if s:
            out.append(s)
    out.sort(key=lambda s: (s['merchant'].lower(), s['amount']))
    return out
