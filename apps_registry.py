"""The apps this codebase can serve, and which of them an instance offers.

WHAT LIVES WHERE

    Here          what an app *is* - its id, its name, where it starts, the
                  icon it shows in a menu. Facts about the code, the same on
                  every deployment, so they belong in the source.

    instance_apps what an app *is switched to* on this deployment. One row per
                  app holding a single boolean. An app with no row is off,
                  which is what a new app looks like the moment its code lands.

Blankee is not in the table and cannot be switched off. It is the application
serving the switch, so a control that could turn it off is a control that could
lock everyone out of the thing holding it. It is in APPS because the menus need
to name it and link to it, and it is marked as the host.

ADDING AN APP
    1. Add an entry to APPS below.
    2. Give it routes, and add each of their endpoints to `endpoints`.
    3. Give it a nav and a menu - see templates/nav.html, which switches on
       current_app_id.
    An administrator turns it on from the admin console. Nothing else is needed:
    no migration, no template edit anywhere else, no change to this module's
    callers.
"""

import time


# id      - stable, stored in instance_apps.app_id, never renamed
# name    - what a user reads
# icon    - a Font Awesome class. Name the face you want - fa-light and the
#           rest - rather than forcing a weight: the menus let the icon
#           decide, so Pro gets the line art and Free gets its own
#           equivalent. Must survive install/build_fa_fallback.py --check.
# start   - the endpoint a menu entry points at
# endpoints - every endpoint that belongs to this app, so a request can be
#             attributed to it; the host app claims everything unclaimed
# host    - true for the application that serves the switch, which is Blankee
APPS = (
    {
        'id': 'blankee',
        'name': 'Blankee',
        'icon': 'fa-regular fa-wallet',
        'start': None,            # the user's chosen landing page, not a fixed one
        'endpoints': (),          # everything not claimed below
        'host': True,
    },
    {
        'id': 'loaf',
        'name': 'Loaf',
        'icon': 'fa-light fa-bread-slice',
        'start': 'loaf_page',
        'endpoints': ('loaf_page',),
        'host': False,
    },
)

HOST_ID = 'blankee'

# Endpoint -> app id, built once. Anything not in here is the host's.
_BY_ENDPOINT = {
    endpoint: app['id']
    for app in APPS
    for endpoint in app['endpoints']
}

_BY_ID = {app['id']: app for app in APPS}

# The switches, cached briefly. This is read on every page render and changes
# about once a year, so a query each time would be pure waste - but the daemon
# runs several processes and a toggle in one is invisible to the others until
# their own cache lapses. A few seconds of that is the cost of not querying.
_CACHE_SECONDS = 15
_cache = {'at': 0.0, 'ids': frozenset()}


def app_for_endpoint(endpoint):
    """Which app a request belongs to. The host owns anything unclaimed."""
    return _BY_ENDPOINT.get(endpoint or '', HOST_ID)


def get(app_id):
    """One app's definition, or None."""
    return _BY_ID.get(app_id)


def all_switchable():
    """Every app an administrator can turn on or off - so, not the host."""
    return tuple(a for a in APPS if not a.get('host'))


def enabled_ids(force=False):
    """The ids switched on for this instance. The host is always among them."""
    now = time.time()
    if not force and (now - _cache['at']) < _CACHE_SECONDS:
        return _cache['ids']

    found = set()
    try:
        from db_connections import get_db_pool
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT app_id FROM instance_apps WHERE enabled = 1")
            found = {row[0] for row in cursor.fetchall()}
            cursor.close()
    except Exception:
        # A missing table or an unreachable database means no sibling apps are
        # offered, which is the safe answer: the menus lose an entry rather
        # than the page losing itself.
        found = set()

    found.add(HOST_ID)
    _cache['at'] = now
    _cache['ids'] = frozenset(found)
    return _cache['ids']


def is_enabled(app_id):
    return app_id in enabled_ids()


def set_enabled(app_id, on):
    """Turn one app on or off for everyone. Refuses to touch the host."""
    if app_id == HOST_ID or app_id not in _BY_ID:
        return False

    from db_connections import get_db_pool
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO instance_apps (app_id, enabled) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE enabled = VALUES(enabled)",
            (app_id, 1 if on else 0))
        conn.commit()
        cursor.close()

    _cache['at'] = 0.0     # this process sees it at once; the others within 15s
    return True


def menu_apps(current_app_id):
    """The Other Apps list, for whichever app is being looked at.

    Everything enabled except the one you are already in - an entry pointing at
    where you are is a dead control - and the host first when you are not in
    it, because getting back is the move a user wants most.
    """
    enabled = enabled_ids()
    out = []
    if current_app_id != HOST_ID:
        out.append(_BY_ID[HOST_ID])
    out.extend(a for a in APPS
               if a['id'] in enabled and a['id'] != current_app_id
               and a['id'] != HOST_ID)
    return out
