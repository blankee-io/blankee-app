// ═══════════════════════════════════════════════════════════════
// MAKE RECURRING
// ═══════════════════════════════════════════════════════════════
// The dialog in templates/_convert_recurring_modal.html: a recurring entry,
// filled in from what Suggested recurring found, saved into a new category
// or an existing one, through the routes the recurring pages use:
//
//   income   new: /add-recurring-income      existing: /convert-to-recurring-income
//   expense  new: /add-recurring-expense     existing: /convert-to-recurring-expense
//   card     new: /add_expense_category (which gives every card its copy),
//                 the copy's real id from /api/recurring-suggested/card-category,
//                 then /add-recurring-ca-expense
//            existing: /add-recurring-ca-expense
//
//   openConvertToRecurringModal({
//       kind,        'income' | 'expense' | 'ca'
//       accountId,   the Blankee card, for 'ca'
//       categoryId, name,   an existing category to make recurring, shown
//                           read-only - or, instead:
//       choice,      {newName, options: [{id, name}]} - a new category
//                    (named newName to start with) or one of options
//       prefill,     {amount, wage_bill, cadence_interval, cadence_unit,
//                    weekdays[], monthly_days[], yearly_day, yearly_month,
//                    start_date, no_end_date}
//       onSuccess    function({kind, categoryName}) - without it the page
//                    reloads
//   })
//
// Opened and closed with .modal--open, the class every dialog uses - the
// change poller in script.js reads it to know a dialog is up and not reload
// under it.
//
// The date and cadence helpers it uses (createMonthlyDaySelect, setGreyedOut,
// calculateEndDateFromOccurrences, validateDateRange, getTodayDateString,
// getFiveYearsFromNowEndDateString) are in script.js, shared with the other
// recurring forms.

let convertState = {};

function showConvertCadenceSection(unit, keepDays) {
    var weekly = document.getElementById('convert-weekly-options');
    var monthly = document.getElementById('convert-monthly-options');
    var yearly = document.getElementById('convert-yearly-options');
    weekly.hidden = unit !== 'weeks';
    monthly.hidden = unit !== 'months';
    yearly.hidden = unit !== 'years';
    if (unit === 'months' && !keepDays) {
        var container = document.getElementById('convert-monthly-day-container');
        container.innerHTML = '';
        container.appendChild(createMonthlyDaySelect('convert_monthly_days[]'));
    }
}

function closeConvertToRecurringModal() {
    document.getElementById('convertToRecurringModal').classList.remove('modal--open');
}

function updateConvertCategoryMode() {
    var existing = document.getElementById('convert-category-mode-existing').checked;
    document.getElementById('convert-category-new-name').disabled = existing;
    document.getElementById('convert-category-existing').disabled = !existing;
}

