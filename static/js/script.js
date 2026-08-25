// General Functions

// ═══════════════════════════════════════════════════════════════
// INLINE DUPLICATE CATEGORY NAME CHECK
// ═══════════════════════════════════════════════════════════════

/**
 * Check if a category name already exists and show/hide an inline warning.
 * Works with both <input> elements and contenteditable elements.
 *
 * The warning div is placed ABOVE the input field:
 * - Inside modal content containers: prepended as first child
 * - Outside flex-row containers: inserted before them
 * - Default: inserted before the input itself
 *
 * @param {HTMLElement} inputEl       - The input or contenteditable element
 * @param {Array}       categories    - Array of { id, name, is_auto_adjustment, ... }
 * @param {Object}      [opts]        - Options
 * @param {string|number} [opts.excludeId] - Category ID to exclude (for renames)
 * @returns {boolean} true if a duplicate exists
 */
function checkCategoryDuplicate(inputEl, categories, opts) {
    opts = opts || {};
    var name = (inputEl.value !== undefined ? inputEl.value : inputEl.textContent || '').trim().toLowerCase();

    // Get or create the warning element
    var warningEl = inputEl._dupWarning;
    if (!warningEl) {
        warningEl = document.createElement('div');
        warningEl.className = 'duplicate-name-warning';
        warningEl.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> <span>A category with this name already exists.</span>';

        // Place warning floating ABOVE the input using absolute positioning.
        // Ensure the input's offset parent can anchor the warning.
        var posParent = inputEl.closest(
            '.category-edit-modal-content, .manage-cat-input-container, ' +
            '.floating-input-container, .category-input-wrapper'
        ) || inputEl.parentNode;
        if (posParent && getComputedStyle(posParent).position === 'static') {
            posParent.style.position = 'relative';
        }
        // Insert inside the positioned parent
        posParent.appendChild(warningEl);
        inputEl._dupWarning = warningEl;
    }

    if (!name) {
        warningEl.classList.remove('visible');
        return false;
    }

    var isDuplicate = false;
    for (var i = 0; i < categories.length; i++) {
        var cat = categories[i];
        if (opts.excludeId && String(cat.id) === String(opts.excludeId)) continue;
        if ((cat.name || '').trim().toLowerCase() === name) {
            isDuplicate = true;
            break;
        }
    }

    if (isDuplicate) {
        warningEl.classList.add('visible');
    } else {
        warningEl.classList.remove('visible');
    }
    return isDuplicate;
}

/**
 * Attach a live duplicate-check listener to an input element.
 * Returns an object with a .check() method for manual re-checks.
 *
 * @param {HTMLElement} inputEl    - The input or contenteditable element
 * @param {Function}    getCats    - Function returning the current categories array
 * @param {Object}      [opts]     - Options passed to checkCategoryDuplicate
 * @returns {{ check: Function }}
 */
function setupCategoryDuplicateCheck(inputEl, getCats, opts) {
    opts = opts || {};
    function doCheck() {
        return checkCategoryDuplicate(inputEl, getCats(), opts);
    }
    inputEl.addEventListener('input', doCheck);
    return { check: doCheck };
}

// ═══════════════════════════════════════════════════════════════
// TOAST NOTIFICATIONS
// ═══════════════════════════════════════════════════════════════

/**
 * Show a temporary toast notification.
 * @param {string} message - The message to display
 * @param {string} [type='error'] - Toast type: 'error' | 'warning' | 'info' | 'success'
 * @param {number} [duration=4000] - Auto-dismiss time in ms (0 to disable)
 */
function showToast(message, type, duration) {
    if (type === undefined || type === null) type = 'error';
    if (duration === undefined || duration === null) duration = 4000;

    // Ensure container exists
    var container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
    }

    var icons = {
        error:   '<i class="fa-solid fa-circle-exclamation toast-icon"></i>',
        warning: '<i class="fa-solid fa-triangle-exclamation toast-icon"></i>',
        info:    '<i class="fa-solid fa-circle-info toast-icon"></i>',
        success: '<i class="fa-solid fa-circle-check toast-icon"></i>'
    };

    var toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.innerHTML =
        (icons[type] || icons.error) +
        '<span class="toast-message">' + _escapeHtml(message) + '</span>' +
        '<button class="toast-close" aria-label="Close">&times;</button>';

    container.appendChild(toast);

    // Close on click
    toast.querySelector('.toast-close').addEventListener('click', function() {
        _removeToast(toast);
    });

    // Auto-dismiss
    if (duration > 0) {
        setTimeout(function() { _removeToast(toast); }, duration);
    }
}

function _escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

function _removeToast(el) {
    if (!el || el.classList.contains('toast-removing')) return;
    el.classList.add('toast-removing');
    el.addEventListener('animationend', function() { el.remove(); });
}

/**
 * Return the icon HTML for a category based on its properties.
 * Works for income, expense, and credit account categories.
 * @param {object} cat  Category object with name, is_bud, is_recurring, is_credit_account, is_auto_adjustment, is_interest
 * @returns {string} HTML string for the icon <i> element
 */
function getCategoryIcon(cat) {
    if (!cat) return '<i class="fa-regular fa-folder category-icon"></i>';
    if (cat.is_bud) return '<i class="fa-regular fa-seedling category-icon"></i>';
    if (cat.is_credit_account && cat.is_recurring) return '<i class="fa-kit fa-regular-credit-card-sync category-icon"></i>';
    if (Number(cat.is_savings) === 1 && cat.is_recurring) return '<i class="fa-kit fa-regular-piggy-bank-sync-bl category-icon category-icon-flip"></i>';
    if (Number(cat.is_savings) === 1) return '<i class="fa-regular fa-piggy-bank category-icon category-icon-flip"></i>';
    if (cat.is_recurring) return '<i class="fa-regular fa-arrows-repeat category-icon"></i>';
    if (cat.is_credit_account) return '<i class="fa-regular fa-credit-card category-icon"></i>';
    if (cat.is_interest) return '<i class="fa-regular fa-percent category-icon"></i>';
    if (cat.is_auto_adjustment) return '<i class="fa-regular fa-lock category-icon"></i>';
    return '<i class="fa-regular fa-folder category-icon"></i>';
}

/**
 * Return the icon HTML for an entry type in the dashboard_d add-entry dropdown.
 * @param {object} type  Entry type object with id and name
 * @returns {string} HTML string for the icon <i> element
 */
function getEntryTypeIcon(type) {
    if (!type) return '';
    if (type.id === 'income') return '<i class="fa-solid fa-plus category-icon"></i>';
    if (type.id === 'expense') return '<i class="fa-solid fa-minus category-icon"></i>';
    if (type.id && type.id.startsWith('ca_')) return '<i class="fa-kit fa-solid-credit-card-circle-minus category-icon"></i>';
    return '';
}

/**
 * Show an inline warning right above the end-date options inside a .date-row,
 * with an orange highlight border around the options area.
 * @param {string} endDateSelector  jQuery selector for the end-date <input>
 */
