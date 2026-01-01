// General Functions

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
function deleteUser(url) {
    if (confirm('Are you sure you want to delete your account?')) {
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
function fitTextToContainer() {
    // For bottom rows and special rows (direct text in td)
    const tdSelector = '#income-bottom-row td, #last-remainder-row-right td, #expenses-bottom-row td, .ca-bottom-row td, #remainder-row-right td, #savings-row-right td';
    document.querySelectorAll(tdSelector).forEach(function(td) {
        if (!td.textContent.trim()) return;
        
        const text = td.textContent;
        const span = document.createElement('span');
        span.style.visibility = 'hidden';
        span.style.position = 'absolute';
        span.style.whiteSpace = 'nowrap';
        span.textContent = text;
        document.body.appendChild(span);
        
        td.style.fontSize = '';
        let fontSize = parseFloat(window.getComputedStyle(td).fontSize);
        const minFontSize = 5;
        
        const style = window.getComputedStyle(td);
        const paddingLeft = parseFloat(style.paddingLeft) || 0;
        const paddingRight = parseFloat(style.paddingRight) || 0;
        const availableWidth = td.offsetWidth - paddingLeft - paddingRight;
        
        span.style.fontSize = fontSize + 'px';
        span.style.fontWeight = style.fontWeight;
        span.style.fontFamily = style.fontFamily;
        
        while (span.offsetWidth > availableWidth && fontSize > minFontSize) {
            fontSize -= 0.5;
            span.style.fontSize = fontSize + 'px';
        }
        
        td.style.fontSize = fontSize + 'px';
        document.body.removeChild(span);
    });
    
    // For table cells with inputs inside (income/expense/ca tables)
    const inputSelector = '#income-table td:not(.category-cell) input, #expenses-table td:not(.category-cell) input, .cas-table td:not(.category-cell) input';
    document.querySelectorAll(inputSelector).forEach(function(input) {
        const text = input.value;
        if (!text.trim()) return;
        
        const td = input.closest('td');
        if (!td) return;
        
        const span = document.createElement('span');
        span.style.visibility = 'hidden';
        span.style.position = 'absolute';
        span.style.whiteSpace = 'nowrap';
        span.textContent = text;
        document.body.appendChild(span);
        
        input.style.fontSize = '';
        let fontSize = parseFloat(window.getComputedStyle(input).fontSize);
        const minFontSize = 5;
        
        const style = window.getComputedStyle(input);
        const inputPaddingLeft = parseFloat(style.paddingLeft) || 0;
        const inputPaddingRight = parseFloat(style.paddingRight) || 0;
        const availableWidth = input.offsetWidth - inputPaddingLeft - inputPaddingRight;
        
        span.style.fontSize = fontSize + 'px';
        span.style.fontWeight = style.fontWeight;
        span.style.fontFamily = style.fontFamily;
        
        while (span.offsetWidth > availableWidth && fontSize > minFontSize) {
            fontSize -= 0.5;
            span.style.fontSize = fontSize + 'px';
        }
        
        input.style.fontSize = fontSize + 'px';
        document.body.removeChild(span);
    });
}

// Debounced fitText handler
let fitTextTimeout;
function fitTextDebounced() {
    clearTimeout(fitTextTimeout);
    fitTextTimeout = setTimeout(fitTextToContainer, 50);
}

// Run fitText on load and resize
document.addEventListener('DOMContentLoaded', function() {
    setTimeout(fitTextToContainer, 100);
    
    // Watch for DOM changes in tables and re-fit text
    const observer = new MutationObserver(fitTextDebounced);
    const tables = document.querySelectorAll('#income-table, #expenses-table, .cas-table');
    tables.forEach(function(table) {
        observer.observe(table, { childList: true, subtree: true, characterData: true });
    });
});
window.addEventListener('resize', fitTextDebounced);
