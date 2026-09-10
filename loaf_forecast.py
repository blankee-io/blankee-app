"""Projecting a Loaf basket forward.

Pure functions only: no Redis, no MySQL, no current_user. Everything arrives as
arguments and leaves as return values, so the arithmetic can be tested on its
own - which matters more here than usual, because two of the rules below fail
by producing plausible wrong numbers rather than by raising.

WHY THIS IS A WALK AND NOT A SUM

    Two effects, pulling opposite ways, both depending on the balance carried
    into each period:

        The ceiling gives hours back.  At cap 120, accruing 8 leaves 120 and
                                       the 8 is lost. Take 40 first and the
                                       same 8 is kept.
        Absence takes them away.       Accrual is per hour worked, so those 40
                                       hours off shrink the next accrual in
                                       proportion.

    `current + future_accruals - planned_usage` cannot express either. Blankee
    has the same shape in update_daily_ca_totals, where interest is computed
    inside the balance walk because the charge depends on balances the walk has
    just produced.

WHY IT NEEDS EVERY BASKET'S ENTRIES

    Accrual is per hour worked and any absence counts, paid or unpaid. So a day
    booked against Unpaid shrinks the next accrual in Vacation, and a basket
    cannot be projected in isolation. `absence` is therefore a {date: hours}
    dict built from ALL of a user's entries, while `usage` is built from one
    basket's. The two are separate arguments precisely so a caller cannot
    conflate them by accident.

WHAT IS DELIBERATELY NOT MODELLED

    Overnight shifts (a day whose end is not after its start is treated as not
    worked), public holidays, split shifts, and DST - an hour is an hour. A
    partial day off is charged its overlap with no break deducted, because
    subtracting a lunch from a two-hour afternoon off would be wrong more often
    than right.
"""

import calendar
from datetime import date, datetime, timedelta

from bucket_utils import recurring_occurrence_dates
from loaf_data import attends, schedule_for, scheduled_minutes


# ------------------------------------------------------------- coercions ----
#
# Both read paths in loaf_data hand back JSON-shaped values - dates as
# 'YYYY-MM-DD' strings - but a caller may also pass real date objects, so
# everything here accepts either rather than trusting one.

def _as_date(value):
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _as_datetime(value):
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip().replace('T', ' ')
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        parsed = _as_date(text)
        return datetime(parsed.year, parsed.month, parsed.day) if parsed else None


def _as_float(value, default=0.0):
    if value is None or value == '':
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value):
    """A stored comma list as a list. 'friday' -> ['friday']; None -> []."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(',') if part.strip()]


def _midnight(day):
    return datetime(day.year, day.month, day.day)


# --------------------------------------------------- a booking, in hours ----

def hours_by_date(basket, entry):
    """What one booking costs, split across the dates it touches.

    Split rather than totalled because the accrual pro-rate needs to know which
    pay period an absence fell in, and the calendar needs a figure per day.

    all_day substitutes the whole scheduled day, which is why the column
    exists: a four-hour Friday costs four and not eight, and nobody has to fake
    a whole day as 00:00-23:59.
    """
    starts = _as_datetime(entry.get('starts_at'))
    ends = _as_datetime(entry.get('ends_at'))
    if not starts or not ends or ends <= starts:
        return {}

    all_day = bool(int(entry.get('all_day') or 0))
    out = {}
    day = starts.date()
    last = ends.date()

    while day <= last:
        shift = schedule_for(basket, day.weekday())

        # attends() as well as schedule_for(), and the two are not the same
        # test. schedule_for says the employer counts this day; attends says
        # somebody is there for it. A day that is counted but never attended
        # has hours - they are what keeps the accrual whole - and costs
        # nothing to book, because no leave is ever requested for it.
        #
        # Only job A and the calendar mask this way. _scheduled_hours_between
        # below must NOT: it is the denominator the accrual is pro-rated
        # against, and masking it there would quietly inflate every accrual.
        if shift and attends(basket, day.weekday()):
            full = shift['end'] - shift['start']
            if all_day:
                minutes = max(0, full - shift['break_minutes'])
            else:
                work_start = _midnight(day) + timedelta(minutes=shift['start'])
                work_end = _midnight(day) + timedelta(minutes=shift['end'])
                overlap_start = max(starts, work_start)
                overlap_end = min(ends, work_end)
                minutes = max(0, int((overlap_end - overlap_start).total_seconds() // 60))
                # The break comes off only when the whole working day is taken.
                if minutes >= full:
                    minutes = max(0, minutes - shift['break_minutes'])
            if minutes:
                out[day] = round(minutes / 60.0, 2)
        day += timedelta(days=1)

    return out


def entry_hours(basket, entry):
    """One booking's total cost - what loaf_entries.computed_hours holds."""
    return round(sum(hours_by_date(basket, entry).values()), 2)