function showEndDateWarning(endDateSelector) {
    var $endDate = $(endDateSelector);
    var $dateRow = $endDate.closest('.date-row');
    if (!$dateRow.length) return;

    // Remove any existing warning first
    $dateRow.find('.end-date-inline-toast').remove();
    $dateRow.removeClass('end-date-warning-highlight');

    // Add highlight
    $dateRow.addClass('end-date-warning-highlight');

    // Create floating toast (absolute positioned, no layout shift)
    var $toast = $('<div class="end-date-inline-toast">' +
        '<i class="fa-solid fa-triangle-exclamation"></i>' +
        '<span>Please select an end date option.</span>' +
        '</div>');

    $dateRow.append($toast);

    // Scroll into view
    $toast[0].scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    // Auto remove after 4 seconds
    setTimeout(function() {
        $dateRow.removeClass('end-date-warning-highlight');
        $toast.addClass('end-date-inline-toast-removing');
        $toast.on('animationend', function() { $toast.remove(); });
    }, 4000);
}

// ═══════════════════════════════════════════════════════════════
// GENERIC CONFIRM MODAL
// ═══════════════════════════════════════════════════════════════

/**
 * Show a confirm/cancel modal (replaces native confirm()).
 * Returns a Promise<boolean>.
 *
 * @param {Object} opts
 * @param {string} opts.message      - Body text
 * @param {string} [opts.title]      - Modal title (default: 'Confirm')
 * @param {string} [opts.confirmText]- Confirm button label (default: 'Confirm')
 * @param {string} [opts.cancelText] - Cancel button label (default: 'Cancel')
 * @param {boolean}[opts.danger]     - Use red confirm button (default: false)
 * @param {boolean}[opts.hideCancel] - Hide the cancel button (default: false)
 * @param {Object} [opts.checkbox]   - Optional "remember this" checkbox
 * @param {string}  opts.checkbox.label      - Its label
 * @param {string}  opts.checkbox.storageKey - localStorage key to remember it under
 * @param {string} [opts.checkbox.persistOn] - 'confirm' (default), 'cancel' or 'both'
 * @param {*}      [opts.checkbox.skipResult]- what to resolve with when remembered
 *                                             (default true)
 */
function showConfirmModal(opts) {
    return new Promise(function(resolve) {
        // A remembered checkbox short-circuits the dialog. What it resolves to
        // depends on what the checkbox meant:
        //
        //   "do not ask again"       -> the user already agreed, so confirm.
        //   "do not show this again" -> the user dismissed it, so do NOT.
        //
        // skipResult says which. It defaults to true, the original behaviour,
        // which is what the entry-update callers in the dashboards expect.
        if (opts.checkbox && opts.checkbox.storageKey) {
            if (localStorage.getItem(opts.checkbox.storageKey) === '1') {
                resolve(opts.checkbox.skipResult !== undefined
                        ? opts.checkbox.skipResult : true);
                return;
            }
        }

        // Ensure modal exists in DOM
        var modal = document.getElementById('generic-confirm-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'generic-confirm-modal';
            modal.className = 'modal';
            modal.innerHTML =
                '<div class="modal-content center-modal">' +
                    '<span id="generic-confirm-close" class="close-modal">&times;</span>' +
                    '<h2 id="generic-confirm-title">Confirm</h2>' +
                    '<p id="generic-confirm-message"></p>' +
                    '<div id="generic-confirm-checkbox-row" class="modal-checkbox-row" style="display:none;">' +
                        '<input type="checkbox" id="generic-confirm-checkbox">' +
                        '<label for="generic-confirm-checkbox" id="generic-confirm-checkbox-label"></label>' +
                    '</div>' +
                    '<div class="modal-buttons">' +
                        '<button id="generic-confirm-btn">Confirm</button>' +
                        '<button id="generic-cancel-btn">Cancel</button>' +
                    '</div>' +
                '</div>';
            document.body.appendChild(modal);
        }

        var titleEl      = document.getElementById('generic-confirm-title');
        var msgEl        = document.getElementById('generic-confirm-message');
        var confirmBtn   = document.getElementById('generic-confirm-btn');
        var cancelBtn    = document.getElementById('generic-cancel-btn');
        var closeBtn     = document.getElementById('generic-confirm-close');
        var checkboxRow  = document.getElementById('generic-confirm-checkbox-row');
        var checkboxEl   = document.getElementById('generic-confirm-checkbox');
        var checkboxLbl  = document.getElementById('generic-confirm-checkbox-label');

        titleEl.textContent   = opts.title || 'Confirm';
        msgEl.textContent     = opts.message || '';
        confirmBtn.textContent= opts.confirmText || 'Confirm';
        cancelBtn.textContent = opts.cancelText || 'Cancel';

        // Hide cancel button if requested (for OK-only informational modals)
        cancelBtn.style.display = opts.hideCancel ? 'none' : '';

        if (opts.danger) {
            confirmBtn.classList.add('danger');
        } else {
            confirmBtn.classList.remove('danger');
        }

        // Checkbox setup
        if (opts.checkbox && opts.checkbox.label) {
            checkboxRow.style.display = '';
            checkboxLbl.textContent = opts.checkbox.label;
            checkboxEl.checked = false;
        } else {
            checkboxRow.style.display = 'none';
        }

        modal.classList.add('modal--open');

        function cleanup(result) {
            modal.classList.remove('modal--open');
            confirmBtn.removeEventListener('click', onConfirm);
            cancelBtn.removeEventListener('click', onCancel);
            closeBtn.removeEventListener('click', onCancel);
            modal.removeEventListener('click', onBackdrop);
            document.removeEventListener('keydown', onKeydown);
            resolve(result);
        }
        function persistCheckbox(outcome) {
            // persistOn says which outcomes record the preference. 'confirm' is
            // the default and the original behaviour; a "do not show again"
            // checkbox needs 'both', because the point is that it was ticked
            // while dismissing.
            if (!opts.checkbox || !opts.checkbox.storageKey || !checkboxEl.checked) {
                return;
            }
            var when = opts.checkbox.persistOn || 'confirm';
            if (when === 'both' || when === outcome) {
                localStorage.setItem(opts.checkbox.storageKey, '1');
            }
        }
        function onConfirm() {
            persistCheckbox('confirm');
            cleanup(true);
        }
        function onCancel()  {
            persistCheckbox('cancel');
            cleanup(false);
        }
        function onBackdrop(e) { if (e.target === modal) cleanup(false); }
        function onKeydown(e) {
            if (e.key === 'Enter') { e.preventDefault(); onConfirm(); }
            else if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
        }

        confirmBtn.addEventListener('click', onConfirm);
        cancelBtn.addEventListener('click', onCancel);
        closeBtn.addEventListener('click', onCancel);
        modal.addEventListener('click', onBackdrop);
        document.addEventListener('keydown', onKeydown);
    });
}

// ===== Bank Reconnection Alert =====
// Check for bank connections needing reconnection on page load
(function() {
    // Only run on pages that have the reconnect modal
    if (!document.getElementById('bank-reconnect-modal')) return;
    
    // Check localStorage to see if user already saw this today
    const dismissedKey = 'bank_reconnect_dismissed_date';
    const dismissedDate = localStorage.getItem(dismissedKey);
    const today = new Date().toDateString();
    if (dismissedDate === today) {
        return; // Already shown today
    }
    
    // Check for connections needing reconnection
    fetch('/api/bank/check-reconnect')
        .then(response => response.json())
        .then(data => {
            if (data.needs_reconnect && data.connections && data.connections.length > 0) {
                showReconnectModal(data.connections);
                // Mark as shown for today
                localStorage.setItem(dismissedKey, today);
            }
        })
        .catch(err => console.error('Error checking reconnect status:', err));
})();

