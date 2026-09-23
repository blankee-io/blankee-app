"""
The two sides of an Upcoming Bills row on the summary page: the last
occurrence and the one coming up.

The page used to draw both sides from bucket *records* alone, and a record is
deleted the moment its bill is paid - confirming a forecast turns the entry
into the record of what happened and drops the bucket record behind it. So a
paid bill had nothing left to draw from, and its "last occurrence" side showed
a dash for the very thing the side exists to show. An allowance had the mirror
problem: the latest record before this week was usually the period still
open, not the one that had finished.

So each side is read from where its truth actually lives:

  bill, coming up   the earliest occurrence still open - a bucket entry, on
                    the day it now sits (the pull moves an unpaid one forward
                    a day at a time), overdue included
  bill, last        the most recent occurrence that was paid - the confirmed
                    entry, which outlives the record, at the figure that
                    actually came through
  allowance, current  the period today falls in (or the next to start): what
                    is left, from its record, which is what typed spending
                    depletes
  allowance, last   the period before that one: from its record when the
                    record is still there and has been drawn on, otherwise
                    from the entries that fell inside its dates

Pure: dicts in, dicts out, no store. `today` is the person's day.
"""

from datetime import date, datetime, timedelta


def _as_date(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _money(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _flag(value):
    return str(value).strip().lower() in ('1', 'true')


def _side(when, original, remaining, paid):
    return {
        'date': when.isoformat() if when else None,
        'original_amount': _money(original),
        'current_amount': _money(remaining),
        'paid': bool(paid),
    }


def _bill_sides(occurrences):
    """
    occurrences: [{period, shown, amount, original, open}] for one category.
    The one coming up is the earliest still open; the last is the latest paid
    before it.
    """
    open_ones = sorted((o for o in occurrences if o['open']), key=lambda o: (o['period'], o['shown']))
    paid = sorted((o for o in occurrences if not o['open']), key=lambda o: (o['period'], o['shown']))
    upcoming = open_ones[0] if open_ones else None
    cutoff = upcoming['period'] if upcoming else None
    last = None
    for o in paid:
        if cutoff is None or o['period'] < cutoff:
            last = o
    return (
        _side(last['shown'], last['amount'], 0.0, True) if last else None,
        _side(upcoming['shown'], upcoming['original'] or upcoming['amount'], upcoming['amount'], False)
        if upcoming else None,
    )


def _allowance_sides(occurrences, records, spending, today):
    """
    records: {anchor date: (remaining, original)} for the category.
    spending: [(date, amount)] - the category's non-bucket entries.
    The anchors are the periods the app knows about: every record, and the
    period of every open bucket entry (the same dates, unless a record has
    gone missing). The current period is the latest anchor on or before
    today, or the first after it.
    """
    anchors = set(records)
    for o in occurrences:
        # An open forecast marks its period; so does a confirmed one that
        # remembers where it started - the record behind it is gone, and
        # original_date is the only thing left saying that period existed.
        if o['open'] or o['period'] != o['shown']:
            anchors.add(o['period'])
    anchors = sorted(anchors)
    if not anchors:
        return None, None

    current = None
    for a in anchors:
        if a <= today:
            current = a
    if current is None:
        current = anchors[0]
    later = [a for a in anchors if a > current]
    earlier = [a for a in anchors if a < current]
    nxt = later[0] if later else None

    by_period = {o['period']: o for o in occurrences if o['open']}

    def figure(anchor):
        """The period's allowance, from its record or its forecast."""
        rec = records.get(anchor)
        entry = by_period.get(anchor)
        return (rec[1] if rec else None) or (entry['original'] if entry else None) or (entry['amount'] if entry else None)

    def period(anchor, end, fallback=None):
        rec = records.get(anchor)
        entry = by_period.get(anchor)
        # A finished period keeps no figure of its own once its record and
        # forecast are gone; the allowance is the same one, so the current
        # period's says what it was.
        original = figure(anchor) or fallback or 0.0
        spent_in_window = sum(amt for d, amt in spending if d >= anchor and (end is None or d < end))
        # The record is what typed spending depletes, so it is the figure when
        # it has been drawn on. One left untouched beside spending inside its
        # dates is a record the confirmation did not clear away, and the
        # entries are the truer account.
        if rec and _money(rec[0]) != _money(rec[1]):
            spent = _money(rec[1]) - _money(rec[0])
        elif rec and not spent_in_window:
            spent = 0.0
        else:
            spent = spent_in_window
        shown = entry['shown'] if entry else anchor
        return _side(shown, original, _money(original) - _money(spent), False)

    current_side = period(current, nxt)

    if earlier:
        prev = earlier[-1]
    elif nxt is not None:
        # No earlier anchor on record: a finished period whose record and
        # forecast are both gone. Its length is taken to be the current one's.
        prev = current - (nxt - current)
    else:
        prev = None
    if prev is None:
        return None, current_side
    last_side = period(prev, current, fallback=figure(current))
    # A period before anyone was spending is not a last occurrence.
    if prev not in records and prev not in by_period and not any(prev <= d < current for d, _ in spending):
        last_side = None
    return last_side, current_side


def upcoming_rows(categories, entries, records, wage_bill, today, account_names=None):
    """
    One row per recurring, unhidden category: name, the card it is on (None
    for the budget), whether it is a bill, and the two sides. Rows are not
    sorted here; the caller merges budget and cards and sorts them together.
    """
    by_cat_entries = {}
    for e in entries or []:
        try:
            cid = int(e.get('category_id'))
        except (TypeError, ValueError):
            continue
        by_cat_entries.setdefault(cid, []).append(e)
    by_cat_records = {}
    for r in records or []:
        try:
            cid = int(r.get('category_id'))
        except (TypeError, ValueError):
            continue
        when = _as_date(r.get('bucket_date'))
        if when:
            by_cat_records.setdefault(cid, {})[when] = (_money(r.get('amount')), _money(r.get('original_amount')) or _money(r.get('amount')))

    rows = []
    for c in categories or []:
        if c.get('id') is None or _flag(c.get('hidden')) or not _flag(c.get('is_recurring')):
            continue
        cid = int(c['id'])
        occurrences, spending = [], []
        for e in by_cat_entries.get(cid, []):
            shown = _as_date(e.get('date'))
            if shown is None:
                continue
            is_open = _flag(e.get('is_bucket'))
            amount = _money(e.get('amount'))
            if is_open and amount <= 0:
                continue
            occurrences.append({
                'period': _as_date(e.get('original_date')) or shown,
                'shown': shown,
                'amount': amount,
                'original': _money(e.get('original_amount')) if e.get('original_amount') is not None else None,
                'open': is_open,
            })
            if not is_open:
                spending.append((shown, amount))
        is_bill = int(wage_bill.get(cid, 1) or 0) == 1
        if is_bill:
            last, upcoming = _bill_sides(occurrences)
        else:
            last, upcoming = _allowance_sides(occurrences, by_cat_records.get(cid, {}), spending, today)
        rows.append({
            'category_id': cid,
            'name': c.get('name') or '',
            'account_name': (account_names or {}).get(int(c.get('account_id') or 0)) if c.get('account_id') is not None else None,
            'is_bill': is_bill,
            'last': last,
            'upcoming': upcoming,
        })
    return rows


def sort_rows(rows):
    """Soonest coming up first; nothing coming up last. Stable, so ties keep their order."""
    return sorted(rows, key=lambda r: (r['upcoming'] is None, (r['upcoming'] or {}).get('date') or ''))
