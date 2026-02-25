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
 */
function showConfirmModal(opts) {
    return new Promise(function(resolve) {
        // Checkbox with storageKey: auto-confirm if user previously opted out
        if (opts.checkbox && opts.checkbox.storageKey) {
            if (localStorage.getItem(opts.checkbox.storageKey) === '1') {
                resolve(true);
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

        modal.style.display = 'flex';

        function cleanup(result) {
            modal.style.display = 'none';
            confirmBtn.removeEventListener('click', onConfirm);
            cancelBtn.removeEventListener('click', onCancel);
            closeBtn.removeEventListener('click', onCancel);
            modal.removeEventListener('click', onBackdrop);
            document.removeEventListener('keydown', onKeydown);
            resolve(result);
        }
        function onConfirm() {
            // If checkbox is shown and checked, persist the preference
            if (opts.checkbox && opts.checkbox.storageKey && checkboxEl.checked) {
                localStorage.setItem(opts.checkbox.storageKey, '1');
            }
            cleanup(true);
        }
        function onCancel()  { cleanup(false); }
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

// ===== Quiltt Reconnection Alert =====
// Check for bank connections needing reconnection on page load
(function() {
    // Only run on pages that have the reconnect modal
    if (!document.getElementById('quiltt-reconnect-modal')) return;
    
    // Check localStorage to see if user already saw this today
    const dismissedKey = 'quiltt_reconnect_dismissed_date';
    const dismissedDate = localStorage.getItem(dismissedKey);
    const today = new Date().toDateString();
    if (dismissedDate === today) {
        return; // Already shown today
    }
    
    // Check for connections needing reconnection
    fetch('/api/check-quiltt-reconnect')
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
window._quilttReconnectData = null;

function showReconnectModal(connections) {
    window._quilttReconnectData = connections;
    
    const modal = document.getElementById('quiltt-reconnect-modal');
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
    modal.style.display = 'flex';
}

function dismissReconnectModal() {
    const modal = document.getElementById('quiltt-reconnect-modal');
    if (modal) {
        modal.style.display = 'none';
    }
    // Already marked as shown for today when modal appeared
}

function goToReconnect() {
    if (window._quilttReconnectData && window._quilttReconnectData.length > 0) {
        // Go to profile page with reconnect parameter for first connection
        const connectionId = window._quilttReconnectData[0].connection_id;
        window.location.href = '/bank_accounts?reconnect=' + encodeURIComponent(connectionId);
    } else {
        window.location.href = '/profile';
    }
}

// ===== End Quiltt Reconnection Alert =====

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

// Function to scale font size to fit text within container width
// Fit text to container — shrinks font only when text overflows, restores when space is available
function fitTextToContainer() {
    var minFontSize = 8;

    // Get the CSS base font size from the table itself (never has inline overrides)
    function getBaseFontSize(el) {
        var table = el.closest('#income-table, #expenses-table, .cas-table, #remainder-row-right, #savings-row-right');
        if (table) return parseFloat(window.getComputedStyle(table).fontSize);
        // For dashboard-d cells, read from parent container
        var wrapper = el.closest('#dashboard-wrapper');
        if (wrapper) return parseFloat(window.getComputedStyle(wrapper).fontSize);
        return parseFloat(window.getComputedStyle(el).fontSize);
    }

    // Measure text width at a given font size
    function textWidth(text, fontSize, fontWeight, fontFamily) {
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
        var baseFontSize = getBaseFontSize(el);
        var style = window.getComputedStyle(el);
        var fw = style.fontWeight, ff = style.fontFamily;

        // Check if text fits at the base (CSS) font size
        if (textWidth(text, baseFontSize, fw, ff) <= availableWidth) {
            el.style.fontSize = ''; // fits — remove any inline override
            return;
        }
        // Shrink until it fits
        var fs = baseFontSize;
        while (fs > minFontSize && textWidth(text, fs, fw, ff) > availableWidth) {
            fs -= 0.5;
        }
        el.style.fontSize = fs + 'px';
    }

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

(function() {
    var DATA_POLL_INTERVAL = 3000; // 3 seconds
    var _knownVersion = null;
    var _pollTimer = null;
    var _toastShowing = false;

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
