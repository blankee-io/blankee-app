"""
Laying the forecasts of a recurring income calculated from pay.

pay_forecast.py works out each paycheck; this module reads what it needs -
the job (Loaf's shelf), the job's pools of hours and the time off booked in
them - and writes one forecast per paycheck, the way generate_income_entries
writes them for a fixed amount. Loaf's data layer is used directly: only
Loaf's pages are switched on and off per instance, never its data, so an
income calculated from a job works whether the Loaf app is enabled or not.

app._chain_generate sends every pay row here; nothing else should write a
pay row's forecasts.
"""
from datetime import date

from log_config import get_logger, log_info, log_warning

logger = get_logger(__name__)
TAG = 'PAY'


def _as_date(value):
    from app import _as_date as app_as_date
    return app_as_date(value)


def job_for(user_id, link):
    """(shelf, baskets on it, entries) for a pay row, or (None, [], [])."""
    import loaf_data
    try:
        shelf_id = int(link.get('shelf_id') or 0)
    except (TypeError, ValueError):
        shelf_id = 0
    shelf = loaf_data.get_shelf(user_id, shelf_id) if shelf_id else None
    if shelf is None:
        return None, [], []
    baskets = [b for b in loaf_data.get_baskets(user_id)
               if int(b.get('shelf_id') or 0) == int(shelf['id'])]
    ids = {int(b['id']) for b in baskets if b.get('id') is not None}
    entries = [e for e in loaf_data.get_entries(user_id) if int(e.get('basket_id') or 0) in ids]
    return shelf, baskets, entries


def paychecks(user_id, link, first, last):
    """[(pay_date, net, detail)] for a pay row's paychecks in [first, last]."""
    import pay_forecast
    shelf, baskets, entries = job_for(user_id, link)
    if shelf is None:
        return []
    return pay_forecast.occurrences(link, shelf, baskets, entries, first, last)


def generate(link, start_date, end_date, user_id):
    """
    Write a pay row's forecasts over [start_date, end_date]. A row whose job
    has gone (deleted in Loaf) falls back to its stored typical paycheck on
    its stored cadence, so the income keeps forecasting rather than stopping.
    """
    from app import _create_bucket_entry_and_record, generate_income_entries
    first = _as_date(start_date)
    last = _as_date(end_date)
    if first is None or last is None or last < first:
        return 0

    shelf, baskets, entries = job_for(user_id, link)
    if shelf is None:
        log_warning(logger, TAG, f'user {user_id}: recurring income {link.get("id")} has no job '
                                 f'{link.get("shelf_id")}; forecasting its typical paycheck')
        generate_income_entries(
            link['id'], link['category_id'], link['amount'], link['cadence_interval'],
            link['cadence_unit'], str(first), str(last),
            [w for w in str(link.get('weekdays') or '').split(',') if w],
            [d for d in str(link.get('monthly_days') or '').split(',') if d],
            link.get('yearly_day'), link.get('yearly_month'), user_id)
        return 0

    import pay_forecast
    made = 0
    for pay_date, net, _detail in pay_forecast.occurrences(link, shelf, baskets, entries, first, last):
        if net <= 0:
            continue
        _create_bucket_entry_and_record('income_entries', user_id, link['category_id'],
                                        pay_date, float(net), link['id'])
        made += 1
    log_info(logger, TAG, f'user {user_id}: {made} paycheck forecast(s) for recurring income '
                          f'{link.get("id")} from {first} to {last}')
    return made