function openConvertToRecurringModal(opts) {
    opts = opts || {};
    var prefill = opts.prefill || {};
    convertState = {
        kind: opts.kind,
        accountId: opts.accountId || null,
        categoryId: opts.categoryId || null,
        name: opts.name || '',
        choice: opts.choice || null,
        onSuccess: typeof opts.onSuccess === 'function' ? opts.onSuccess : null
    };

    // Wage/bill wording follows the kind.
    var opt1 = document.getElementById('convert-wage-bill-option-1');
    var opt0 = document.getElementById('convert-wage-bill-option-0');
    if (opts.kind === 'income') {
        opt1.textContent = 'Wages (fixed income)';
        opt0.textContent = 'Variable (flexible income)';
    } else {
        opt1.textContent = 'Bill (fixed expense)';
        opt0.textContent = 'Allowance (flexible spending)';
    }

    document.getElementById('convert-to-recurring-form').reset();

    // The category: a fixed one, or a choice between a new and an existing one.
    var fixed = document.getElementById('convert-category-name');
    var choiceBox = document.getElementById('convert-category-choice');
    if (convertState.choice) {
        fixed.hidden = true;
        choiceBox.hidden = false;
        document.getElementById('convert-category-new-name').value = convertState.choice.newName || '';
        var select = document.getElementById('convert-category-existing');
        select.innerHTML = '';
        (convertState.choice.options || []).forEach(function (o) {
            var option = document.createElement('option');
            option.value = String(o.id);
            option.textContent = o.name;
            select.appendChild(option);
        });
        var radioExisting = document.getElementById('convert-category-mode-existing');
        radioExisting.disabled = !select.options.length;
        document.getElementById('convert-category-mode-new').checked = true;
        updateConvertCategoryMode();
    } else {
        fixed.hidden = false;
        choiceBox.hidden = true;
        fixed.value = convertState.name;
    }

    document.getElementById('convert-amount').value = prefill.amount != null ? prefill.amount : '';
    document.getElementById('convert-wage-bill-select').value = prefill.wage_bill != null ? String(prefill.wage_bill) : '1';
    document.getElementById('convert-cadence-interval').value = prefill.cadence_interval || '';
    document.getElementById('convert-start-date').value = prefill.start_date || getTodayDateString();
    document.getElementById('convert-end-date').value = '';
    document.getElementById('convert-no-end-date-checkbox').checked = !!prefill.no_end_date;
    document.getElementById('convert-use-occurrences-checkbox').checked = false;
    document.getElementById('convert-occurrences-count').disabled = true;
    document.getElementById('convert-occurrences-count').value = '';
    document.getElementById('convert-end-date').disabled = false;
    document.querySelectorAll('input[name="convert_weekdays"]').forEach(function (cb) { cb.checked = false; });
    document.getElementById('convert-monthly-day-container').innerHTML = '';

    var unit = prefill.cadence_unit || '';
    if (unit) {
        document.getElementById('convert-cadence-unit').value = unit;
        showConvertCadenceSection(unit, true);
        if (unit === 'weeks') {
            (prefill.weekdays || []).forEach(function (day) {
                var cb = document.querySelector('input[name="convert_weekdays"][value="' + day + '"]');
                if (cb) { cb.checked = true; }
            });
        } else if (unit === 'months') {
            var container = document.getElementById('convert-monthly-day-container');
            var days = (prefill.monthly_days && prefill.monthly_days.length) ? prefill.monthly_days : [null];
            days.forEach(function (day) {
                var row = createMonthlyDaySelect('convert_monthly_days[]');
                var sel = row.querySelector('select');
                if (sel && day != null) { sel.value = String(day); }
                container.appendChild(row);
            });
        } else if (unit === 'years') {
            if (prefill.yearly_day) { document.getElementById('convert-yearly-day').value = prefill.yearly_day; }
            if (prefill.yearly_month) { document.getElementById('convert-yearly-month').value = String(prefill.yearly_month); }
        }
    } else {
        showConvertCadenceSection('', true);
    }
    updateConvertEndDateState();

    document.getElementById('convertToRecurringModal').classList.add('modal--open');
}

// The category the form will save into: {existing: true, id, name} or
// {existing: false, name}. Null when a new one has no name.
function convertTarget() {
    if (!convertState.choice) {
        return { existing: true, id: convertState.categoryId, name: convertState.name };
    }
    if (document.getElementById('convert-category-mode-existing').checked) {
        var select = document.getElementById('convert-category-existing');
        var option = select.options[select.selectedIndex];
        return option ? { existing: true, id: option.value, name: option.textContent } : null;
    }
    var name = document.getElementById('convert-category-new-name').value.trim();
    return name ? { existing: false, name: name } : null;
}

function convertPostJson(url, data) {
    return $.ajax({ url: url, method: 'POST', contentType: 'application/json', data: JSON.stringify(data) });
}

// Saves the recurring entry for the target; resolves with the response of
// the last request.
function convertSave(kind, target, formData) {
    if (kind === 'income' || kind === 'expense') {
        if (target.existing) {
            return convertPostJson('/convert-to-recurring-' + kind, $.extend({ category_id: target.id }, formData));
        }
        return convertPostJson('/add-recurring-' + kind, $.extend({ category_name: target.name }, formData));
    }
    var accountId = convertState.accountId;
    if (target.existing) {
        return convertPostJson('/add-recurring-ca-expense', $.extend({ category_id: target.id, account_id: accountId }, formData));
    }
    // A new card category is an expense category first: every card gets its
    // copy, and this card's copy is made recurring.
    return $.ajax({ url: '/add_expense_category', method: 'POST', data: { name: target.name } })
        .then(function (made) {
            if (!made || made.status !== 'success') { return $.Deferred().reject({ responseJSON: made }); }
            return $.getJSON('/api/recurring-suggested/card-category', { account_id: accountId, name: target.name });
        })
        .then(function (found) {
            return convertPostJson('/add-recurring-ca-expense', $.extend({ category_id: found.category_id, account_id: accountId }, formData));
        });
}