def _entry_split(basket, entry):
    """A booking's per-date hours, honouring a manual override.

    An unedited booking is recomputed from the schedule: the projection owns
    it, so editing a work week restates it. An overridden one keeps the total
    the user typed, and the per-date split is scaled to sum to it - the shape
    still has to come from somewhere, and the schedule is the only thing that
    knows which days the range covers.
    """
    if str(entry.get('status') or 'planned') == 'cancelled':
        return {}

    split = hours_by_date(basket, entry)
    if not int(entry.get('hours_overridden') or 0):
        return split

    stored = _as_float(entry.get('hours'))
    computed = round(sum(split.values()), 2)

    if computed > 0:
        if abs(stored - computed) < 0.005:
            return split
        factor = stored / computed
        return {day: round(hours * factor, 2) for day, hours in split.items()}

    # No working day in the range, but a figure was insisted on. Put it on the
    # first date rather than dropping it: a booking the user can see and the
    # projection ignores is worse than one attributed to a plausible day.
    starts = _as_datetime(entry.get('starts_at'))
    return {starts.date(): stored} if starts and stored else {}


def _accumulate(target, split):
    for day, hours in split.items():
        target[day] = round(target.get(day, 0.0) + hours, 2)


def usage_by_date(basket, entries):
    """One basket's time off as {date: hours}, cancelled ones excluded.

    Hours spent, so credits are not here - see credit_by_date. Keeping the two
    apart rather than letting a credit be negative usage is what stops it
    reaching absence_by_date, where a negative would push hours worked above
    hours scheduled and quietly over-accrue.
    """
    total = {}
    basket_id = int(basket.get('id') or 0)
    for entry in entries:
        if int(entry.get('basket_id') or 0) != basket_id:
            continue
        if str(entry.get('direction') or 'use') != 'use':
            continue
        _accumulate(total, _entry_split(basket, entry))
    return total


def credit_by_date(basket, entries):
    """One basket's manual accruals as {date: hours}, cancelled excluded.

    Hours handed over rather than earned: a figure on a date, never costed
    against the working week. They are this basket's own, unlike absence,
    because being given hours in one pot says nothing about any other.
    """
    total = {}
    basket_id = int(basket.get('id') or 0)
    for entry in entries:
        if int(entry.get('basket_id') or 0) != basket_id:
            continue
        if str(entry.get('direction') or 'use') != 'accrue':
            continue
        if str(entry.get('status') or 'planned') == 'cancelled':
            continue
        when = _as_date(str(entry.get('starts_at') or '')[:10])
        if when is not None:
            _accumulate(total, {when: _as_float(entry.get('hours'))})
    return total


def absence_by_date(baskets, entries):
    """Every basket's bookings as {date: hours} - the hours NOT worked.

    Each entry is split against its own basket's schedule, because that is the
    week it was booked against. This is the figure the accrual pro-rate
    subtracts, and it deliberately does not care which basket the time came
    out of: an hour not worked is an hour not worked.
    """
    by_id = {int(b.get('id') or 0): b for b in baskets}
    total = {}
    for entry in entries:
        # A credit is not an absence. Being handed eight hours is not eight
        # hours away from work, and counting it as such would shrink the very
        # accrual it was meant to top up.
        if str(entry.get('direction') or 'use') != 'use':
            continue
        basket = by_id.get(int(entry.get('basket_id') or 0))
        if basket is None:
            continue
        _accumulate(total, _entry_split(basket, entry))
    return total