// Store reconnect data globally for the modal
window._bankReconnectData = null;

function showReconnectModal(connections) {
    window._bankReconnectData = connections;
    
    const modal = document.getElementById('bank-reconnect-modal');
    const detailsDiv = document.getElementById('reconnect-modal-details');
    const messageEl = document.getElementById('reconnect-modal-message');
    
    if (!modal || !detailsDiv) return;
    
    // Build details HTML
    let detailsHtml = '';
    connections.forEach(conn => {
        detailsHtml += `
            <div class="bank-name"><i class="fa-solid fa-building-columns"></i> ${conn.institution_name}</div>
            <div class="bank-status">Disconnected</div>
        `;
    });
    detailsDiv.innerHTML = detailsHtml;
    
    // Update message if multiple
    if (connections.length > 1) {
        messageEl.textContent = `${connections.length} bank connections need to be reconnected to continue syncing transactions.`;
    } else {
        messageEl.textContent = 'One of your bank connections needs to be reconnected to continue syncing transactions.';
    }
    
    // Show modal
    modal.classList.add('modal--open');
}

function dismissReconnectModal() {
    const modal = document.getElementById('bank-reconnect-modal');
    if (modal) {
        modal.classList.remove('modal--open');
    }
    // Already marked as shown for today when modal appeared
}

function goToReconnect() {
    if (window._bankReconnectData && window._bankReconnectData.length > 0) {
        // Go to profile page with reconnect parameter for first connection
        const connectionId = window._bankReconnectData[0].connection_id;
        window.location.href = '/bank_accounts?reconnect=' + encodeURIComponent(connectionId);
    } else {
        window.location.href = '/profile';
    }
}

// ===== End Bank Reconnection Alert =====

// Calendarnav shadow on scroll
(function() {
    const calendarnav = document.querySelector('.calendarnav');
    if (calendarnav) {
        function updateCalendarnavShadow() {
            if (window.scrollY > 0) {
                calendarnav.classList.add('scrolled');
            } else {
                calendarnav.classList.remove('scrolled');
            }
        }
        window.addEventListener('scroll', updateCalendarnavShadow);
        // Run once immediately in case page is already scrolled
        updateCalendarnavShadow();
    }
})();

// Format a number with commas and 2 decimal places (e.g., 1234567.89 -> "1,234,567.89")
function formatNumberWithCommas(value) {
    const num = parseFloat(value) || 0;
    return num.toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });
}

// Function to refresh the notification badge in the nav
function refreshNotificationBadge() {
    $.ajax({
        url: '/get-unread-notification-count',
        method: 'GET',
        success: function(response) {
            const badge = $('.notification-badge');
            const count = response.count || 0;
            
            if (count === 0) {
                // Remove the badge if no unread notifications
                badge.remove();
            } else if (badge.length === 0) {
                // Add badge if it doesn't exist and there are unread notifications
                $('.nav-notifications').append('<span class="notification-badge"></span>');
            }
        },
        error: function() {
            console.error('Failed to refresh notification badge');
        }
    });
}

// Function to toggle the display of the side navigation
function toggleSidenav() {
    const sidenav = document.getElementById('sidenav');
    const button = document.querySelector('.hamburger-button');
    if (sidenav.classList.contains('open')) {
        sidenav.classList.remove('open');
        button.style.left = '10px';
    } else {
        sidenav.classList.add('open');
        button.style.left = '218px'; // Adjusted for the open state
    }
}

// Function to delete a user account
async function deleteUser(url) {
    var confirmed = await showConfirmModal({
        title: 'Delete Account',
        message: 'Are you sure you want to delete your account? This action cannot be undone.',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        danger: true
    });
    if (confirmed) {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = url;
        document.body.appendChild(form);
        form.submit(); // Submit the form to delete the account
    }
}

// Function to toggle the upload prompt for profile picture
function toggleUploadPrompt() {
    const uploadPrompt = document.getElementById("uploadPrompt");
    if (uploadPrompt.style.display === "block") {
        uploadPrompt.style.display = "none";
    } else {
        uploadPrompt.style.display = "block";

        // Close the upload prompt when clicking outside of it
        document.addEventListener('click', closeUploadPromptOnClickOutside);
    }
}

function closeUploadPromptOnClickOutside(event) {
    const uploadPrompt = document.getElementById("uploadPrompt");
    const picEditButton = document.querySelector('.pic-edit-button');

    // Check if the upload prompt is open and the clicked element is outside of the prompt and the button
    if (uploadPrompt.style.display === "block" && !uploadPrompt.contains(event.target) && !picEditButton.contains(event.target)) {
        uploadPrompt.style.display = "none";

        // Remove the event listener after closing the prompt
        document.removeEventListener('click', closeUploadPromptOnClickOutside);
    }
}

// Register Page

document.addEventListener("DOMContentLoaded", function() {
    if (document.body.id === "register-body") {
        const usernameInput = document.getElementById("username");
        const submitButton = document.getElementById("submit-button");
        const submitIcon = document.getElementById("submit-icon");

        // Event listener for when the user leaves the username field
        usernameInput.addEventListener("blur", function() {
            const username = usernameInput.value;
            const emailPattern = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;

            if (!emailPattern.test(username)) {
                submitIcon.classList.remove("fa-check");
                submitIcon.classList.add("fa-times");
                submitButton.disabled = true; // Disable the submit button
            } else {
                // Simulate an AJAX request to check if the username is taken
                $.ajax({
                    url: "{{ url_for('check_username') }}",
                    method: "POST",
                    data: { username: username },
                    success: function(response) {
                        if (response.status == 'taken') {
                            submitIcon.classList.remove("fa-check");
                            submitIcon.classList.add("fa-times");
                            submitButton.disabled = true; // Disable the submit button
                        } else {
                            submitIcon.classList.remove("fa-times");
                            submitIcon.classList.add("fa-check");
                            submitButton.disabled = false; // Enable the submit button
                        }
                    }
                });
            }
        });
    }
});

// Login Page

document.addEventListener("DOMContentLoaded", function() {
    if (document.body.id === "login-body") {
        const loginModal = document.getElementById("loginModal");
        const loginMessage = document.getElementById("loginMessage");

        // Function to open the modal with a specific message
        function openModal(message) {
            loginMessage.textContent = message;
            loginModal.style.display = "block";
            setTimeout(function() {
                loginModal.style.display = "none";
            }, 3000); // Auto-close modal after 3 seconds
        }

        // Check if there are any messages passed via Flask
        const urlParams = new URLSearchParams(window.location.search);
        const message = urlParams.get('message');

        if (message) {
            openModal(message);
        }
    }
});

// Profile Page

document.addEventListener("DOMContentLoaded", function() {
    if (document.body.id === "profile-body") {
        const successModal = document.getElementById("successModal");
        const successMessage = document.getElementById("successMessage");

        // Only proceed if the modal elements are found
        if (successModal && successMessage) {
            // Function to open the modal with a specific message and auto-close it after 3 seconds
            function openModal(message) {
                successMessage.textContent = message;
                successModal.style.display = "block";
                setTimeout(function() {
                    successModal.style.display = "none";
                    // Clear the URL parameters after the modal is closed
                    window.history.replaceState({}, document.title, window.location.pathname);
                }, 3000); // Auto-close modal after 3 seconds
            }

            // Close the modal when the user clicks anywhere outside of the modal
            window.addEventListener("click", function(event) {
                if (event.target == successModal) {
                    successModal.style.display = "none";
                    // Clear the URL parameters after the modal is closed
                    window.history.replaceState({}, document.title, window.location.pathname);
                }
            });

            // Trigger the modal after a successful update
            if (window.location.search.includes("success=password")) {
                openModal("Your password has been updated successfully!");
            }
        }
    }
});

