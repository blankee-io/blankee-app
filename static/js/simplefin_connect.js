/* The SimpleFIN connect flow: paste a Setup Token, claim it, classify the
   accounts, save. One script for the setup wizard and the bank page so the
   two never drift. Requires the markup in templates/_simplefin_connect.html
   and jQuery; the CSRF header is added by _csrf.html's fetch hook. */
(function () {
    'use strict';

    var SUBTYPES = [
        ['checking', 'Checking'],
        ['savings', 'Savings'],
        ['credit_card', 'Credit card'],
        ['skip', "Don't import"]
    ];

    function esc(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function money(value, currency) {
        if (value === null || value === undefined) return '';
        var n = Number(value);
        if (isNaN(n)) return '';
        try {
            return n.toLocaleString(undefined, { style: 'currency', currency: currency || 'USD' });
        } catch (e) {
            return n.toFixed(2);
        }
    }

    function status(el, text, isError) {
        el.textContent = text || '';
        el.classList.toggle('sf-error', !!isError);
    }

    async function post(url, body) {
        var resp = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify(body || {})
        });
        var data = {};
        try { data = await resp.json(); } catch (e) { /* non-JSON: fall through */ }
        if (!resp.ok || data.status === 'error') {
            throw new Error(data.message || ('Request failed (' + resp.status + ')'));
        }
        return data;
    }

    function accountRow(acc, cards, stored, seen) {
        var chosen = (stored && stored.account_subtype) || acc.guessed_subtype || 'checking';
        if (stored && Number(stored.is_active) === 0) chosen = 'skip';
        // A second guessed checking (or savings) defaults to "Don't import":
        // the person picks which one is theirs rather than starting invalid.
        if ((chosen === 'checking' || chosen === 'savings') && seen[chosen]) chosen = 'skip';
        if (chosen === 'checking' || chosen === 'savings') seen[chosen] = true;
        var html = '<div class="account-selection-item sf-account" data-account-id="' + esc(acc.account_id) +
                   '" data-connection-id="' + esc(acc.connection_id) + '">' +
            '<div class="account-selection-info">' +
              '<div class="account-selection-name">' + esc(acc.account_name) +
                (acc.mask ? ' <span class="account-selection-mask">&middot;&middot;&middot;&middot;' + esc(acc.mask) + '</span>' : '') +
              '</div>' +
              '<div class="account-selection-details">' + esc(money(acc.current_balance, acc.currency)) + '</div>' +
            '</div>' +
            '<div class="sf-account-controls">' +
              '<select class="login-input sf-subtype" aria-label="Account type">';
        SUBTYPES.forEach(function (s) {
            html += '<option value="' + s[0] + '"' + (s[0] === chosen ? ' selected' : '') + '>' + s[1] + '</option>';
        });
        html += '</select>' +
              '<select class="login-input sf-card" aria-label="Blankee card"' + (chosen === 'credit_card' ? '' : ' hidden') + '>' +
                '<option value="new">Create a new card</option>';
        (cards || []).forEach(function (c) {
            var taken = c.linked_account_id && c.linked_account_id !== acc.account_id;
            var sel = (c.linked_account_id === acc.account_id) ? ' selected' : '';
            html += '<option value="' + esc(c.id) + '"' + sel + (taken ? ' disabled' : '') + '>' +
                    'Link to ' + esc(c.name) + (c.mask ? ' ····' + esc(c.mask) : '') +
                    (taken ? ' (already linked)' : '') + '</option>';
        });
        // The new card's name in Blankee, the bank's name to start with. Only
        // while "Create a new card" is chosen: an existing card keeps its own.
        var newCard = chosen === 'credit_card' && !(cards || []).some(function (c) { return c.linked_account_id === acc.account_id; });
        // What the Add Credit Account form asks for, so the card is complete
        // from the start: the bank sends only its name and balance.
        var days = '<option value="">not set</option>';
        for (var d = 1; d <= 31; d++) days += '<option value="' + d + '">' + d + '</option>';
        days += '<option value="LAST_DAY">last day of the month</option>';
        html += '</select>' +
              '<div class="sf-card-new"' + (newCard ? '' : ' hidden') + '>' +
                '<input type="text" class="login-input sf-card-name" aria-label="Card name in Blankee" ' +
                  'placeholder="Card name in Blankee" maxlength="60" value="' + esc(acc.account_name) + '">' +
                '<div class="sf-card-terms">' +
                  '<input type="number" class="login-input sf-card-rate" aria-label="Interest rate (%)" ' +
                    'placeholder="Rate %" min="0" max="100" step="0.001" inputmode="decimal">' +
                  '<select class="login-input sf-card-statement" aria-label="Statement closes on">' +
                    days.replace('not set', 'Statement: not set') + '</select>' +
                  '<select class="login-input sf-card-due" aria-label="Payment due on">' +
                    days.replace('not set', 'Due: not set') + '</select>' +
                '</div>' +
              '</div>' +
            '</div>' +
          '</div>';
        return html;
    }

    function syncCardName(row) {
        var sub = row.querySelector('.sf-subtype');
        var card = row.querySelector('.sf-card');
        var block = row.querySelector('.sf-card-new');
        if (!block) return;
        block.hidden = !(sub.value === 'credit_card' && card.value === 'new');
    }

    function render(root, payload) {
        var list = root.querySelector('#sf-account-list');
        var byConn = {};
        (payload.connections || []).forEach(function (c) { byConn[c.connection_id] = c; });
        var groups = {};
        (payload.accounts || []).forEach(function (a) {
            (groups[a.connection_id] = groups[a.connection_id] || []).push(a);
        });
        var stored = {};
        (payload.stored_accounts || []).forEach(function (s) { stored[s.account_id] = s; });
        var html = '';
        var seen = {};
        Object.keys(groups).forEach(function (cid) {
            var conn = byConn[cid] || {};
            html += '<div class="account-group-title">' + esc(conn.institution_name || 'Bank') +
                    (conn.error_msg ? ' <span class="sf-error">' + esc(conn.error_msg) + '</span>' : '') + '</div>';
            groups[cid].forEach(function (a) { html += accountRow(a, payload.existing_cards, stored[a.account_id], seen); });
        });
        if (!html) {
            html = '<p class="sf-status sf-error">SimpleFIN returned no accounts. Add a bank under ' +
                   'Financial Institutions on the Bridge, then press Connect again with a new token.</p>';
        }
        list.innerHTML = html;
        root.querySelector('#sf-accounts').hidden = false;
        list.querySelectorAll('.sf-subtype').forEach(function (sel) {
            sel.addEventListener('change', function () {
                var card = sel.parentElement.querySelector('.sf-card');
                card.hidden = sel.value !== 'credit_card';
                syncCardName(sel.closest('.sf-account'));
                enforceSingletons(list.querySelectorAll('.sf-subtype'));
            });
        });
        list.querySelectorAll('.sf-card').forEach(function (sel) {
            sel.addEventListener('change', function () { syncCardName(sel.closest('.sf-account')); });
        });
        enforceSingletons(list.querySelectorAll('.sf-subtype'));
    }

    /* One checking and one savings account per person - the rest of the app
       is built on that. Once a select holds one of them, the other selects
       lose that option, so the rule is visible before Save rather than a
       message after it. The server enforces it too. */
    function enforceSingletons(selects) {
        ['checking', 'savings'].forEach(function (kind) {
            var holders = Array.prototype.filter.call(selects, function (s) { return s.value === kind; });
            Array.prototype.forEach.call(selects, function (s) {
                var opt = s.querySelector('option[value="' + kind + '"]');
                if (!opt) return;
                var taken = holders.length > 0 && holders.indexOf(s) === -1;
                opt.disabled = taken;
                opt.textContent = (kind === 'checking' ? 'Checking' : 'Savings') + (taken ? ' (already chosen)' : '');
            });
        });
    }
    function collect(root) {
        var rows = [];
        root.querySelectorAll('.sf-account').forEach(function (row) {
            var subtype = row.querySelector('.sf-subtype').value;
            var cardSel = row.querySelector('.sf-card');
            var entry = {
                account_id: row.dataset.accountId,
                connection_id: row.dataset.connectionId,
                subtype: subtype
            };
            if (subtype === 'credit_card') {
                if (cardSel.value === 'new') {
                    var nameField = row.querySelector('.sf-card-name');
                    var rate = row.querySelector('.sf-card-rate');
                    entry.card = {
                        mode: 'new',
                        name: nameField ? nameField.value.trim() : '',
                        interest_rate: rate && rate.value !== '' ? rate.value : null,
                        statement_day: (row.querySelector('.sf-card-statement') || {}).value || null,
                        payment_due_day: (row.querySelector('.sf-card-due') || {}).value || null
                    };
                } else {
                    entry.card = { mode: 'existing', credit_account_id: Number(cardSel.value) };
                }
            }
            rows.push(entry);
        });
        return rows;
    }

    function validate(rows) {
        var checking = rows.filter(function (r) { return r.subtype === 'checking'; }).length;
        var savings = rows.filter(function (r) { return r.subtype === 'savings'; }).length;
        if (checking > 1) return 'Only one checking account can be imported. Set the others to "Don\'t import".';
        if (savings > 1) return 'Only one savings account can be imported. Set the others to "Don\'t import".';
        if (!rows.some(function (r) { return r.subtype !== 'skip'; })) return 'Choose at least one account to import, or skip this step.';
        return null;
    }

    window.SimpleFINConnect = {
        enforceSingletons: enforceSingletons,
        /* opts: { onLinked(result), onClaimed(payload) } */
        init: function (opts) {
            opts = opts || {};
            var root = document.getElementById('sf-connect');
            if (!root) return;
            var mode = root.dataset.mode || 'page';
            var tokenEl = root.querySelector('#sf-token');
            var claimBtn = root.querySelector('#sf-claim-btn');
            var claimStatus = root.querySelector('#sf-claim-status');
            var linkBtn = root.querySelector('#sf-link-btn');
            var linkStatus = root.querySelector('#sf-link-status');
            var claimUrl = mode === 'replace' ? '/bank/simplefin/replace-token' : '/bank/simplefin/claim';

            async function claim() {
                var token = tokenEl.value.trim();
                if (!token) { status(claimStatus, 'Paste the Setup Token first.', true); tokenEl.focus(); return; }
                claimBtn.disabled = true;
                status(claimStatus, 'Connecting to SimpleFIN…', false);
                try {
                    var data = await post(claimUrl, { token: token });
                    tokenEl.value = '';
                    status(claimStatus, data.message || 'Connected.', false);
                    render(root, data);
                    if (opts.onClaimed) opts.onClaimed(data);
                    root.querySelector('#sf-accounts').scrollIntoView({ behavior: 'smooth', block: 'start' });
                } catch (e) {
                    status(claimStatus, e.message, true);
                } finally {
                    claimBtn.disabled = false;
                }
            }

            claimBtn.addEventListener('click', claim);
            // Pasting is the whole gesture; do not make them find the button.
            tokenEl.addEventListener('paste', function () { setTimeout(claim, 50); });

            // A Setup Token is spent the moment it is claimed. Browsers restore
            // textarea contents on reload and on back/forward, which would put
            // a spent token back in the box and let it be resubmitted - and
            // SimpleFIN answers that with "already used". So the box is
            // emptied whenever the page is shown, not only after a claim.
            tokenEl.value = '';
            window.addEventListener('pageshow', function () { tokenEl.value = ''; });
            window.addEventListener('load', function () { setTimeout(function () { tokenEl.value = ''; }, 0); });

            linkBtn.addEventListener('click', async function () {
                var rows = collect(root);
                var problem = validate(rows);
                if (problem) { status(linkStatus, problem, true); return; }
                linkBtn.disabled = true;
                status(linkStatus, 'Saving…', false);
                try {
                    var data = await post('/bank/simplefin/link-accounts',
                                          { accounts: rows, mode: root.dataset.mode || 'page' });
                    status(linkStatus, data.message || 'Saved.', false);
                    if (opts.onLinked) opts.onLinked(data);
                } catch (e) {
                    status(linkStatus, e.message, true);
                } finally {
                    linkBtn.disabled = false;
                }
            });

            // Already connected (bank page "edit accounts" / wizard resume):
            // show the accounts straight away.
            if (opts.preload) {
                render(root, opts.preload);
            } else if (root.dataset.autoload === '1') {
                // Connected but nothing chosen yet - a reload between Connect
                // and Save, or a wizard resumed at this step. One request.
                var note = root.querySelector('#sf-already-connected');
                if (note) note.hidden = false;
                fetch('/bank/simplefin/accounts', { headers: { 'Accept': 'application/json' } })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (data.status === 'error') { status(claimStatus, data.message || 'Could not load your accounts.', true); return; }
                        render(root, data);
                        if (opts.onClaimed) opts.onClaimed(data);
                    })
                    .catch(function () { status(claimStatus, 'Could not load your accounts. Reload to try again.', true); });
            }
        },
        render: function (payload) {
            var root = document.getElementById('sf-connect');
            if (root) render(root, payload);
        }
    };
})();
