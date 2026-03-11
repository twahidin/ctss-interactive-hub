// =========================================================
// CTSS Interactive Hub — Admin Dashboard JS
// =========================================================

function showToast(message, type = 'success') {
    const container = document.getElementById('toastContainer');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.animation = 'slideOut 0.3s ease forwards';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// --- Dashboard: save row ---
async function saveRow(btn) {
    const row = btn.closest('tr');
    const slug = row.dataset.slug;
    const passcodeInput = row.querySelector('[data-field="passcode"]');
    const activeToggle = row.querySelector('[data-field="is_active"]');

    const data = { slug };
    if (passcodeInput) data.passcode = passcodeInput.value;
    if (activeToggle) data.is_active = activeToggle.checked;

    try {
        const res = await fetch('/admin/api/update-interactive', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        const json = await res.json();
        if (json.ok) {
            showToast('Saved!');
        } else {
            showToast(json.error || 'Error saving', 'error');
        }
    } catch (e) {
        showToast('Network error', 'error');
    }
}

// --- Dashboard: scan for new interactives ---
async function scanInteractives() {
    try {
        const res = await fetch('/admin/api/scan', { method: 'POST' });
        const json = await res.json();
        if (json.ok) {
            if (json.new_count > 0) {
                showToast(`Found ${json.new_count} new interactive(s). Refreshing...`);
                setTimeout(() => location.reload(), 1500);
            } else {
                showToast('No new interactives found.');
            }
        } else {
            showToast(json.error || 'Scan failed', 'error');
        }
    } catch (e) {
        showToast('Network error', 'error');
    }
}

// --- Dashboard: filter table ---
function filterTable() {
    const query = document.getElementById('searchInput').value.toLowerCase();
    const rows = document.querySelectorAll('#interactivesBody tr');
    rows.forEach(row => {
        const subject = row.dataset.subject || '';
        const title = row.dataset.title || '';
        row.style.display = (subject.includes(query) || title.includes(query)) ? '' : 'none';
    });
}

// --- Teacher Management ---
async function addTeacher(e) {
    e.preventDefault();
    const name = document.getElementById('newName').value.trim();
    const email = document.getElementById('newEmail').value.trim();
    const password = document.getElementById('newPassword').value;
    const role = document.getElementById('newRole').value;
    const subjectsRaw = document.getElementById('newSubjects').value;
    const subjects = subjectsRaw.split(',').map(s => s.trim()).filter(Boolean);

    try {
        const res = await fetch('/admin/api/add-teacher', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, email, password, role, subjects }),
        });
        const json = await res.json();
        if (json.ok) {
            showToast('Teacher added!');
            setTimeout(() => location.reload(), 1000);
        } else {
            showToast(json.error || 'Error adding teacher', 'error');
        }
    } catch (e) {
        showToast('Network error', 'error');
    }
}

async function removeTeacher(email, name) {
    if (!confirm(`Remove ${name}? This cannot be undone.`)) return;

    try {
        const res = await fetch('/admin/api/remove-teacher', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email }),
        });
        const json = await res.json();
        if (json.ok) {
            showToast('Teacher removed.');
            const row = document.querySelector(`tr[data-email="${email}"]`);
            if (row) row.remove();
        } else {
            showToast(json.error || 'Error removing teacher', 'error');
        }
    } catch (e) {
        showToast('Network error', 'error');
    }
}