let dashboardSpinnerCount = 0;

// Use localStorage to remember if the spinner has ever been shown
function hasSpinnerEverShown() {
    return localStorage.getItem('dashboardSpinnerEverShown') === 'true';
}

function setSpinnerEverShown() {
    localStorage.setItem('dashboardSpinnerEverShown', 'true');
}

function showDashboardSpinner(show = true, context = "") {
    const container = document.getElementById('dashboard-loading-spinner-container');
    if (show) {
        dashboardSpinnerCount++;
        container.style.display = 'block';

        // Only on the first ever show (per browser), center spinner and then move to bottom right, never again
        if (!hasSpinnerEverShown()) {
            container.classList.remove('spinner-bottom-right');
            container.classList.add('spinner-center');
            setTimeout(() => {
                container.classList.remove('spinner-center');
                container.classList.add('spinner-bottom-right');
                setSpinnerEverShown();
            }, 1000); // 1 second delay before moving
        } else {
            container.classList.add('spinner-bottom-right');
            container.classList.remove('spinner-center');
        }
    } else {
        dashboardSpinnerCount = Math.max(0, dashboardSpinnerCount - 1);
        if (dashboardSpinnerCount === 0) {
            container.style.display = 'none';
        }
    }
}

// ===================== Text Fit Helpers (global) =====================
var _fitMinFontSize = 8;

// Get the CSS base font size from the table itself (never has inline overrides)
function _fitGetBaseFontSize(el) {
    var table = el.closest('#income-table, #expenses-table, .cas-table, #income-categories-table, #expense-categories-table, .ca-categories-table, #remainder-row-right, #savings-row-right');
    if (table) return parseFloat(window.getComputedStyle(table).fontSize);
    var wrapper = el.closest('#dashboard-wrapper');
    if (wrapper) return parseFloat(window.getComputedStyle(wrapper).fontSize);
    return parseFloat(window.getComputedStyle(el).fontSize);
}

// Measure text width at a given font size
function _fitTextWidth(text, fontSize, fontWeight, fontFamily) {
    var span = document.createElement('span');
    span.style.cssText = 'visibility:hidden;position:absolute;white-space:nowrap;font-size:' + fontSize + 'px;font-weight:' + fontWeight + ';font-family:' + fontFamily;
    span.textContent = text;
    document.body.appendChild(span);
    var w = span.offsetWidth;
    document.body.removeChild(span);
    return w;
}

// Fit a single element: shrink if needed, restore if possible
function fitElement(el, text, availableWidth) {
    if (!text.trim() || availableWidth <= 0) { el.style.fontSize = ''; return; }
    var baseFontSize = _fitGetBaseFontSize(el);
    var style = window.getComputedStyle(el);
    var fw = style.fontWeight, ff = style.fontFamily;

    if (_fitTextWidth(text, baseFontSize, fw, ff) <= availableWidth) {
        el.style.fontSize = '';
        return;
    }
    var fs = baseFontSize;
    while (fs > _fitMinFontSize && _fitTextWidth(text, fs, fw, ff) > availableWidth) {
        fs -= 0.5;
    }
    el.style.fontSize = fs + 'px';
}

// Function to scale font size to fit text within container width
// Fit text to container — shrinks font only when text overflows, restores when space is available
function fitTextToContainer() {
    // Bottom rows / special rows (direct text in td)
    document.querySelectorAll('#income-bottom-row td, #last-remainder-row-right td, #expenses-bottom-row td, .ca-bottom-row td, #remainder-row-right td, #savings-row-right td').forEach(function(td) {
        var s = window.getComputedStyle(td);
        var avail = td.offsetWidth - (parseFloat(s.paddingLeft) || 0) - (parseFloat(s.paddingRight) || 0);
        fitElement(td, td.textContent, avail);
    });

    // Input cells in weekly dashboard tables
    document.querySelectorAll('#income-table td:not(.category-cell) input, #expenses-table td:not(.category-cell) input, .cas-table td:not(.category-cell) input').forEach(function(input) {
        var s = window.getComputedStyle(input);
        var avail = input.offsetWidth - (parseFloat(s.paddingLeft) || 0) - (parseFloat(s.paddingRight) || 0);
        fitElement(input, input.value, avail);
    });

    // Dashboard-d amount cells
    document.querySelectorAll('.dashboard-d-amount-cell').forEach(function(cell) {
        var input = cell.querySelector('input');
        var target = input || cell;
        var text = input ? input.value : cell.textContent;
        var s = window.getComputedStyle(target);
        var avail = target.offsetWidth - (parseFloat(s.paddingLeft) || 0) - (parseFloat(s.paddingRight) || 0);
        fitElement(target, text, avail);
    });

    // Group header sum cells
    document.querySelectorAll('.group-sum-cell').forEach(function(td) {
        var s = window.getComputedStyle(td);
        var avail = td.offsetWidth - (parseFloat(s.paddingLeft) || 0) - (parseFloat(s.paddingRight) || 0);
        fitElement(td, td.textContent, avail);
    });

    // Category name spans in sidebar cells
    document.querySelectorAll('.category-cell .category-name').forEach(function(span) {
        var td = span.closest('.category-cell');
        if (!td) return;
        var s = window.getComputedStyle(td);
        var avail = td.offsetWidth - (parseFloat(s.paddingLeft) || 0) - (parseFloat(s.paddingRight) || 0);
        // Account for icon width
        var icon = span.querySelector('.category-icon');
        if (icon) avail -= icon.offsetWidth + 4;
        // Account for sort handle / lock when in edit mode
        var sortHandle = td.querySelector('.sort-handle, .sort-handle-lock');
        if (sortHandle && sortHandle.offsetWidth > 0) avail -= sortHandle.offsetWidth + 4;
        // Account for edit button when visible
        var editBtn = td.querySelector('.rename-category-btn, .delete-category-btn');
        if (editBtn && editBtn.offsetWidth > 0) avail -= editBtn.offsetWidth + 4;
        // Get just the text node content (excluding icon text)
        var text = '';
        span.childNodes.forEach(function(n) { if (n.nodeType === 3) text += n.textContent; });
        text = text.trim();
        if (text) fitElement(span, text, avail);
    });

    // Group name spans in sidebar group headers
    document.querySelectorAll('.category-cell .group-toggle').forEach(function(span) {
        var td = span.closest('.category-cell');
        if (!td) return;
        var s = window.getComputedStyle(td);
        var avail = td.offsetWidth - (parseFloat(s.paddingLeft) || 0) - (parseFloat(s.paddingRight) || 0);
        // Account for chevron icon
        var chevron = span.querySelector('.group-chevron');
        if (chevron) avail -= chevron.offsetWidth + 6;
        // Account for sort handle when in edit mode
        var sortHandle = td.querySelector('.group-sort-handle, .group-sort-handle-lock');
        if (sortHandle && sortHandle.offsetWidth > 0) avail -= sortHandle.offsetWidth + 4;
        // Get just the text node content
        var text = '';
        span.childNodes.forEach(function(n) { if (n.nodeType === 3) text += n.textContent; });
        text = text.trim();
        if (text) fitElement(span, text, avail);
    });
}

