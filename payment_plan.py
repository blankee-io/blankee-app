"""
A payment plan for a credit account (2026-10-18): equal monthly payments that
clear the card by a chosen month, or the fewest months a chosen maximum
payment allows. Pure - the search only; app.py's payment-plan routes read the
card and write the plan as its payment category's recurring expense.

THE WALK
  The plan is solved against a walk: walk(payment, months, stop) pays
  `payment` on each of the first `months` payment days and returns what is
  owed after each of them, and the interest charged on the way. app.py's walk
  replays the card's own forecast - the same charges, the same statement
  cycle and grace period (_InterestCycle) - so a plan that clears the card in
  the walk clears it in the forecast the person then sees. An estimate would
  not: a statement paid in full by its due date charges nothing, which an
  APR / 12 model cannot know, and on dev a five-month plan worked out that way
  left a 2,000 card 64.70 in credit.

  monthly_walk is the same thing without a card behind it - APR / 12 on the
  balance, a fixed amount of charges a month - for the tests, and for a card
  with no forecast to replay.

THE SEARCH
  More paid never leaves more owed, so the least payment that clears the card
  in n months is found by bisection, to the cent. The last payment lands at or
  a few cents past zero, never short of it.
"""
from decimal import Decimal, ROUND_HALF_UP

MAX_MONTHS = 600   # fifty years; anything longer is "never" for a person


def _cents(x):
    return float(Decimal(str(x)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def monthly_walk(balance, apr, charges):
    """A walk with no card behind it: APR / 12 a month, `charges` added a month."""
    try:
        r = max(float(apr or 0), 0.0) / 100.0 / 12.0
    except (TypeError, ValueError):
        r = 0.0
    charges = max(float(charges or 0), 0.0)

    def walk(payment, months, stop=False):
        owed, interest, after = float(balance), 0.0, []
        for _ in range(months):
            charge = round(owed * r, 2) if owed > 0 else 0.0
            interest += charge
            owed = round(owed + charge + charges - payment, 2)
            after.append(owed)
            if stop and owed <= 0:
                break
        return after, interest
    return walk


def _clears(walk, cents, months):
    owed, _ = walk(cents / 100.0, months)
    return bool(owed) and owed[-1] <= 0.005


def least_payment(walk, months, ceiling=None):
    """The least payment, in cents, that clears the card in `months` payments - or None."""
    lo, hi = 0, 100
    while not _clears(walk, hi, months):
        if ceiling is not None and hi >= ceiling:
            if not _clears(walk, ceiling, months):
                return None
            hi = ceiling
            break
        lo, hi = hi, hi * 2
        if hi > 10 ** 12:
            return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _clears(walk, mid, months):
            hi = mid
        else:
            lo = mid
    return hi


def plan(balance, mode, value, walk, least=None):
    """
    mode 'payment': value is the most the person can pay a month.
    mode 'months':  value is how many months to pay it off in.

    `least` is what a month adds to the card - its interest and charges - for
    the message when a payment never gets anywhere.

    Returns {'ok', 'error', 'payment', 'months', 'interest'}: payment each
    month for months months, and the interest that costs on the way.
    """
    out = {'ok': False, 'error': None, 'payment': None, 'months': None, 'interest': None}
    try:
        balance = float(balance or 0)
        value = float(value)
    except (TypeError, ValueError):
        out['error'] = 'Enter a number.'
        return out
    if balance <= 0:
        out['error'] = 'There is nothing owed on this card to pay off.'
        return out

    if mode == 'months':
        months = int(value)
        if months < 1 or months != value:
            out['error'] = 'Enter a whole number of months, 1 or more.'
            return out
        if months > MAX_MONTHS:
            out['error'] = f'Pick {MAX_MONTHS} months or fewer.'
            return out
        cents = least_payment(walk, months)
        if cents is None:
            out['error'] = 'Could not work out a payment for that.'
            return out
    elif mode == 'payment':
        if value <= 0:
            out['error'] = 'Enter the most you can pay a month.'
            return out
        ceiling = int(round(value * 100))
        owed, _ = walk(ceiling / 100.0, MAX_MONTHS, stop=True)
        if not owed or owed[-1] > 0.005:
            grows = owed and len(owed) > 1 and owed[-1] >= owed[0]
            if grows or len(owed) < MAX_MONTHS:
                tail = (f" - the card's own charges and its interest come to about "
                        f"{least:,.2f} a month. Pay more than that." if least else '.')
                out['error'] = f'{value:,.2f} a month never pays it off{tail}'
            else:
                out['error'] = 'At that payment it would take more than fifty years. Pay a little more.'
            return out
        months = len(owed)
        # The fewest months at the most they can pay; then the least payment
        # that still does it in that many, which is at most what they said.
        cents = least_payment(walk, months, ceiling=ceiling) or ceiling
    else:
        out['error'] = 'Choose a maximum payment or a number of months.'
        return out

    payment = cents / 100.0
    _, interest = walk(payment, months)
    out.update(ok=True, payment=payment, months=months, interest=_cents(max(interest, 0.0)))
    return out