# ------------------------------------------------------------- the walk ----

def _year_turns(basket, after, through):
    """The dates this basket's accrual year turns over, within (after, through].

    Read from the basket rather than assumed to be 1 January: a hire
    anniversary or a fiscal year is at least as common, and hard-coding January
    would be wrong with no visible symptom.
    """
    month = int(_as_float(basket.get('year_start_month'), 1))
    day = int(_as_float(basket.get('year_start_day'), 1))
    month = min(max(month, 1), 12)

    out = []
    for year in range(after.year, through.year + 1):
        # A 29 February boundary is clamped to the 28th rather than skipped.
        # bucket_utils skips an out-of-range monthly day, which is right for a
        # payment and wrong here - skipping would mean no rollover at all in
        # three years out of four.
        last = calendar.monthrange(year, month)[1]
        turn = date(year, month, min(day, last))
        if after < turn <= through:
            out.append(turn)
    return out


def _pay_date_before(when, interval, unit, **pattern):
    """The pay date one period before `when`.

    The anchor is the NEXT pay date, so the period it closes began at the pay
    date before it - and that one is not in the generated series, because
    generation starts AT the anchor. Without this the first occurrence ends up
    as its own period start, the window is zero days long, nothing is
    scheduled in it, and the guard on `scheduled > 0` quietly earns nothing.
    The symptom is a first pay date that pays out zero and a forecast that
    only starts accruing on the second.

    Walks the same pattern from exactly one cadence earlier rather than just
    subtracting a span. Two reasons, and both bite:

      A semi-monthly basket paid on the 1st and the 15th, anchored on 15
      September, has a previous pay date of 1 September - not 15 August, which
      is what subtracting one month gives. The window would otherwise be twice
      its real length, and the pro-rate wrong for any absence inside it.

      Stepping back an exact multiple of the interval keeps the phase for the
      cadences that have no intrinsic one. "Every 14 days" generated from 14
      days earlier lands back on the anchor; from 21 days earlier it never
      lands on it at all.

    Falls back to the stepped-back date when the pattern yields nothing before
    `when`, which is the best available answer and still a real period.
    """
    from dateutil.relativedelta import relativedelta

    if unit == 'weeks':
        back = when - timedelta(weeks=interval)
    elif unit == 'months':
        back = when - relativedelta(months=interval)
    elif unit == 'years':
        back = when - relativedelta(years=interval)
    else:
        back = when - timedelta(days=interval)

    earlier = [d for d in recurring_occurrence_dates(
        interval, unit, back, when, **pattern) if d < when]
    return earlier[-1] if earlier else back


def _scheduled_hours_between(basket, after, through):
    """Hours this basket's week says are worked in (after, through].

    DO NOT add attends() here. This is the denominator the accrual is
    pro-rated against, and it has to keep counting a day the employer counts
    even when nobody is there for it - that is the entire point of
    accrual_only_weekdays. Masking it would drop a four-day week's denominator
    from forty hours to thirty-two and quietly inflate every accrual from
    then on, with numbers that still look plausible. The mask belongs in
    hours_by_date, which costs a booking, and in month_rows, which draws one.
    """
    minutes = 0
    day = after + timedelta(days=1)
    while day <= through:
        minutes += scheduled_minutes(basket, day.weekday())
        day += timedelta(days=1)
    return round(minutes / 60.0, 2)


def _hours_between(by_date, after, through):
    """Hours from a {date: hours} dict falling in (after, through]."""
    return round(sum(hours for day, hours in by_date.items()
                     if after < day <= through), 2)


