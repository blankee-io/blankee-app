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
    2. Give it a blueprint with a url_prefix, register it in app.py, and put
       its name in `blueprint`. Gate the whole blueprint with a before_request,
       not each route.
    3. Give it a nav and a menu - see templates/nav.html, which switches on
       current_app_id.
    4. Add its wordmark, mark and favicon to static/, matching Blankee's set:
       one wordmark for its own bar, one for a light background, one outline
       mark, one favicon. Include _head_icons.html in each page's head.
    An administrator turns it on from the admin console. Nothing else is needed:
    no migration, no template edit anywhere else, no change to this module's
    callers.
"""

import time


# id      - stable, stored in instance_apps.app_id, never renamed
# name    - what a user reads, and the alt text on its wordmark
# wordmark      - the app's name drawn, for its own bar: light on the teal
# wordmark_light- the same for a light background, where the light one vanishes
# mark          - the outline mark, for a menu row or a list. Shown as drawn
# favicon       - the browser tab icon, so a tab says which app it is
# mobile_icon   - the home-screen icon, or None to use Blankee's. A PNG at a
#                 fixed size, so an app without one borrows rather than breaks
# start   - the endpoint a menu entry points at
# blueprint - the app's blueprint name, which is also its endpoint namespace.
#             A request is attributed by the part of its endpoint before the
#             dot, so every page the app ever adds is recognised as its own
#             without being listed here. The host claims everything unclaimed
# host    - true for the application that serves the switch, which is Blankee
APPS = (
    {
        'id': 'blankee',
        'name': 'Blankee',
        'wordmark': 'logotext.svg',
        'wordmark_light': 'teallogotext.svg',
        'mark': 'logooutline.svg',
        'favicon': 'favicon.svg',
        'mobile_icon': 'mobileicon.png',
        'start': None,            # the user's chosen landing page, not a fixed one
        'blueprint': None,        # Blankee is the bare app; it has no prefix
        'host': True,
    },
    {
        'id': 'loaf',
        'name': 'Loaf',
        'wordmark': 'loaflogotext.svg',
        'wordmark_light': 'orangeloaflogotext.svg',
        'mark': 'loaflogooutline.svg',
        'favicon': 'loaffavicon.svg',
        'mobile_icon': None,      # borrows Blankee's until Loaf ships a PNG
        'start': 'loaf.dashboard',
        'blueprint': 'loaf',      # so /loaf/... and loaf.* are both its own
        'host': False,
    },
)

HOST_ID = 'blankee'

# Blueprint name -> app id, built once. Anything else is the host's.
_BY_BLUEPRINT = {
    app['blueprint']: app['id']
    for app in APPS
    if app.get('blueprint')
}

_BY_ID = {app['id']: app for app in APPS}

# The switches, cached briefly. This is read on every page render and changes
# about once a year, so a query each time would be pure waste - but the daemon
# runs several processes and a toggle in one is invisible to the others until
# their own cache lapses. A few seconds of that is the cost of not querying.
_CACHE_SECONDS = 15
_cache = {'at': 0.0, 'ids': frozenset()}


def app_for_endpoint(endpoint):
    """Which app a request belongs to. The host owns anything unclaimed.

    Flask names a blueprint's endpoints "<blueprint>.<view>", so the part
    before the dot is the answer for every page an app will ever have - there
    is no list to keep up to date, and no way to add a page that the shell then
    renders with the wrong nav.
    """
    namespace = (endpoint or '').split('.')[0] if '.' in (endpoint or '') else ''
    return _BY_BLUEPRINT.get(namespace, HOST_ID)


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
