/**
 * Page Tutorial Engine
 * 
 * Shared system for guided tutorials across all pages.
 * Uses localStorage to track completion (shown once per user/browser).
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

    /**
     * Start a page tutorial.
     * 
     * @param {string} storageKey - localStorage key (e.g. 'tutorial_dashboard_3m')
     * @param {Array} steps - Array of { selector: string, text: string }
     *   selector: CSS selector for the target element(s). If multiple match, their
     *             bounding rects are merged into one highlight region.
     *   text:     The instructional text shown in the modal.
     * @param {object} [options] - Optional overrides
     *   options.delay    - ms to wait before showing first step (default 500)
     *   options.padding  - px padding around highlight (default 8)
     *   options.force    - if true, show even if localStorage key exists
     */
    function startTutorial(storageKey, steps, options) {
        options = options || {};
        const delay = options.delay !== undefined ? options.delay : 500;
        const padding = options.padding !== undefined ? options.padding : 8;
        const force = options.force || false;

        // Guard: already completed
        if (!force && localStorage.getItem(storageKey)) return;
        // Guard: no steps
        if (!steps || steps.length === 0) return;
        // Guard: already running
        if (activeTutorial) return;

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

            var step = steps[idx];
            var rect = getBoundingRect(step.selector);

            // Skip steps with invisible/missing targets
            if (!rect) {
                currentStep = idx + 1;
                showStep(currentStep);
                return;
            }

            // Scroll target into view if it's off-screen
            var targetCenterY = rect.top + (rect.bottom - rect.top) / 2;
            if (targetCenterY < 60 || targetCenterY > window.innerHeight - 60) {
                var scrollTarget = document.querySelector(step.selector);
                if (scrollTarget) {
                    scrollTarget.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    // Re-measure after scroll settles
                    setTimeout(function() {
                        rect = getBoundingRect(step.selector);
                        if (rect) positionElements(rect, step, idx);
                    }, 350);
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
            textEl.textContent = step.text;
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
        }

        function endTutorial(completed) {
            overlay.style.display = 'none';
            highlight.style.display = 'none';
            modal.style.display = 'none';
            if (completed) {
                localStorage.setItem(storageKey, '1');
            }
            // Remove listeners
            skipBtn.removeEventListener('click', onSkip);
            nextBtn.removeEventListener('click', onNext);
            overlay.removeEventListener('click', onOverlayClick);
            window.removeEventListener('resize', onResize);
            window.removeEventListener('scroll', onScroll);
            activeTutorial = null;
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
})();
