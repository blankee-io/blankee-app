"""
Tying a bank's credit-card account to a Blankee credit account.

The rest of the app already reads the link - credit_accounts.linked_account_id
and is_linked drive entry locking, the "bank linked" badge, and which cards the
transaction importer routes purchases to - but nothing had written it since the
old vendor's routes were removed. These are the writers.

Redis-first, like every other credit_accounts write: the cached list is edited
and the table is marked dirty; credit_accounts is in the forced-flush list, so
a flush after linking persists it before anything depends on the row.
"""

import json
from typing import Any, Dict, List, Optional

from log_config import get_logger, log_info, log_error, log_warning

logger = get_logger(__name__)


def _cards(user_id: int) -> List[Dict[str, Any]]:
    from app import _get_credit_accounts_from_redis
    return _get_credit_accounts_from_redis(user_id) or []


def _save_cards(user_id: int, cards: List[Dict[str, Any]]) -> bool:
    from redis_manager import _redis_client, DecimalEncoder
    from app import PERSISTENT_CACHE_TTL
    if _redis_client is None:
        return False
    _redis_client.setex(f'credit_accounts:v1:{user_id}', PERSISTENT_CACHE_TTL,
                        json.dumps(cards, cls=DecimalEncoder))
    dirty = f'dirty_tables:{user_id}'
    _redis_client.sadd(dirty, 'credit_accounts')
    _redis_client.expire(dirty, PERSISTENT_CACHE_TTL)
    return True


def link_credit_account(user_id: int, linked_account_id: str, credit_account_id: int,
                        mask: Optional[str] = None) -> bool:
    """Point an existing Blankee card at a bank account. One bank account per card."""
    cards = _cards(user_id)
    if not cards:
        return False
    found = False
    for card in cards:
        if int(card.get('id', 0)) == int(credit_account_id):
            card['linked_account_id'] = linked_account_id
            card['is_linked'] = 1
            if mask and not card.get('mask'):
                card['mask'] = mask
            found = True
        elif card.get('linked_account_id') == linked_account_id:
            # The same bank account cannot back two cards.
            card['linked_account_id'] = None
            card['is_linked'] = 0
    if not found:
        log_warning(logger, 'CREDIT_LINK', f'credit account {credit_account_id} not found for user {user_id}')
        return False
    ok = _save_cards(user_id, cards)
    log_info(logger, 'CREDIT_LINK', f'user {user_id}: card {credit_account_id} linked to bank account {linked_account_id}')
    return ok


def unlink_credit_account(user_id: int, linked_account_id: str) -> bool:
    """Drop the link but keep the card and everything recorded against it."""
    cards = _cards(user_id)
    changed = False
    for card in cards:
        if card.get('linked_account_id') == linked_account_id:
            card['linked_account_id'] = None
            card['is_linked'] = 0
            changed = True
    if changed:
        _save_cards(user_id, cards)
        log_info(logger, 'CREDIT_LINK', f'user {user_id}: bank account {linked_account_id} unlinked')
    return changed


