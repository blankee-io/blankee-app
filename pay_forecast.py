"""
What a paycheck will be, for a recurring income calculated from pay.

A recurring income is normally one amount on a schedule. Calculated from pay
(recurring_income.pay_type 'hourly' or 'salary'), each paycheck is worked out
from the job it comes from - Loaf's job, the shelf: its working week, the days
the office is shut, when pay lands and how many days after the period closes.
Unpaid time off booked in Loaf comes off the paychecks it falls in.

Pure: dicts in, numbers out. No database, no Redis, no Flask - pay_income.py
reads the rows and writes the forecasts, and the tests exercise this alone.

How a paycheck is placed:

  * The shelf's cadence and next pay date (accrual_anchor_date) give the
    NOMINAL pay dates, walked the way Loaf walks them so the phase holds.
  * The period a check pays for ends pay_lag_days before its nominal date, and
    starts the day after the previous one ended. Period ends never move.
  * The money arrives on the nominal date, moved off a weekend or a bank
    holiday (or one of the job's own holidays) - to the business day before
    by default, after, or not at all (shelf.payday_shift).

How it is priced:

  * Hourly: every hour worked is paid at the rate in the period it was
    worked; paid time off (PTO) is paid; a holiday is paid when the income
    says holidays are paid. With overtime on, each workweek's hours over 40
    earn the extra half in the period that workweek ENDS in - the same total
    as paying them at 1.5x, and it never leaves a period negative when a
    workweek straddles two of them. PTO and holidays do not count toward 40.
  * Salary: a yearly or monthly salary (salary_period), spread over the
    job's paychecks - a year over the number of pay dates the cadence gives
    in one (26 every other week, 24 twice a month), a month over the same
    times twelve - then docked for unpaid time off pro rata: by the job's
    period_hours when it has them, else by the hours scheduled in the
    period. Holidays and PTO change nothing; there is no overtime.
  * Take-home: gross times take_home_pct, rounded half-up to the cent.

Not modelled, said here so nobody assumes otherwise: daylight-saving changes
(an hour either way on two nights a year), overnight shifts (the job form
refuses them), a raise in the middle of a period (a raise prices every check
paid on or after the day it starts), and a short first period (the start date
means the first paycheck to forecast; a short one is edited by hand).
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

import loaf_holidays
from bucket_utils import recurring_occurrence_dates
from loaf_data import attends, scheduled_minutes
from loaf_forecast import _as_date, _as_list, _entry_split, _generate_from, _pay_date_before

PAY_TYPES = ('hourly', 'salary')
PAYDAY_SHIFTS = ('before', 'after', 'none')

OVERTIME_AFTER_HOURS = Decimal('40')
OVERTIME_PREMIUM = Decimal('0.5')       # on top of straight time: 1.5x in all
SATURDAY = 5

# The Federal Reserve's holidays - the days a deposit cannot land. A subset of
# Loaf's list, and observed differently: a holiday on a Sunday is kept on the
# Monday, but one on a Saturday is not moved to the Friday, because the banks
# are open that Friday. So loaf_holidays.date_of, which moves both, is not used.
BANK_HOLIDAY_SLUGS = ('new_year', 'mlk', 'presidents', 'memorial', 'juneteenth',
                      'independence', 'labor', 'columbus', 'veterans',
                      'thanksgiving', 'christmas')

CENT = Decimal('0.01')
HUNDRED = Decimal('100')


def _dec(value, default='0'):
    try:
        return Decimal(str(value)) if value not in (None, '') else Decimal(default)
    except Exception:
        return Decimal(default)


def _cents(amount):
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------- pay dates ----

def bank_holidays(first, last):
    """The dates in [first, last] a bank is shut for a holiday."""
    out = set()
    if first is None or last is None or last < first:
        return out
    for year in range(first.year, last.year + 1):
        for slug in BANK_HOLIDAY_SLUGS:
            _label, rule = loaf_holidays.BY_SLUG[slug]
            if rule[0] == 'fixed':
                when = date(year, rule[1], rule[2])
                if when.weekday() == 6:
                    when += timedelta(days=1)
            else:
                when = loaf_holidays._nth_weekday(year, rule[1], rule[2], rule[3])
            if first <= when <= last:
                out.add(when)
    return out


def shift_payday(when, rule, closed):
    """The day the money actually arrives: off weekends and `closed` days."""
    if rule == 'none':
        return when
    step = timedelta(days=1 if rule == 'after' else -1)
    guard = 0
    while (when.weekday() >= 5 or when in closed) and guard < 14:
        when += step
        guard += 1
    return when


def _pattern(shelf):
    interval = int(_dec(shelf.get('cadence_interval'), '1')) or 1
    unit = str(shelf.get('cadence_unit') or 'weeks')
    pattern = dict(weekdays=_as_list(shelf.get('weekdays')),
                   monthly_days=_as_list(shelf.get('monthly_days')),
                   yearly_day=shelf.get('yearly_day'),
                   yearly_month=shelf.get('yearly_month'))
    return interval, unit, pattern


def closed_days(shelf, first, last):
    """Bank holidays and the job's own holidays, between first and last."""
    return bank_holidays(first, last) | loaf_holidays.dates_between(
        shelf.get('holidays'), first, last, shelf.get('custom_holidays'))


