(function() {
    const pageTag = (() => {
        const path = (window.location && window.location.pathname) ? window.location.pathname : '/';
        const cleaned = path.replace(/^\/+/, '').replace(/\//g, '_') || 'home';
        return `page:${cleaned}`;
    })();
    const pageTagSlug = pageTag.replace(/[^a-zA-Z0-9_-]/g, '-');

    async function fetchJson(url, options = {}) {
        const resp = await fetch(url, Object.assign({
            headers: { 'Content-Type': 'application/json' }
        }, options));
        if (!resp.ok) {
            const text = await resp.text();
            throw new Error(text || resp.statusText || 'Request failed');
        }
        if (resp.status === 204) return {};
        return resp.json();
    }

    // Unread post numbers injected by the server
    const _unreadPosts = new Set(
        (window.__unreadPostNumbers || []).map(n => String(n))
    );
    // Maps post_number (string) → comment_id (number) for badge placement
    const _unreadCommentIds = window.__unreadCommentIds || {};

    function formatFiderDate(iso) {
        if (!iso) return '';
        try {
            const d = new Date(iso);
            const mm = String(d.getMonth() + 1).padStart(2, '0');
            const dd = String(d.getDate()).padStart(2, '0');
            const yyyy = d.getFullYear();
            let hours = d.getHours();
            const minutes = String(d.getMinutes()).padStart(2, '0');
            const ampm = hours >= 12 ? 'PM' : 'AM';
            hours = hours % 12 || 12;
            return `${mm}-${dd}-${yyyy} ${hours}:${minutes} ${ampm}`;
        } catch (e) { return ''; }
    }

    function renderPostList(items, targetEl) {
        if (!targetEl) return;
        if (!items || !items.length) {
            targetEl.innerHTML = '<div class="feedback-empty">No posts yet.</div>';
            return;
        }
        targetEl.innerHTML = items.map(item => {
            const tags = (item.tags || []).map(t => `<span class="feedback-tag">${t}</span>`).join('');
            const unreadBadge = _unreadPosts.has(String(item.number)) ? '<span class="feedback-card-badge">New Comment</span>' : '';
            return `<div class="feedback-card" data-number="${item.number}">
                ${unreadBadge}
                <div class="feedback-card-header">
                    <div class="feedback-card-title">${item.title || ''}</div>
                    <div class="feedback-card-votes"><i class="fa-solid fa-thumbs-up"></i> ${item.votesCount || 0}</div>
                </div>
                <div class="feedback-card-body">${item.description ? item.description.substring(0, 160) : ''}</div>
                <div class="feedback-card-meta">
                    <span class="feedback-card-status">${item.status || 'open'}</span>
                    <span class="feedback-card-comments"><i class="fa-regular fa-comments"></i> ${item.commentsCount || 0}</span>
                    <div class="feedback-card-tags">${tags}</div>
                </div>
            </div>`;
        }).join('');
    }

    async function loadTags(selectEl) {
        if (!selectEl) return;
        try {
            const data = await fetchJson('/api/feedback/tags');
            const options = ['<option value="">All tags</option>'];
            (data || []).forEach(tag => {
                options.push(`<option value="${tag.slug}">${tag.name}</option>`);
            });
            selectEl.innerHTML = options.join('');
        } catch (err) {
            console.error('Failed to load tags', err);
        }
    }

    async function loadPosts({ container, view, query, tag, limit, includeContext = false }) {
        if (!container) return;
        const params = new URLSearchParams();
        if (view) params.set('view', view);
        if (query) params.set('query', query);
        if (tag) params.set('tags', tag);
        if (includeContext) {
            // include both name and slug to match existing and future tags
            params.append('page_context', `${pageTag},${pageTagSlug}`);
        }
        if (limit) params.set('limit', String(limit));
        try {
            container.dataset.loading = 'true';
            const data = await fetchJson(`/api/feedback/posts?${params.toString()}`);
            renderPostList(data, container);
        } catch (err) {
            container.innerHTML = `<div class="feedback-error">${err.message}</div>`;
        } finally {
            delete container.dataset.loading;
        }
    }

    async function loadPostDetail(number, detailEl, commentListEl, isUnread) {
        if (!detailEl || !number) return;
        try {
            detailEl.dataset.loading = 'true';
            const post = await fetchJson(`/api/feedback/posts/${number}`);
            const comments = await fetchJson(`/api/feedback/posts/${number}/comments`);
            const voted = !!post.hasVoted;
            detailEl.innerHTML = `
                <div class="feedback-detail-header">
                    <div class="feedback-detail-title">${post.title || ''}</div>
                    <button class="feedback-vote-btn ${voted ? 'voted' : ''}" data-number="${post.number}" data-voted="${voted}">
                        <i class="fa-solid fa-thumbs-up"></i> ${voted ? 'Voted' : 'Vote'}
                    </button>
                </div>
                <div class="feedback-detail-body">${post.description || ''}</div>
                <div class="feedback-detail-meta">Status: ${post.status || 'open'}</div>
                ${post.createdAt ? `<span class="feedback-timestamp">${formatFiderDate(post.createdAt)}</span>` : ''}
            `;
            if (commentListEl) {
                const commentItems = comments || [];
                const targetIds = isUnread ? new Set(((_unreadCommentIds[String(number)] || [])).map(id => id)) : new Set();
                commentListEl.innerHTML = commentItems.map((c) => {
                    const isNewComment = targetIds.size > 0 && targetIds.has(c.id);
                    return `<div class="feedback-comment${isNewComment ? ' feedback-comment--new' : ''}">
                        ${isNewComment ? '<span class="feedback-comment-badge">New Comment</span>' : ''}
                        <div class="feedback-comment-author">${c.user?.name || 'User'}</div>
                        <div class="feedback-comment-body">${c.content || ''}</div>
                        ${c.createdAt ? `<span class="feedback-timestamp">${formatFiderDate(c.createdAt)}</span>` : ''}
                    </div>`;
                }).join('');

                if (isUnread && targetIds.size > 0) {
                    const firstNew = commentListEl.querySelector('.feedback-comment--new');
                    if (firstNew) firstNew.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                    const clearBadges = (e) => {
                        const newComments = commentListEl.querySelectorAll('.feedback-comment--new');
                        let anyContains = false;
                        newComments.forEach(el => { if (el.contains(e.target)) anyContains = true; });
                        if (!anyContains) {
                            newComments.forEach(el => {
                                el.querySelector('.feedback-comment-badge')?.remove();
                                el.classList.remove('feedback-comment--new');
                            });
                            document.removeEventListener('pointerdown', clearBadges);
                        }
                    };
                    setTimeout(() => document.addEventListener('pointerdown', clearBadges), 150);
                }
            }
        } catch (err) {
            detailEl.innerHTML = `<div class="feedback-error">${err.message}</div>`;
        } finally {
            delete detailEl.dataset.loading;
        }
    }

    async function submitPost(formEl, onDone) {
        if (!formEl) return;
        const title = formEl.querySelector('[name="title"]').value.trim();
        const description = formEl.querySelector('[name="description"]').value.trim();
        const context = formEl.querySelector('[name="context_tag"]')?.value || '';
        if (!title) {
            showToast('Please add a title', 'warning');
            return;
        }
        try {
            formEl.querySelector('button[type="submit"]').disabled = true;
            await fetchJson('/api/feedback/posts', {
                method: 'POST',
                body: JSON.stringify({ title, description, context_tag: context })
            });
            formEl.reset();
            if (typeof onDone === 'function') onDone();
        } catch (err) {
            showToast(err.message || 'Failed to create post', 'error');
        } finally {
            formEl.querySelector('button[type="submit"]').disabled = false;
        }
    }

    async function submitComment(formEl, number, onDone) {
        if (!formEl || !number) return;
        const content = formEl.querySelector('[name="content"]').value.trim();
        if (!content) return;
        try {
            formEl.querySelector('button[type="submit"]').disabled = true;
            await fetchJson(`/api/feedback/posts/${number}/comments`, {
                method: 'POST',
                body: JSON.stringify({ content })
            });
            formEl.reset();
            if (typeof onDone === 'function') onDone();
        } catch (err) {
            showToast(err.message || 'Failed to add comment', 'error');
        } finally {
            formEl.querySelector('button[type="submit"]').disabled = false;
        }
    }

    function wireFeedbackPage() {
        const page = document.getElementById('feedback-page');
        if (!page) return;
        const listEl = document.getElementById('feedback-list');
        const detailEl = document.getElementById('feedback-detail');
        const commentsEl = document.getElementById('feedback-comments');
        const searchInput = document.getElementById('feedback-search');
        const viewSelect = document.getElementById('feedback-view');
        const tagSelect = document.getElementById('feedback-tag');
        const newPostForm = document.getElementById('feedback-new-post-form');
        const commentForm = document.getElementById('feedback-comment-form');

        loadTags(tagSelect);
        loadPosts({ container: listEl, view: 'trending', tag: '', limit: 30, includeContext: false });

        listEl?.addEventListener('click', (e) => {
            const card = e.target.closest('.feedback-card');
            if (!card) return;
            const number = card.dataset.number;
            const isUnread = _unreadPosts.has(String(number));
            loadPostDetail(number, detailEl, commentsEl, isUnread);
            commentForm.dataset.number = number;

            // Mark this post as read
            if (isUnread) {
                _unreadPosts.delete(String(number));
                const badge = card.querySelector('.feedback-card-badge');
                if (badge) badge.remove();
                fetch(`/api/feedback/mark-read/${number}`, { method: 'POST' }).catch(() => {});
                // Update nav badge if no more unread
                if (_unreadPosts.size === 0) {
                    const navBadge = document.querySelector('.nav-feedback .feedback-badge');
                    if (navBadge) navBadge.remove();
                }
            }
        });

        if (searchInput) {
            searchInput.addEventListener('input', () => {
                loadPosts({ container: listEl, view: viewSelect.value, query: searchInput.value, tag: tagSelect.value, limit: 30, includeContext: false });
            });
        }
        if (viewSelect) {
            viewSelect.addEventListener('change', () => {
                loadPosts({ container: listEl, view: viewSelect.value, query: searchInput.value, tag: tagSelect.value, limit: 30, includeContext: false });
            });
        }
        if (tagSelect) {
            tagSelect.addEventListener('change', () => {
                loadPosts({ container: listEl, view: viewSelect.value, query: searchInput.value, tag: tagSelect.value || '', limit: 30, includeContext: false });
            });
        }

        if (newPostForm) {
            newPostForm.querySelector('[name="context_tag"]').value = pageTagSlug;
            newPostForm.addEventListener('submit', (e) => {
                e.preventDefault();
                submitPost(newPostForm, () => loadPosts({ container: listEl, view: viewSelect.value, query: searchInput.value, tag: tagSelect.value, limit: 30, includeContext: false }));
            });
        }

        if (commentForm) {
            commentForm.addEventListener('submit', (e) => {
                e.preventDefault();
                const number = commentForm.dataset.number;
                submitComment(commentForm, number, () => loadPostDetail(number, detailEl, commentsEl));
            });
        }

        // Vote button inside the detail pane
        detailEl?.addEventListener('click', (e) => {
            const voteBtn = e.target.closest('.feedback-vote-btn');
            if (!voteBtn) return;
            const number = voteBtn.dataset.number;
            if (!number) return;
            const alreadyVoted = voteBtn.dataset.voted === 'true';
            const method = alreadyVoted ? 'DELETE' : 'POST';
            voteBtn.disabled = true;
            // Optimistic UI toggle
            voteBtn.classList.toggle('voted', !alreadyVoted);
            voteBtn.dataset.voted = (!alreadyVoted).toString();
            voteBtn.innerHTML = `<i class="fa-solid fa-thumbs-up"></i> ${!alreadyVoted ? 'Voted' : 'Vote'}`;

            fetchJson(`/api/feedback/posts/${number}/votes`, { method })
                .then(() => {
                    loadPostDetail(number, detailEl, commentsEl);
                    loadPosts({ container: listEl, view: viewSelect.value, query: searchInput.value, tag: tagSelect.value, limit: 30, includeContext: false });
                })
                .catch(err => showToast(err.message || 'Failed to vote', 'error'))
                .finally(() => { voteBtn.disabled = false; });
        });
    }

    function wireFloatingModal() {
        const fab = document.getElementById('feedback-fab');
        const modal = document.getElementById('feedback-quick-modal');
        const closeBtn = document.getElementById('close-feedback-quick');
        const listEl = document.getElementById('feedback-quick-list');
        const formEl = document.getElementById('feedback-quick-form');
        const statusEl = document.getElementById('feedback-quick-status');
        if (!fab || !modal) return;

        loadPosts({ container: listEl, view: 'trending', tag: pageTag, limit: 6, includeContext: true });

        fab.addEventListener('click', () => {
            modal.style.display = 'flex';
        });
        closeBtn?.addEventListener('click', () => modal.style.display = 'none');
        window.addEventListener('click', (e) => {
            if (e.target === modal) modal.style.display = 'none';
        });

        if (formEl) {
            formEl.querySelector('[name="context_tag"]').value = pageTagSlug;
            formEl.addEventListener('submit', (e) => {
                e.preventDefault();
                if (statusEl) statusEl.textContent = '';
                submitPost(formEl, () => {
                    if (statusEl) {
                        statusEl.textContent = 'Thanks! Your feedback was submitted.';
                        statusEl.className = 'feedback-status success';
                    }
                    loadPosts({ container: listEl, view: 'trending', tag: pageTag, limit: 6, includeContext: true });
                });
            });
        }

        listEl?.addEventListener('click', (e) => {
            const card = e.target.closest('.feedback-card');
            if (!card) return;
            const number = card.dataset.number;
            fetchJson(`/api/feedback/posts/${number}/votes`, { method: 'POST' })
                .then(() => loadPosts({ container: listEl, view: 'trending', tag: pageTag, limit: 6, includeContext: true }))
                .catch(err => showToast(err.message || 'Failed to vote', 'error'));
        });
    }

    document.addEventListener('DOMContentLoaded', function() {
        wireFeedbackPage();
        wireFloatingModal();
    });
})();