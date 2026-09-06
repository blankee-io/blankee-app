"""Loaf - the time off planner. Everything it serves lives under /loaf.

A blueprint rather than routes in app.py, because Loaf will have as many pages
as Blankee has and they should not be interleaved with them. It brings three
things worth having:

    /loaf/...   one prefix, declared once. A page adds a route and gets the
                prefix for free rather than repeating it and eventually
                mistyping it.

    loaf.*      one endpoint namespace. apps_registry attributes a request by
                the part before the dot, so a page added here is recognised as
                Loaf's without being listed anywhere.

    one gate    the check below runs for every route in the blueprint,
                including the ones not written yet. Guarding each route
                separately is a guard someone forgets on the twentieth page.

Templates live in templates/loaf/ for the same reason the routes live here.
"""

from flask import Blueprint, render_template, abort
from flask_login import login_required

import apps_registry


loaf = Blueprint('loaf', __name__, url_prefix='/loaf')


@loaf.before_request
def _require_enabled():
    """Nothing in Loaf answers unless this instance has Loaf switched on.

    On the blueprint rather than on each route: hiding the menu entry is not
    access control, and a per-route check is one somebody eventually leaves off
    a new page. This one covers pages that do not exist yet.
    """
    if not apps_registry.is_enabled('loaf'):
        abort(404)


@loaf.route('/')
@login_required
def home():
    """Whatever Loaf opens on. Its own dashboard, once there is one."""
    return render_template('loaf/dashboard.html')


@loaf.route('/dashboard')
@login_required
def dashboard():
    """The planner. A placeholder until there is something to plan with."""
    return render_template('loaf/dashboard.html')