// Three-way toggle: End Date / No End Date / Use Occurrences.
function updateConvertEndDateState() {
    var noEnd = $('#convert-no-end-date-checkbox').is(':checked');
    var useOcc = $('#convert-use-occurrences-checkbox').is(':checked');
    if (noEnd) {
        setGreyedOut('.date-checkbox-row:has(#convert-no-end-date-checkbox)', false);
        setGreyedOut('.date-checkbox-row:has(#convert-use-occurrences-checkbox)', true);
        $('#convert-end-date-label-container, #convert-end-date-input-container').css('opacity', 0.5);
        $('#convert-end-date').prop('disabled', true);
        $('#convert-occurrences-count').prop('disabled', true).val('');
    } else if (useOcc) {
        setGreyedOut('.date-checkbox-row:has(#convert-no-end-date-checkbox)', true);
        setGreyedOut('.date-checkbox-row:has(#convert-use-occurrences-checkbox)', false);
        $('#convert-end-date-label-container, #convert-end-date-input-container').css('opacity', 0.5);
        $('#convert-end-date').prop('disabled', true);
        $('#convert-occurrences-count').prop('disabled', false);
    } else {
        var hasEndDate = $('#convert-end-date').val() !== '';
        setGreyedOut('.date-checkbox-row:has(#convert-no-end-date-checkbox)', hasEndDate);
        setGreyedOut('.date-checkbox-row:has(#convert-use-occurrences-checkbox)', hasEndDate);
        $('#convert-end-date-label-container, #convert-end-date-input-container').css('opacity', 1);
        $('#convert-end-date').prop('disabled', false);
        $('#convert-occurrences-count').prop('disabled', true).val('');
    }
}