// Debounced fitText handler
let fitTextTimeout;
function fitTextDebounced() {
    clearTimeout(fitTextTimeout);
    fitTextTimeout = setTimeout(fitTextToContainer, 150);
}

// Run fitText on load and resize
document.addEventListener('DOMContentLoaded', function() {
    setTimeout(fitTextToContainer, 100);
    
    // Watch for DOM changes in tables and re-fit text
    const observer = new MutationObserver(fitTextDebounced);
    
    // Weekly dashboard tables
    const tables = document.querySelectorAll('#income-table, #expenses-table, .cas-table');
    tables.forEach(function(table) {
        observer.observe(table, { childList: true, subtree: true, characterData: true });
    });

    // Sidebar category tables
    const sidebarTables = document.querySelectorAll('#income-categories-table, #expense-categories-table, .ca-categories-table');
    sidebarTables.forEach(function(table) {
        observer.observe(table, { childList: true, subtree: true, characterData: true });
    });
    
    // Day dashboard wrapper
    const dashboardWrapper = document.getElementById('dashboard-wrapper');
    if (dashboardWrapper) {
        observer.observe(dashboardWrapper, { childList: true, subtree: true, characterData: true });
    }
});
window.addEventListener('resize', fitTextDebounced);

/* Modal scroll indicator — pulsing arrow when submit button is out of view */
document.addEventListener('DOMContentLoaded', function() {
    var formSelectors = '.income-form-container, .expense-form-container, .ca-form-container, .convert-recurring-form-container';

    document.querySelectorAll(formSelectors).forEach(function(container) {
        var submitBtn = container.querySelector('button[type="submit"]');
        if (!submitBtn) return;

        var indicator = document.createElement('div');
        indicator.className = 'modal-scroll-indicator hidden';
        indicator.innerHTML = '<i class="fa-solid fa-chevron-down"></i>';
        submitBtn.parentNode.insertBefore(indicator, submitBtn);

        function checkSubmitVisible() {
            var cRect = container.getBoundingClientRect();
            // Skip if container has no size (not rendered yet)
            if (cRect.height === 0) return;
            var bRect = submitBtn.getBoundingClientRect();
            // Submit is visible when its top edge is within the container's visible area
            if (bRect.top < cRect.bottom - 10) {
                indicator.classList.add('hidden');
            } else {
                indicator.classList.remove('hidden');
            }
        }

        container.addEventListener('scroll', checkSubmitVisible, { passive: true });
        // Also listen on the modal itself in case it's the scroll container
        var modal = container.closest('.modal');
        if (modal) {
            modal.addEventListener('scroll', checkSubmitVisible, { passive: true });
        }

        // Detect when the parent .modal is shown
        if (modal) {
            var obs = new MutationObserver(function() {
                if (modal.style.display === 'flex' || modal.style.display === 'block') {
                    // Poll until container has layout, then check
                    var attempts = 0;
                    var poll = setInterval(function() {
                        attempts++;
                        checkSubmitVisible();
                        if (container.getBoundingClientRect().height > 0 || attempts > 10) {
                            clearInterval(poll);
                        }
                    }, 50);
                }
            });
            obs.observe(modal, { attributes: true, attributeFilter: ['style'] });
        }

        // Also check on window resize (keyboard open/close on mobile)
        window.addEventListener('resize', checkSubmitVisible);

        // Watch for DOM changes inside the form (e.g. cadence change showing/hiding fields)
        var contentObs = new MutationObserver(function() {
            // Small delay to let layout settle after DOM change
            setTimeout(checkSubmitVisible, 30);
        });
        contentObs.observe(container, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'] });

        // Also listen for input/select changes that may toggle field visibility
        container.addEventListener('change', function() { setTimeout(checkSubmitVisible, 30); });
    });
});

// ═══════════════════════════════════════════════════════════════
// CROSS-TAB DATA SYNC (poll for changes made in other tabs/browsers)
// ═══════════════════════════════════════════════════════════════
// DISABLED: cross-browser sync is currently broken — skip entirely
(function() {
    return;

    // Restore scroll position after auto-refresh
    var savedScroll = sessionStorage.getItem('_scrollY');
    if (savedScroll !== null) {
        sessionStorage.removeItem('_scrollY');
        window.addEventListener('load', function() {
            window.scrollTo(0, parseInt(savedScroll, 10));
        });
    }

    function pollDataVersion() {
        fetch('/api/data-version', { credentials: 'same-origin' })
            .then(function(r) {
                if (r.status === 401 || r.status === 302) {
                    // Not logged in; stop polling
                    clearInterval(_pollTimer);
                    return null;
                }
                return r.json();
            })
            .then(function(data) {
                if (!data) return;
                var v = data.version;
                if (_knownVersion === null) {
                    // First fetch — just record the baseline
                    _knownVersion = v;
                    return;
                }
                if (v !== _knownVersion && v !== '0') {
                    if (window._disableDataVersionReload) {
                        // Page opted out of auto-reload (e.g. setup_profile)
                        _knownVersion = v;
                        return;
                    }
                    // Save scroll position before reload
                    sessionStorage.setItem('_scrollY', window.scrollY);
                    location.reload();
                }
            })
            .catch(function() {
                // Silently ignore network errors
            });
    }

    function showDataChangedToast() {
        // Create a persistent toast with a refresh button
        var toast = document.createElement('div');
        toast.className = 'data-changed-toast';
        toast.innerHTML =
            '<i class="fa-solid fa-arrows-rotate"></i> ' +
            '<span>Data updated in another session.</span> ' +
            '<button onclick="location.reload()">Refresh</button>' +
            '<button class="data-changed-dismiss" title="Dismiss">&times;</button>';
        document.body.appendChild(toast);

        // Animate in
        requestAnimationFrame(function() {
            toast.classList.add('visible');
        });

        // Dismiss button
        toast.querySelector('.data-changed-dismiss').addEventListener('click', function() {
            toast.classList.remove('visible');
            setTimeout(function() { toast.remove(); }, 300);
            _toastShowing = false;
            // Update known version so we don't show again until next change
            fetch('/api/data-version', { credentials: 'same-origin' })
                .then(function(r) { return r.json(); })
                .then(function(data) { if (data) _knownVersion = data.version; })
                .catch(function() {});
        });
    }

    // Reset known version on every successful AJAX mutation (same tab)
    // so polling doesn't trigger for your own changes
    if (typeof $ !== 'undefined') {
        $(document).ajaxSuccess(function(event, xhr, settings) {
            if (settings.type && settings.type !== 'GET') {
                // Bump known version after a short delay to let the server set it
                setTimeout(function() {
                    fetch('/api/data-version', { credentials: 'same-origin' })
                        .then(function(r) { return r.json(); })
                        .then(function(data) { if (data) _knownVersion = data.version; })
                        .catch(function() {});
                }, 500);
            }
        });
    }

    // Re-check immediately when user focuses this tab
    document.addEventListener('visibilitychange', function() {
        if (!document.hidden && _knownVersion !== null) {
            pollDataVersion();
        }
    });

    // Start polling after page loads
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() {
            _pollTimer = setInterval(pollDataVersion, DATA_POLL_INTERVAL);
            // Initial baseline fetch
            pollDataVersion();
        });
    } else {
        _pollTimer = setInterval(pollDataVersion, DATA_POLL_INTERVAL);
        pollDataVersion();
    }
})();