def create_linked_credit_account(user_id: int, name: str, linked_account_id: str,
                                 mask: Optional[str] = None,
                                 starting_balance: Optional[float] = None,
                                 interest_rate: Optional[float] = None,
                                 statement_day=None, payment_due_day=None) -> Optional[int]:
    """
    Create a Blankee card for a bank credit-card account and link it.

    Same side effects as the "Add credit account" form - the three system
    categories, the copy of the user's expense categories onto the card, and
    the "<name> payment" expense category - via the same helpers, so the two
    ways of making a card cannot drift. Interest rate is unknown to the bank
    feed, so 0 until the user edits the card. The starting balance is what
    the bank reports owed right now (SimpleFIN gives card balances as
    negative numbers; a card's starting balance in Blankee is positive).
    """
    from app import (_add_credit_account_to_redis, _add_categories_batch_to_redis,
                     _copy_expense_categories_to_new_credit_account, _add_category_to_redis,
                     _get_categories_from_redis, _next_display_order)
    owed = abs(float(starting_balance)) if starting_balance is not None else 0.0
    # The terms the bank does not send - the person can give them at link
    # time now; the statement and due days only mean anything together, as
    # the edit form treats them.
    both = bool(statement_day and payment_due_day)
    account_data = {
        'name': name,
        'interest_rate': float(interest_rate) if interest_rate is not None else 0.0,
        'statement_day': statement_day if both else None,
        'payment_due_day': payment_due_day if both else None,
        'is_card': 1,
        'is_line': 0,
        'starting_balance': owed,
        'mask': mask or None,
        'linked_account_id': linked_account_id,
        'is_linked': 1,
    }
    card_id = _add_credit_account_to_redis(user_id, account_data)
    if card_id is None:
        log_error(logger, 'CREDIT_LINK', f'user {user_id}: could not create a card for {linked_account_id}')
        return None
    default_ids = _add_categories_batch_to_redis('c_expense_categories', user_id, [
        {'account_id': card_id, 'name': 'Interest Charge', 'display_order': 0.0003, 'group_id': None,
         'is_recurring': 0, 'no_end_date': 0, 'hidden': 0, 'is_bundle': 0, 'is_interest': 1,
         'is_auto_adjustment': 0, 'is_system': 1},
        {'account_id': card_id, 'name': 'Uncategorized', 'display_order': 0.0001, 'group_id': None,
         'is_recurring': 0, 'no_end_date': 0, 'hidden': 0, 'is_bundle': 0, 'is_interest': 0,
         'is_auto_adjustment': 1, 'is_system': 1},
        {'account_id': card_id, 'name': 'Starting Balance', 'display_order': 0.0002, 'group_id': None,
         'is_recurring': 0, 'no_end_date': 0, 'hidden': 0, 'is_bundle': 0, 'is_interest': 0,
         'is_auto_adjustment': 0, 'is_system': 1},
    ])
    # What the bank says is owed becomes a Starting Balance entry, dated
    # today, as the Add Credit Account form records one. The figure on the
    # card row alone is not in the balance the walk computes; without the
    # entry the card measured as 0 at the next pull and the whole balance was
    # written in as an Uncategorized correction - the right total, looking
    # like a purchase.
    if owed > 0 and len(default_ids or []) >= 3:
        try:
            from app import _update_entry_in_redis, _user_today_for
            _update_entry_in_redis('c_expense_entries', user_id, default_ids[2],
                                   _user_today_for(user_id).isoformat(), owed, processed=1)
        except Exception as e:
            log_warning(logger, 'CREDIT_LINK', f'user {user_id}: could not record the starting balance for card {card_id}: {e}')
    _copy_expense_categories_to_new_credit_account(user_id, card_id)
    expense_categories = _get_categories_from_redis('expense_categories', user_id)
    display_order = _next_display_order(expense_categories or [], tier=1)
    _add_category_to_redis('expense_categories', user_id, {
        'user_id': user_id,
        'name': f'{name} payment',
        'display_order': display_order,
        'group_id': None,
        'is_recurring': 0,
        'is_auto_adjustment': 0,
        'no_end_date': 0,
        'hidden': 0,
        'is_bundle': 0,
        'is_credit_account': 1,
        'credit_account_id': card_id,
        'is_system': 0,
    })
    log_info(logger, 'CREDIT_LINK', f'user {user_id}: card {card_id} created for bank account {linked_account_id}')
    return card_id


def cards_for_linking(user_id: int) -> List[Dict[str, Any]]:
    """What the link screen offers under "link to an existing card"."""
    out = []
    for card in _cards(user_id):
        out.append({
            'id': card.get('id'),
            'name': card.get('name'),
            'mask': card.get('mask') or '',
            'linked_account_id': card.get('linked_account_id'),
            'is_linked': int(card.get('is_linked') or 0),
        })
    return out
