/**
 * Page Tutorial Engine
 * 
 * Shared system for guided tutorials across all pages.
 * Uses server-side tracking (Redis → MySQL) to persist completion across devices.
 * 
 * Usage:
 *   startTutorial('tutorial_dashboard_3m', [
 *     { selector: '#my-element', text: 'This is step 1.' },
 *     { selector: '.my-class',   text: 'This is step 2.' },
 *   ]);
 */

(function() {
    'use strict';

    // Tracks the currently active tutorial instance
    let activeTutorial = null;

    // In-memory cache of completed tutorials (fetched from server on first use)
    let _completedCache = null;
    let _fetchPromise = null;

    /**
     * Fetch completed tutorials from the server (once per page load).
     * Returns a Promise that resolves to {page_key: timestamp, ...}.
     */
    function fetchCompletedTutorials() {
        if (_completedCache !== null) return Promise.resolve(_completedCache);
        if (_fetchPromise) return _fetchPromise;

        _fetchPromise = fetch('/api/tutorial/status', { credentials: 'same-origin' })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.status === 'success' && data.completed) {
                    _completedCache = data.completed;
                } else {
                    _completedCache = {};
                }
                return _completedCache;
            })
            .catch(function() {
                // Fallback: treat localStorage keys as the source of truth
                _completedCache = {};
                return _completedCache;
            });

        return _fetchPromise;
    }

    /**
     * Mark a tutorial as completed on the server.
     */
    function markTutorialComplete(storageKey) {
        // Update local cache immediately
        if (_completedCache) _completedCache[storageKey] = new Date().toISOString();

        // Fire-and-forget POST to server
        fetch('/api/tutorial/complete', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ page_key: storageKey })
        }).catch(function() { /* silently ignore */ });
    }

    /**
     * Start a page tutorial.
     * 
     * @param {string} storageKey - tutorial key (e.g. 'tutorial_dashboard_3m')
     * @param {Array} steps - Array of { selector: string, text: string }
     * @param {object} [options] - Optional overrides
     *   options.delay    - ms to wait before showing first step (default 500)
     *   options.padding  - px padding around highlight (default 8)
     *   options.force    - if true, show even if already completed
     */
    function startTutorial(storageKey, steps, options) {
        options = options || {};
        const delay = options.delay !== undefined ? options.delay : 500;
        const padding = options.padding !== undefined ? options.padding : 8;
        const force = options.force || false;
        const onComplete = options.onComplete || null;

        // Guard: no steps or already running
        if (!steps || steps.length === 0) return;
        if (activeTutorial) return;

        // Fetch server status, then decide whether to show
        fetchCompletedTutorials().then(function(completed) {
            if (!force && completed[storageKey]) return;
            // Guard: another tutorial started while we were fetching
            if (activeTutorial) return;

            _launchTutorial(storageKey, steps, delay, padding, onComplete);
        });
    }

    /**
     * Internal: actually build and show the tutorial UI.
     */
    function _launchTutorial(storageKey, steps, delay, padding, onComplete) {

        // Suppress data-version auto-reload while tutorial is active
        window._disableDataVersionReload = true;

        // Create DOM elements (idempotent - reuse if already present)
        let overlay = document.querySelector('.tutorial-overlay');
        let highlight = document.querySelector('.tutorial-highlight');
        let modal = document.querySelector('.tutorial-modal');

        if (!overlay) {
            overlay = document.createElement('div');
            overlay.className = 'tutorial-overlay';
            document.body.appendChild(overlay);
        }
        if (!highlight) {
            highlight = document.createElement('div');
            highlight.className = 'tutorial-highlight';
            document.body.appendChild(highlight);
        }
        if (!modal) {
            modal = document.createElement('div');
            modal.className = 'tutorial-modal';
            modal.innerHTML = '<div class="tutorial-modal-text"></div>' +
                '<div class="tutorial-modal-footer">' +
                    '<span class="tutorial-step-counter"></span>' +
                    '<div class="tutorial-modal-buttons">' +
                        '<button class="tutorial-btn-skip">Skip</button>' +
                        '<button class="tutorial-btn-next">Next</button>' +
                    '</div>' +
                '</div>';
            document.body.appendChild(modal);
        }

        const textEl = modal.querySelector('.tutorial-modal-text');
        const counterEl = modal.querySelector('.tutorial-step-counter');
        const skipBtn = modal.querySelector('.tutorial-btn-skip');
        const nextBtn = modal.querySelector('.tutorial-btn-next');

        let currentStep = 0;

        activeTutorial = { storageKey, steps, overlay, highlight, modal };

        function getBoundingRect(selector) {
            const elements = document.querySelectorAll(selector);
            if (!elements.length) return null;

            let rect = null;
            elements.forEach(function(el) {
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) return;
                if (!rect) {
                    rect = { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
                } else {
                    rect.left = Math.min(rect.left, r.left);
                    rect.top = Math.min(rect.top, r.top);
                    rect.right = Math.max(rect.right, r.right);
                    rect.bottom = Math.max(rect.bottom, r.bottom);
                }
            });

            return rect;
        }

        function showStep(idx) {
            if (idx >= steps.length) {
                endTutorial(true);
                return;
            }

            // Call onHide for the previous step if it had one
            var prevIdx = idx - 1;
            if (prevIdx >= 0 && prevIdx < steps.length && typeof steps[prevIdx].onHide === 'function') {
                try { steps[prevIdx].onHide(); } catch(e) { console.warn('Tutorial onHide error:', e); }
            }

            var step = steps[idx];

            // Call onShow before measuring
            if (typeof step.onShow === 'function') {
                try { step.onShow(); } catch(e) { console.warn('Tutorial onShow error:', e); }
            }

            var rect = getBoundingRect(step.selector);

            // Skip steps with invisible/missing targets
            if (!rect) {
                currentStep = idx + 1;
                showStep(currentStep);
                return;
            }

            // Scroll target into view if it's off-screen
            // Only scroll if the element is truly outside the visible viewport
            var isAboveViewport = rect.bottom < 0;
            var isBelowViewport = rect.top > window.innerHeight;
            var isMostlyHidden = rect.top < -20 || rect.bottom > window.innerHeight + 20;
            var needsScroll = isAboveViewport || isBelowViewport || isMostlyHidden;
            if (needsScroll) {
                var scrollTarget = document.querySelector(step.selector);
                if (scrollTarget) {
                    // Hide highlight/modal while scrolling
                    highlight.style.display = 'none';
                    modal.style.display = 'none';
                    scrollTarget.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    // Re-measure after scroll fully settles
                    setTimeout(function() {
                        rect = getBoundingRect(step.selector);
                        if (rect) positionElements(rect, step, idx);
                    }, 700);
                    return;
                }
            }

            positionElements(rect, step, idx);
        }

        function positionElements(rect, step, idx) {
            // Show overlay
            overlay.style.display = 'block';

            // Position highlight
            highlight.style.display = 'block';
            highlight.style.left = (rect.left + window.scrollX - padding) + 'px';
            highlight.style.top = (rect.top + window.scrollY - padding) + 'px';
            highlight.style.width = (rect.right - rect.left + padding * 2) + 'px';
            highlight.style.height = (rect.bottom - rect.top + padding * 2) + 'px';

            // Update modal content
            textEl.innerHTML = step.text;
            counterEl.textContent = (idx + 1) + ' of ' + steps.length;
            nextBtn.textContent = idx === steps.length - 1 ? 'Done' : 'Next';

            // Show modal to measure its size
            modal.style.display = 'block';
            modal.style.left = '0px';
            modal.style.top = '0px';

            var modalW = modal.offsetWidth;
            var modalH = modal.offsetHeight;

            // Determine modal position: prefer below the highlight, fall back to above
            var spaceBelow = window.innerHeight - rect.bottom;
            var spaceAbove = rect.top;
            var modalTop, modalLeft;

            if (spaceBelow >= modalH + 20) {
                // Place below
                modalTop = rect.bottom + window.scrollY + 12;
            } else if (spaceAbove >= modalH + 20) {
                // Place above
                modalTop = rect.top + window.scrollY - modalH - 12;
            } else {
                // Not enough space above or below — place below anyway
                modalTop = rect.bottom + window.scrollY + 12;
            }

            // Horizontally: center on highlight, clamp to viewport
            modalLeft = rect.left + window.scrollX + (rect.right - rect.left) / 2 - modalW / 2;
            if (modalLeft < 8) modalLeft = 8;
            if (modalLeft + modalW > window.innerWidth - 8) {
                modalLeft = window.innerWidth - modalW - 8;
            }

            modal.style.left = modalLeft + 'px';
            modal.style.top = modalTop + 'px';

            // Ensure the bubble stays within the visible viewport by clamping
            var viewportTop = window.scrollY + 8;
            var viewportBottom = window.scrollY + window.innerHeight - 8;
            if (modalTop + modalH > viewportBottom) {
                modalTop = viewportBottom - modalH;
            }
            if (modalTop < viewportTop) {
                modalTop = viewportTop;
            }
            modal.style.top = modalTop + 'px';
        }

        function endTutorial(completed) {
            // Call onHide for the last shown step
            if (currentStep >= 0 && currentStep < steps.length && typeof steps[currentStep].onHide === 'function') {
                try { steps[currentStep].onHide(); } catch(e) { console.warn('Tutorial onHide error:', e); }
            }
            overlay.style.display = 'none';
            highlight.style.display = 'none';
            modal.style.display = 'none';
            if (completed) {
                markTutorialComplete(storageKey);
            }
            // Remove listeners
            skipBtn.removeEventListener('click', onSkip);
            nextBtn.removeEventListener('click', onNext);
            overlay.removeEventListener('click', onOverlayClick);
            window.removeEventListener('resize', onResize);
            window.removeEventListener('scroll', onScroll);
            activeTutorial = null;

            // Re-enable data-version reload after a delay
            // (gives time for the tutorial completion POST to settle)
            setTimeout(function() {
                window._disableDataVersionReload = false;
            }, 5000);

            // Call onComplete callback if provided (slight delay for DOM to settle)
            if (onComplete) {
                setTimeout(function() {
                    try { onComplete(); } catch(e) { console.warn('Tutorial onComplete error:', e); }
                }, 300);
            }
        }

        function onNext(e) {
            e.stopPropagation();
            e.preventDefault();
            currentStep++;
            showStep(currentStep);
        }

        function onSkip(e) {
            e.stopPropagation();
            e.preventDefault();
            endTutorial(true);
        }

        function onOverlayClick(e) {
            // Clicking the overlay advances to next step
            e.stopPropagation();
            e.preventDefault();
            currentStep++;
            showStep(currentStep);
        }

        var resizeTimeout;
        function onResize() {
            clearTimeout(resizeTimeout);
            resizeTimeout = setTimeout(function() {
                if (activeTutorial && currentStep < steps.length) {
                    showStep(currentStep);
                }
            }, 150);
        }

        var scrollTimeout;
        function onScroll() {
            clearTimeout(scrollTimeout);
            scrollTimeout = setTimeout(function() {
                if (activeTutorial && currentStep < steps.length) {
                    showStep(currentStep);
                }
            }, 100);
        }

        // Bind event listeners
        skipBtn.addEventListener('click', onSkip);
        nextBtn.addEventListener('click', onNext);
        overlay.addEventListener('click', onOverlayClick);
        window.addEventListener('resize', onResize);
        window.addEventListener('scroll', onScroll);

        // Start after delay
        setTimeout(function() {
            showStep(0);
        }, delay);
    }

    // Expose globally
    window.startTutorial = startTutorial;
    window.resetTutorial = function(pageKey) {
        if (_completedCache) delete _completedCache[pageKey];
        return fetch('/api/tutorial/reset', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ page_key: pageKey })
        }).then(function(r) { return r.json(); });
    };
    window.resetAllTutorials = function() {
        _completedCache = {};
        return fetch('/api/tutorial/reset', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        }).then(function(r) { return r.json(); });
    };
})();
