/**
 * Copy post text to clipboard, preserving LinkedIn formatting (line breaks).
 */
async function copyPost(button) {
    const text = button.dataset.text
        // Unescape HTML entities
        .replace(/&amp;/g, "&")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/&#34;/g, '"');

    try {
        await navigator.clipboard.writeText(text);
        const original = button.textContent;
        button.textContent = "Copied!";
        button.classList.add("copied");
        setTimeout(() => {
            button.textContent = original;
            button.classList.remove("copied");
        }, 2000);
    } catch (err) {
        // Fallback for older browsers
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
        button.textContent = "Copied!";
        setTimeout(() => { button.textContent = "Copy to Clipboard"; }, 2000);
    }
}

/**
 * Update post status via API.
 */
async function updateStatus(postId, status) {
    try {
        const resp = await fetch(`/api/posts/${postId}/status`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status }),
        });

        if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}`);
        }

        showToast(`Post ${status}`);

        // Remove card from drafts view or update badge
        const card = document.querySelector(`[data-post-id="${postId}"]`);
        if (card && (status === "approved" || status === "rejected")) {
            card.style.opacity = "0.5";
            card.style.transition = "opacity 0.3s ease";
            setTimeout(() => card.remove(), 300);
        }

        // If no cards left, show empty state
        setTimeout(() => {
            const remaining = document.querySelectorAll(".draft-card");
            if (remaining.length === 0) {
                const list = document.querySelector(".draft-list");
                if (list) {
                    list.innerHTML = `
                        <div class="empty-state">
                            <p>All drafts reviewed! Check the history page.</p>
                        </div>
                    `;
                }
            }
        }, 350);

    } catch (err) {
        showToast("Error updating status: " + err.message);
    }
}

/**
 * Show a brief toast notification.
 */
function showToast(message) {
    let toast = document.querySelector(".toast");
    if (!toast) {
        toast = document.createElement("div");
        toast.className = "toast";
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add("show");
    setTimeout(() => toast.classList.remove("show"), 2500);
}


// --- Candidate Selection & Generation ---

/**
 * Select or deselect all candidate checkboxes.
 */
function toggleSelectAll(selectAll) {
    const checkboxes = document.querySelectorAll(".candidate-checkbox");
    checkboxes.forEach(cb => { cb.checked = selectAll; });
    updateGenerateButton();
}

/**
 * Update the Generate button state based on checked candidates.
 */
function updateGenerateButton() {
    const btn = document.getElementById("generateBtn");
    if (!btn) return;
    const checked = document.querySelectorAll(".candidate-checkbox:checked");
    btn.disabled = checked.length === 0;
    btn.textContent = checked.length > 0
        ? `Generate ${checked.length} Post${checked.length > 1 ? "s" : ""}`
        : "Generate Posts";
}

/**
 * Generate posts for all selected candidates sequentially.
 */
async function generateSelected() {
    const checked = document.querySelectorAll(".candidate-checkbox:checked");
    if (checked.length === 0) return;

    const btn = document.getElementById("generateBtn");
    btn.disabled = true;
    btn.textContent = "Generating...";

    let successCount = 0;
    let errorCount = 0;

    for (const cb of checked) {
        const candidateId = cb.dataset.candidateId;
        const card = document.querySelector(`.candidate-card[data-candidate-id="${candidateId}"]`);
        const statusBadge = card.querySelector(".badge-status-candidate");

        // Update UI to show generating
        if (statusBadge) {
            statusBadge.textContent = "generating";
            statusBadge.className = "badge badge-status-generating";
        }

        try {
            const resp = await fetch("/api/generate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ candidate_id: parseInt(candidateId) }),
            });

            const data = await resp.json();

            if (resp.ok) {
                successCount++;
                cb.checked = false;
                cb.disabled = true;
                card.dataset.status = "generated";
                if (statusBadge) {
                    statusBadge.textContent = "generated";
                    statusBadge.className = "badge badge-status-generated";
                }
                // Show generated post preview
                const preview = document.getElementById(`preview-${candidateId}`);
                const postText = document.getElementById(`post-text-${candidateId}`);
                if (preview && postText) {
                    postText.textContent = data.full_post;
                    preview.style.display = "block";
                }
            } else {
                errorCount++;
                card.dataset.status = "error";
                if (statusBadge) {
                    statusBadge.textContent = "error";
                    statusBadge.className = "badge badge-status-error";
                }
                // Show error message
                const errorEl = card.querySelector(".candidate-error");
                if (errorEl) {
                    errorEl.textContent = `Error: ${data.error}`;
                    errorEl.style.display = "block";
                }
            }
        } catch (err) {
            errorCount++;
            card.dataset.status = "error";
            if (statusBadge) {
                statusBadge.textContent = "error";
                statusBadge.className = "badge badge-status-error";
            }
        }
    }

    const parts = [];
    if (successCount > 0) parts.push(`${successCount} generated`);
    if (errorCount > 0) parts.push(`${errorCount} failed`);
    showToast(parts.join(", "));

    updateGenerateButton();
}

/**
 * Reject a candidate article so it won't appear in future rankings.
 */
async function rejectCandidate(candidateId) {
    try {
        const resp = await fetch(`/api/candidates/${candidateId}/reject`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
        });

        if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}`);
        }

        showToast("Article rejected");

        const card = document.querySelector(`.candidate-card[data-candidate-id="${candidateId}"]`);
        if (card) {
            card.style.opacity = "0.5";
            card.style.transition = "opacity 0.3s ease";
            setTimeout(() => card.remove(), 300);
        }

        updateGenerateButton();
    } catch (err) {
        showToast("Error rejecting candidate: " + err.message);
    }
}

/**
 * Copy a generated post's text to clipboard.
 */
async function copyGeneratedPost(candidateId) {
    const postText = document.getElementById(`post-text-${candidateId}`);
    if (!postText) return;
    try {
        await navigator.clipboard.writeText(postText.textContent);
        showToast("Copied to clipboard");
    } catch (err) {
        const textarea = document.createElement("textarea");
        textarea.value = postText.textContent;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
        showToast("Copied to clipboard");
    }
}
