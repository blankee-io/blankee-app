"""Public holidays, as rules rather than dates.

WHY RULES. Christmas is the 25th, but Thanksgiving is the fourth Thursday and
Memorial Day is the last Monday in May - and the fixed ones move when they
land on a weekend. Storing a date would need re-entering every January and
would be wrong the first time nobody did. Storing the rule and computing the
date per year is the only version still right in 2031.

WHAT A HOLIDAY DOES, and it is not the same as a day off:

    it cannot be booked      the office is shut; there is no leave to take
    it is not worked         so on a basket that accrues per hour worked it
                             reduces the accrual, exactly as absence does
    the denominator is       the period is still a period. A holiday that
    untouched                shrank both sides of worked/scheduled would
                             cancel itself out and change nothing, which is
                             the opposite of what payroll does

That last one is the whole subtlety. See loaf_forecast.absence_by_date.

US only for now, which the caller is expected to know - there is no country
on a basket and this returns nothing for anybody who names no holidays. The
shape is ready for more: add entries to HOLIDAYS and the rest follows.
"""

from datetime import date, timedelta

# slug -> (label, rule). The rule is (kind, *args):
#   ('fixed', month, day)          the same date every year, shifted off a
#                                  weekend the way US federal holidays are
#   ('nth', month, weekday, n)     the nth weekday of a month, n negative for
#                                  counting back from the end
#   ('easter', offset)             days either side of Easter Sunday
#
# Ordered as they fall through the year, because that is the order anybody
# ticking them down a list expects to read.
HOLIDAYS = (
    ('new_year', "New Year's Day", ('fixed', 1, 1)),
    ('mlk', 'Martin Luther King Jr. Day', ('nth', 1, 0, 3)),
    ('presidents', "Presidents' Day", ('nth', 2, 0, 3)),
    ('good_friday', 'Good Friday', ('easter', -2)),
    ('memorial', 'Memorial Day', ('nth', 5, 0, -1)),
    ('juneteenth', 'Juneteenth', ('fixed', 6, 19)),
    ('independence', 'Independence Day', ('fixed', 7, 4)),
    ('labor', 'Labor Day', ('nth', 9, 0, 1)),
    ('columbus', 'Columbus Day', ('nth', 10, 0, 2)),
    ('veterans', 'Veterans Day', ('fixed', 11, 11)),
    ('thanksgiving', 'Thanksgiving', ('nth', 11, 3, 4)),
    ('day_after_thanksgiving', 'Day after Thanksgiving', ('nth', 11, 4, 4)),
    ('christmas_eve', 'Christmas Eve', ('fixed', 12, 24)),
    ('christmas', 'Christmas Day', ('fixed', 12, 25)),
    ('new_years_eve', "New Year's Eve", ('fixed', 12, 31)),
)

BY_SLUG = {slug: (label, rule) for slug, label, rule in HOLIDAYS}


def _observed(when):
    """A fixed-date holiday landing on a weekend is taken next to it.

    Saturday moves back to the Friday and Sunday forward to the Monday, which
    is the US federal rule and what almost every employer follows. Floating
    holidays never need it - they are defined as a weekday already.
    """
    if when.weekday() == 5:
        return when - timedelta(days=1)
    if when.weekday() == 6:
        return when + timedelta(days=1)
    return when