def project(basket, usage, absence, through, today=None, credits=None):
    """Walk one basket forward and report what happens to its balance.

    usage    {date: hours} for THIS basket - what comes off the balance.
    absence  {date: hours} for EVERY basket - what reduces hours worked, and
             therefore what the next accrual is pro-rated by.
    through  the last date to project to. There is no materialised timeline, so
             the horizon is the caller's to choose.
    credits  {date: hours} for THIS basket - hours handed over by hand. Note
             they appear here and NOT in absence: a credit adds to the balance
             without anybody having been away, so it must not touch the
             pro-rate.

    Returns {'events', 'checkpoints', 'summary'}.
    """
    through = _as_date(through)
    today = _as_date(today) or date.today()

    accrual_hours = basket.get('accrual_hours')
    grant_hours = basket.get('grant_hours')
    ceiling = basket.get('max_balance_hours')
    anchor = _as_date(basket.get('accrual_anchor_date'))

    start = _as_date(basket.get('starting_date')) or anchor or today
    balance = _as_float(basket.get('starting_hours'))

    if through is None or through < start:
        through = start

    # Accrual dates are generated from the anchor rather than from the start,
    # so a basket adopted mid-year still lands its pay dates on the employer's
    # rhythm instead of on the day somebody typed a balance in.
    #
    # The anchor is the NEXT pay date, so it is one to pay and not one to
    # skip. It has no predecessor in this series - it is the first element -
    # and _pay_date_before supplies one below. This comment used to claim the
    # first occurrence already had one, which is what made the first pay date
    # earn nothing.
    interval = int(_as_float(basket.get('cadence_interval'), 1)) or 1
    unit = str(basket.get('cadence_unit') or 'weeks')

    occurrences = []
    if accrual_hours is not None and anchor:
        occurrences = recurring_occurrence_dates(
            interval, unit,
            anchor, through,
            weekdays=_as_list(basket.get('weekdays')),
            monthly_days=_as_list(basket.get('monthly_days')),
            yearly_day=basket.get('yearly_day'),
            yearly_month=basket.get('yearly_month'))

    period_start = {}
    if occurrences:
        # The first one needs a predecessor that the series does not contain -
        # see _pay_date_before. Everything after it has a real one.
        previous = _pay_date_before(
            occurrences[0], interval, unit,
            weekdays=_as_list(basket.get('weekdays')),
            monthly_days=_as_list(basket.get('monthly_days')),
            yearly_day=basket.get('yearly_day'),
            yearly_month=basket.get('yearly_month'))
    else:
        previous = anchor
    for occurrence in occurrences:
        period_start[occurrence] = previous
        previous = occurrence

    accrual_dates = sorted(set(d for d in occurrences if d > start))
    turn_dates = sorted(set(_year_turns(basket, start, through)))

    # Every date the balance can move on, gathered before the loop so the loop
    # itself is pure arithmetic - the property that makes Blankee's walks
    # readable.
    credits = credits or {}
    stops = set(accrual_dates) | set(turn_dates)
    stops |= set(d for d in usage if start <= d <= through)
    stops |= set(d for d in credits if start <= d <= through)

    events = []
    checkpoints = [(start, balance)]
    accrued_total = 0.0
    used_total = 0.0

    for when in sorted(stops):
        moved = {'date': when, 'turned_over': False,
                 'granted': 0.0, 'accrued': 0.0, 'credited': 0.0, 'used': 0.0}

        # 1. The year turns first: carryover, then any flat grant.
        if when in turn_dates:
            mode = str(basket.get('carryover_mode') or 'reset')
            if mode == 'reset':
                balance = 0.0
            elif mode == 'capped':
                cap = basket.get('carryover_cap_hours')
                balance = min(balance, _as_float(cap, 0.0))
            # 'all' carries the balance across untouched.
            moved['turned_over'] = True

            if grant_hours is not None:
                granted = _as_float(grant_hours)
                balance += granted
                moved['granted'] = granted
                # Not clamped to the ceiling. The ceiling stops ACCRUAL; a
                # grant is a grant, and an employer who hands over 80 hours
                # has handed them over whatever the cap says.

        # 2. Then accrual, pro-rated by hours actually worked in the period
        #    that just ended, and clamped by the ceiling.
        if when in accrual_dates:
            window_start = period_start.get(when, start)
            scheduled = _scheduled_hours_between(basket, window_start, when)
            absent = _hours_between(absence, window_start, when)
            worked = max(0.0, scheduled - absent)

            if scheduled > 0:
                earned = round(_as_float(accrual_hours) * (worked / scheduled), 2)
            else:
                # A period with nothing scheduled earns nothing. Guards the
                # division, and is also the right answer for a basket whose
                # week is entirely blank.
                earned = 0.0

            before = balance
            balance += earned
            if ceiling is not None:
                # THE CLAMP. Inside the loop on purpose: this single line is
                # what makes booking time off preserve future accrual, because
                # it is the balance carried in that decides how much of the
                # accrual survives. Hoisted out, or replaced by
                # periods * rate, the app still produces plausible numbers -
                # just wrong ones, understating what can be taken.
                balance = min(balance, _as_float(ceiling))
            moved['accrued'] = round(balance - before, 2)
            accrued_total = round(accrued_total + moved['accrued'], 2)

        # 3. Then anything handed over by hand. Not clamped by the ceiling,
        #    for the reason the grant above is not: the cap stops ACCRUAL, and
        #    somebody who has been given eight hours has been given them
        #    whatever the cap says. It is also why a credit sits here rather
        #    than being folded into the accrual step - the two are added the
        #    same way and clamped differently.
        if when in credits and start <= when <= through:
            credited = round(_as_float(credits[when]), 2)
            balance += credited
            moved['credited'] = credited
            accrued_total = round(accrued_total + credited, 2)

        # 4. Then what was taken that day.
        if when in usage and start <= when <= through:
            taken = usage[when]
            balance -= taken
            moved['used'] = taken
            used_total = round(used_total + taken, 2)

        balance = round(balance, 2)
        moved['balance'] = balance
        events.append(moved)
        checkpoints.append((when, balance))

    return {
        'events': events,
        'checkpoints': checkpoints,
        'summary': _summarise(basket, checkpoints, events, through, today,
                              accrued_total, used_total),
    }


