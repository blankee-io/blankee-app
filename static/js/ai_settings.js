/* AI categorization settings: save key + model, test, toggle, remove. One
   script for the wizard and the profile page. Requires the markup in
   templates/_ai_settings_panel.html; the CSRF header is added by
   _csrf.html's fetch hook. */
(function () {
    'use strict';

    async function post(url, body) {
        var resp = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify(body || {})
        });
        var data = null;
        try { data = await resp.json(); } catch (e) { data = null; }
        if (data === null) {
            // Not our JSON: a login redirect followed by fetch, or an error
            // page. Never treat it as success.
            throw new Error(resp.ok ? 'Unexpected reply - reload the page and try again.'
                                    : ('Request failed (' + resp.status + ')'));
        }
        if (!resp.ok || data.status === 'error') {
            var err = new Error(data.message || ('Request failed (' + resp.status + ')'));
            err.ai = data.ai;
            throw err;
        }
        return data;
    }

    window.AISettings = {
        /* opts: { onChanged(ai) } */
        init: function (opts) {
            opts = opts || {};
            var root = document.getElementById('ai-settings');
            if (!root) return;
            var keyEl = root.querySelector('#ai-api-key');
            var editBtn = root.querySelector('#ai-edit-key-btn');
            var cancelEditBtn = root.querySelector('#ai-cancel-edit-btn');
            var MASK = 'sk-ant-\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022';
            var keyStored = editBtn ? !editBtn.hidden : false;
            var editing = false;

            // Stored key: masked, read-only, with Edit. Editing: an empty
            // password field, with Cancel to go back to the mask.
            function showKeyField() {
                if (keyStored && !editing) {
                    keyEl.type = 'text';
                    keyEl.readOnly = true;
                    keyEl.value = MASK;
                    keyEl.placeholder = '';
                    editBtn.hidden = false;
                    cancelEditBtn.hidden = true;
                } else {
                    keyEl.type = 'password';
                    keyEl.readOnly = false;
                    if (keyEl.value === MASK) keyEl.value = '';
                    keyEl.placeholder = 'sk-ant-...';
                    editBtn.hidden = true;
                    cancelEditBtn.hidden = !keyStored;
                }
            }
            editBtn.addEventListener('click', function () {
                editing = true; keyTyped = false; keyEl.value = '';
                showKeyField(); keyEl.focus();
            });
            cancelEditBtn.addEventListener('click', function () {
                editing = false; keyTyped = false;
                showKeyField();
            });
            var modelEl = root.querySelector('#ai-model');
            var saveBtn = root.querySelector('#ai-save-btn');
            var testBtn = root.querySelector('#ai-test-btn');
            var clearBtn = root.querySelector('#ai-clear-btn');
            var statusEl = root.querySelector('#ai-status');
            var verifiedLine = root.querySelector('#ai-verified-line');
            var toggleBtn = root.querySelector('#ai-toggle-btn');
            var checkbox = root.querySelector('#ai-enabled');
            var gateNote = root.querySelector('#ai-gate-note');

            // Password managers autofill the key box; a value the user never
            // typed must not be submitted. Same defence as the SMTP password.
            var keyTyped = false;
            keyEl.addEventListener('input', function () { if (!keyEl.readOnly) keyTyped = true; });
            function clearUntyped() { if (!keyTyped && !keyEl.readOnly) keyEl.value = ''; }
            window.addEventListener('load', function () { setTimeout(clearUntyped, 0); });
            keyEl.addEventListener('focus', clearUntyped);
            showKeyField();

            function status(text, isError) {
                statusEl.textContent = text || '';
                statusEl.classList.toggle('sf-error', !!isError);
            }

            // The one place the page reflects server state, so save/test/toggle
            // cannot disagree with each other.
            function apply(ai) {
                if (!ai) return;
                root.dataset.enabled = ai.enabled ? '1' : '0';
                root.dataset.verified = ai.verified ? '1' : '0';
                root.dataset.bankLinked = ai.bank_linked ? '1' : '0';
                keyStored = !!ai.key_stored;
                editing = false;
                showKeyField();
                testBtn.disabled = !ai.key_stored;
                clearBtn.hidden = !ai.key_stored;
                verifiedLine.hidden = !ai.verified;
                if (modelEl.value !== ai.model) modelEl.value = ai.model;
                var canToggle = !!(ai.verified && ai.bank_linked);
                toggleBtn.disabled = !canToggle;
                checkbox.checked = !!ai.effective;
                toggleBtn.setAttribute('aria-pressed', ai.effective ? 'true' : 'false');
                toggleBtn.querySelector('.toggle-icon').classList.toggle('toggle-off', !ai.effective);
                if (!ai.bank_linked) gateNote.textContent = 'Connect a bank first - there is nothing to categorize without one.';
                else if (!ai.verified) gateNote.textContent = 'Test your key first.';
                else if (ai.enabled) gateNote.textContent = 'On. New transactions get a suggested category to confirm.';
                else gateNote.textContent = 'Off. Nothing is sent anywhere until you switch this on.';
                if (opts.onChanged) opts.onChanged(ai);
            }

            saveBtn.addEventListener('click', async function () {
                saveBtn.disabled = true;
                status('Saving…', false);
                try {
                    var data = await post('/ai/settings', {
                        api_key: (keyTyped && !keyEl.readOnly) ? keyEl.value.trim() : '',
                        model: modelEl.value
                    });
                    keyTyped = false;
                    status(data.message || 'Saved.', false);
                    apply(data.ai);
                } catch (e) {
                    status(e.message, true);
                    if (e.ai) apply(e.ai);
                } finally {
                    saveBtn.disabled = false;
                }
            });

            testBtn.addEventListener('click', async function () {
                testBtn.disabled = true;
                status('Asking Claude for a one-word answer…', false);
                try {
                    var data = await post('/ai/test', {});
                    status(data.message || 'The key works.', false);
                    apply(data.ai);
                } catch (e) {
                    status(e.message, true);
                    if (e.ai) apply(e.ai);
                } finally {
                    testBtn.disabled = false;
                }
            });

            clearBtn.addEventListener('click', async function () {
                if (!window.confirm('Remove your Anthropic API key from Blankee? AI categorization will be off.')) return;
                clearBtn.disabled = true;
                try {
                    var data = await post('/ai/clear-key', {});
                    status(data.message || 'Key removed.', false);
                    apply(data.ai);
                } catch (e) {
                    status(e.message, true);
                } finally {
                    clearBtn.disabled = false;
                }
            });

            toggleBtn.addEventListener('click', async function () {
                if (toggleBtn.disabled) return;
                var want = !checkbox.checked;
                toggleBtn.disabled = true;
                try {
                    var data = await post('/ai/toggle', { enabled: want });
                    status(data.message || '', false);
                    apply(data.ai);
                } catch (e) {
                    status(e.message, true);
                    if (e.ai) apply(e.ai);
                } finally {
                    toggleBtn.disabled = !(root.dataset.verified === '1' && root.dataset.bankLinked === '1');
                }
            });
        },
        apply: function (ai) {
            // Lets another script (the bank page, after a disconnect) push new
            // state into the panel without a reload.
            var root = document.getElementById('ai-settings');
            if (!root) return;
            var ev = new CustomEvent('ai-settings:apply', { detail: ai });
            root.dispatchEvent(ev);
        }
    };
})();