// ===== Recurring Mismatch Detection =====

/**
 * Fetch and display mismatch badges on a recurring page.
 * Call this from the recurring page's DOMContentLoaded handler.
 * @param {string} recurringTable - 'recurring_income', 'recurring_expense', or 'recurring_c_expense'
 */
function initRecurringMismatchBadges(recurringTable) {
    fetch('/api/recurring-mismatches?recurring_table=' + encodeURIComponent(recurringTable))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.status !== 'success' || !data.mismatches || !data.mismatches.length) return;
            data.mismatches.forEach(function(m) {
                var row = document.querySelector('tr[data-recurring-id="' + m.recurring_id + '"]');
                if (!row) return;
                var nameCell = row.querySelector('td[data-column="category_name"]');
                if (!nameCell) return;
                // Don't add duplicate badge
                if (nameCell.querySelector('.recurring-mismatch-badge')) return;
                // Wrap the text node in an anchor span so badge centers on the word
                var anchor = nameCell.querySelector('.mismatch-badge-anchor');
                if (!anchor) {
                    anchor = document.createElement('span');
                    anchor.className = 'mismatch-badge-anchor';
                    // Move the first text node into the anchor
                    var textNode = null;
                    for (var i = 0; i < nameCell.childNodes.length; i++) {
                        if (nameCell.childNodes[i].nodeType === 3 && nameCell.childNodes[i].textContent.trim()) {
                            textNode = nameCell.childNodes[i];
                            break;
                        }
                    }
                    if (textNode) {
                        nameCell.insertBefore(anchor, textNode);
                        anchor.appendChild(textNode);
                    } else {
                        nameCell.insertBefore(anchor, nameCell.firstChild);
                    }
                }
                var badge = document.createElement('span');
                badge.className = 'recurring-mismatch-badge';
                var inner = document.createElement('span');
                inner.className = 'recurring-mismatch-badge-inner';
                inner.innerHTML = '<i class="fa-solid fa-exclamation"></i>';
                badge.appendChild(inner);
                badge.setAttribute('data-mismatch-id', m.id);
                badge.setAttribute('title', 'Detected change');
                anchor.appendChild(badge);
                badge.addEventListener('click', function(e) {
                    e.stopPropagation();
                    showMismatchModal(m, recurringTable);
                });
            });
        })
        .catch(function(err) { console.error('Mismatch fetch error:', err); });
}

/**
 * Show the mismatch comparison modal.
 * @param {Object} m - Enriched mismatch object from API
 * @param {string} recurringTable - 'recurring_income', 'recurring_expense', or 'recurring_c_expense'
 */
