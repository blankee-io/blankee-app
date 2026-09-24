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
  allowance, coming up  the earliest forecast still open, like a bill's, on
                    the day it now sits
  allowance, last   the period before it - the one being spent now, or the
                    last one finished: what is left, from its record when
                    that is still there and drawn on, otherwise from the
                    entries that fell inside its dates

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


def _span_days(link):
    n = max(1, int(link.get('cadence_interval') or 1))
    return {'days': n, 'weeks': 7 * n, 'months': 31 * n, 'years': 366 * n}.get(link.get('cadence_unit'), 31 * n)


def _default_occurrences(link, start, end):
    from bucket_utils import recurring_occurrence_dates
    split = lambda v: [p.strip() for p in str(v or '').split(',') if p.strip()]
    return recurring_occurrence_dates(
        link.get('cadence_interval'), link.get('cadence_unit'), start, end,
        weekdays=split(link.get('weekdays')),
        monthly_days=split(link.get('monthly_days') or link.get('monthly_day')),
        yearly_day=link.get('yearly_day'), yearly_month=link.get('yearly_month'))


def _template_anchors(links, today, occurrences_for):
    """
    The periods the recurring template itself lays down around today. A
    period whose record was released and whose forecast was consumed leaves
    nothing behind to say it existed - which is exactly the period a person
    is usually in the middle of. The template still knows.
    """
    out = set()
    for link in links or []:
        start = _as_date(link.get('start_date'))
        if start is None:
            continue
        span = _span_days(link)
        end = today + timedelta(days=2 * span)
        link_end = _as_date(link.get('end_date'))
        if link_end and link_end < end:
            end = link_end
        if start > end:
            continue
        try:
            dates = occurrences_for(link, start, end)
        except Exception:
            continue
        floor = today - timedelta(days=3 * span)
        out.update(d for d in dates if d >= floor)
    return out


def _allowance_sides(occurrences, records, spending, today, template_anchors=(), template_amount=None,
                     template_span=None):
    """
    records: {anchor date: (remaining, original)} for the category.
    spending: [(date, amount)] - the category's non-bucket entries.
    The anchors are the periods the app knows about: every record, the
    period of every open bucket entry, a confirmed entry's remembered period,
    and - where those leave a gap - the template's own dates. The current
    period is the latest anchor on or before today, or the first after it.
    """
    known = set(records)
    for o in occurrences:
        # An open forecast marks its period; so does a confirmed one that
        # remembers where it started - the record behind it is gone, and
        # original_date is the only thing left saying that period existed.
        if o['open'] or o['period'] != o['shown']:
            known.add(o['period'])
    anchors = set(known)
    # The template fills gaps, and only gaps. Its dates and the buckets' can
    # disagree - a bucket moved by hand, a change of day - and a template date
    # dropped beside a real one would cut a phantom period between them.
    slack = timedelta(days=max(1, (template_span or 7) // 2))
    for t in template_anchors:
        if not any(abs((k - t).days) < slack.days for k in known):
            anchors.add(t)
    anchors = sorted(anchors)
    if not anchors:
        return None, None

    by_period = {o['period']: o for o in occurrences if o['open']}

    def figure(anchor):
        """The period's allowance, from its record or its forecast."""
        rec = records.get(anchor)
        entry = by_period.get(anchor)
        return (rec[1] if rec else None) or (entry['original'] if entry else None) or (entry['amount'] if entry else None)

    def after(anchor):
        later = [x for x in anchors if x > anchor]
        return later[0] if later else None

    def period(anchor, end, fallback=None):
        rec = records.get(anchor)
        entry = by_period.get(anchor)
        # A period keeps no figure of its own once its record and forecast
        # are gone - released, or consumed - and the one a person is in the
        # middle of is often exactly that. The allowance is the same one, so
        # the template's figure says what it was.
        original = figure(anchor) or fallback or template_amount or 0.0
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

    # The one coming up is the earliest period still ahead - or still open,
    # like a forecast the pull has pushed to today, which is coming up even
    # though its period began. Both count, and the earlier wins: a period
    # whose forecast was already consumed but which has not started yet is
    # still ahead. What just happened is the period before it: the one being
    # spent now, or the last one finished. With nothing ahead or open, the
    # period today falls in is the last thing that happened.
    ahead = [x for x in anchors if x > today] + [o['period'] for o in occurrences if o['open']]
    if ahead:
        up = min(ahead)
        upcoming_side = period(up, after(up))
        before = [x for x in anchors if x < up]
        if not before:
            return None, upcoming_side
        last = before[-1]
        last_side = period(last, up, fallback=figure(up))
    else:
        current = None
        for x in anchors:
            if x <= today:
                current = x
        if current is None:
            return None, None
        last, upcoming_side = current, None
        last_side = period(last, after(last))
    # Not a last occurrence if it is before the category's first known
    # period - the series had not started.
    end = after(last)
    if known and last < min(known) and not any(last <= d and (end is None or d < end) for d, _ in spending):
        last_side = None
    return last_side, upcoming_side


def upcoming_rows(categories, entries, records, wage_bill, today, account_names=None,
                  templates=None, occurrences_for=None):
    """
    One row per recurring, unhidden category: name, the card it is on (None
    for the budget), whether it is a bill, and the two sides. Rows are not
    sorted here; the caller merges budget and cards and sorts them together.

    templates: {category_id: [recurring rows]} - the cadence behind each
    category, for the periods an allowance has been through. occurrences_for
    walks one of those rows between two dates; the default is the app's own
    walker, and a test can hand in a plain one.
    """
    occurrences_for = occurrences_for or _default_occurrences
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
            # An open forecast with nothing left - an allowance overspent before
            # its period even began - is still that period, and still coming
            # up. The prompt leaves it out; this must not.
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
            links = (templates or {}).get(cid, [])
            anchors = _template_anchors(links, today, occurrences_for)
            # The link in force today: the last one starting on or before it.
            in_force = None
            for link in sorted(links, key=lambda l: _as_date(l.get('start_date')) or date.min):
                start = _as_date(link.get('start_date'))
                if start is None or start <= today:
                    in_force = link
            amount = _money(in_force.get('amount')) if in_force else None
            last, upcoming = _allowance_sides(occurrences, by_cat_records.get(cid, {}), spending, today,
                                              anchors, template_amount=amount or None,
                                              template_span=_span_days(in_force) if in_force else None)
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