def balance_on(result, when):
    """The balance at the end of a given day.

    The series only has points where something happened, because the balance is
    flat between them - which is the whole reason Loaf projects on demand
    instead of storing a row per day.
    """
    when = _as_date(when)
    answer = None
    for day, balance in result['checkpoints']:
        if day <= when:
            answer = balance
        else:
            break
    if answer is None:
        answer = result['checkpoints'][0][1] if result['checkpoints'] else 0.0
    return answer


def _summarise(basket, checkpoints, events, through, today, accrued, used):
    """The figures a screen actually shows.

    year_end and lowest are different numbers and diverge whenever a large
    booking sits before later accruals - one says what is left, the other says
    whether the plan is affordable at all. Both are reported rather than one
    standing in for the other.
    """
    result = {'checkpoints': checkpoints}
    current = None
    for day, balance in checkpoints:
        if day <= today:
            current = balance
        else:
            break
    if current is None:
        current = checkpoints[0][1] if checkpoints else 0.0

    # The end of the accrual year today falls in: the day before the next turn,
    # or the horizon if none arrives first.
    upcoming = [e['date'] for e in events if e['turned_over'] and e['date'] > today]
    year_end_date = (upcoming[0] - timedelta(days=1)) if upcoming else through

    lowest = None
    lowest_date = None
    first_negative = None
    for day, balance in checkpoints:
        if day < today:
            continue
        if lowest is None or balance < lowest:
            lowest, lowest_date = balance, day
        if first_negative is None and balance < 0:
            first_negative = day

    threshold = basket.get('low_balance_hours')
    result.update({
        'balance_today': current,
        'year_end_date': year_end_date,
        'balance_year_end': _balance_at(checkpoints, year_end_date),
        'lowest': current if lowest is None else lowest,
        'lowest_date': lowest_date or today,
        'first_negative': first_negative,
        'accrued_total': accrued,
        'used_total': used,
        'low_threshold': None if threshold is None else _as_float(threshold),
    })
    return result


