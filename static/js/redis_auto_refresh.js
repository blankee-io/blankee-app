/**
 * Redis Auto-Refresh Handler
 * 
 * This script polls the server to check if Redis hydration has completed
 * and automatically refreshes the page when new data is available.
 * 
 * Usage:
 * Include this script in pages that should auto-refresh after hydration:
 * <script src="{{ url_for('static', filename='js/redis_auto_refresh.js') }}"></script>
 */

(function() {
    'use strict';
    
    // Configuration
    const POLL_INTERVAL = 3000; // Check every 3 seconds
    const MAX_POLLS = 40; // Stop polling after 2 minutes (40 * 3s = 120s)
    
    let pollCount = 0;
    let pollTimer = null;
    let isHydrated = false;
    let pageLoadedWhileHydrating = false;
    
    /**
     * Check if page was loaded before hydration completed
     */
    function checkInitialHydrationState() {
        // Check if there's a meta tag or data attribute indicating hydration status
        const hydratedMeta = document.querySelector('meta[name="redis-hydrated"]');
        if (hydratedMeta) {
            isHydrated = hydratedMeta.content === 'true';
            if (!isHydrated) {
                pageLoadedWhileHydrating = true;
            }
        }
    }
    
    /**
     * Check server for refresh signal
     */
    function checkForRefresh() {
        // Don't poll if we're not waiting for hydration
        if (!pageLoadedWhileHydrating && isHydrated) {
            return;
        }
        
        // Stop polling after max attempts
        if (pollCount >= MAX_POLLS) {
            stopPolling();
            return;
        }
        
        pollCount++;
        
        fetch('/api/redis/refresh-check', {
            method: 'GET',
            credentials: 'same-origin',
            headers: {
                'Accept': 'application/json'
            }
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            
            if (data.refresh_needed && pageLoadedWhileHydrating) {
                
                // Refresh immediately without notification
                window.location.reload();
                
                stopPolling();
            } else if (data.hydrated && !isHydrated) {
                // Hydration completed but no explicit refresh flag
                // This means hydration happened but page might still need refresh
                isHydrated = true;
                
                // Continue polling for a few more cycles to catch refresh flag
            }
        })
        .catch(error => {
            console.error('[Redis Auto-Refresh] Poll error:', error);
            // Continue polling despite errors (server might be temporarily busy)
        });
    }
    
    /**
     * Show notification that data is loading
     */
    function showRefreshNotification() {
        // Create notification element if it doesn't exist
        let notification = document.getElementById('redis-refresh-notification');
        if (!notification) {
            notification = document.createElement('div');
            notification.id = 'redis-refresh-notification';
            notification.innerHTML = `
                <div style="
                    position: fixed;
                    top: 20px;
                    right: 20px;
                    background: #4CAF50;
                    color: white;
                    padding: 16px 24px;
                    border-radius: 4px;
                    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                    z-index: 10000;
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    animation: slideIn 0.3s ease-out;
                ">
                    <strong>✓ Data loaded!</strong> Refreshing page...
                </div>
            `;
            document.body.appendChild(notification);
            
            // Add animation
            if (!document.getElementById('redis-refresh-styles')) {
                const style = document.createElement('style');
                style.id = 'redis-refresh-styles';
                style.textContent = `
                    @keyframes slideIn {
                        from {
                            transform: translateX(100%);
                            opacity: 0;
                        }
                        to {
                            transform: translateX(0);
                            opacity: 1;
                        }
                    }
                `;
                document.head.appendChild(style);
            }
        }
    }
    
    /**
     * Show loading indicator while waiting for hydration
     */
    function showLoadingIndicator() {
        // Only show if page loaded before hydration
        if (!pageLoadedWhileHydrating) {
            return;
        }
        
        let indicator = document.getElementById('redis-loading-indicator');
        if (!indicator) {
            indicator = document.createElement('div');
            indicator.id = 'redis-loading-indicator';
            indicator.innerHTML = `
                <div style="
                    position: fixed;
                    bottom: 20px;
                    right: 20px;
                    background: rgba(33, 150, 243, 0.95);
                    color: white;
                    padding: 12px 20px;
                    border-radius: 4px;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
                    z-index: 9999;
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    font-size: 14px;
                    display: flex;
                    align-items: center;
                    gap: 10px;
                ">
                    <div class="spinner" style="
                        width: 16px;
                        height: 16px;
                        border: 2px solid rgba(255,255,255,0.3);
                        border-top-color: white;
                        border-radius: 50%;
                        animation: spin 0.8s linear infinite;
                    "></div>
                    <span>Loading latest data...</span>
                </div>
            `;
            document.body.appendChild(indicator);
            
            // Add spinner animation
            if (!document.getElementById('redis-loading-styles')) {
                const style = document.createElement('style');
                style.id = 'redis-loading-styles';
                style.textContent = `
                    @keyframes spin {
                        to { transform: rotate(360deg); }
                    }
                `;
                document.head.appendChild(style);
            }
        }
    }
    
    /**
     * Stop polling
     */
    function stopPolling() {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
        
        // Remove loading indicator
        const indicator = document.getElementById('redis-loading-indicator');
        if (indicator) {
            indicator.remove();
        }
    }
    
    /**
     * Start polling for refresh signals
     */
    function startPolling() {
        // Check immediately
        checkForRefresh();
        
        // Then poll at intervals
        pollTimer = setInterval(checkForRefresh, POLL_INTERVAL);
        
    }
    
    /**
     * Initialize on page load
     */
    function init() {
        // Check initial state
        checkInitialHydrationState();
        
        // Only start polling if page loaded before hydration
        if (pageLoadedWhileHydrating) {

            // Don't show loading indicator - silent refresh
            startPolling();
        } else {
        }
        
        // Clean up on page unload
        window.addEventListener('beforeunload', stopPolling);
    }
    
    // Start when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
    
    // Expose API for debugging
    window.RedisAutoRefresh = {
        getStatus: function() {
            return {
                isHydrated: isHydrated,
                pageLoadedWhileHydrating: pageLoadedWhileHydrating,
                pollCount: pollCount,
                isPolling: !!pollTimer
            };
        },
        forceCheck: checkForRefresh,
        stop: stopPolling
    };
    
})();