def _easter(year):
    """Easter Sunday, by the anonymous Gregorian algorithm.

    Here only because Good Friday hangs off it, and Good Friday is a working
    holiday often enough to be worth offering.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month, day = divmod(h + L - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year, month, weekday, n):
    """The nth `weekday` of a month, or the last one when n is negative."""
    if n < 0:
        # Walk back from the first of the next month.
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        when = nxt - timedelta(days=1)
        while when.weekday() != weekday:
            when -= timedelta(days=1)
        return when - timedelta(weeks=abs(n) - 1)

    when = date(year, month, 1)
    while when.weekday() != weekday:
        when += timedelta(days=1)
    return when + timedelta(weeks=n - 1)


def date_of(slug, year):
    """When a holiday falls in one year, or None if the slug is unknown."""
    entry = BY_SLUG.get(slug)
    if not entry:
        return None
    kind = entry[1][0]
    if kind == 'fixed':
        return _observed(date(year, entry[1][1], entry[1][2]))
    if kind == 'nth':
        return _nth_weekday(year, entry[1][1], entry[1][2], entry[1][3])
    if kind == 'easter':
        return _easter(year) + timedelta(days=entry[1][1])
    return None


def as_list(value):
    """Whichever recognised slugs were given, in the order they fall.

    Mirrors loaf_data._weekday_list: unrecognised entries are dropped rather
    than stored, so nothing can name a holiday the calculator cannot place.
    """
    if isinstance(value, (list, tuple)):
        sent = [str(v).strip().lower() for v in value]
    else:
        sent = [p.strip().lower() for p in str(value or '').split(',')]
    return [slug for slug, _, _ in HOLIDAYS if slug in sent]


ENTRY_SEP, FIELD_SEP = ';', '|'
MAX_CUSTOM = 20


def parse_custom(value):
    """A stored custom list as [(month, day, name)], bad entries dropped.

    Tolerant on the way in and strict about what it returns: anything that is
    not a real date in a real month is discarded rather than stored, so
    nothing downstream has to defend against 31 February.
    """
    if isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = str(value or '').split(ENTRY_SEP)

    out = []
    for entry in raw:
        if isinstance(entry, dict):
            when, name = entry.get('date') or '', entry.get('name') or ''
        else:
            parts = str(entry).split(FIELD_SEP, 1)
            when, name = parts[0], (parts[1] if len(parts) > 1 else '')

        bits = str(when).strip().split('-')
        if len(bits) != 2:
            continue
        try:
            month, day = int(bits[0]), int(bits[1])
        except (TypeError, ValueError):
            continue
        if not 1 <= month <= 12:
            continue
        # A real day of that month, in a leap year so 29 February survives.
        try:
            date(2024, month, day)
        except ValueError:
            continue

        name = ' '.join(str(name).replace(ENTRY_SEP, ' ')
                        .replace(FIELD_SEP, ' ').split())[:60]
        out.append((month, day, name or 'Holiday'))
        if len(out) >= MAX_CUSTOM:
            break
    return out


def format_custom(value):
    """Back to the stored form, or None when there is nothing to store."""
    parsed = parse_custom(value)
    if not parsed:
        return None
    return ENTRY_SEP.join('%02d-%02d%s%s' % (m, d, FIELD_SEP, n)
                          for m, d, n in parsed)


def custom_dates_between(value, first, last):
    """Every custom holiday falling in [first, last].

    Deliberately NOT shifted off a weekend, unlike the fixed federal ones.
    That rule is real where it applies, and inventing it here would hand
    somebody a Friday their employer never gave them. One landing on a
    Saturday simply does nothing.
    """
    parsed = parse_custom(value)
    if not parsed or first is None or last is None or last < first:
        return set()

    out = set()
    for year in range(first.year, last.year + 1):
        for month, day, _name in parsed:
            try:
                when = date(year, month, day)
            except ValueError:
                continue        # 29 February in a year that has none
            if first <= when <= last:
                out.add(when)
    return out


def dates_between(slugs, first, last, custom=None):
    """Every observed holiday date in [first, last], as a set.

    Computed per year across the span rather than per date, because the rules
    are annual and a projection asks about a whole grid at a time.
    """
    out = custom_dates_between(custom, first, last)

    chosen = as_list(slugs)
    if not chosen or first is None or last is None or last < first:
        return out

    for year in range(first.year, last.year + 1):
        for slug in chosen:
            when = date_of(slug, year)
            if when is not None and first <= when <= last:
                out.add(when)
    return out


def describe(slugs):
    """The chosen holidays as names, for a page to print."""
    return [BY_SLUG[slug][0] for slug in as_list(slugs)]