def _balance_at(checkpoints, when):
    answer = checkpoints[0][1] if checkpoints else 0.0
    for day, balance in checkpoints:
        if day <= when:
            answer = balance
        else:
            break
    return answer


# ----------------------------------------------------------- the horizon ----

def accrual_year_bounds(basket, when):
    """The accrual year a given date falls in, as (first, last).

    Read from the basket, so a fiscal or anniversary year is handled the same
    way a calendar one is. The last day is the day before the next turn, which
    is what makes "what will be left at the end of the year" a date rather than
    an assumption about December.
    """
    when = _as_date(when) or date.today()
    month = int(_as_float(basket.get('year_start_month'), 1))
    day = int(_as_float(basket.get('year_start_day'), 1))
    month = min(max(month, 1), 12)

    def turn(year):
        last = calendar.monthrange(year, month)[1]
        return date(year, month, min(day, last))

    first = turn(when.year)
    if first > when:
        first = turn(when.year - 1)
    return first, turn(first.year + 1) - timedelta(days=1)


def month_bounds(year, month):
    """The first and last day of a calendar month."""
    year, month = int(year), int(month)
    return (date(year, month, 1),
            date(year, month, calendar.monthrange(year, month)[1]))


def horizon_for(basket, viewing, today=None):
    """How far to project when someone is looking at one month.

    Far enough to answer both questions on the screen: what happens in the
    month being viewed, and what will be left at the end of the accrual year.
    Whichever of those is later wins - projecting only to the end of the viewed
    month would leave the year-end figure blank every January.
    """
    today = _as_date(today) or date.today()
    _, month_end = month_bounds(viewing.year, viewing.month)
    _, year_end = accrual_year_bounds(basket, today)
    return max(month_end, year_end)


def grid_bounds(year, month, week_starts_on=5):
    """The six-by-seven span a month calendar draws.

    week_starts_on is a weekday index with monday=0; 5 is Saturday, which is
    what dashboard_m starts its week on. The grid always covers whole weeks, so
    it reaches into the months either side - those are the cells that get
    dimmed rather than left blank.
    """
    first, last = month_bounds(year, month)
    lead = (first.weekday() - week_starts_on) % 7
    start = first - timedelta(days=lead)
    # Six rows always, so the grid does not change height from month to month.
    return start, start + timedelta(days=41), first, last

# ------------------------------------------------- what a calendar shows ----

def month_rows(basket, result, absence, first, last):
    """One row per day for the detail calendar.

    worked is what the week says, less whatever was taken that day in ANY
    basket - you are not at work regardless of which pot the time came from.
    taken is this basket's own hours, since the page shows one basket at a
    time and a basket is wholly PTO or wholly UTO.
    """
    first, last = _as_date(first), _as_date(last)
    by_date = {e['date']: e for e in result['events']}

    rows = []
    day = first
    while day <= last:
        event = by_date.get(day)

        # Masked, so a counted-but-unattended day draws as the day off it is:
        # non_working below turns true and worked falls to zero. The accrual
        # is unaffected - project() reads the unmasked week through
        # _scheduled_hours_between and never comes through here.
        scheduled = 0.0 if not attends(basket, day.weekday()) else round(
            scheduled_minutes(basket, day.weekday()) / 60.0, 2)
        away = absence.get(day, 0.0)
        rows.append({
            'date': day,
            'non_working': scheduled == 0,
            'scheduled': scheduled,
            'worked': round(max(0.0, scheduled - away), 2),
            'taken': (event or {}).get('used', 0.0),
            'accrued': (event or {}).get('accrued', 0.0),
            'granted': (event or {}).get('granted', 0.0),
            'credited': (event or {}).get('credited', 0.0),
            'balance': balance_on(result, day),
        })
        day += timedelta(days=1)
    return rows
