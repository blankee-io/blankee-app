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

    function build(P, cfg) {
    var modal = document.getElementById(P + '-modal');
    if (!modal) { return null; }

    var form = document.getElementById(P + '-form');
    var WEEKDAY_PREFIXES = JSON.parse(modal.getAttribute('data-weekday-prefixes'));

    // Both lists are Monday-first and index-aligned - loaf_data.WEEKDAY_PREFIXES
    // and WEEKDAY_NAMES, handed over together. The inputs are keyed by prefix
    // and accrual_only_weekdays stores names, so that alignment is load-bearing.
    var WEEKDAY_NAMES = JSON.parse(modal.getAttribute('data-weekday-names'));

    // The counted-week column is for one shape of schedule - a compressed week
    // the employer accrues as a standard one - so it stays out of the way until
    // somebody asks for it from the console:
    //
    //     loafBasketModal.countedWeek()        show it
    //     loafBasketModal.countedWeek(false)   put it away
    //
    // localStorage rather than a server setting: it decides what a form shows,
    // not what anything means, and it should not follow the account onto a
    // machine where nobody asked for it. Reads are wrapped because a browser
    // set to block site data throws on access rather than returning null.
    var COUNTED_KEY = 'loafBasketCountedWeek';

    function countedWeekOn() {
        try {
            return localStorage.getItem(COUNTED_KEY) === '1';
        } catch (err) {
            return false;
        }
    }

    function syncCountedWeek() {
        modal.classList.toggle('loaf-counted-week', countedWeekOn());
    }

    // A control the OPEN modal does not have. The job's settings live on one
    // form and the pool's on the other, and both are driven from here - so
    // roughly a third of the lines below address something that is not on the
    // page this time. Writing to this is a no-op and reading it gives the
    // empty answer, which is what "not asked" should mean.
    //
    // It is not a way to survive typos: a mistyped id would fail silently and
    // that is the cost. The alternative was a second copy of the working week
    // grid and the holiday picker, and the note at the top of this file
    // records what two copies of a form did to recurring_i.html.
    // A FRESH one every call, never a shared singleton. Sharing it was a real
    // bug and a quiet one: openAdd sets el('...-warn-negative-only').checked =
    // true, and with one object behind every absent control that wrote `true`
    // onto all of them - so collect() then read "no accrual" as ticked and
    // posted accrual_hours: '' from a form that has no such field. Writing to
    // a thing that is not there has to actually go nowhere.
    function nowhere() {
        return {
            value: '', checked: false, textContent: '', innerHTML: '',
            disabled: false, selectionStart: null, style: {},
            classList: {add: noop, remove: noop, toggle: noop,
                        contains: function () { return false; }},
            setAttribute: noop, removeAttribute: noop,
            getAttribute: function () { return null; },
            focus: noop, blur: noop, select: noop, setSelectionRange: noop,
            addEventListener: noop, appendChild: noop, remove: noop,
            querySelectorAll: function () { return []; },
            querySelector: function () { return null; },
            closest: function () { return null; }
        };
    }

    function noop() {}
    function at(id) { return document.getElementById(id); }
    function el(id) { return at(id) || nowhere(); }

    // undefined, not '', for a control that is not here - JSON.stringify drops
    // an undefined value, so collect() sends only the fields this form
    // actually asked about. An empty string would be a claim that the user
    // cleared it, and clearing a working week is not nothing.
    function val(id) { var node = at(id); return node ? node.value : undefined; }

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
            var container = el(P + '-monthly-container');
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
        var unit = val(P + '-cadence-unit');
        el(P + '-weekly').style.display = unit === 'weeks' ? 'flex' : 'none';
        el(P + '-monthly').style.display = unit === 'months' ? 'block' : 'none';
        el(P + '-yearly').style.display = unit === 'years' ? 'block' : 'none';

        if (unit === 'months' &&
            !el(P + '-monthly-container').querySelector('.monthly-day-select')) {
            el(P + '-monthly-container').appendChild(monthlyDaySelect(null));
        }
    }

    // ---------------------------------------------------------- sections ----
    //
    // One open at a time, and the form fills itself in in order: finish a
    // section and it closes and hands over to the next.
    //
    // The rule for "finished" is deliberately not "the last field has a
    // value". It is: the section has everything it needs, AND focus has left
    // it. Advancing on a keystroke shuts a section while somebody is still
    // deciding, which is the difference between a form that helps and one
    // that feels possessed.
    //
    // And only once each. Reopen a section afterwards and it stays open,
    // however much you edit - being marched forward a second time is worse
    // than not being marched at all.
    var SECTIONS = cfg.sections;
    var advanced = {};

    function sectionOpen(key) {
        var panel = el(P + '-section-' + key);
        return panel && !panel.hidden;
    }

    function showSection(key) {
        SECTIONS.forEach(function (other) {
            var panel = el(P + '-section-' + other);
            var button = el(P + '-section-' + other + '-toggle');
            if (!panel || !button) { return; }
            var open = other === key;
            panel.hidden = !open;
            button.setAttribute('aria-expanded', open ? 'true' : 'false');
            button.classList.toggle('is-open', open);
            button.querySelector('i').className = open
                ? 'fa-solid fa-chevron-down' : 'fa-solid fa-chevron-right';
        });
    }

    // What each section needs before it will hand over. Kept small on
    // purpose: a section nobody has to fill in should not trap anybody in it.
    function sectionDone(key) {
        if (key === 'holds') {
            return $.trim(val(P + '-starting-hours')) !== '';
        }
        if (key === 'fills') {
            var earns = el(P + '-no-accrual').checked
                || $.trim(val(P + '-accrual-hours')) !== '';
            var granted = el(P + '-no-grant').checked
                || $.trim(val(P + '-grant-hours')) !== '';
            // Nothing arriving at all is a complete answer too - a pot topped
            // up by hand needs no pay date.
            if (el(P + '-no-accrual').checked && el(P + '-no-grant').checked) {
                return true;
            }
            return earns && granted
                && $.trim(val(P + '-accrual-anchor-date')) !== '';
        }
        if (key === 'week') {
            return WEEKDAY_PREFIXES.some(function (prefix) {
                return el(P + '-' + prefix + '-off').checked
                    || $.trim(val(P + '-' + prefix + '-start')) !== '';
            });
        }
        return true;            // the holidays are nobody's obligation
    }

    function advanceFrom(key) {
        if (advanced[key] || !sectionOpen(key) || !sectionDone(key)) { return; }
        var next = SECTIONS[SECTIONS.indexOf(key) + 1];
        // Nothing after the last one - Advanced is only ever opened by hand -
        // so it stays where it is rather than collapsing to nothing. A form
        // that shuts itself completely reads as having gone away.
        if (!next) { return; }
        advanced[key] = true;
        showSection(next);
    }

    // A plain show/hide rather than the side menu's .menu-collapse, which
    // animates a max-height fixed per list - this section changes height when
    // the carry-over cap appears, and a fixed maximum would clip it.
    // "None", the one name, or a count - never a list long enough to
    // outgrow the button and wrap it onto three lines.
    // Holidays this employer gives that Loaf has no rule for, as
    // [{date: 'MM-DD', name: '...'}]. Held here rather than read back out of
    // the DOM, so the order and the stored form have exactly one owner.
    var customHolidays = [];

    function drawCustomHolidays() {
        var host = el(P + '-custom-holidays');
        host.innerHTML = '';
        customHolidays.forEach(function (entry) {
            var row = document.createElement('div');
            row.className = 'category-dropdown-item loaf-holiday-custom';

            var text = document.createElement('span');
            text.textContent = entry.name + ' \u00b7 ' + entry.date;
            row.appendChild(text);

            // No checkbox: it is on the list because you get it, so the way
            // to stop getting it is to take it off.
            var drop = document.createElement('button');
            drop.type = 'button';
            drop.className = 'loaf-holiday-drop';
            drop.title = 'Remove ' + entry.name;
            drop.setAttribute('aria-label', 'Remove ' + entry.name);
            drop.innerHTML = '<i class="fa-solid fa-xmark"></i>';
            drop.addEventListener('click', function () {
                var at = customHolidays.indexOf(entry);
                if (at >= 0) { customHolidays.splice(at, 1); }
                drawCustomHolidays();
                syncHolidays();
            });
            row.appendChild(drop);
            host.appendChild(row);
        });
    }

    function showNewHoliday(open) {
        el(P + '-holiday-new').hidden = !open;
        el(P + '-holiday-add').hidden = open;
        if (open) { el(P + '-holiday-name').focus(); }
    }

    // MM-DD, and a real day of a real month. February is checked against a
    // leap year so the 29th is allowed - loaf_holidays does the same.
    function readNewHoliday() {
        var name = $.trim(el(P + '-holiday-name').value);
        var bits = $.trim(el(P + '-holiday-date').value).split('-');
        if (bits.length !== 2) { return null; }
        var month = parseInt(bits[0], 10);
        var day = parseInt(bits[1], 10);
        if (!(month >= 1 && month <= 12) || !(day >= 1)) { return null; }
        var probe = new Date(2024, month - 1, day);
        if (probe.getMonth() !== month - 1 || probe.getDate() !== day) {
            return null;
        }
        return {name: name || 'Holiday',
                date: ('0' + month).slice(-2) + '-' + ('0' + day).slice(-2)};
    }

    // "None", the one name, or a count - never a list long enough to outgrow
    // the button and wrap it onto three lines. Counts both kinds: to somebody
    // reading it there is only one list.
    function syncHolidays() {
        var picked = $('.' + P + '-holiday:checked');
        var total = picked.length + customHolidays.length;
        var text = 'None';
        if (total === 1) {
            text = picked.length
                ? $.trim(picked.first().closest('label').text())
                : customHolidays[0].name;
        } else if (total) {
            text = total + ' selected';
        }
        el(P + '-holiday-summary').textContent = text;
    }

    function showHolidays(open) {
        el(P + '-holiday-menu').hidden = !open;
        el(P + '-holiday-toggle').setAttribute(
            'aria-expanded', open ? 'true' : 'false');
    }

    function showAdvanced(open) {
        var panel = el(P + '-advanced');
        var button = el(P + '-advanced-toggle');
        panel.hidden = !open;
        button.setAttribute('aria-expanded', open ? 'true' : 'false');
        button.querySelector('i').className = open
            ? 'fa-solid fa-chevron-down' : 'fa-solid fa-chevron-right';
    }

    // How many minutes the working week comes to, breaks removed. The same
    // sum loaf_data.scheduled_minutes does, on the form's own fields.
    function weeklyMinutes() {
        var total = 0;
        WEEKDAY_PREFIXES.forEach(function (prefix) {
            if (el(P + '-' + prefix + '-off').checked) { return; }
            var from = val(P + '-' + prefix + '-start');
            var to = val(P + '-' + prefix + '-end');
            if (!from || !to) { return; }
            var a = from.split(':'), b = to.split(':');
            var span = (Number(b[0]) * 60 + Number(b[1]))
                - (Number(a[0]) * 60 + Number(a[1]));
            if (span > 0) {
                total += span - (Number(val(P + '-break')) || 0);
            }
        });
        return total;
    }

    // How many times a year the cadence comes round.
    function periodsPerYear() {
        var every = Number(val(P + '-cadence-interval')) || 1;
        var unit = val(P + '-cadence-unit');
        if (unit === 'days') { return 365 / every; }
        if (unit === 'weeks') {
            return (52 / every)
                * ($('.' + P + '-weekday:checked').length || 1);
        }
        if (unit === 'months') {
            return (12 / every)
                * ($('#' + P + '-monthly-container .monthly-day-select').length || 1);
        }
        return 1 / every;
    }

    // The figure payroll would use: a year of hours split evenly across the
    // year's pay periods. 40 hours a week paid twice a month is 2080/24 =
    // 86.67; paid fortnightly it is 2080/26 = 80. Both are the numbers that
    // actually appear on payslips, which is the point - Loaf can work this
    // out rather than asking somebody to.
    function suggestedPeriodHours() {
        var weekly = weeklyMinutes() / 60;
        var periods = periodsPerYear();
        if (!(weekly > 0) || !(periods > 0)) { return null; }
        return Math.round(weekly * 52 / periods * 100) / 100;
    }

    // The flat denominator is for the per-hour basis alone. Hidden rather
    // than disabled on a flat accrual, because a greyed field still reads as
    // a question somebody has to have an answer for.
    //
    // Filled in rather than merely suggested through a placeholder: an empty
    // box means "walk the weekdays instead", so a figure shown but not used
    // would be telling somebody the opposite of what is happening. Clearing
    // it is still how you ask for that.
    function syncBasis() {
        // Absent means show it, and that is the shelf form's whole case: the
        // basis moved to the basket when the shelf was split out, the
        // denominator stayed here, and there is no basis control on this form
        // to ask. Read as a plain === 'worked' the answer is undefined, which
        // is falsey, so the field sat in the markup and could never be
        // reached - a shelf could not be given the figure payroll divides by.
        // One shelf can carry baskets on either basis anyway, so the shelf is
        // not the place the question can be answered.
        var basis = val(P + '-accrual-basis');
        var worked = basis === undefined || basis === 'worked';
        el(P + '-period-hours-row').style.display = worked ? '' : 'none';
    }

    // The figure appears when somebody says their employer uses one, and is
    // filled in at that moment rather than when the form opens. Opening a
    // shelf to look at it must not change what it does, and a pre-fill on
    // open did exactly that: the box was empty because nobody had ever been
    // able to reach it, so merely opening and saving would have switched the
    // divisor from the walked weekdays to a flat 86.67 without being asked.
    //
    // Only when empty, so a figure typed from a payslip survives unticking
    // and re-ticking the box.
    function syncPeriodHours() {
        var on = !!at(P + '-same-period-hours') && at(P + '-same-period-hours').checked;
        el(P + '-period-hours-amount').style.display = on ? '' : 'none';
        if (on && !$.trim(val(P + '-period-hours'))) {
            var guess = suggestedPeriodHours();
            if (guess) { el(P + '-period-hours').value = guess; }
        }
    }

    // The name a new basket opens with: the shelf's, and what the pool is.
    // "Acme Corp PTO" - which is what most people would have typed, and what
    // the server would have filled in had the field been left empty.
    //
    // It follows the type picker until somebody types their own, and then
    // never again: a name being rewritten under the cursor because the type
    // changed is the sort of thing that loses a word somebody meant.
    var shelfStem = '';
    var nameTouched = false;

    function suggestName() {
        if (nameTouched || !shelfStem) { return; }
        var kind = val(P + '-type') === 'uto' ? 'UTO' : 'PTO';
        el(P + '-name').value = (shelfStem + ' ' + kind).trim();
    }

    function syncCarryover() {
        el(P + '-carryover-cap-row').style.display =
            val(P + '-carryover-mode') === 'capped' ? 'block' : 'none';
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
        syncNone(P + '-no-accrual', P + '-accrual-hours');
        syncNone(P + '-no-grant', P + '-grant-hours');
        syncNone(P + '-warn-negative-only', P + '-low-balance-hours');

        // Whether time off reduces the next accrual is a question about an
        // accrual. A basket that does not have one is not being asked it -
        // and the stored answer is left alone rather than reset, so ticking
        // "No accrual" and changing your mind does not quietly flip a basket
        // from per-hour-worked back to flat.
        el(P + '-accrual-basis-row').style.display =
            el(P + '-no-accrual').checked ? 'none' : '';
    }

    // Off is the same idea for a day: the engine reads a missing start as a day
    // not worked, and the box says so rather than leaving two blanks that read
    // as unfinished.
    function syncDay(prefix) {
        var off = el(P + '-' + prefix + '-off').checked;
        var notIn = el(P + '-' + prefix + '-notin');
        var free = el(P + '-' + prefix + '-free');

        // Off wins, because the two are different states and not degrees of
        // one. Off means the employer does not count the day at all; Not in
        // means it counts the day and nobody is there for it. Holding both
        // would name an accrual-only weekday with no hours behind it.
        if (off && notIn.checked) { notIn.checked = false; }

        // Off wins over Free for the same reason, and Not in wins over Free
        // because it already answers the question Free asks. A day nobody is
        // ever at costs nothing without needing to be told it is not billed,
        // and holding both would leave two settings to disagree about one day.
        if ((off || notIn.checked) && free.checked) { free.checked = false; }
        free.disabled = off || notIn.checked;

        ['start', 'end'].forEach(function (part) {
            var field = el(P + '-' + prefix + '-' + part);
            field.disabled = off;
            if (off) { field.value = ''; }
        });
    }

    function syncWeek() {
        WEEKDAY_PREFIXES.forEach(syncDay);
    }

    function clearForm() {
        form.reset();
        el(P + '-id').value = '';
        el(P + '-monthly-container').innerHTML = '';
        $('.' + P + '-weekday').prop('checked', false);
        el(P + '-accrual-basis').value = 'flat';
        el(P + '-period-hours').value = '';
        el(P + '-same-period-hours').checked = false;
        syncBasis();
        syncPeriodHours();
        advanced = {};
        showSection('holds');
        $('.' + P + '-holiday').prop('checked', false);
        customHolidays = [];
        drawCustomHolidays();
        showNewHoliday(false);
        syncHolidays();
        showHolidays(false);
        showAdvanced(false);
        el(P + '-no-accrual').checked = false;
        el(P + '-no-grant').checked = false;
        el(P + '-warn-negative-only').checked = false;
        el(P + '-break').value = '0';
        WEEKDAY_PREFIXES.forEach(function (prefix) {
            el(P + '-' + prefix + '-start').value = '';
            el(P + '-' + prefix + '-end').value = '';
            el(P + '-' + prefix + '-off').checked = false;
            el(P + '-' + prefix + '-notin').checked = false;
            el(P + '-' + prefix + '-free').checked = false;
        });
        syncAmounts();
        syncWeek();
    }

    function openAdd(seed) {
        clearForm();
        seed = seed || {};

        // The shelf's name is not a field - it fills the basket's name, so
        // the form opens saying what this will be called rather than blank
        // with a placeholder promising it later.
        shelfStem = seed.shelf_name || '';
        delete seed.shelf_name;

        // Named on the form as `on-shelf`, so that it reads as "which shelf
        // this is on" beside the basket's own id. The generic key-to-id rule
        // below would have looked for `shelf-id` and found nothing, and a
        // basket created from this form went out carrying no shelf at all -
        // which the server only survived while there was exactly one to fall
        // back to.
        if (seed.shelf_id !== undefined) {
            el(P + '-on-shelf').value = seed.shelf_id;
            delete seed.shelf_id;
        }

        // What the caller already knows. Adding a basket begins at the shelf
        // it will sit on, so that shelf arrives here rather than being asked
        // for on a form that has no business guessing.
        Object.keys(seed).forEach(function (key) {
            el(P + '-' + key.replace(/_/g, '-')).value = seed[key];
        });
        nameTouched = false;
        suggestName();
        syncCountedWeek();
        el(P + '-modal-title').textContent = 'Add a ' + cfg.noun;
        el(P + '-submit').textContent = 'Add ' + cfg.noun;
        el(P + '-starting-hours').value = '0.00';
        el(P + '-cadence-interval').value = '2';
        el(P + '-cadence-unit').value = 'weeks';
        el(P + '-year-start-month').value = '1';
        el(P + '-year-start-day').value = '1';
        el(P + '-carryover-mode').value = 'reset';
        // No threshold by default, which IS "only when negative" - so the box
        // starts ticked rather than the field starting blank with nothing
        // saying what blank means.
        el(P + '-warn-negative-only').checked = true;
        // A sensible week rather than a blank grid: Mon-Fri, nine to five, with
        // an unpaid half hour. Every part is editable, and a blank grid would
        // silently mean "I never work", which no basket wants.
        ['mon', 'tue', 'wed', 'thu', 'fri'].forEach(function (prefix) {
            el(P + '-' + prefix + '-start').value = '09:00';
            el(P + '-' + prefix + '-end').value = '17:00';
        });
        ['sat', 'sun'].forEach(function (prefix) {
            el(P + '-' + prefix + '-off').checked = true;
        });
        el(P + '-break').value = '30';
        $('.' + P + '-weekday[value="friday"]').prop('checked', true);
        syncCadence();
        syncCarryover();
        syncAmounts();
        syncWeek();
        modal.style.display = 'flex';
    }

    function openEdit(rowId) {
        syncCountedWeek();
        // Both forms hydrate from the same table. Its rows carry the job's
        // settings as well as the pool's - see the comment on the <tr> in
        // baskets.html - so the only difference is which attribute identifies
        // the row wanted.
        var row = $('tr[' + cfg.rowKey + '="' + rowId + '"]').first();
        if (!row.length) { return; }
        var basketId = rowId;
        clearForm();

        el(P + '-modal-title').textContent = 'Edit ' + cfg.noun;
        el(P + '-submit').textContent = 'Save ' + cfg.noun;
        el(P + '-id').value = basketId;

        // Straight off the row's data-* attributes. The cells hold formatted
        // strings; these hold the figures.
        el(P + '-name').value = row.attr('data-name') || '';
        // Stays where it is. A basket is moved between shelves deliberately,
        // not as a side effect of saving an edit, so the form carries the
        // shelf it arrived on straight back out again.
        el(P + '-on-shelf').value = row.attr('data-on-shelf') || '';
        el(P + '-type').value = row.attr('data-basket-type') || 'pto';
        el(P + '-starting-hours').value = row.attr('data-starting-hours') || '0';
        var warn = row.attr('data-low-balance-hours') || '';
        el(P + '-low-balance-hours').value = warn;
        el(P + '-warn-negative-only').checked = warn === '';

        // An empty figure IS "does not accrue" - that is what the NULL means -
        // so the box comes back ticked rather than the field coming back blank
        // with nothing saying why.
        var accrual = row.attr('data-accrual-hours') || '';
        var grant = row.attr('data-grant-hours') || '';
        el(P + '-accrual-hours').value = accrual;
        el(P + '-grant-hours').value = grant;
        el(P + '-no-accrual').checked = accrual === '';
        el(P + '-no-grant').checked = grant === '';
        el(P + '-accrual-anchor-date').value = row.attr('data-accrual-anchor-date') || '';
        el(P + '-cadence-interval').value = row.attr('data-cadence-interval') || '1';
        el(P + '-cadence-unit').value = row.attr('data-cadence-unit') || 'weeks';
        el(P + '-yearly-day').value = row.attr('data-yearly-day') || '';
        el(P + '-yearly-month').value = row.attr('data-yearly-month') || '1';
        el(P + '-year-start-month').value = row.attr('data-year-start-month') || '1';
        el(P + '-year-start-day').value = row.attr('data-year-start-day') || '1';
        el(P + '-carryover-mode').value = row.attr('data-carryover-mode') || 'reset';
        el(P + '-accrual-basis').value =
            row.attr('data-accrual-basis') || 'flat';
        // A stored figure is what "my employer uses one" looks like once
        // saved, so the box comes back ticked from the value itself. There is
        // no separate column for the answer and there should not be: two
        // places to say the same thing is two places to disagree.
        var flat = row.attr('data-period-hours') || '';
        el(P + '-period-hours').value = flat;
        el(P + '-same-period-hours').checked = flat !== '' && Number(flat) > 0;
        syncBasis();
        syncPeriodHours();

        var shut = (row.attr('data-holidays') || '').split(',');
        $('.' + P + '-holiday').each(function () {
            this.checked = shut.indexOf(this.value) >= 0;
        });

        // Stored as MM-DD|Name, separated by semicolons - see
        // loaf_holidays.parse_custom. A name may not contain either
        // delimiter, so splitting is safe in both directions.
        // Every section counts as already dealt with: this basket is filled
        // in, and being walked through it a field at a time is a setup flow
        // wearing out its welcome.
        SECTIONS.forEach(function (key) { advanced[key] = true; });

        customHolidays = (row.attr('data-custom-holidays') || '')
            .split(';').filter(Boolean).map(function (entry) {
                var parts = entry.split('|');
                return {date: parts[0],
                        name: parts.slice(1).join('|') || 'Holiday'};
            });
        drawCustomHolidays();
        syncHolidays();

        // Opened for anybody who has something in there worth seeing, so an
        // unusual basket does not look like an ordinary one until you go
        // hunting. A default basket stays shut.
        showAdvanced(shut.length > 0 && shut[0] !== ''
            || el(P + '-accrual-basis').value !== 'flat'
            || (row.attr('data-carryover-mode') || 'reset') !== 'reset'
            || !!row.attr('data-low-balance-hours')
            || !!row.attr('data-period-hours'));
        el(P + '-carryover-cap-hours').value = row.attr('data-carryover-cap-hours') || '';

        var days = (row.attr('data-weekdays') || '').split(',');
        // Different column, same shape: these are days counted for accrual and
        // never attended, not days the pay lands on.
        var accrualOnly = (row.attr('data-accrual-only-weekdays') || '').split(',');
        var uncharged = (row.attr('data-uncharged-weekdays') || '').split(',');
        $('.' + P + '-weekday').each(function () {
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
            el(P + '-' + prefix + '-start').value = start ? start.substring(0, 5) : '';
            el(P + '-' + prefix + '-end').value = end ? end.substring(0, 5) : '';
            el(P + '-' + prefix + '-off').checked = !start;
            el(P + '-' + prefix + '-notin').checked =
                accrualOnly.indexOf(WEEKDAY_NAMES[WEEKDAY_PREFIXES.indexOf(prefix)]) >= 0;
            el(P + '-' + prefix + '-free').checked =
                uncharged.indexOf(WEEKDAY_NAMES[WEEKDAY_PREFIXES.indexOf(prefix)]) >= 0;
            longest = Math.max(longest,
                parseInt(row.attr('data-' + prefix + '-break-minutes') || '0', 10) || 0);
        });
        el(P + '-break').value = String(longest);

        syncCadence();
        syncCarryover();
        syncAmounts();
        syncWeek();

        var monthly = (row.attr('data-monthly-days') || '').split(',').filter(Boolean);
        if (val(P + '-cadence-unit') === 'months') {
            var container = el(P + '-monthly-container');
            container.innerHTML = '';
            (monthly.length ? monthly : ['1']).forEach(function (day) {
                container.appendChild(monthlyDaySelect(day));
            });
        }

        modal.style.display = 'flex';
    }

    function close() { modal.style.display = 'none'; }

    function collect() {
        // Nothing should be left as text: a field still being edited when
        // Save is clicked would be read as one, and the row that stores it
        // does not care - but the next open would find a type it did not
        // put there.
        $('#' + P + '-form input[data-was-number]').each(function () {
            backToNumber(this);
        });

        var payload = {
            name: val(P + '-name'),
            shelf_id: val(P + '-on-shelf'),
            basket_type: val(P + '-type'),
            starting_hours: val(P + '-starting-hours'),
            // No starting_date and no max_balance_hours: the server stamps the
            // first whenever the balance changes, and nothing sets the second
            // any more. See the note at the top of _basket_modal.html.
            low_balance_hours: el(P + '-warn-negative-only').checked
                ? '' : val(P + '-low-balance-hours'),
            accrual_hours: el(P + '-no-accrual').checked
                ? '' : val(P + '-accrual-hours'),
            grant_hours: el(P + '-no-grant').checked
                ? '' : val(P + '-grant-hours'),
            accrual_anchor_date: val(P + '-accrual-anchor-date'),
            cadence_interval: val(P + '-cadence-interval'),
            cadence_unit: val(P + '-cadence-unit'),
            year_start_month: val(P + '-year-start-month'),
            year_start_day: val(P + '-year-start-day'),
            carryover_mode: val(P + '-carryover-mode'),
            carryover_cap_hours: val(P + '-carryover-cap-hours'),
            weekdays: [],
            monthly_days: []
        };

        if (payload.cadence_unit === 'weeks') {
            $('.' + P + '-weekday:checked').each(function () {
                payload.weekdays.push(this.value);
            });
        } else if (payload.cadence_unit === 'months') {
            $('#' + P + '-monthly-container .monthly-day-select').each(function () {
                if (this.value) { payload.monthly_days.push(this.value); }
            });
        } else if (payload.cadence_unit === 'years') {
            payload.yearly_day = val(P + '-yearly-day');
            payload.yearly_month = val(P + '-yearly-month');
        }

        // One break, written to all seven columns. They stay per day in the
        // schema and the projection still reads them per day; this form simply
        // does not offer that.
        var brk = val(P + '-break') || '0';
        payload.accrual_basis = val(P + '-accrual-basis');
        // Unticked posts a real 0 rather than nothing. Nothing means "leave
        // it alone" to the update route - the 1.25.1 guard for controls a
        // form does not show - so an absent value could never turn a flat
        // divisor back off. 0 is how the projection already spells "walk the
        // weekdays": fixed if fixed > 0, else count them.
        if (at(P + '-same-period-hours')) {
            payload.period_hours =
                at(P + '-same-period-hours').checked
                    ? val(P + '-period-hours') : 0;
        }
        payload.holidays = $('.' + P + '-holiday:checked')
            .map(function () { return this.value; }).get();
        payload.custom_holidays = customHolidays.map(function (h) {
            return h.date + '|' + h.name;
        }).join(';');

        var accrualOnly = [];
        var uncharged = [];
        WEEKDAY_PREFIXES.forEach(function (prefix, index) {
            var off = el(P + '-' + prefix + '-off').checked;
            payload[prefix + '_start'] = off ? '' : val(P + '-' + prefix + '-start');
            payload[prefix + '_end'] = off ? '' : val(P + '-' + prefix + '-end');
            payload[prefix + '_break_minutes'] = off ? '0' : brk;
            if (!off && el(P + '-' + prefix + '-notin').checked) {
                accrualOnly.push(WEEKDAY_NAMES[index]);
            }
            if (!off && el(P + '-' + prefix + '-free').checked) {
                uncharged.push(WEEKDAY_NAMES[index]);
            }
        });

        // The whole safety story, in one place: hidden means the key is absent
        // and api_update_basket leaves the column alone, so saving a basket
        // from a browser without the flag cannot wipe a value set from one
        // that has it. Shown means the key is present - an empty list included,
        // which is how the setting is turned back off.
        if (countedWeekOn()) { payload.accrual_only_weekdays = accrualOnly; }

        // Always sent, unlike the line above: the Free column is not behind a
        // flag, so it is always on screen and an empty list always means the
        // user cleared it rather than never having been shown it.
        if (at(P + '-mon-free')) { payload.uncharged_weekdays = uncharged; }

        return payload;
    }

    form.addEventListener('submit', function (event) {
        event.preventDefault();
        var basketId = el(P + '-id').value;
        var url = basketId ? cfg.api + '/' + basketId : cfg.api;

        el(P + '-submit').disabled = true;
        $.ajax({
            url: url,
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify(collect()),
            success: function (response) {
                if (response.status === 'success') {
                    location.reload();
                } else {
                    el(P + '-submit').disabled = false;
                    showToast(response.message
                        || ('Could not save that ' + cfg.noun.toLowerCase() + '.'),
                        'error');
                }
            },
            error: function (xhr) {
                el(P + '-submit').disabled = false;
                var body = xhr.responseJSON || {};
                showToast(body.message || 'Could not save that basket.', 'error');
            }
        });
    });

    $('#' + P + '-cadence-unit').on('change', syncCadence);
    $('#' + P + '-carryover-mode').on('change', syncCarryover);

    // The suggested name follows the type - PTO to UTO rewrites it - right
    // up until somebody types their own, at which point it never touches
    // the field again.
    $('#' + P + '-type').on('change', suggestName);
    $('#' + P + '-name').on('input', function () { nameTouched = true; });
    $('#' + P + '-add-day').on('click', function () {
        el(P + '-monthly-container').appendChild(monthlyDaySelect(null));
    });

    $('#' + P + '-no-accrual, ' + P + '-no-grant, ' + P + '-warn-negative-only')
        .on('change', syncAmounts);
    $('#' + P + '-accrual-basis').on('change', syncBasis);
    $('#' + P + '-same-period-hours').on('change', syncPeriodHours);

    $('#' + P + '-advanced-toggle').on('click', function () {
        showAdvanced(el(P + '-advanced').hidden);
    });

    // A header opens its own section and shuts the rest. Clicking the one
    // already open leaves it open rather than closing to nothing: a form
    // with every section shut has nowhere obvious to look.
    $('.loaf-section-toggle').on('click', function () {
        var key = this.getAttribute('data-section');
        advanced[key] = true;         // asked for by hand; do not march on
        showSection(key);
    });

    // focusout, not change: it fires once focus has actually left, so a
    // section is never pulled out from under the field being typed in.
    // relatedTarget is where focus went - still inside means stay put.
    SECTIONS.forEach(function (key) {
        var panel = el(P + '-section-' + key);
        if (!panel) { return; }
        panel.addEventListener('focusout', function (event) {
            if (panel.contains(event.relatedTarget)) { return; }
            // A tick later, so a click landing on the next section's header
            // has run first and this does not fight it.
            setTimeout(function () { advanceFrom(key); }, 0);
        });
    });

    $('#' + P + '-holiday-toggle').on('click', function () {
        showHolidays(el(P + '-holiday-menu').hidden);
    });
    $('.' + P + '-holiday').on('change', syncHolidays);

    $('#' + P + '-holiday-add').on('click', function () { showNewHoliday(true); });
    $('#' + P + '-holiday-save').on('click', function () {
        var entry = readNewHoliday();
        if (!entry) {
            showToast('Give the holiday a date as MM-DD, like 03-17.', 'error');
            return;
        }
        customHolidays.push(entry);
        el(P + '-holiday-name').value = '';
        el(P + '-holiday-date').value = '';
        showNewHoliday(false);
        drawCustomHolidays();
        syncHolidays();
    });
    // Enter adds the holiday rather than submitting the whole basket, which
    // is what a text input inside a form does otherwise.
    $('#' + P + '-holiday-name, ' + P + '-holiday-date').on('keydown', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            $('#' + P + '-holiday-save').trigger('click');
        }
    });

    // Clicking away closes the panel and nothing else. Scoped to the modal so
    // it cannot interfere with the page behind it, and it must not reach the
    // modal's own backdrop handler - that closes the whole form.
    modal.addEventListener('click', function (event) {
        if (!event.target.closest('.loaf-holiday-picker')) {
            showHolidays(false);
        }
    }, true);
    // All three boxes, not just Off. They constrain each other now - Off
    // clears and disables Free, and so does Not in, because a day nobody
    // attends is already free and two settings for one day is two settings
    // that can disagree. Wired to Off alone, ticking Not in left Free set and
    // enabled, and the row said two contradictory things at once.
    $('.loaf-schedule-off, .loaf-schedule-counted, .loaf-schedule-free')
        .on('change', function () {
            syncDay(this.id.replace(P + '-', '')
                        .replace(/-(off|notin|free)$/, ''));
        });

    // Times and Off, but NOT the counted-week box. The day somebody is absent
    // is never the day being copied from, so carrying the flag across would
    // put it on precisely the four days it does not belong to.
    $('#' + P + '-copy-monday').on('click', function () {
        var start = val(P + '-mon-start');
        var end = val(P + '-mon-end');
        var off = el(P + '-mon-off').checked;
        ['tue', 'wed', 'thu', 'fri'].forEach(function (prefix) {
            el(P + '-' + prefix + '-off').checked = off;
            syncDay(prefix);
            if (!off) {
                el(P + '-' + prefix + '-start').value = start;
                el(P + '-' + prefix + '-end').value = end;
            }
        });
    });

    // The button says every day, so the counted-week boxes go too. Left
    // behind, they would make a freshly cleared week still cost nothing on a
    // Thursday, with nothing on screen saying why.
    $('#' + P + '-clear-week').on('click', function () {
        WEEKDAY_PREFIXES.forEach(function (prefix) {
            el(P + '-' + prefix + '-off').checked = false;
            el(P + '-' + prefix + '-notin').checked = false;
            el(P + '-' + prefix + '-free').checked = false;
            el(P + '-' + prefix + '-start').value = '';
            el(P + '-' + prefix + '-end').value = '';
            syncDay(prefix);
        });
        el(P + '-break').value = '0';
    });

    // Clicking a field selects what is already in it, so typing replaces the
    // figure rather than landing next to it.
    //
    // Two halves, and the second is the one that is easy to leave out. A
    // click fires mousedown, then focus, then mouseup - and mouseup is what
    // places the caret, which throws away whatever focus just selected. So
    // focus selects, and the mouseup that belongs to the SAME click is
    // suppressed. A second click on a field already focused is left alone,
    // which is how somebody puts the caret somewhere or drags over part of
    // the value.
    //
    // text and number only. A date or time input is a set of segments, and
    // select() does not apply to them - clicking a segment already selects
    // that segment.
    // A number input cannot be selected at all - measured, not assumed:
    // select() on one is a no-op in Chrome and selectionStart reads null,
    // because the selection API does not apply to that type. The only thing
    // that works is to make it a text input for as long as it is being
    // edited, and put it back on the way out.
    //
    // What that costs while focused: the spinner arrows, and the browser's
    // own min/step checking. inputmode keeps a numeric keypad on a phone,
    // and the field is a number again by the time anything validates it.
    // Type something that is not a number and it comes back empty, which is
    // what a number input does with nonsense anyway.
    var selectingIn = null;

    function editAsText(node) {
        if (node.type !== 'number') { return; }
        node.dataset.wasNumber = '1';
        node.type = 'text';
        node.inputMode = 'decimal';
    }

    function backToNumber(node) {
        if (!node.dataset || node.dataset.wasNumber !== '1') { return; }
        delete node.dataset.wasNumber;
        node.type = 'number';
    }

    modal.addEventListener('focusin', function (event) {
        var node = event.target;
        if (!node.matches || !node.matches('input[type="text"], '
                                           + 'input[type="number"]')) {
            return;
        }
        editAsText(node);
        selectingIn = node;
        try {
            node.select();
        } catch (err) {
            selectingIn = null;     // not a control that can be selected
        }
    });

    modal.addEventListener('mouseup', function (event) {
        if (selectingIn && event.target === selectingIn) {
            event.preventDefault();
            selectingIn = null;
        }
    });

    modal.addEventListener('focusout', function (event) {
        selectingIn = null;
        if (event.target && event.target.dataset) { backToNumber(event.target); }
    });

    // Backdrop and Escape, the way every other modal on the site closes.
    modal.addEventListener('click', function (event) {
        if (event.target === modal) { close(); }
    });
    $(document).on('keydown', function (event) {
        if (event.key === 'Escape' && modal.style.display === 'flex') { close(); }
    });

    return {
        openAdd: openAdd,
        openEdit: openEdit,
        close: close,
        isOpen: function () { return modal.style.display === 'flex'; },

        // Show the counted-week column, for a compressed schedule the employer
        // accrues as a standard one. Hung here rather than on a global of its
        // own so that typing loafBasketModal. into the console finds it.
        // Returns the resulting state, so the console prints confirmation.
        countedWeek: function (on) {
            try {
                if (on === false) { localStorage.removeItem(COUNTED_KEY); }
                else { localStorage.setItem(COUNTED_KEY, '1'); }
            } catch (err) {
                return 'this browser will not store the setting';
            }
            syncCountedWeek();      // so an already-open modal changes now
            return countedWeekOn();
        }
    };
    }

    // One implementation, two forms. The basket form holds a pool of hours;
    // the shelf form holds the job those hours are earned at - the working
    // week, the days the office is shut, when pay lands. Both are built from
    // the same markup with a different id prefix, so neither can drift.
    window.loafBasketModal = build('basket', {
        noun: 'Basket',
        api: '/loaf/api/baskets',
        rowKey: 'data-basket-id',
        // The week and the holidays moved to the shelf form with the rest
        // of the job. What is left here is the pool itself: what it holds
        // and how it fills.
        sections: ['holds', 'fills']
    });

    // "Shelf" is the table; "Job" is the only word the user ever sees.
    window.loafShelfModal = build('shelf', {
        noun: 'Shelf',
        api: '/loaf/api/shelves',
        rowKey: 'data-shelf-id',
        sections: ['pays', 'week', 'shut']
    });
})();
