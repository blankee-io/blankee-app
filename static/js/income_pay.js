/* Simple or Advanced - "calculate from my pay", the optional part of a Wages form.
 *
 * Pairs with templates/_income_pay_fields.html (one per form, by prefix) and
 * templates/_income_pay_job_modal.html (the job editor, once per page). Each
 * form that offers it calls, once:
 *
 *     incomePay.attach({prefix: 'add', form: '#add-recurring-income-form',
 *                       wageBill: '#wage-bill-select', startDate: '#start_date',
 *                       amount: '#amount'})
 *
 * and, building the request it sends:
 *
 *     incomePay.extend('add', formData)            add / edit
 *     incomePay.extend('edit', raiseData, {raise: true})
 *
 * which adds the pay fields when the section is open (and, editing a row that
 * was calculated from pay with the section now closed, asks for a fixed
 * amount again). incomePay.load(prefix, row) fills it from a table row's
 * data-pay-* attributes when an edit opens; incomePay.reset(prefix) closes it.
 *
 * Closed - the default - the form is untouched. Open, the fields the job now
 * decides (amount, cadence, a number of repeats) are hidden and stop being
 * required, and a preview shows the next paychecks.
 */
window.incomePay = (function () {
    'use strict';

    // What the job decides instead of the form, inside each form.
    var REPLACED = ['label[for$="amount"]', '.currency-input-container:has(input.recurring-amount:not([id$="pay-rate"]))',
                    'label[for$="cadence-interval"]', '.cadence-row',
                    '[id$="weekly-options"]', '[id$="monthly-options"]', '[id$="yearly-options"]',
                    '.date-checkbox-row:has([id$="use-occurrences-checkbox"])'];

    var forms = {};
    var jobs = null;
    var jobsLoading = null;

    function money(n) {
        var symbol = (document.querySelector('.r-currency-symbol') || {}).textContent || '$';
        return symbol.trim() + Number(n || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }

    function shortDate(iso) {
        var d = new Date(iso + 'T00:00:00');
        return d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
    }

    function loadJobs(force) {
        if (jobs && !force) { return $.Deferred().resolve(jobs).promise(); }
        if (jobsLoading && !force) { return jobsLoading; }
        jobsLoading = $.getJSON('/api/jobs').then(function (body) {
            jobs = (body && body.jobs) || [];
            jobsLoading = null;
            Object.keys(forms).forEach(function (p) { forms[p].drawJobs(); });
            return jobs;
        }, function () {
            jobsLoading = null;
            jobs = jobs || [];
            return jobs;
        });
        return jobsLoading;
    }

    function jobById(id) {
        return (jobs || []).find(function (j) { return String(j.id) === String(id); }) || null;
    }

    function attach(opts) {
        var p = opts.prefix;
        var form = $(opts.form);
        var wrap = $('#' + p + '-income-pay');
        if (!form.length || !wrap.length) { return; }

        var toggle = $('#' + p + '-pay-advanced');
        var fields = $('#' + p + '-pay-fields');
        var jobSelect = $('#' + p + '-pay-job');
        var rate = $('#' + p + '-pay-rate');
        var pct = $('#' + p + '-pay-pct');
        var overtime = $('#' + p + '-pay-overtime');
        var paidHolidays = $('#' + p + '-pay-paid-holidays');
        var preview = $('#' + p + '-pay-preview');
        var replaced = form.find(REPLACED.join(','));
        var requiredOnes = replaced.find('[required]').addBack('[required]');
        var state = {wasPay: false, raise: false, wantJob: null, timer: null, seq: 0};

        function isOpen() { return toggle.is(':checked') && !wrap.prop('hidden'); }
        function payType() { return form.find('input[name="' + p + '_pay_type"]:checked').val() || 'hourly'; }
        function salaryPeriod() { return form.find('input[name="' + p + '_salary_period"]:checked').val() || 'year'; }
        function isWages() { return String($(opts.wageBill).val()) === '1'; }

        function drawJobs() {
            var keep = jobSelect.val() || state.wantJob;
            jobSelect.empty();
            if (!jobs || !jobs.length) {
                jobSelect.append($('<option>').val('').text('No jobs yet - add one'));
            } else {
                jobs.forEach(function (j) {
                    var label = j.name + (j.has_pay_date ? '' : ' (needs a next pay date)');
                    jobSelect.append($('<option>').val(String(j.id)).text(label));
                });
            }
            if (keep && jobById(keep)) { jobSelect.val(String(keep)); }
            state.wantJob = null;
            $('#' + p + '-pay-job-edit').prop('disabled', !jobSelect.val());
        }

        function syncType() {
            var hourly = payType() === 'hourly';
            $('#' + p + '-pay-rate-label').text(hourly ? 'Hourly rate, before tax'
                : (salaryPeriod() === 'month' ? 'Monthly salary, before tax' : 'Annual salary, before tax'));
            $('#' + p + '-pay-period').prop('hidden', hourly);
            $('#' + p + '-pay-hourly-flags').prop('hidden', !hourly);
        }

        function setOpen(open) {
            toggle.prop('checked', open);
            $('#' + p + '-pay-simple').prop('checked', !open);
            fields.prop('hidden', !open);
            replaced.toggleClass('income-pay-replaced', open);
            requiredOnes.prop('required', !open);
            if (open) {
                syncType();
                loadJobs().then(function () { drawJobs(); refresh(); });
            } else {
                preview.empty();
            }
        }

        function syncVisible() {
            // Wages only - and in a raise, only on a row already calculated from
            // pay: a raise keeps the kind of income it splits.
            var show = isWages() && (!state.raise || state.wasPay);
            wrap.prop('hidden', !show);
            $('#' + p + '-pay-mode').prop('hidden', !show);
            if (!show && toggle.is(':checked')) { setOpen(false); }
        }

        function values() {
            return {
                pay_type: payType(),
                shelf_id: jobSelect.val(),
                pay_rate: rate.val(),
                take_home_pct: pct.val() || 100,
                salary_period: payType() === 'salary' ? salaryPeriod() : null,
                overtime: payType() === 'hourly' && overtime.is(':checked') ? 1 : 0,
                paid_holidays: payType() === 'hourly' && paidHolidays.is(':checked') ? 1 : 0
            };
        }

        function refresh() {
            clearTimeout(state.timer);
            state.timer = setTimeout(drawPreview, 300);
        }

        function drawPreview() {
            if (!isOpen()) { return; }
            var v = values();
            if (!v.shelf_id) { preview.empty(); return; }
            if (!(parseFloat(v.pay_rate) > 0)) { preview.html('<p class="income-pay-note">Enter the rate to see your next paychecks.</p>'); return; }
            var seq = ++state.seq;
            $.ajax({
                url: '/api/pay-preview', method: 'POST', contentType: 'application/json',
                data: JSON.stringify($.extend({start_date: $(opts.startDate).val(), count: 6}, v))
            }).done(function (body) {
                if (seq !== state.seq) { return; }
                var rows = (body && body.paychecks) || [];
                if (!rows.length) { preview.html('<p class="income-pay-note">No paychecks found for this job.</p>'); return; }
                var table = $('<table class="income-pay-table"><thead><tr><th>Payday</th><th>Hours</th><th>Gross</th><th>Take-home</th></tr></thead><tbody></tbody></table>');
                rows.forEach(function (r) {
                    var notes = [];
                    if (r.holiday) { notes.push('holiday ' + r.holiday + 'h'); }
                    if (r.pto) { notes.push('PTO ' + r.pto + 'h'); }
                    if (r.unpaid) { notes.push('unpaid ' + r.unpaid + 'h'); }
                    if (r.overtime) { notes.push('OT ' + r.overtime + 'h'); }
                    var hours = $('<td>').text(r.hours + 'h');
                    if (notes.length) { hours.append($('<span class="income-pay-sub">').text(notes.join(', '))); }
                    $('<tr>')
                        .append($('<td>').text(shortDate(r.pay_date))
                            .append($('<span class="income-pay-sub">').text(shortDate(r.period_start) + ' - ' + shortDate(r.period_end))))
                        .append(hours)
                        .append($('<td>').text(money(r.gross)))
                        .append($('<td class="income-pay-net">').text(money(r.net)))
                        .appendTo(table.find('tbody'));
                });
                preview.empty().append(table);
            }).fail(function (xhr) {
                if (seq !== state.seq) { return; }
                var msg = (xhr.responseJSON && xhr.responseJSON.message) || 'Could not work the paychecks out.';
                preview.empty().append($('<p class="income-pay-note">').text(msg));
            });
        }

        function openJob(job) {
            if (!window.loafShelfModal) { showToast('The job editor is not available on this page.', 'error'); return; }
            window.loafShelfModal.onSaved = function (response) {
                state.wantJob = (response && response.shelf_id) || jobSelect.val();
                loadJobs(true).then(function () { jobSelect.val(String(state.wantJob)); drawJobs(); refresh(); });
            };
            if (job) { window.loafShelfModal.openEditFrom(job); }
            else { window.loafShelfModal.openAdd(); }
        }

        form.find('input[name="' + p + '_pay_mode"]').on('change', function () { setOpen(toggle.is(':checked')); });
        // A form opened fresh is reset by whoever opens it - every opener does -
        // so that is when the section closes too; after the reset has run.
        form.on('reset', function () { setTimeout(function () { forms[p].reset(); }, 0); });
        $(opts.wageBill).on('change', syncVisible);
        form.find('input[name="' + p + '_pay_type"], input[name="' + p + '_salary_period"]')
            .on('change', function () { syncType(); refresh(); });
        jobSelect.on('change', function () { $('#' + p + '-pay-job-edit').prop('disabled', !jobSelect.val()); refresh(); });
        rate.add(pct).on('input', refresh);
        overtime.add(paidHolidays).on('change', refresh);
        $(opts.startDate).on('change', refresh);
        $('#' + p + '-pay-job-edit').on('click', function () { openJob(jobById(jobSelect.val())); });
        $('#' + p + '-pay-job-new').on('click', function () { openJob(null); });

        forms[p] = {
            drawJobs: drawJobs,
            isOpen: isOpen,
            values: values,
            state: state,
            reset: function () {
                state.wasPay = false; state.raise = false;
                rate.val(''); pct.val(''); overtime.prop('checked', false); paidHolidays.prop('checked', false);
                form.find('input[name="' + p + '_pay_type"][value="hourly"]').prop('checked', true);
                form.find('input[name="' + p + '_salary_period"][value="year"]').prop('checked', true);
                jobSelect.prop('disabled', false);
                setOpen(false);
                syncVisible();
            },
            load: function (row, loadOpts) {
                loadOpts = loadOpts || {};
                var type = row.attr('data-pay-type') || '';
                state.wasPay = !!type;
                state.raise = !!loadOpts.raise;
                rate.val(type ? (parseFloat(row.attr('data-pay-rate')) || '') : '');
                // Stored to 8 places: 82.00000000 shows as 82, 78.1234 as itself.
                var storedPct = parseFloat(row.attr('data-take-home-pct'));
                pct.val(type && storedPct > 0 ? String(storedPct) : '');
                overtime.prop('checked', row.attr('data-overtime') === '1');
                paidHolidays.prop('checked', row.attr('data-paid-holidays') === '1');
                form.find('input[name="' + p + '_pay_type"][value="' + (type || 'hourly') + '"]').prop('checked', true);
                form.find('input[name="' + p + '_salary_period"][value="' + (row.attr('data-salary-period') || 'year') + '"]').prop('checked', true);
                state.wantJob = row.attr('data-shelf-id') || null;
                // A raise keeps the job: it changes what the job pays.
                jobSelect.prop('disabled', state.raise);
                form.find('input[name="' + p + '_pay_type"]').prop('disabled', state.raise);
                syncVisible();
                setOpen(state.wasPay && !wrap.prop('hidden'));
                if (state.wasPay && opts.amount) {
                    // Turned off, the row goes back to a fixed amount: start from
                    // the typical paycheck it has been showing.
                    $(opts.amount).val(row.attr('data-amount') || '');
                }
            }
        };
        syncVisible();
    }

    // The first field still missing is outlined where it is, as the end date
    // is (showFieldWarning in script.js), and its message returned - so a
    // caller only stops the submit.
    function validate(p) {
        var f = forms[p];
        if (!f || !f.isOpen()) { return null; }
        var v = f.values();
        var box = null, message = null;
        if (!v.shelf_id && !f.state.raise) {
            box = '#' + p + '-pay-job';
            message = 'Choose the job this pay comes from, or add one.';
        } else if (!(parseFloat(v.pay_rate) > 0)) {
            box = '#' + p + '-pay-rate';
            message = v.pay_type === 'hourly' ? 'Enter the hourly rate.' : 'Enter the salary.';
        } else {
            var pct = parseFloat(v.take_home_pct);
            if (!(pct > 0 && pct <= 100)) {
                box = '#' + p + '-pay-pct';
                message = 'Take-home has to be a percentage above 0 and up to 100.';
            }
        }
        if (!message) { return null; }
        showFieldWarning($(box).parent(), message);
        return message;
    }

    function extend(p, data, extendOpts) {
        var f = forms[p];
        if (!f) { return data; }
        extendOpts = extendOpts || {};
        if (f.isOpen()) {
            var v = f.values();
            if (extendOpts.raise) {
                data.pay_rate = v.pay_rate;
                data.take_home_pct = v.take_home_pct;
                data.overtime = v.overtime;
                data.paid_holidays = v.paid_holidays;
                if (v.salary_period) { data.salary_period = v.salary_period; }
            } else {
                $.extend(data, v);
            }
        } else if (f.state.wasPay && !extendOpts.raise) {
            data.pay_type = '';
        }
        return data;
    }

    return {
        attach: attach,
        extend: extend,
        validate: validate,
        load: function (p, row, loadOpts) { if (forms[p]) { forms[p].load(row, loadOpts); } },
        reset: function (p) { if (forms[p]) { forms[p].reset(); } },
        isOpen: function (p) { return !!(forms[p] && forms[p].isOpen()); }
    };
})();
