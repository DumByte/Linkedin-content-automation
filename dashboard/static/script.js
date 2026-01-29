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
