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

// --- Dashboard: delete interactive ---
async function deleteInteractive(btn) {
    const row = btn.closest('tr');
    const slug = row.dataset.slug;
    const title = row.dataset.title || slug;
    if (!confirm(`Delete "${title}"? This cannot be undone.`)) return;

    try {
        const res = await fetch('/admin/api/delete-interactive', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ slug }),
        });
        const json = await res.json();
        if (json.ok) {
            showToast('Deleted.');
            row.remove();
        } else {
            showToast(json.error || 'Error deleting', 'error');
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

// --- Dashboard: upload interactive ---
function toggleUploadForm() {
    const panel = document.getElementById('uploadPanel');
    if (panel) panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

async function uploadInteractive(e) {
    e.preventDefault();
    const title = document.getElementById('uploadTitle').value.trim();
    const subject = document.getElementById('uploadSubject').value;
    const description = document.getElementById('uploadDescription').value.trim();
    const fileInput = document.getElementById('uploadFile');
    const file = fileInput.files[0];

    if (!file || !file.name.endsWith('.html')) {
        showToast('Please select an HTML file', 'error');
        return;
    }

    const formData = new FormData();
    formData.append('title', title);
    formData.append('subject', subject);
    formData.append('description', description);
    formData.append('file', file);

    try {
        const res = await fetch('/admin/api/upload-interactive', {
            method: 'POST',
            body: formData,
        });
        const json = await res.json();
        if (json.ok) {
            showToast('Interactive uploaded!');
            setTimeout(() => location.reload(), 1000);
        } else {
            showToast(json.error || 'Upload failed', 'error');
        }
    } catch (e) {
        showToast('Network error', 'error');
    }
}

// --- Change Password ---
function togglePasswordForm() {
    const panel = document.getElementById('passwordPanel');
    if (panel) panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

async function changePassword(e) {
    e.preventDefault();
    const currentPassword = document.getElementById('currentPassword').value;
    const newPassword = document.getElementById('newPasswordInput').value;
    const confirmPassword = document.getElementById('confirmPasswordInput').value;

    if (newPassword !== confirmPassword) {
        showToast('New passwords do not match', 'error');
        return;
    }

    try {
        const res = await fetch('/admin/api/change-password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                current_password: currentPassword,
                new_password: newPassword,
                confirm_password: confirmPassword,
            }),
        });
        const json = await res.json();
        if (json.ok) {
            showToast('Password changed successfully!');
            document.getElementById('currentPassword').value = '';
            document.getElementById('newPasswordInput').value = '';
            document.getElementById('confirmPasswordInput').value = '';
            togglePasswordForm();
        } else {
            showToast(json.error || 'Error changing password', 'error');
        }
    } catch (e) {
        showToast('Network error', 'error');
    }
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

// --- Invite Management ---
function toggleInviteForm() {
    const panel = document.getElementById('invitePanel');
    if (panel) panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

async function generateInvite() {
    const emailHint = document.getElementById('inviteEmailHint')?.value.trim() || '';

    try {
        const res = await fetch('/admin/api/create-invite', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email_hint: emailHint }),
        });
        const json = await res.json();
        if (json.ok) {
            const link = `${window.location.origin}/join/${json.token}`;
            const display = document.getElementById('inviteLinkDisplay');
            const input = document.getElementById('inviteLinkInput');
            if (display && input) {
                input.value = link;
                display.style.display = 'block';
            }
            const panel = document.getElementById('invitePanel');
            if (panel) panel.style.display = 'none';
            showToast('Invite link created!');
        } else {
            showToast(json.error || 'Failed to create invite', 'error');
        }
    } catch (e) {
        showToast('Network error', 'error');
    }
}

function copyInviteLink() {
    const input = document.getElementById('inviteLinkInput');
    if (input) {
        navigator.clipboard.writeText(input.value).then(() => showToast('Link copied!'));
    }
}