function showMismatchModal(m, recurringTable) {
    // Remove existing modal if any
    var existing = document.getElementById('mismatch-modal');
    if (existing) existing.remove();

    var typeLabel = recurringTable === 'recurring_income' ? 'Income' : 'Bill/Wage';
    var currencySymbol = (typeof window.currencySymbol !== 'undefined') ? window.currencySymbol : '$';

    // Build comparison rows (only show rows that differ)
    var rows = '';
    var amountDiff = Math.abs(m.detected_amount - m.current_amount);
    var threshold = Math.max(1.0, m.current_amount * 0.02);
    if (amountDiff > threshold) {
        rows += '<tr><td class="mismatch-label">Amount</td>' +
            '<td class="mismatch-current">' + currencySymbol + m.current_amount.toFixed(2) + '</td>' +
            '<td class="mismatch-detected">' + currencySymbol + m.detected_amount.toFixed(2) + '</td></tr>';
    }
    if (m.detected_cadence && m.current_cadence && m.detected_cadence !== m.current_cadence) {
        rows += '<tr><td class="mismatch-label">Frequency</td>' +
            '<td class="mismatch-current">' + _escHtml(m.current_cadence) + '</td>' +
            '<td class="mismatch-detected">' + _escHtml(m.detected_cadence) + '</td></tr>';
    }

    var paymentInfo = '';
    if (m.enrichment_last_payment_date) {
        paymentInfo = '<p class="mismatch-payment-info">Last detected payment: ' + _escHtml(m.enrichment_last_payment_date) + '</p>';
    }

    var modal = document.createElement('div');
    modal.id = 'mismatch-modal';
    modal.className = 'modal';
    modal.innerHTML =
        '<div class="modal-content center-modal mismatch-modal-content">' +
            '<span class="close-modal mismatch-close">&times;</span>' +
            '<h2>' + typeLabel + ' Update Detected</h2>' +
            '<p class="mismatch-desc">Blankee detected that your <strong>' + _escHtml(m.category_name) + '</strong> may have changed based on recent bank transactions.</p>' +
            '<table class="mismatch-table">' +
                '<thead><tr><th></th><th>Current</th><th>Detected</th></tr></thead>' +
                '<tbody>' + rows + '</tbody>' +
            '</table>' +
            paymentInfo +
            '<p class="mismatch-warning">Updating will change your projected future amounts for this category.</p>' +
            '<div class="modal-buttons">' +
                '<button class="mismatch-update-btn" id="mismatch-update-btn">Update</button>' +
                '<button class="mismatch-dismiss-btn" id="mismatch-dismiss-btn">Dismiss</button>' +
            '</div>' +
        '</div>';

    document.body.appendChild(modal);
    modal.classList.add('modal--open');

    var closeBtn = modal.querySelector('.mismatch-close');
    var updateBtn = document.getElementById('mismatch-update-btn');
    var dismissBtn = document.getElementById('mismatch-dismiss-btn');

    function closeModal() {
        modal.classList.remove('modal--open');
        modal.remove();
    }

    closeBtn.addEventListener('click', closeModal);
    modal.addEventListener('click', function(e) { if (e.target === modal) closeModal(); });

    // Update button — call existing edit recurring endpoint, then dismiss
    updateBtn.addEventListener('click', function() {
        updateBtn.disabled = true;
        updateBtn.textContent = 'Updating...';

        // Build the update payload with detected values
        var editUrl = {
            'recurring_income': '/update-recurring-income',
            'recurring_expense': '/update-recurring-expense',
            'recurring_c_expense': '/update-recurring-ca-expense'
        }[recurringTable];

        var payload = { recurring_id: m.recurring_id };
        // Always include all required fields from current values
        payload.category_name = m.category_name || '';
        payload.amount = (amountDiff > threshold) ? m.detected_amount : m.current_amount;
        payload.cadence_interval = (m.detected_cadence_interval && m.detected_cadence_unit && m.detected_cadence !== m.current_cadence) ? m.detected_cadence_interval : m.current_cadence_interval;
        payload.cadence_unit = (m.detected_cadence_interval && m.detected_cadence_unit && m.detected_cadence !== m.current_cadence) ? m.detected_cadence_unit : m.current_cadence_unit;
        payload.start_date = new Date().toISOString().slice(0, 10);
        payload.end_date = m.current_end_date || new Date().toISOString().slice(0, 10);
        payload.no_end_date = m.current_no_end_date || 0;
        payload.wage_bill = 1;
        // Weekdays / monthly_days
        if (m.detected_cadence_interval && m.detected_cadence_unit && m.detected_cadence !== m.current_cadence) {
            if (m.detected_cadence_unit === 'weeks' && m.detected_weekday) {
                payload.weekdays = [m.detected_weekday];
            } else if (m.current_weekdays) {
                payload.weekdays = m.current_weekdays.split(',');
            }
            if (m.detected_cadence_unit === 'months' && m.detected_day_of_month) {
                payload.monthly_days = [String(m.detected_day_of_month)];
            } else if (m.current_monthly_days) {
                payload.monthly_days = m.current_monthly_days.split(',');
            }
        } else {
            if (m.current_weekdays) payload.weekdays = m.current_weekdays.split(',');
            if (m.current_monthly_days) payload.monthly_days = m.current_monthly_days.split(',');
        }

        fetch(editUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(function(r) { return r.json(); })
        .then(function(result) {
            if (result.status === 'success' || result.success) {
                // Dismiss mismatch
                return fetch('/api/recurring-mismatches/' + m.id + '/dismiss', { method: 'POST' })
                    .then(function() {
                        _removeBadge(m.recurring_id);
                        _updateRowValues(m, recurringTable);
                        showToast('Recurring entry updated!', 'success');
                        closeModal();
                    });
            } else {
                showToast(result.message || 'Failed to update recurring entry.', 'error');
                updateBtn.disabled = false;
                updateBtn.textContent = 'Update';
            }
        })
        .catch(function(err) {
            showToast('Error updating entry.', 'error');
            updateBtn.disabled = false;
            updateBtn.textContent = 'Update';
        });
    });

    // Dismiss button
    dismissBtn.addEventListener('click', function() {
        dismissBtn.disabled = true;
        fetch('/api/recurring-mismatches/' + m.id + '/dismiss', { method: 'POST' })
            .then(function() {
                _removeBadge(m.recurring_id);
                showToast('Dismissed', 'info');
                closeModal();
            })
            .catch(function() {
                showToast('Error dismissing.', 'error');
                dismissBtn.disabled = false;
            });
    });
}

function _removeBadge(recurringId) {
    var row = document.querySelector('tr[data-recurring-id="' + recurringId + '"]');
    if (row) {
        var badge = row.querySelector('.recurring-mismatch-badge');
        if (badge) badge.remove();
    }
}

function _updateRowValues(m, recurringTable) {
    var row = document.querySelector('tr[data-recurring-id="' + m.recurring_id + '"]');
    if (!row) return;
    // Update amount cell
    var amountCell = row.querySelector('td[data-column="amount"]');
    if (amountCell && m.detected_amount) {
        var sym = (typeof window.currencySymbol !== 'undefined') ? window.currencySymbol : '$';
        amountCell.textContent = sym + m.detected_amount.toFixed(2);
    }
}

function _escHtml(str) {
    if (!str) return '';
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}


// ─── Recurring Suggestions (Suggested Recurring Categories) ──────────────────

/**
 * Build a human-readable cadence string from raw fields (for confirm modal text).
 */
function _formatSuggestionCadence(interval, unit, weekday, monthlyDay) {
    if (!interval || !unit) return '';
    var unitSingular = unit.replace(/s$/, '');
    var base = (interval == 1) ? 'Every ' + unitSingular : 'Every ' + interval + ' ' + unit;
    if (unitSingular === 'week' && weekday) {
        return base + ' on ' + weekday.charAt(0).toUpperCase() + weekday.slice(1) + 's';
    }
    if (unitSingular === 'month' && monthlyDay) {
        var suffix = _ordinalSuffix(monthlyDay);
        return base + ' on the ' + monthlyDay + suffix;
    }
    return base;
}

function _ordinalSuffix(n) {
    n = parseInt(n, 10);
    if (n >= 11 && n <= 13) return 'th';
    switch (n % 10) {
        case 1: return 'st';
        case 2: return 'nd';
        case 3: return 'rd';
        default: return 'th';
    }
}

/**
 * Build the cadence icon class based on unit (matches recurring table icons).
 */
function _cadenceIcon(unit) {
    var u = (unit || '').replace(/s$/, '');
    if (u === 'year') return 'fa-solid fa-calendars';
    if (u === 'month') return 'fa-solid fa-calendar-days';
    if (u === 'week') return 'fa-solid fa-calendar-week';
    return 'fa-solid fa-calendar-day';
}

/**
 * Build the cadence detail string (day/weekday line under the icon).
 */
function _cadenceDetailStr(unit, weekday, monthlyDay) {
    var u = (unit || '').replace(/s$/, '');
    if (u === 'month' && monthlyDay) return monthlyDay + '<sup>' + _ordinalSuffix(monthlyDay) + '</sup>';
    if (u === 'week' && weekday) {
        var map = {monday:'Mo',tuesday:'Tu',wednesday:'We',thursday:'Th',friday:'Fr',saturday:'Sa',sunday:'Su'};
        return map[weekday.toLowerCase()] || weekday;
    }
    return '';
}

/**
 * Fetch and render the suggested recurring categories as notification-style bubbles.
 * @param {string} suggestionType - 'recurring_income', 'recurring_expense', or 'recurring_c_expense'
 */
function initRecurringSuggestions(suggestionType) {
    fetch('/api/recurring-suggestions?suggestion_type=' + encodeURIComponent(suggestionType))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.status !== 'success' || !data.suggestions || !data.suggestions.length) return;

            var section = document.getElementById('suggested-recurring-section');
            var list = document.getElementById('suggested-recurring-list');
            if (!section || !list) return;

            var currSym = (typeof window.currencySymbol !== 'undefined') ? window.currencySymbol : '$';

            var addUrlMap = {
                'recurring_income': '/add-recurring-income',
                'recurring_expense': '/add-recurring-expense',
                'recurring_c_expense': '/add-recurring-ca-expense'
            };

            data.suggestions.forEach(function(s) {
                var cadenceStr = _formatSuggestionCadence(
                    s.detected_cadence_interval, s.detected_cadence_unit,
                    s.detected_weekday, s.detected_monthly_day
                );
                var detail = _cadenceDetailStr(s.detected_cadence_unit, s.detected_weekday, s.detected_monthly_day);

                var bubble = document.createElement('div');
                bubble.className = 'suggestion-bubble';
                bubble.setAttribute('data-suggestion-id', s.id);
                bubble.innerHTML =
                    '<div class="suggestion-bubble-info">' +
                        '<span class="suggestion-category">' + _escHtml(s.category_name) + '</span>' +
                        '<span class="suggestion-mobile-type">Bill</span>' +
                    '</div>' +
                    '<div class="suggestion-type">Bill</div>' +
                    '<div class="suggestion-amount">' + currSym + parseFloat(s.detected_amount).toFixed(2) + '</div>' +
                    '<div class="suggestion-bubble-cadence">' +
                        '<span class="cadence-top">' + s.detected_cadence_interval + ' <i class="' + _cadenceIcon(s.detected_cadence_unit) + '"></i></span>' +
                        (detail ? '<span class="cadence-detail">' + detail + '</span>' : '') +
                    '</div>' +
                    '<div class="suggestion-bubble-actions">' +
                        '<button class="suggestion-action-btn suggestion-accept-btn" title="Create recurring entry"><i class="fa-solid fa-check"></i></button>' +
                        '<button class="suggestion-action-btn suggestion-dismiss-btn" title="Dismiss suggestion"><i class="fa-solid fa-xmark"></i></button>' +
                    '</div>';

                bubble.querySelector('.suggestion-accept-btn').addEventListener('click', function() {
                    _handleSuggestionAccept(s, suggestionType, addUrlMap[suggestionType], currSym, cadenceStr);
                });
                bubble.querySelector('.suggestion-dismiss-btn').addEventListener('click', function() {
                    _handleSuggestionDismiss(s.id);
                });

                list.appendChild(bubble);
            });

            section.style.display = '';
        })
        .catch(function(err) { console.error('Suggestions fetch error:', err); });
}