def pay_periods(shelf, first, last):
    """
    [{'pay_date', 'nominal', 'start', 'end'}] for every paycheck whose money
    arrives in [first, last], oldest first. Empty when the job has no next pay
    date - there is then nothing to keep the phase by.
    """
    anchor = _as_date(shelf.get('accrual_anchor_date'))
    if anchor is None or first is None or last is None or last < first:
        return []
    lag = timedelta(days=max(0, int(_dec(shelf.get('pay_lag_days')))))
    rule = shelf.get('payday_shift') or 'before'
    if rule not in PAYDAY_SHIFTS:
        rule = 'before'
    interval, unit, pattern = _pattern(shelf)

    # A shifted payday moves at most a few days, so a margin either side finds
    # every nominal date whose money lands in the window.
    margin = timedelta(days=10)
    walk_from = _generate_from(anchor, first - margin, interval, unit)
    nominals = sorted(set(recurring_occurrence_dates(
        interval, unit, walk_from, last + margin, **pattern)))
    if not nominals:
        return []

    closed = closed_days(shelf, first - margin * 3, last + margin * 3)
    previous = _pay_date_before(nominals[0], interval, unit, **pattern)
    out = []
    for nominal in nominals:
        end = nominal - lag
        start = (previous - lag) + timedelta(days=1)
        pay_date = shift_payday(nominal, rule, closed)
        if first <= pay_date <= last:
            out.append({'pay_date': pay_date, 'nominal': nominal, 'start': start, 'end': end})
        previous = nominal
    return out


# ----------------------------------------------------------------- hours ----

def _time_off_by_date(shelf, baskets, entries):
    """({date: pto hours}, {date: uto hours}) for this job's bookings."""
    kinds = {}
    for basket in baskets or []:
        try:
            kinds[int(basket.get('id'))] = str(basket.get('basket_type') or 'pto').lower()
        except (TypeError, ValueError):
            continue
    pto, uto = {}, {}
    for entry in entries or []:
        if str(entry.get('direction') or 'use') != 'use':
            continue
        try:
            kind = kinds.get(int(entry.get('basket_id') or 0))
        except (TypeError, ValueError):
            kind = None
        if kind is None:
            continue
        target = uto if kind == 'uto' else pto
        # costing=False: the hours away, which is what pay follows - a day the
        # employer does not bill for is still a day nobody worked.
        for day, hours in _entry_split(shelf, entry, costing=False).items():
            target[day] = target.get(day, 0.0) + float(hours)
    return pto, uto


def day_hours(shelf, first, last, time_off=None):
    """
    {date: {'scheduled', 'holiday', 'pto', 'uto', 'worked'}} for each day in
    [first, last], in Decimal hours.

    scheduled counts only days somebody attends - a four-day week counted as
    five for leave is still paid for the four. A holiday's hours are taken out
    of scheduled ones; time off is taken from what is left (Loaf already gives
    a shut day no time-off hours, so nothing comes off twice).
    """
    pto, uto = time_off or ({}, {})
    shut = loaf_holidays.dates_between(shelf.get('holidays'), first, last,
                                       shelf.get('custom_holidays'))
    out = {}
    day = first
    while day <= last:
        weekday = day.weekday()
        scheduled = (Decimal(scheduled_minutes(shelf, weekday)) / 60
                     if attends(shelf, weekday) else Decimal('0'))
        scheduled = scheduled.quantize(CENT)
        holiday = scheduled if day in shut else Decimal('0')
        p = _dec(round(pto.get(day, 0.0), 2))
        u = _dec(round(uto.get(day, 0.0), 2))
        worked = max(Decimal('0'), scheduled - holiday - p - u)
        out[day] = {'scheduled': scheduled, 'holiday': holiday, 'pto': p, 'uto': u, 'worked': worked}
        day += timedelta(days=1)
    return out


def _workweek_end_weekday(shelf, period):
    """The weekday a workweek ends on: the period's own end for weekly pay,
    Saturday otherwise."""
    if str(shelf.get('cadence_unit') or 'weeks') == 'weeks':
        return period['end'].weekday()
    return SATURDAY


