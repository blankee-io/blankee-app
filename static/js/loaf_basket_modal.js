/* The add/edit basket form, for every page that offers one.
 *
 * Pairs with templates/loaf/_basket_modal.html. A page includes that, loads
 * this, and opens it from whatever it likes:
 *
 *     loafBasketModal.openAdd()          a new basket, with a sensible week
 *     loafBasketModal.openEdit(id)       filled from a table row's data-*
 *
 * openEdit needs a `tr[data-basket-id]` carrying the figures, which only the
 * Manage Baskets table has. openAdd needs nothing, which is why the month view
 * can offer it beside the basket switcher.
 *
 * A plain file rather than an inline block, because two pages want it and two
 * copies of a form this size is the trap this codebase has already been caught
 * by: recurring_i.html carries two near-identical modals and they have drifted.
 * The weekday lists arrive on the element as data attributes so nothing here
 * needs Jinja.
 *
 * Saving reloads the page. Both callers want that - the table has a new row,
 * the switcher has a new option - and it is what Manage Baskets already did.
 */
(function () {
    'use strict';

    var modal = document.getElementById('basket-modal');
    if (!modal) { return; }

    var form = document.getElementById('basket-form');
    var WEEKDAY_PREFIXES = JSON.parse(modal.getAttribute('data-weekday-prefixes'));

    function el(id) { return document.getElementById(id); }
    function val(id) { return el(id).value; }

    // The monthly day picker, in the markup the recurring forms use so it
    // inherits .monthly-day-select-container and .remove-day-btn unchanged.
    // Rebuilt here rather than imported because recurring_i.html defines it
    // inline too - it has never lived anywhere shared.
    function monthlyDaySelect(selected) {
        var wrap = document.createElement('div');
        wrap.className = 'monthly-day-select-container';

        var select = document.createElement('select');
        select.className = 'monthly-day-select';
        for (var day = 1; day <= 31; day++) {
            select.appendChild(new Option(String(day), String(day)));
        }
        select.appendChild(new Option('Last Day', 'Last Day'));
        if (selected) { select.value = selected; }

        var remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'remove-day-btn';
        remove.textContent = '-';
        remove.addEventListener('click', function () {
            var container = el('basket-monthly-container');
            if (container.querySelectorAll('.monthly-day-select-container').length <= 1) {
                showToast('At least one day must be selected.', 'warning');
                return;
            }
            wrap.remove();
        });

        wrap.appendChild(select);
        wrap.appendChild(remove);
        return wrap;
    }

    // Weekly shows as flex and monthly as block. Not a typo - it is what the
    // stylesheet expects, and swapping them collapses the weekday row.
    function syncCadence() {
        var unit = val('basket-cadence-unit');
        el('basket-weekly').style.display = unit === 'weeks' ? 'flex' : 'none';
        el('basket-monthly').style.display = unit === 'months' ? 'block' : 'none';
        el('basket-yearly').style.display = unit === 'years' ? 'block' : 'none';

        if (unit === 'months' &&
            !el('basket-monthly-container').querySelector('.monthly-day-select')) {
            el('basket-monthly-container').appendChild(monthlyDaySelect(null));
        }
    }

    function syncCarryover() {
        el('basket-carryover-cap-row').style.display =
            val('basket-carryover-mode') === 'capped' ? 'block' : 'none';
    }

    // A ticked box greys its figure out and clears it, so what is submitted is
    // empty - which clean_basket turns into NULL. Disabled rather than hidden,
    // so the form does not change height as the box is toggled.
    function syncNone(boxId, fieldId) {
        var off = el(boxId).checked;
        var field = el(fieldId);
        field.disabled = off;
        if (off) { field.value = ''; }
    }

    function syncAmounts() {
        syncNone('basket-no-accrual', 'basket-accrual-hours');
        syncNone('basket-no-grant', 'basket-grant-hours');
        syncNone('basket-warn-negative-only', 'basket-low-balance-hours');
    }

    // Off is the same idea for a day: the engine reads a missing start as a day
    // not worked, and the box says so rather than leaving two blanks that read
    // as unfinished.
    function syncDay(prefix) {
        var off = el('basket-' + prefix + '-off').checked;
        ['start', 'end'].forEach(function (part) {
            var field = el('basket-' + prefix + '-' + part);
            field.disabled = off;
            if (off) { field.value = ''; }
        });
    }

    function syncWeek() {
        WEEKDAY_PREFIXES.forEach(syncDay);
    }

    function clearForm() {
        form.reset();
        el('basket-id').value = '';
        el('basket-monthly-container').innerHTML = '';
        $('.basket-weekday').prop('checked', false);
        el('basket-no-accrual').checked = false;
        el('basket-no-grant').checked = false;
        el('basket-warn-negative-only').checked = false;
        el('basket-break').value = '0';
        WEEKDAY_PREFIXES.forEach(function (prefix) {
            el('basket-' + prefix + '-start').value = '';
            el('basket-' + prefix + '-end').value = '';
            el('basket-' + prefix + '-off').checked = false;
        });
        syncAmounts();
        syncWeek();
    }

    function openAdd() {
        clearForm();
        el('basket-modal-title').textContent = 'Add a Basket';
        el('basket-submit').textContent = 'Add Basket';
        el('basket-starting-hours').value = '0.00';
        el('basket-cadence-interval').value = '2';
        el('basket-cadence-unit').value = 'weeks';
        el('basket-year-start-month').value = '1';
        el('basket-year-start-day').value = '1';
        el('basket-carryover-mode').value = 'reset';
        // No threshold by default, which IS "only when negative" - so the box
        // starts ticked rather than the field starting blank with nothing
        // saying what blank means.
        el('basket-warn-negative-only').checked = true;
        // A sensible week rather than a blank grid: Mon-Fri, nine to five, with
        // an unpaid half hour. Every part is editable, and a blank grid would
        // silently mean "I never work", which no basket wants.
        ['mon', 'tue', 'wed', 'thu', 'fri'].forEach(function (prefix) {
            el('basket-' + prefix + '-start').value = '09:00';
            el('basket-' + prefix + '-end').value = '17:00';
        });
        ['sat', 'sun'].forEach(function (prefix) {
            el('basket-' + prefix + '-off').checked = true;
        });
        el('basket-break').value = '30';
        $('.basket-weekday[value="friday"]').prop('checked', true);
        syncCadence();
        syncCarryover();
        syncAmounts();
        syncWeek();
        modal.style.display = 'flex';
    }

    function openEdit(basketId) {
        var row = $('tr[data-basket-id="' + basketId + '"]');
        if (!row.length) { return; }
        clearForm();

        el('basket-modal-title').textContent = 'Edit Basket';
        el('basket-submit').textContent = 'Save Basket';
        el('basket-id').value = basketId;

        // Straight off the row's data-* attributes. The cells hold formatted
        // strings; these hold the figures.
        el('basket-name').value = row.attr('data-name') || '';
        el('basket-type').value = row.attr('data-basket-type') || 'pto';
        el('basket-starting-hours').value = row.attr('data-starting-hours') || '0';
        var warn = row.attr('data-low-balance-hours') || '';
        el('basket-low-balance-hours').value = warn;
        el('basket-warn-negative-only').checked = warn === '';

        // An empty figure IS "does not accrue" - that is what the NULL means -
        // so the box comes back ticked rather than the field coming back blank
        // with nothing saying why.
        var accrual = row.attr('data-accrual-hours') || '';
        var grant = row.attr('data-grant-hours') || '';
        el('basket-accrual-hours').value = accrual;
        el('basket-grant-hours').value = grant;
        el('basket-no-accrual').checked = accrual === '';
        el('basket-no-grant').checked = grant === '';
        el('basket-accrual-anchor-date').value = row.attr('data-accrual-anchor-date') || '';
        el('basket-cadence-interval').value = row.attr('data-cadence-interval') || '1';
        el('basket-cadence-unit').value = row.attr('data-cadence-unit') || 'weeks';
        el('basket-yearly-day').value = row.attr('data-yearly-day') || '';
        el('basket-yearly-month').value = row.attr('data-yearly-month') || '1';
        el('basket-year-start-month').value = row.attr('data-year-start-month') || '1';
        el('basket-year-start-day').value = row.attr('data-year-start-day') || '1';
        el('basket-carryover-mode').value = row.attr('data-carryover-mode') || 'reset';
        el('basket-carryover-cap-hours').value = row.attr('data-carryover-cap-hours') || '';

        var days = (row.attr('data-weekdays') || '').split(',');
        $('.basket-weekday').each(function () {
            this.checked = days.indexOf(this.value) !== -1;
        });

        // One break for the week now, so the largest of the seven is shown -
        // a basket saved before this form existed may genuinely differ per day,
        // and reading the biggest is closer than reading Monday's.
        var longest = 0;
        WEEKDAY_PREFIXES.forEach(function (prefix) {
            // Stored as HH:MM:SS; an input[type=time] wants HH:MM.
            var start = row.attr('data-' + prefix + '-start') || '';
            var end = row.attr('data-' + prefix + '-end') || '';
            el('basket-' + prefix + '-start').value = start ? start.substring(0, 5) : '';
            el('basket-' + prefix + '-end').value = end ? end.substring(0, 5) : '';
            el('basket-' + prefix + '-off').checked = !start;
            longest = Math.max(longest,
                parseInt(row.attr('data-' + prefix + '-break-minutes') || '0', 10) || 0);
        });
        el('basket-break').value = String(longest);

        syncCadence();
        syncCarryover();
        syncAmounts();
        syncWeek();

        var monthly = (row.attr('data-monthly-days') || '').split(',').filter(Boolean);
        if (val('basket-cadence-unit') === 'months') {
            var container = el('basket-monthly-container');
            container.innerHTML = '';
            (monthly.length ? monthly : ['1']).forEach(function (day) {
                container.appendChild(monthlyDaySelect(day));
            });
        }

        modal.style.display = 'flex';
    }

    function close() { modal.style.display = 'none'; }

    function collect() {
        var payload = {
            name: val('basket-name'),
            basket_type: val('basket-type'),
            starting_hours: val('basket-starting-hours'),
            // No starting_date and no max_balance_hours: the server stamps the
            // first whenever the balance changes, and nothing sets the second
            // any more. See the note at the top of _basket_modal.html.
            low_balance_hours: el('basket-warn-negative-only').checked
                ? '' : val('basket-low-balance-hours'),
            accrual_hours: el('basket-no-accrual').checked
                ? '' : val('basket-accrual-hours'),
            grant_hours: el('basket-no-grant').checked
                ? '' : val('basket-grant-hours'),
            accrual_anchor_date: val('basket-accrual-anchor-date'),
            cadence_interval: val('basket-cadence-interval'),
            cadence_unit: val('basket-cadence-unit'),
            year_start_month: val('basket-year-start-month'),
            year_start_day: val('basket-year-start-day'),
            carryover_mode: val('basket-carryover-mode'),
            carryover_cap_hours: val('basket-carryover-cap-hours'),
            weekdays: [],
            monthly_days: []
        };

        if (payload.cadence_unit === 'weeks') {
            $('.basket-weekday:checked').each(function () {
                payload.weekdays.push(this.value);
            });
        } else if (payload.cadence_unit === 'months') {
            $('#basket-monthly-container .monthly-day-select').each(function () {
                if (this.value) { payload.monthly_days.push(this.value); }
            });
        } else if (payload.cadence_unit === 'years') {
            payload.yearly_day = val('basket-yearly-day');
            payload.yearly_month = val('basket-yearly-month');
        }

        // One break, written to all seven columns. They stay per day in the
        // schema and the projection still reads them per day; this form simply
        // does not offer that.
        var brk = val('basket-break') || '0';
        WEEKDAY_PREFIXES.forEach(function (prefix) {
            var off = el('basket-' + prefix + '-off').checked;
            payload[prefix + '_start'] = off ? '' : val('basket-' + prefix + '-start');
            payload[prefix + '_end'] = off ? '' : val('basket-' + prefix + '-end');
            payload[prefix + '_break_minutes'] = off ? '0' : brk;
        });

        return payload;
    }

    form.addEventListener('submit', function (event) {
        event.preventDefault();
        var basketId = el('basket-id').value;
        var url = basketId ? '/loaf/api/baskets/' + basketId : '/loaf/api/baskets';

        el('basket-submit').disabled = true;
        $.ajax({
            url: url,
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify(collect()),
            success: function (response) {
                if (response.status === 'success') {
                    location.reload();
                } else {
                    el('basket-submit').disabled = false;
                    showToast(response.message || 'Could not save that basket.', 'error');
                }
            },
            error: function (xhr) {
                el('basket-submit').disabled = false;
                var body = xhr.responseJSON || {};
                showToast(body.message || 'Could not save that basket.', 'error');
            }
        });
    });

    $('#basket-cadence-unit').on('change', syncCadence);
    $('#basket-carryover-mode').on('change', syncCarryover);
    $('#basket-add-day').on('click', function () {
        el('basket-monthly-container').appendChild(monthlyDaySelect(null));
    });

    $('#basket-no-accrual, #basket-no-grant, #basket-warn-negative-only')
        .on('change', syncAmounts);
    $('.loaf-schedule-off').on('change', function () {
        syncDay(this.id.replace('basket-', '').replace('-off', ''));
    });

    $('#basket-copy-monday').on('click', function () {
        var start = val('basket-mon-start');
        var end = val('basket-mon-end');
        var off = el('basket-mon-off').checked;
        ['tue', 'wed', 'thu', 'fri'].forEach(function (prefix) {
            el('basket-' + prefix + '-off').checked = off;
            syncDay(prefix);
            if (!off) {
                el('basket-' + prefix + '-start').value = start;
                el('basket-' + prefix + '-end').value = end;
            }
        });
    });

    $('#basket-clear-week').on('click', function () {
        WEEKDAY_PREFIXES.forEach(function (prefix) {
            el('basket-' + prefix + '-off').checked = false;
            el('basket-' + prefix + '-start').value = '';
            el('basket-' + prefix + '-end').value = '';
            syncDay(prefix);
        });
        el('basket-break').value = '0';
    });

    // Backdrop and Escape, the way every other modal on the site closes.
    modal.addEventListener('click', function (event) {
        if (event.target === modal) { close(); }
    });
    $(document).on('keydown', function (event) {
        if (event.key === 'Escape' && modal.style.display === 'flex') { close(); }
    });

    window.loafBasketModal = {
        openAdd: openAdd,
        openEdit: openEdit,
        close: close,
        isOpen: function () { return modal.style.display === 'flex'; }
    };
})();