/**
 * Handle accepting a recurring suggestion: confirm modal → add recurring → dismiss suggestion.
 */
function _handleSuggestionAccept(s, suggestionType, addUrl, currSym, cadenceStr) {
    var msg = 'Create a recurring <strong>' + _escHtml(s.category_name) + '</strong> entry for <strong>' +
        currSym + parseFloat(s.detected_amount).toFixed(2) + '</strong> ' + _escHtml(cadenceStr) + '?';

    showConfirmModal({
        title: 'Add Recurring Entry',
        message: msg,
        confirmText: 'Create',
        cancelText: 'Cancel',
        danger: false
    }).then(function(confirmed) {
        if (!confirmed) return;

        // Build payload
        var today = new Date().toISOString().slice(0, 10);
        var threeYears = new Date();
        threeYears.setFullYear(threeYears.getFullYear() + 3);
        var endDate = threeYears.toISOString().slice(0, 10);

        var payload = {
            category_id: s.category_id,
            amount: s.detected_amount,
            cadence_interval: s.detected_cadence_interval,
            cadence_unit: s.detected_cadence_unit,
            start_date: today,
            end_date: endDate,
            no_end_date: 1,
            wage_bill: 1
        };

        if (s.detected_cadence_unit === 'weeks' && s.detected_weekday) {
            payload.weekdays = [s.detected_weekday];
        }
        if (s.detected_cadence_unit === 'months' && s.detected_monthly_day) {
            payload.monthly_days = [String(s.detected_monthly_day)];
        }
        if (suggestionType === 'recurring_c_expense' && s.account_id) {
            payload.account_id = s.account_id;
        }

        fetch(addUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(function(r) { return r.json(); })
        .then(function(result) {
            if (result.status === 'success') {
                // Dismiss the suggestion
                return fetch('/api/recurring-suggestions/' + s.id + '/dismiss', { method: 'POST' })
                    .then(function() {
                        showToast('Recurring entry created!', 'success');
                        // Reload to show new entry in main table
                        setTimeout(function() { location.reload(); }, 600);
                    });
            } else {
                showToast(result.message || 'Failed to create recurring entry.', 'error');
            }
        })
        .catch(function(err) {
            showToast('Error creating entry.', 'error');
        });
    });
}

/**
 * Handle dismissing a recurring suggestion.
 */
function _handleSuggestionDismiss(suggestionId) {
    fetch('/api/recurring-suggestions/' + suggestionId + '/dismiss', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.status === 'success') {
                var bubble = document.querySelector('.suggestion-bubble[data-suggestion-id="' + suggestionId + '"]');
                if (bubble) bubble.remove();
                showToast('Suggestion dismissed', 'info');
                var list = document.getElementById('suggested-recurring-list');
                if (list && list.children.length === 0) {
                    var section = document.getElementById('suggested-recurring-section');
                    if (section) section.style.display = 'none';
                }
            }
        })
        .catch(function(err) {
            showToast('Error dismissing suggestion.', 'error');
        });
}

// ===== Styled Select Dropdown =====
// Gives a native <select> the same look as the add-entry fields on the daily
// dashboard: a compact trigger plus a real .category-dropdown list, so the open
// list matches (a native select's option list is drawn by the OS and cannot be
// styled at all, which is why the markup has to be replaced rather than themed).
//
// The <select> stays in the DOM and remains the source of truth. Choosing from
// the custom list sets select.value and dispatches a `change` event, so whatever
// handlers the page already assigned - including .onchange - keep working and no
// existing logic needs to know this enhancement exists.
//
// Idempotent: call it again after the select is repopulated and it just refreshes
// the label rather than building a second trigger.
function enhanceSelectAsDropdown(select) {
    if (typeof select === 'string') select = document.getElementById(select);
    if (!select) return;

    let wrap = select.closest('.styled-dropdown');
    let trigger, list;

    function syncLabel() {
        const opt = select.options[select.selectedIndex];
        // Fall back to the raw value so the trigger is never blank - a blank
        // one collapses to a caret-width sliver that cannot be clicked.
        trigger.textContent = opt ? opt.textContent : (select.value || '...');
    }

    function buildList() {
        list.innerHTML = '';
        Array.from(select.options).forEach(function (opt) {
            const item = document.createElement('div');
            item.className = 'category-dropdown-item';
            if (opt.selected) item.classList.add('is-selected');
            item.textContent = opt.textContent;
            item.addEventListener('click', function (e) {
                e.stopPropagation();
                select.value = opt.value;
                syncLabel();
                wrap.classList.remove('is-open');
                // Let the page's own handler react exactly as it would to a
                // real user interaction with the native control.
                select.dispatchEvent(new Event('change', { bubbles: true }));
            });
            list.appendChild(item);
        });
    }

    if (wrap) {
        trigger = wrap.querySelector('.styled-dropdown-trigger');
        list = wrap.querySelector('.styled-dropdown-list');
        syncLabel();
        return;
    }

    wrap = document.createElement('div');
    wrap.className = 'styled-dropdown';
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    select.classList.add('styled-dropdown-native');

    trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'styled-dropdown-trigger';
    wrap.appendChild(trigger);

    list = document.createElement('div');
    list.className = 'category-dropdown styled-dropdown-list';
    wrap.appendChild(list);

    trigger.addEventListener('click', function (e) {
        e.stopPropagation();
        const wasOpen = wrap.classList.contains('is-open');
        // Only one of these open at a time.
        document.querySelectorAll('.styled-dropdown.is-open').forEach(function (w) {
            w.classList.remove('is-open');
        });
        if (!wasOpen) {
            // Re-read the options every time rather than trusting a cached
            // render: these selects are repopulated on every view change.
            syncLabel();
            buildList();
            wrap.classList.add('is-open');
        }
    });

    trigger.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') wrap.classList.remove('is-open');
    });

    document.addEventListener('click', function (e) {
        if (!wrap.contains(e.target)) wrap.classList.remove('is-open');
    });

    // Keep the label correct if the value is changed from elsewhere.
    select.addEventListener('change', syncLabel);

    // ...and when the options themselves are replaced. The dashboards rebuild
    // these lists on every view change, and the order in which that happens
    // relative to this enhancement is not guaranteed - without this the label
    // can be left empty, which renders as an unclickable sliver.
    if (typeof MutationObserver === 'function') {
        new MutationObserver(syncLabel).observe(select, { childList: true });
    }

    syncLabel();
}
// Safety net. The month/year pickers are enhanced from each dashboard's own
// populate function, but on the weekly and 3-month views that function only
// runs when updateView() is passed a section list containing "monthYearHeader",
// so a code path that updates other sections would leave the picker native.
// Enhancing again here on load is idempotent and makes the result independent
// of which path ran first.
document.addEventListener('DOMContentLoaded', function () {
    ['month-dropdown',
     'year-dropdown',
     'dashboard-m-month-dropdown',
     'dashboard-m-year-dropdown'].forEach(function (id) {
        if (document.getElementById(id)) enhanceSelectAsDropdown(id);
    });
});

// ===== End Styled Select Dropdown =====