def overtime_hours(days, period, week_end_weekday):
    """Hours over 40 in each workweek that ENDS inside the period."""
    total = Decimal('0')
    day = period['start']
    while day <= period['end']:
        if day.weekday() == week_end_weekday:
            worked = sum((days[day - timedelta(days=k)]['worked'] for k in range(7)
                          if (day - timedelta(days=k)) in days), Decimal('0'))
            total += max(Decimal('0'), worked - OVERTIME_AFTER_HOURS)
        day += timedelta(days=1)
    return total


# -------------------------------------------------------------- paychecks ----

def paychecks_per_year(shelf):
    """How many pay dates the job's cadence gives in a year - the divisor a
    salary is spread by. Every other Friday is 26, the 15th and the last day
    24, monthly 12."""
    interval, unit, pattern = _pattern(shelf)
    if unit == 'weeks':
        per = Decimal(52) * max(1, len(pattern['weekdays'] or [])) / interval
    elif unit == 'months':
        per = Decimal(12) * max(1, len(pattern['monthly_days'] or [])) / interval
    elif unit == 'years':
        per = Decimal(1) / interval
    else:
        per = Decimal(365) / interval
    return per


def salary_per_check(link, shelf):
    """The gross per paycheck of a salary: yearly or monthly, spread."""
    rate = _dec(link.get('pay_rate'))
    per_year = paychecks_per_year(shelf)
    if per_year <= 0:
        return Decimal('0')
    yearly = rate * 12 if link.get('salary_period') == 'month' else rate
    return yearly / per_year


def paycheck(link, shelf, period, days):
    """
    (net, detail) for one period. `days` must cover the period and the six
    days before it (a workweek ending inside it can start earlier).
    """
    pay_type = link.get('pay_type')
    rate = _dec(link.get('pay_rate'))
    pct = _dec(link.get('take_home_pct'), '100')

    inside = [days[d] for d in sorted(days) if period['start'] <= d <= period['end']]
    scheduled = sum((d['scheduled'] for d in inside), Decimal('0'))
    holiday = sum((d['holiday'] for d in inside), Decimal('0'))
    pto = sum((d['pto'] for d in inside), Decimal('0'))
    uto = sum((d['uto'] for d in inside), Decimal('0'))
    worked = sum((d['worked'] for d in inside), Decimal('0'))
    detail = {'start': period['start'], 'end': period['end'], 'scheduled': scheduled,
              'worked': worked, 'holiday': holiday, 'pto': pto, 'uto': uto,
              'overtime': Decimal('0')}

    if pay_type == 'salary':
        denom = _dec(shelf.get('period_hours')) or scheduled
        share = (uto / denom) if denom > 0 else Decimal('0')
        gross = salary_per_check(link, shelf) * max(Decimal('0'), Decimal('1') - share)
    else:
        paid_hours = worked + pto + (holiday if int(_dec(link.get('paid_holidays'))) else Decimal('0'))
        gross = rate * paid_hours
        if int(_dec(link.get('overtime'))):
            ot = overtime_hours(days, period, _workweek_end_weekday(shelf, period))
            detail['overtime'] = ot
            gross += rate * OVERTIME_PREMIUM * ot

    gross = _cents(gross)
    net = _cents(gross * pct / HUNDRED)
    detail['gross'] = gross
    detail['net'] = net
    return net, detail


def occurrences(link, shelf, baskets, entries, first, last):
    """
    [(pay_date, net, detail)] for the paychecks arriving in [first, last],
    within the link's own start and end.
    """
    start = _as_date(link.get('start_date'))
    end = _as_date(link.get('end_date'))
    lo = max(first, start) if start else first
    hi = min(last, end) if end else last
    periods = pay_periods(shelf, lo, hi)
    if not periods:
        return []
    time_off = _time_off_by_date(shelf, baskets, entries)
    days = day_hours(shelf, periods[0]['start'] - timedelta(days=6), periods[-1]['end'], time_off)
    out = []
    for period in periods:
        net, detail = paycheck(link, shelf, period, days)
        out.append((period['pay_date'], net, detail))
    return out


def typical_net(link, shelf, today):
    """
    One full paycheck with no holidays and no time off - what the row's
    `amount` holds, for everything that reads a single figure (tables, the
    iOS app, widgets). None when the job has no pay dates.
    """
    clean = dict(shelf, holidays=None, custom_holidays=None)
    periods = pay_periods(clean, today, today + timedelta(days=400))
    if not periods:
        return None
    period = periods[0]
    days = day_hours(clean, period['start'] - timedelta(days=6), period['end'])
    net, _detail = paycheck(link, clean, period, days)
    return net
