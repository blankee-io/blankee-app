/**
 * Redis Hydration Refresh Handler
 * 
 * This script periodically checks if the user's data has been hydrated to Redis
 * while they have the page open. If hydration occurs, it automatically refreshes
 * the page so the user sees the latest data.
 * 
 * How it works:
 * 1. On page load, start polling the /api/check_refresh endpoint
 * 2. If the server indicates refresh is needed, reload the page
 * 3. Stop polling after the page is refreshed
 */

(function() {
    'use strict';
    
    // Configuration
    const CHECK_INTERVAL = 5000; // Check every 5 seconds
    const MAX_CHECKS = 60; // Stop after 5 minutes (60 * 5s = 300s)
    
    let checkCount = 0;
    let pollInterval = null;
    
    /**
     * Check if a refresh is needed by calling the backend API
     */
    function checkRefreshSignal() {
        // Don't check if we've exceeded max checks
        if (checkCount >= MAX_CHECKS) {
            stopPolling();
            return;
        }
        
        checkCount++;
        
        fetch('/api/check_refresh', {
            method: 'GET',
            credentials: 'same-origin',
            headers: {
                'X-Requested-With': 'XMLHttpRequest'
            }
        })
        .then(response => {
            if (!response.ok) {
                throw new Error('Network response was not ok');
            }
            return response.json();
        })
        .then(data => {
            if (data.refresh === true) {
                // Stop polling
                stopPolling();
                
                // Show a brief notification (optional)
                showRefreshNotification();
                
                // Reload the page after a short delay
                setTimeout(() => {
                    window.location.reload();
                }, 1000);
            }
        })
        .catch(error => {
            console.error('[Redis Refresh] Error checking refresh signal:', error);
            // Don't stop polling on error - might be temporary network issue
        });
    }
    
    /**
     * Show a notification to the user that the page is refreshing
     */
    function showRefreshNotification() {
        // Create a simple notification element
        const notification = document.createElement('div');
        notification.id = 'redis-refresh-notification';
        notification.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            background-color: #4CAF50;
            color: white;
            padding: 15px 20px;
            border-radius: 4px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.2);
            z-index: 10000;
            font-family: Arial, sans-serif;
            font-size: 14px;
        `;
        notification.textContent = 'Refreshing with latest data...';
        
        document.body.appendChild(notification);
    }
    
    /**
     * Start polling for refresh signals
     */
    function startPolling() {
        // Check immediately
        checkRefreshSignal();
        
        // Then check at regular intervals
        pollInterval = setInterval(checkRefreshSignal, CHECK_INTERVAL);
    }
    
    /**
     * Stop polling for refresh signals
     */
    function stopPolling() {
        if (pollInterval) {
            clearInterval(pollInterval);
            pollInterval = null;
        }
    }
    
    /**
     * Initialize the refresh handler when DOM is ready
     */
    function init() {
        // Only start polling if user is authenticated
        // You can check this by looking for user-specific elements in the DOM
        const isAuthenticated = document.body.classList.contains('authenticated') ||
                                document.querySelector('[data-user-authenticated]') !== null ||
                                window.location.pathname !== '/login';
        
        if (isAuthenticated) {
            // Start polling after page is fully loaded
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', startPolling);
            } else {
                // DOM already loaded
                startPolling();
            }
            
            // Stop polling when user leaves the page
            window.addEventListener('beforeunload', stopPolling);
        }
    }
    
    // Initialize
    init();
    
    // Export for manual control if needed
    window.RedisRefreshHandler = {
        start: startPolling,
        stop: stopPolling,
        checkNow: checkRefreshSignal
    };
})();