$(function () {
    var modal = document.getElementById('convertToRecurringModal');
    if (!modal) { return; }

    // Close on a press outside the dialog, or on Escape.
    ['mousedown', 'touchstart'].forEach(function (evtType) {
        modal.addEventListener(evtType, function (e) {
            if (e.target === modal) { closeConvertToRecurringModal(); }
        });
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && modal.classList.contains('modal--open')) { closeConvertToRecurringModal(); }
    });

    $('input[name="convert_category_mode"]').on('change', updateConvertCategoryMode);

    document.getElementById('convert-cadence-unit').addEventListener('change', function () {
        showConvertCadenceSection(this.value, false);
    });

    document.getElementById('convert-add-monthly-day-btn').addEventListener('click', function () {
        document.getElementById('convert-monthly-day-container').appendChild(
            createMonthlyDaySelect('convert_monthly_days[]')
        );
    });

    $('#convert-no-end-date-checkbox').on('change', function () {
        if (this.checked) { $('#convert-use-occurrences-checkbox').prop('checked', false); }
        updateConvertEndDateState();
    });
    $('#convert-use-occurrences-checkbox').on('change', function () {
        if (this.checked) { $('#convert-no-end-date-checkbox').prop('checked', false); }
        updateConvertEndDateState();
    });
    $('#convert-end-date').on('change input', function () {
        updateConvertEndDateState();
    });
    // Click on the end date to activate it.
    $('#convert-end-date-label-container, #convert-end-date-input-container').on('click', function () {
        if ($('#convert-end-date').prop('disabled')) {
            $('#convert-no-end-date-checkbox').prop('checked', false);
            $('#convert-use-occurrences-checkbox').prop('checked', false);
            updateConvertEndDateState();
            $('#convert-end-date').focus();
        }
    });

    // The end date worked out from a number of occurrences.
    $('#convert-occurrences-count, #convert-start-date, #convert-cadence-interval, #convert-cadence-unit, input[name="convert_weekdays"], #convert-yearly-day, #convert-yearly-month').on('input change', function () {
        if ($('#convert-use-occurrences-checkbox').is(':checked')) {
            var selectedWeekdays = [];
            $('input[name="convert_weekdays"]:checked').each(function () { selectedWeekdays.push($(this).val()); });
            var selectedMonthlyDays = [];
            $('div#convert-monthly-day-container .monthly-day-select').each(function () {
                if ($(this).val()) { selectedMonthlyDays.push($(this).val()); }
            });
            $('#convert-end-date').val(calculateEndDateFromOccurrences(
                $('#convert-start-date').val(), $('#convert-cadence-interval').val(), $('#convert-cadence-unit').val(),
                $('#convert-occurrences-count').val(), selectedWeekdays, selectedMonthlyDays,
                $('#convert-yearly-day').val(), $('#convert-yearly-month').val()));
        }
    });

    // Two decimal places at most.
    document.getElementById('convert-amount').addEventListener('input', function () {
        var value = this.value;
        var parts = value.split('.');
        if (parts.length > 2) {
            this.value = parts[0] + '.' + parts.slice(1).join('');
            value = this.value;
        }
        if (value.indexOf('.') !== -1) {
            var decimals = value.split('.');
            if (decimals[1] && decimals[1].length > 2) {
                this.value = decimals[0] + '.' + decimals[1].substring(0, 2);
            }
        }
    });

    $('#convert-to-recurring-form').on('submit', function (event) {
        event.preventDefault();
        var $form = $(this);
        var startDate = $form.find('#convert-start-date').val();
        var endDate = $form.find('#convert-end-date').val();
        var noEndDateChecked = $form.find('#convert-no-end-date-checkbox').is(':checked');
        var useOccChecked = $form.find('#convert-use-occurrences-checkbox').is(':checked');

        if (!noEndDateChecked && !useOccChecked && !endDate) {
            showEndDateWarning('#convert-end-date');
            return false;
        }
        if (!noEndDateChecked && !useOccChecked && new Date(endDate) < new Date(startDate)) {
            showToast('End date cannot be earlier than the start date.', 'warning');
            return false;
        }
        if (!validateDateRange(startDate, endDate, noEndDateChecked)) {
            return false;
        }
        var target = convertTarget();
        if (!target) {
            showToast('Give the new category a name.', 'warning');
            $('#convert-category-new-name').focus();
            return false;
        }

        var selectedWeekdays = [];
        $form.find('input[name="convert_weekdays"]:checked').each(function () { selectedWeekdays.push($(this).val()); });
        var selectedMonthlyDays = [];
        $form.find('div#convert-monthly-day-container .monthly-day-select').each(function () {
            if ($(this).val()) { selectedMonthlyDays.push($(this).val()); }
        });

        var formData = {
            amount: $form.find('#convert-amount').val(),
            cadence_interval: $form.find('#convert-cadence-interval').val(),
            cadence_unit: $form.find('#convert-cadence-unit').val(),
            start_date: startDate,
            end_date: endDate,
            weekdays: selectedWeekdays,
            monthly_days: selectedMonthlyDays,
            wage_bill: parseInt($form.find('#convert-wage-bill-select').val(), 10),
            no_end_date: noEndDateChecked ? 1 : 0
        };

        if (formData.cadence_unit === 'years') {
            var yearlyDay = $form.find('#convert-yearly-day').val();
            var yearlyMonth = $form.find('#convert-yearly-month').val();
            if (!yearlyDay || !yearlyMonth) {
                showToast('Please select both a day and a month for yearly recurrence.', 'warning');
                return false;
            }
            formData.yearly_day = yearlyDay;
            formData.yearly_month = yearlyMonth;
        }
        if (useOccChecked) {
            var occ = $form.find('#convert-occurrences-count').val();
            formData.end_date = calculateEndDateFromOccurrences(
                startDate, formData.cadence_interval, formData.cadence_unit, occ,
                selectedWeekdays, selectedMonthlyDays,
                formData.yearly_day || null, formData.yearly_month || null);
            formData.occurrences_count = occ;
        }
        if (noEndDateChecked) {
            formData.end_date = getFiveYearsFromNowEndDateString();
        }

        var kind = convertState.kind;
        var onSuccess = convertState.onSuccess;
        var loading = document.getElementById('loading-screen');
        loading.style.display = 'flex';

        // The dialog stays open until it has saved, so a refusal - a name
        // already taken - can be corrected rather than typed again.
        convertSave(kind, target, formData)
            .then(function (response) {
                if (!response || response.status !== 'success') {
                    return $.Deferred().reject({ responseJSON: response });
                }
                closeConvertToRecurringModal();
                if (onSuccess) {
                    loading.style.display = 'none';
                    onSuccess({ kind: kind, categoryName: target.name });
                    return;
                }
                if (kind === 'ca' && typeof saveCaDailyBalance === 'function') {
                    saveCaDailyBalance();
                } else if (typeof saveDailyTotalsAndRemainders === 'function') {
                    saveDailyTotalsAndRemainders();
                }
                location.reload();
            })
            .fail(function (xhr) {
                loading.style.display = 'none';
                var message = (xhr && xhr.responseJSON && xhr.responseJSON.message) || (xhr && xhr.statusText) || 'Something went wrong.';
                showToast('Could not make it recurring: ' + message, 'error');
            });
    });
});
