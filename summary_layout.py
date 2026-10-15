"""
How one person has arranged the Summary page (dashboard_summary.html), 2026-10-14.

Stored as JSON in users.summary_layout - Redis-first like every user setting,
written through _update_user_setting_in_redis and flushed with the rest of the
users row. Pure: nothing here touches Redis or MySQL.

    {
      "order":  ["bills", "balance", "ca:12", "savings"],
      "hidden": ["savings", "buffer"],
      "bills":  {"pinned": ["e:45", "c:12"],
                 "filter": {"source": "card", "kind": "all", "status": "unpaid"}}
    }

order   the right column's sections, top to bottom. A section missing from
        it (a card opened since, a section added in a later release) keeps
        its place in the page's own order, so nothing new is ever lost below
        the fold or dropped altogether.
hidden  sections shown only while arranging. 'buffer' is the left column's
        Short-term Buffer card: it can be hidden, not moved.
bills   pinned: Upcoming Bills rows by key - 'e:<expense category id>' for
        the budget's, 'c:<card expense category id>' for a card's - which sit
        at the top. A bill that no longer exists simply never matches.
        filter: which rows the list shows, by kind rather than by name -
        source (the budget's or a card's), kind (a bill, paid in one go, or a
        bucket spent down) and status this month (summary_buckets.
        month_status). 'all' on each is the list unfiltered.

Anything malformed is dropped rather than refused: the page posts the whole
layout on every change, and one bad key must not lose the rest.
"""
import json
import re

SECTION = re.compile(r'^(balance|savings|bills|ca:\d{1,10})$')
HIDEABLE = re.compile(r'^(buffer|balance|savings|bills|ca:\d{1,10})$')
BILL = re.compile(r'^[ec]:\d{1,10}$')
LIMIT = 500
FILTERS = {
    'source': ('all', 'budget', 'card'),
    'kind': ('all', 'bill', 'bucket'),
    'status': ('all', 'unpaid', 'paid'),
}


def _keys(values, pattern):
    out = []
    if not isinstance(values, list):
        return out
    for v in values[:LIMIT]:
        if isinstance(v, str) and pattern.match(v) and v not in out:
            out.append(v)
    return out


def clean(payload):
    """The layout a page sent, with everything that is not a known key dropped."""
    payload = payload if isinstance(payload, dict) else {}
    bills = payload.get('bills') if isinstance(payload.get('bills'), dict) else {}
    chosen = bills.get('filter') if isinstance(bills.get('filter'), dict) else {}
    return {
        'order': _keys(payload.get('order'), SECTION),
        'hidden': _keys(payload.get('hidden'), HIDEABLE),
        'bills': {
            'pinned': _keys(bills.get('pinned'), BILL),
            'filter': {name: chosen.get(name) if chosen.get(name) in allowed else 'all'
                       for name, allowed in FILTERS.items()},
        },
    }


def parse(raw):
    """The stored column as a layout - the empty one when unset or unreadable."""
    if isinstance(raw, dict):
        return clean(raw)
    try:
        return clean(json.loads(raw)) if raw else clean({})
    except (TypeError, ValueError):
        return clean({})


def bill_key(kind, category_id):
    """'e:' for the budget's bills, 'c:' for a card's - see the module docstring."""
    return ('c:' if kind == 'c' else 'e:') + str(int(category_id))
