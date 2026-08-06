(function () {
  const ROLE_LABELS = { super_admin: "Super Admin", doctor: "Doctor", user: "User", auditor: "Auditor" };
  const NAV = {
    super_admin: [
      { page: "dashboard", label: "Dashboard" },
      { page: "create-user", label: "Create User" },
      { page: "change-password", label: "Change My Password" },
      { page: "reset-user-password", label: "Reset User Password" },
      { page: "bank-details", label: "Bank Details" },
      { page: "payment-sheet", label: "Payment Sheet" },
      { page: "upload-excel", label: "Upload Excel" },
      { page: "claim-rules", label: "Claim Rules" },
      { page: "diagnosis-criteria", label: "Diagnosis Criteria" },
      { page: "rule-suggestions", label: "Rule Suggestions" },
      { page: "medicines", label: "Medicines" },
      { page: "storage-maintenance", label: "Storage Maintenance" },
      { page: "ai-prompt", label: "AI Prompt" },
      { page: "legacy-sync", label: "Legacy Migration" },
    ],
    doctor: [
      { page: "dashboard", label: "Dashboard" },
      { page: "assigned-cases", label: "Assigned Cases" },
      { page: "change-password", label: "Change My Password" },
    ],
    user: [
      { page: "dashboard", label: "Dashboard" },
      { page: "upload-excel", label: "Upload Excel" },
      { page: "assign-cases", label: "Assign Cases" },
      { page: "withdrawn-claims", label: "Withdrawn Claims" },
      { page: "upload-document", label: "Upload Document" },
      { page: "completed-not-uploaded", label: "Completed (Not Uploaded)" },
      { page: "completed-uploaded", label: "Completed (Uploaded)" },
      { page: "export-data", label: "Export Data" },
      { page: "allotment-date-wise", label: "Allotment Date Wise" },
      { page: "change-password", label: "Change My Password" },
    ],
    auditor: [
      { page: "dashboard", label: "Dashboard" },
      { page: "audit-claims", label: "Audit Claims" },
      { page: "change-password", label: "Change My Password" },
    ],
  };

  const PAGE_TITLES = {
    dashboard: "Dashboard",
    "create-user": "Create User",
    "change-password": "Change Password",
    "reset-user-password": "Reset User Password",
    "claim-rules": "Claim Rules",
    "diagnosis-criteria": "Diagnosis Criteria",
    "rule-suggestions": "Rule Suggestions",
    medicines: "Medicines",
    "storage-maintenance": "Storage Maintenance",
    "ai-prompt": "AI Prompt",
    "legacy-sync": "Legacy Migration",
    "assigned-cases": "Assigned Cases",
    "case-detail": "Case Detail",
    "upload-excel": "Upload Excel",
    "assign-cases": "Assign Cases",
    "withdrawn-claims": "Withdrawn Claims",
    "upload-document": "Upload Document",
    "completed-not-uploaded": "Completed Reports (Not Uploaded)",
    "completed-uploaded": "Completed Reports (Uploaded)",
    "export-data": "Export Full Data",
    "allotment-date-wise": "Allotment Date Wise",
    "bank-details": "Bank Details",
    "payment-sheet": "Payment Sheet",
    "audit-claims": "Audit Claims",
  };

  const pageTitleEl = document.getElementById("page-title");
  const welcomeLineEl = document.getElementById("welcome-line");
  const sideNavEl = document.getElementById("side-nav-links");
  const contentPanel = document.getElementById("content-panel");
  const headerActions = document.getElementById("header-actions");
  let completedReportsMessageHandler = null;
  const CLAIM_SYNC_STORAGE_KEY = 'qc_claim_refresh_signal';
  const CLAIM_SYNC_CHANNEL = 'qc_claim_events';
  let claimSyncListenerBound = false;
  let claimSyncChannelRef = null;
  function formatRoleLabelGlobal(role) {
    const raw = String(role || '').trim().toLowerCase();
    if (!raw) return '-';
    if (raw === 'super_admin') return 'Super Admin';
    if (raw === 'doctor') return 'Doctor';
    if (raw === 'auditor') return 'Auditor';
    if (raw === 'user') return 'User';
    return raw.split('_').map(function (part) {
      return part ? (part.charAt(0).toUpperCase() + part.slice(1)) : '';
    }).join(' ');
  }

  function targetPageAfterCompletion(routeRole) {
    const role = String(routeRole || '').trim().toLowerCase();
    if (role === 'doctor') return 'assigned-cases';
    if (role === 'user') return 'upload-document';
    if (role === 'auditor') return 'audit-claims';
    return 'dashboard';
  }

  function shouldReloadForClaimSyncPage(page) {
    const p = String(page || '').trim();
    return [
      'dashboard',
      'assigned-cases',
      'assign-cases',
      'upload-document',
      'completed-not-uploaded',
      'completed-uploaded',
      'audit-claims',
      'withdrawn-claims'
    ].includes(p);
  }

  function handleClaimSyncPayload(payload) {
    if (!payload || typeof payload !== 'object') return;
    const type = String(payload.type || '').trim();
    if (!type) return;
    if (type !== 'claim-status-updated' && type !== 'qc-updated') return;

    const route = parseRoute();
    const page = String(route && route.page ? route.page : 'dashboard');
    const role = String(route && route.routeRole ? route.routeRole : '').trim();

    if (type === 'claim-status-updated') {
      const status = String(payload.status || '').trim().toLowerCase();
      if (status === 'completed' && page === 'case-detail') {
        const q = new URLSearchParams(window.location.search || '');
        const currentClaimUuid = String(q.get('claim_uuid') || '').trim();
        const payloadClaimUuid = String(payload.claim_uuid || '').trim();
        if (!payloadClaimUuid || !currentClaimUuid || payloadClaimUuid === currentClaimUuid) {
          window.location.href = '/qc/' + encodeURIComponent(role || 'user') + '/' + targetPageAfterCompletion(role);
          return;
        }
      }
    }

    if (shouldReloadForClaimSyncPage(page)) {
      window.location.reload();
    }
  }

  function attachClaimSyncListeners() {
    if (claimSyncListenerBound) return;
    claimSyncListenerBound = true;

    window.addEventListener('message', function (event) {
      try {
        if (!event || event.origin !== window.location.origin) return;
        handleClaimSyncPayload(event.data && typeof event.data === 'object' ? event.data : null);
      } catch (_err) {
      }
    });

    window.addEventListener('storage', function (event) {
      try {
        if (!event || event.key !== CLAIM_SYNC_STORAGE_KEY || !event.newValue) return;
        const payload = JSON.parse(String(event.newValue || '{}'));
        handleClaimSyncPayload(payload);
      } catch (_err) {
      }
    });

    try {
      if (typeof window.BroadcastChannel === 'function') {
        claimSyncChannelRef = new window.BroadcastChannel(CLAIM_SYNC_CHANNEL);
        claimSyncChannelRef.onmessage = function (event) {
          try {
            const payload = event && event.data && typeof event.data === 'object' ? event.data : null;
            handleClaimSyncPayload(payload);
          } catch (_err) {
          }
        };
      }
    } catch (_err) {
    }
  }

  function detachCompletedReportsMessageListener() {
    if (!completedReportsMessageHandler) return;
    window.removeEventListener('message', completedReportsMessageHandler);
    completedReportsMessageHandler = null;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function setMessage(id, type, text) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = type ? "msg " + type : "";
    el.textContent = text || "";
  }

  function renderError(message) {
    contentPanel.innerHTML = '<p class="msg err">' + escapeHtml(message || "Unexpected error") + "</p>";
  }

  function parseListInput(text) {
    return String(text || "").split(/\r?\n|,/).map((s) => s.trim()).filter(Boolean);
  }

  function listToTextarea(items) {
    return (items || []).join("\n");
  }

  function statusChip(status) {
    const raw = String(status || "").trim();
    const s = raw.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
    let cls = "info";
    if (["completed", "uploaded", "approve", "approved", "active", "yes", "succeeded"].includes(s)) cls = "success";
    if (["withdrawn", "reject", "rejected", "failed", "hard_reject"].includes(s)) cls = "warn";
    if (["pending", "processing", "inactive", "no"].includes(s)) cls = "muted";
    return '<span class="status-chip ' + cls + '">' + escapeHtml(status || "-") + "</span>";
  }

  function formatDateTime(value) {
    if (!value) return "-";
    const dt = new Date(value);
    return Number.isNaN(dt.getTime()) ? String(value) : dt.toLocaleString();
  }
  function formatDateOnly(value) {
    if (!value) return "-";
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) {
      const raw = String(value);
      if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
        const parts = raw.split("-");
        return parts[2] + "-" + parts[1] + "-" + parts[0];
      }
      return raw;
    }
    return dt.toLocaleDateString("en-GB");
  }

  function parseAllotmentDateValue(value) {
    const raw = String(value || '').trim();
    if (!raw) return Number.NaN;
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
      const dt = new Date(raw + 'T00:00:00');
      return Number.isNaN(dt.getTime()) ? Number.NaN : dt.getTime();
    }
    if (/^\d{2}-\d{2}-\d{4}$/.test(raw)) {
      const parts = raw.split('-');
      const dt = new Date(parts[2] + '-' + parts[1] + '-' + parts[0] + 'T00:00:00');
      return Number.isNaN(dt.getTime()) ? Number.NaN : dt.getTime();
    }
    const dt = new Date(raw);
    return Number.isNaN(dt.getTime()) ? Number.NaN : dt.getTime();
  }

  function sortClaimsByAllotmentDateFirst(items, ascending) {
    const list = Array.isArray(items) ? items.slice() : [];
    const isAsc = ascending !== false;
    list.sort(function (a, b) {
      const aAllot = parseAllotmentDateValue(a && a.allotment_date);
      const bAllot = parseAllotmentDateValue(b && b.allotment_date);
      const aHasAllot = Number.isFinite(aAllot);
      const bHasAllot = Number.isFinite(bAllot);

      if (aHasAllot !== bHasAllot) return aHasAllot ? -1 : 1;
      if (aHasAllot && bHasAllot && aAllot !== bAllot) return isAsc ? (aAllot - bAllot) : (bAllot - aAllot);

      const aFallback = parseAllotmentDateValue((a && (a.assigned_at || a.updated_at || a.created_at || a.last_upload)) || '');
      const bFallback = parseAllotmentDateValue((b && (b.assigned_at || b.updated_at || b.created_at || b.last_upload)) || '');
      if (Number.isFinite(aFallback) && Number.isFinite(bFallback) && aFallback !== bFallback) {
        return isAsc ? (aFallback - bFallback) : (bFallback - aFallback);
      }

      const aId = String((a && (a.external_claim_id || a.id)) || '');
      const bId = String((b && (b.external_claim_id || b.id)) || '');
      return aId.localeCompare(bId);
    });
    return list;
  }


  function formatStatusText(value) {
    const raw = String(value || "").trim();
    if (!raw) return "-";
    return raw
      .split("_")
      .filter(Boolean)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(" ");
  }

  function formatTreatmentType(value) {
    const raw = String(value || '').trim();
    const normalized = raw.toLowerCase();
    if (!normalized) return '';
    if (normalized === 'surgical') return 'Surgical';
    if (normalized === 'non_surgical' || normalized === 'non-surgical' || normalized === 'nonsurgical') return 'Non-Surgical';
    return raw;
  }

  function resolveTreatmentType(item) {
    const payload = item && item.legacy_payload && typeof item.legacy_payload === 'object' ? item.legacy_payload : {};
    const raw = (item && (item.treatment_type || item.claim_type))
      || payload.treatment_type
      || payload['treatment type']
      || payload.claim_type
      || payload['claim type']
      || '';
    return formatTreatmentType(raw);
  }

  function formatBytes(n) {
    const num = Number(n || 0);
    if (!Number.isFinite(num) || num <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let idx = 0;
    let cur = num;
    while (cur >= 1024 && idx < units.length - 1) {
      cur /= 1024;
      idx += 1;
    }
    return cur.toFixed(cur >= 10 || idx === 0 ? 0 : 2) + " " + units[idx];
  }

  function getToken() {
    return localStorage.getItem("qc_access_token") || "";
  }

  function clearAuthAndRedirect() {
    localStorage.removeItem("qc_access_token");
    localStorage.removeItem("qc_user");
    localStorage.removeItem("qc_acting_role");
    window.location.href = "/qc/login";
  }

  async function apiFetch(path, options) {
    const token = getToken();
    if (!token) {
      clearAuthAndRedirect();
      throw new Error("Not authenticated");
    }
    const opts = options || {};
    const headers = new Headers(opts.headers || {});
    headers.set("Authorization", "Bearer " + token);

    const resp = await fetch(path, { ...opts, headers });
    const raw = await resp.text();
    let body = null;
    try {
      body = raw ? JSON.parse(raw) : null;
    } catch (_err) {
      body = { detail: raw };
    }

    if (!resp.ok) {
      if (resp.status === 401) clearAuthAndRedirect();
      if (resp.status === 413) {
        throw new Error('Upload size too large. Please upload smaller files or fewer files at once.');
      }
      const detail = body ? body.detail : null;
      const detailText = typeof detail === 'string'
        ? detail
        : (detail != null ? JSON.stringify(detail) : '');
      throw new Error(detailText || ("HTTP " + resp.status));
    }
    return body;
  }

    async function apiFetchFile(path) {
    const token = getToken();
    if (!token) {
      clearAuthAndRedirect();
      throw new Error("Not authenticated");
    }
    const resp = await fetch(path, { headers: { Authorization: "Bearer " + token } });
    if (!resp.ok) {
      if (resp.status === 401) clearAuthAndRedirect();
      const raw = await resp.text();
      throw new Error(raw || ("HTTP " + resp.status));
    }

    const contentDisposition = String(resp.headers.get('Content-Disposition') || '');
    let filename = '';
    const match = /filename\*?=(?:UTF-8''|\"?)([^\";]+)/i.exec(contentDisposition);
    if (match && match[1]) {
      try {
        filename = decodeURIComponent(String(match[1] || '').replace(/\"/g, '').trim());
      } catch (_err) {
        filename = String(match[1] || '').replace(/\"/g, '').trim();
      }
    }

    const blob = await resp.blob();
    return {
      blob: blob,
      filename: filename,
      contentType: String(resp.headers.get('Content-Type') || ''),
    };
  }

  function parseRoute() {
    const trimmed = window.location.pathname.replace(/^\/qc\/?/, "");
    const parts = trimmed.split("/").filter(Boolean);
    if (parts.length === 0 || parts[0] === "login") return { routeRole: null, page: null };
    return { routeRole: parts[0], page: parts.slice(1).join("-") || "dashboard" };
  }

  function navHref(role, page) {
    return "/qc/" + role + "/" + page;
  }

  function renderHeader(user, activeRole, page) {
    pageTitleEl.textContent = (ROLE_LABELS[activeRole] || activeRole) + " " + (PAGE_TITLES[page] || "Workspace");
    welcomeLineEl.textContent = "Welcome, " + user.username + " (" + (ROLE_LABELS[activeRole] || activeRole) + ")";

    const canSwitchRole = user.role === "super_admin";
    const roleSwitch = canSwitchRole
      ? '<form class="role-switch-form" onsubmit="return false;">'
        + '<label class="role-switch-form__label" for="acting-role-switch">Role</label>'
        + '<select id="acting-role-switch">'
        + '<option value="super_admin"' + (activeRole === "super_admin" ? " selected" : "") + '>Super Admin</option>'
        + '<option value="doctor"' + (activeRole === "doctor" ? " selected" : "") + '>Doctor</option>'
        + '<option value="user"' + (activeRole === "user" ? " selected" : "") + '>User</option>'
        + '<option value="auditor"' + (activeRole === "auditor" ? " selected" : "") + '>Auditor</option>'
        + "</select></form>"
      : "";

    headerActions.innerHTML = roleSwitch
      + '<a class="btn btn-soft" href="/">Home</a>'
      + '<button id="btn-logout" type="button">Logout</button>';

    const switchEl = document.getElementById("acting-role-switch");
    if (switchEl) {
      switchEl.addEventListener("change", function () {
        const role = this.value;
        localStorage.setItem("qc_acting_role", role);
        window.location.href = "/qc/" + role + "/dashboard";
      });
    }

    document.getElementById("btn-logout").addEventListener("click", async function () {
      try {
        await apiFetch("/api/v1/auth/logout", { method: "POST" });
      } catch (_err) {
        // ignore
      }
      clearAuthAndRedirect();
    });
  }

  function renderNav(activeRole, activePage) {
    const links = NAV[activeRole] || [];
    sideNavEl.innerHTML = links.map((item) => {
      const activeClass = item.page === activePage ? " active" : "";
      return '<a class="side-nav__link' + activeClass + '" href="' + navHref(activeRole, item.page) + '">' + escapeHtml(item.label) + "</a>";
    }).join("");
  }

  function renderClaimsTable(items) {
    if (!items || items.length === 0) return "<p class='muted'>No claims found.</p>";
    const rows = items.map((item) => {
      return "<tr>"
        + "<td><code>" + escapeHtml(item.external_claim_id) + "</code></td>"
        + "<td>" + escapeHtml(item.patient_name || "-") + "</td>"
        + "<td>" + statusChip(item.status) + "</td>"
        + "<td>" + escapeHtml(item.assigned_doctor_id || "-") + "</td>"
        + "<td>" + escapeHtml(formatDateTime(item.created_at)) + "</td>"
        + "</tr>";
    }).join("");
    return '<div class="table-wrap"><table><thead><tr><th>Claim ID</th><th>Patient</th><th>Status</th><th>Doctor</th><th>Created</th></tr></thead><tbody>' + rows + "</tbody></table></div>";
  }

  async function fetchDoctors() {
    try {
      const response = await apiFetch('/api/v1/auth/doctor-usernames');
      const names = (response.items || [])
        .map((name) => String(name || '').trim())
        .filter(Boolean);
      return Array.from(new Set(names)).sort((a, b) => a.localeCompare(b));
    } catch (_primaryErr) {
      try {
        const users = await apiFetch('/api/v1/auth/users?limit=500');
        const names = (users.items || [])
          .filter((u) => u.role === 'doctor' || u.role === 'super_admin')
          .map((u) => String(u.username || '').trim())
          .filter(Boolean);
        return Array.from(new Set(names)).sort((a, b) => a.localeCompare(b));
      } catch (_err) {
        const fallback = await apiFetch('/api/v1/user-tools/completed-reports?status_filter=all&qc_filter=all&limit=500&offset=0');
        const names = (fallback.items || [])
          .flatMap((r) => String(r.assigned_doctor_id || '').split(','))
          .map((s) => s.trim())
          .filter(Boolean);
        return Array.from(new Set(names)).sort((a, b) => a.localeCompare(b));
      }
    }
  }
  async function renderSuperAdminDashboard() {
    const [users, rules, criteria, suggestions] = await Promise.all([
      apiFetch("/api/v1/auth/users?limit=500"),
      apiFetch("/api/v1/admin/claim-rules?limit=1"),
      apiFetch("/api/v1/admin/diagnosis-criteria?limit=1"),
      apiFetch("/api/v1/admin/rule-suggestions?status_filter=pending&limit=1"),
    ]);

    const doctors = (users.items || []).filter((u) => u.role === "doctor").length;
    const operations = (users.items || []).filter((u) => u.role === "user").length;

    contentPanel.innerHTML = '<h2>Dashboard Overview</h2><p class="muted">Legacy QC admin controls in one screen.</p>'
      + '<div class="stats-grid">'
      + '<article class="stat-card"><div class="muted">Total Users</div><div class="value">' + users.total + "</div></article>"
      + '<article class="stat-card"><div class="muted">Doctor Users</div><div class="value">' + doctors + "</div></article>"
      + '<article class="stat-card"><div class="muted">Operations Users</div><div class="value">' + operations + "</div></article>"
      + "</div>"
      + '<div class="stats-grid" style="margin-top:12px;">'
      + '<article class="stat-card"><div class="muted">Claim Rules</div><div class="value">' + rules.total + "</div></article>"
      + '<article class="stat-card"><div class="muted">Diagnosis Criteria</div><div class="value">' + criteria.total + "</div></article>"
      + '<article class="stat-card"><div class="muted">Pending Suggestions</div><div class="value">' + suggestions.total + "</div></article>"
      + '</div>'
      + '<section class="claim-status-panel" style="margin-top:16px;">'
      + '<h3 class="claim-status-title">Import Analysis SQL</h3>'
      + '<p class="muted">Upload legacy SQL dump to import analysis reports into super admin report generation.</p>'
      + '<p id="import-analysis-sql-msg"></p>'
      + '<form id="import-analysis-sql-form">'
      + '<div class="form-row"><label>SQL Dump File</label><input type="file" name="file" accept=".sql" required></div>'
      + '<div class="form-row"><label>Limit (optional)</label><input type="number" name="limit" min="0" step="1" placeholder="0 = all rows"></div>'
      + '<button type="submit" id="import-analysis-sql-btn">Import Analysis</button>'
      + '</form>'
      + '</section>';

    const importForm = document.getElementById("import-analysis-sql-form");
    const importBtn = document.getElementById("import-analysis-sql-btn");
    if (importForm) {
      importForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        setMessage("import-analysis-sql-msg", "", "");

        const fd = new FormData(importForm);
        const rawLimit = String(fd.get("limit") || "").trim();
        const limitNum = Number(rawLimit || "0");
        const qs = Number.isFinite(limitNum) && limitNum > 0
          ? ("?limit=" + encodeURIComponent(String(Math.floor(limitNum))))
          : "";
        fd.delete("limit");

        const originalBtnText = importBtn ? importBtn.textContent : "";
        if (importBtn) {
          importBtn.disabled = true;
          importBtn.textContent = "Importing...";
        }

        try {
          const result = await apiFetch("/api/v1/admin/analysis/import-sql" + qs, { method: "POST", body: fd });
          const msg = "Import complete. Processed: " + String(result.processed || 0)
            + ", matched claim: " + String(result.matched_claim || 0)
            + ", inserted reports: " + String(result.reports_inserted || 0)
            + ", updated reports: " + String(result.reports_updated || 0)
            + ", missing claim: " + String(result.no_claim_match || 0)
            + ", no report html: " + String(result.no_report_html || 0);
          setMessage("import-analysis-sql-msg", "ok", msg);
        } catch (err) {
          setMessage("import-analysis-sql-msg", "err", err.message);
        } finally {
          if (importBtn) {
            importBtn.disabled = false;
            importBtn.textContent = originalBtnText || "Import Analysis";
          }
        }
      });
    }
  }

  async function renderDoctorDashboard() {
    const me = await apiFetch('/api/v1/auth/me');
    const dashboardUrl = new URL(window.location.href);
    const doctorFromQuery = String(dashboardUrl.searchParams.get('doctor_username') || '').trim();
    const doctorId = doctorFromQuery || String((me && me.username) || '').trim();
    const doctorParam = doctorId ? ('&assigned_doctor_id=' + encodeURIComponent(doctorId)) : '';

    const openParams = new URLSearchParams();
    openParams.set('status_filter', 'all');
    openParams.set('exclude_completed', 'true');
    openParams.set('exclude_withdrawn', 'true');
    openParams.set('exclude_tagged', 'true');
    openParams.set('sort_order', 'asc');
    openParams.set('limit', '200');
    openParams.set('offset', '0');
    if (doctorId) openParams.set('doctor_filter', doctorId);

    const [allSummary, completedSummary, withdrawnSummary, listPayload, openClaimsPayload, completionStats] = await Promise.all([
      apiFetch('/api/v1/claims?limit=1' + doctorParam),
      apiFetch('/api/v1/claims?status=completed&limit=1' + doctorParam),
      apiFetch('/api/v1/claims?status=withdrawn&limit=1' + doctorParam),
      apiFetch('/api/v1/claims?limit=200' + doctorParam),
      apiFetch('/api/v1/user-tools/claim-document-status?' + openParams.toString()),
      apiFetch('/api/v1/user-tools/doctor-completion-stats?doctor_username=' + encodeURIComponent(doctorId || '')).catch(function () {
        return { month_wise_closed: [], day_wise_closed: [], selected_month: '' };
      }),
    ]);
    const rawAssigned = Number((allSummary && allSummary.total) || 0);
    const doneCount = Number((completedSummary && completedSummary.total) || 0);
    const withdrawnCount = Number((withdrawnSummary && withdrawnSummary.total) || 0);
    const totalAssigned = Math.max(0, rawAssigned - withdrawnCount);
    const pendingCount = Math.max(0, totalAssigned - doneCount);

    const monthWiseClosed = Array.isArray(completionStats && completionStats.month_wise_closed)
      ? completionStats.month_wise_closed
      : [];
    let activeMonth = String((completionStats && completionStats.selected_month) || '').trim();
    if (!activeMonth && monthWiseClosed.length > 0) {
      activeMonth = String(monthWiseClosed[0].month || '').trim();
    }

    const items = (listPayload && listPayload.items) || [];
    const visibleItems = (function () {
      const baseByClaimId = new Map();
      items.forEach(function (item) {
        const key = String((item && item.external_claim_id) || '').trim();
        if (key) baseByClaimId.set(key, item);
      });

      const orderedOpenItems = sortClaimsByAllotmentDateFirst((openClaimsPayload && openClaimsPayload.items) || [], true);
      if (orderedOpenItems.length > 0) {
        return orderedOpenItems.map(function (item) {
          const claimId = String((item && item.external_claim_id) || '').trim();
          const base = baseByClaimId.get(claimId) || {};
          return {
            external_claim_id: claimId || String(base.external_claim_id || ''),
            patient_name: String(base.patient_name || '').trim() || '-',
            status: String(item.status_display || item.status || base.status || '').trim() || '-',
            assigned_doctor_id: String(item.assigned_doctor_id || base.assigned_doctor_id || '').trim() || '-',
            created_at: item.allotment_date || item.assigned_at || item.last_upload || base.created_at || '',
            allotment_date: item.allotment_date || '',
          };
        });
      }

      const fallbackOpen = items.filter(function (c) {
        const status = String(c && c.status || '').toLowerCase();
        return status !== 'completed' && status !== 'withdrawn';
      });
      return sortClaimsByAllotmentDateFirst(fallbackOpen, true);
    }());

    function renderMonthRows(selectedMonth) {
      return monthWiseClosed.map(function (item) {
        const monthKey = String((item && item.month) || '').trim();
        const monthLabel = String((item && item.label) || monthKey || '-').trim();
        const closedCount = Number((item && item.closed) || 0);
        const isActive = monthKey && monthKey === selectedMonth;
        return '<tr>'
          + '<td>' + escapeHtml(monthLabel) + '</td>'
          + '<td>' + escapeHtml(String(closedCount)) + '</td>'
          + '<td><button type="button" class="btn-soft doctor-month-drill-btn' + (isActive ? ' is-active' : '') + '" data-month="' + escapeHtml(monthKey) + '">View Day-wise</button></td>'
          + '</tr>';
      }).join('');
    }

    function renderDayWiseTable(dayItems) {
      const rows = (Array.isArray(dayItems) ? dayItems : []).map(function (item) {
        return '<tr>'
          + '<td>' + escapeHtml(String((item && item.date) || '-')) + '</td>'
          + '<td>' + escapeHtml(String((item && item.closed) || 0)) + '</td>'
          + '</tr>';
      }).join('');
      return '<table><thead><tr><th>Date</th><th>Closed Cases</th></tr></thead><tbody>'
        + (rows || '<tr><td colspan="2">No day-wise completed cases found.</td></tr>')
        + '</tbody></table>';
    }

    function readDayWise(payload) {
      return Array.isArray(payload && payload.day_wise_closed) ? payload.day_wise_closed : [];
    }

    const initialDayWise = readDayWise(completionStats);
    contentPanel.innerHTML = '<h2>Dashboard Overview</h2><p class="muted">Quick summary of your assigned and completed workload.</p>'
      + '<div class="stats-grid">'
      + '<article class="stat-card"><div class="muted">Assigned Cases</div><div class="value">' + totalAssigned + '</div></article>'
      + '<article class="stat-card"><div class="muted">Pending Cases</div><div class="value">' + pendingCount + '</div></article>'
      + '<article class="stat-card"><div class="muted">Done Cases</div><div class="value">' + doneCount + '</div></article>'
      + '</div>'
      + '<h3 style="margin-top:18px">Month-wise Closed Cases</h3>'
      + '<div class="table-wrap"><table><thead><tr><th>Month</th><th>Closed Cases</th><th>Action</th></tr></thead><tbody>'
      + (renderMonthRows(activeMonth) || '<tr><td colspan="3">No closed cases found.</td></tr>')
      + '</tbody></table></div>'
      + '<h3 style="margin-top:18px" id="doctor-daywise-title">Day-wise Closed Cases' + (activeMonth ? (' (' + escapeHtml(activeMonth) + ')') : '') + '</h3>'
      + '<div class="table-wrap" id="doctor-daywise-wrap">' + renderDayWiseTable(initialDayWise) + '</div>'
      + '<h3 style="margin-top:18px">Assigned Cases (Open)</h3>'
      + renderClaimsTable(visibleItems.slice(0, 50));

    const dayWiseTitleEl = document.getElementById('doctor-daywise-title');
    const dayWiseWrapEl = document.getElementById('doctor-daywise-wrap');
    const monthButtons = Array.from(contentPanel.querySelectorAll('.doctor-month-drill-btn'));

    monthButtons.forEach(function (btn) {
      btn.addEventListener('click', async function () {
        const monthKey = String(btn.getAttribute('data-month') || '').trim();
        if (!monthKey) return;
        monthButtons.forEach(function (b) { b.disabled = true; });
        if (dayWiseTitleEl) dayWiseTitleEl.textContent = 'Day-wise Closed Cases (' + monthKey + ')';
        if (dayWiseWrapEl) {
          dayWiseWrapEl.innerHTML = '<table><thead><tr><th>Date</th><th>Closed Cases</th></tr></thead><tbody><tr><td colspan="2">Loading...</td></tr></tbody></table>';
        }
        try {
          const drilldown = await apiFetch('/api/v1/user-tools/doctor-completion-stats?month=' + encodeURIComponent(monthKey) + '&doctor_username=' + encodeURIComponent(doctorId || '')); 
          const dayItems = readDayWise(drilldown);
          activeMonth = monthKey;
          monthButtons.forEach(function (b) {
            const bMonth = String(b.getAttribute('data-month') || '').trim();
            b.classList.toggle('is-active', !!bMonth && bMonth === activeMonth);
          });
          if (dayWiseWrapEl) dayWiseWrapEl.innerHTML = renderDayWiseTable(dayItems);
        } catch (err) {
          if (dayWiseWrapEl) {
            dayWiseWrapEl.innerHTML = '<table><thead><tr><th>Date</th><th>Closed Cases</th></tr></thead><tbody><tr><td colspan="2">Failed to load day-wise closed cases.</td></tr></tbody></table>';
          }
        } finally {
          monthButtons.forEach(function (b) { b.disabled = false; });
        }
      });
    });
  }

  async function renderUserDashboard() {
    const [allClaims, ready, waiting, inReview, completed, withdrawn, dashboardData] = await Promise.all([
      apiFetch("/api/v1/claims?limit=50"),
      apiFetch("/api/v1/claims?status=ready_for_assignment&limit=1"),
      apiFetch("/api/v1/claims?status=waiting_for_documents&limit=1"),
      apiFetch("/api/v1/claims?status=in_review&limit=1"),
      apiFetch("/api/v1/claims?status=completed&limit=1"),
      apiFetch("/api/v1/claims?status=withdrawn&limit=1"),
      apiFetch('/api/v1/user-tools/dashboard-overview').catch(function () {
        return { day_wise_completed: [], assignee_wise: [] };
      }),
    ]);

    function formatRoleLabel(role) {
      const raw = String(role || '').trim().toLowerCase();
      if (!raw) return '-';
      if (raw === 'super_admin') return 'Super Admin';
      if (raw === 'doctor') return 'Doctor';
      if (raw === 'auditor') return 'Auditor';
      if (raw === 'user') return 'User';
      return raw.split('_').map(function (part) {
        return part ? (part.charAt(0).toUpperCase() + part.slice(1)) : '';
      }).join(' ');
    }

    const dayWiseItems = Array.isArray(dashboardData && dashboardData.day_wise_completed)
      ? dashboardData.day_wise_completed
      : [];
    const assigneeItems = Array.isArray(dashboardData && dashboardData.assignee_wise)
      ? dashboardData.assignee_wise
      : [];

    const now = new Date();
    const currentMonthPrefix = String(now.getFullYear()) + '-' + String(now.getMonth() + 1).padStart(2, '0');
    const dayWiseCurrentMonthItems = dayWiseItems.filter(function (item) {
      const dateText = String(item && item.date ? item.date : '').trim();
      return dateText.indexOf(currentMonthPrefix + '-') === 0;
    });

    const dayWiseRows = dayWiseCurrentMonthItems.map(function (item) {
      return '<tr>'
        + '<td>' + escapeHtml(String(item && item.date ? item.date : '-')) + '</td>'
        + '<td>' + escapeHtml(String(item && item.completed ? item.completed : 0)) + '</td>'
        + '</tr>';
    }).join('');

    const assigneeRows = assigneeItems.map(function (item) {
      return '<tr>'
        + '<td>' + escapeHtml(String(item && item.username ? item.username : '-')) + '</td>'
        + '<td>' + escapeHtml(formatRoleLabelGlobal(item && item.role ? item.role : '')) + '</td>'
        + '<td>' + escapeHtml(String(item && item.completed ? item.completed : 0)) + '</td>'
        + '<td>' + escapeHtml(String(item && item.pending ? item.pending : 0)) + '</td>'
        + '<td>' + escapeHtml(String(item && item.total ? item.total : 0)) + '</td>'
        + '</tr>';
    }).join('');

    contentPanel.innerHTML = '<h2>Dashboard Overview</h2><p class="muted">Day-wise completed and pending status across assigned cases.</p>'
      + '<div class="stats-grid">'
      + '<article class="stat-card"><div class="muted">Ready for Assignment</div><div class="value">' + ready.total + '</div></article>'
      + '<article class="stat-card"><div class="muted">Waiting for Documents</div><div class="value">' + waiting.total + '</div></article>'
      + '<article class="stat-card"><div class="muted">In Review</div><div class="value">' + inReview.total + '</div></article>'
      + '</div><div class="stats-grid" style="margin-top:12px;">'
      + '<article class="stat-card"><div class="muted">Completed</div><div class="value">' + completed.total + '</div></article>'
      + '<article class="stat-card"><div class="muted">Withdrawn</div><div class="value">' + withdrawn.total + '</div></article>'
      + '<article class="stat-card"><div class="muted">Total Claims</div><div class="value">' + allClaims.total + '</div></article>'
      + '</div>'
      + '<h3 style="margin-top:22px">Day-wise Completed Cases</h3>'
      + '<div class="table-wrap"><table><thead><tr><th>Date</th><th>Completed Cases</th></tr></thead><tbody>'
      + (dayWiseRows || '<tr><td colspan="2">No completed cases found.</td></tr>')
      + '</tbody></table></div>'
      + '<h3 style="margin-top:22px">Assignee-wise Completed and Pending Status</h3>'
      + '<div class="table-wrap"><table><thead><tr><th>User</th><th>Role</th><th>Completed</th><th>Pending</th><th>Total</th></tr></thead><tbody>'
      + (assigneeRows || '<tr><td colspan="5">No assignee data found.</td></tr>')
      + '</tbody></table></div>';
  }

  async function renderPaymentSheet() {
    function getDefaultMonth() {
      const now = new Date();
      const prev = new Date(now.getFullYear(), now.getMonth() - 1, 1);
      return String(prev.getFullYear()) + '-' + String(prev.getMonth() + 1).padStart(2, '0');
    }

    function toNumber(value) {
      const n = Number(value);
      return Number.isFinite(n) ? n : 0;
    }

    function formatMoney(value) {
      return toNumber(value).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    contentPanel.innerHTML = '<h2>Payment Sheet</h2><p class="muted">Previous month payment sheet based on completed cases and configured rate.</p>'
      + '<p id="payment-sheet-msg"></p>'
      + '<form id="payment-sheet-filter-form" class="claim-status-filters">'
      + '<div class="claim-filter-group"><label for="payment-sheet-month">Month</label><input id="payment-sheet-month" type="month"></div>'
      + '<div class="claim-filter-group"><label for="payment-sheet-include-zero">Include Zero Cases</label><select id="payment-sheet-include-zero"><option value="1">Yes</option><option value="0">No</option></select></div>'
      + '<div class="claim-filter-action"><button type="submit" class="claim-apply-btn">Load Sheet</button></div>'
      + '</form>'
      + '<p class="muted" id="payment-sheet-summary">-</p>'
      + '<div class="table-wrap"><table><thead><tr><th>User</th><th>Role</th><th>Rate</th><th>Cases</th><th>Amount</th></tr></thead><tbody id="payment-sheet-tbody"><tr><td colspan="5">Loading...</td></tr></tbody></table></div>';

    const formEl = document.getElementById('payment-sheet-filter-form');
    const monthEl = document.getElementById('payment-sheet-month');
    const includeZeroEl = document.getElementById('payment-sheet-include-zero');
    const summaryEl = document.getElementById('payment-sheet-summary');
    const tbodyEl = document.getElementById('payment-sheet-tbody');

    if (monthEl && !monthEl.value) monthEl.value = getDefaultMonth();

    async function loadSheet() {
      const monthVal = String((monthEl && monthEl.value) || '').trim();
      if (!/^\d{4}-\d{2}$/.test(monthVal)) {
        setMessage('payment-sheet-msg', 'err', 'Please select month in YYYY-MM format.');
        return;
      }
      const includeZero = String((includeZeroEl && includeZeroEl.value) || '1') === '1';
      const qs = 'month=' + encodeURIComponent(monthVal) + '&include_zero_cases=' + encodeURIComponent(includeZero ? 'true' : 'false');

      try {
        setMessage('payment-sheet-msg', '', 'Loading payment sheet...');
        const payload = await apiFetch('/api/v1/user-tools/payment-sheet?' + qs);
        const items = Array.isArray(payload && payload.items) ? payload.items : [];

        const rows = items.map(function (item) {
          return '<tr>'
            + '<td>' + escapeHtml(String(item && item.username ? item.username : '-')) + '</td>'
            + '<td>' + escapeHtml(formatRoleLabelGlobal(item && item.role ? item.role : '')) + '</td>'
            + '<td>' + escapeHtml(String(item && item.rate_raw ? item.rate_raw : '0')) + '</td>'
            + '<td>' + escapeHtml(String(item && item.completed_cases != null ? item.completed_cases : 0)) + '</td>'
            + '<td>' + escapeHtml(formatMoney(item && item.amount_total != null ? item.amount_total : 0)) + '</td>'
            + '</tr>';
        }).join('');

        tbodyEl.innerHTML = rows || '<tr><td colspan="5">No payment rows found for selected month.</td></tr>';
        summaryEl.textContent = 'Month: ' + String(payload && payload.month_label ? payload.month_label : monthVal)
          + ' | Total Users: ' + String(payload && payload.total_users != null ? payload.total_users : 0)
          + ' | Total Cases: ' + String(payload && payload.total_cases != null ? payload.total_cases : 0)
          + ' | Total Amount: ' + formatMoney(payload && payload.total_amount != null ? payload.total_amount : 0);
        setMessage('payment-sheet-msg', 'ok', 'Payment sheet loaded.');
      } catch (err) {
        tbodyEl.innerHTML = '<tr><td colspan="5">Failed to load payment sheet.</td></tr>';
        summaryEl.textContent = '-';
        setMessage('payment-sheet-msg', 'err', err && err.message ? err.message : 'Failed to load payment sheet.');
      }
    }

    formEl.addEventListener('submit', async function (e) {
      e.preventDefault();
      await loadSheet();
    });

    await loadSheet();
  }
  function renderCreateUser() {
    contentPanel.innerHTML = '<h2>Create User</h2><p class="muted">Create doctor/user/super_admin/auditor accounts.</p>'
      + '<p id="create-user-msg"></p><form id="create-user-form">'
      + '<div class="form-row"><label>Username</label><input name="username" required></div>'
      + '<div class="form-row"><label>Password</label><input type="password" name="password" required></div>'
      + '<div class="form-row"><label>Role</label><select name="role"><option value="user">User</option><option value="doctor">Doctor</option><option value="super_admin">Super Admin</option><option value="auditor">Auditor</option></select></div>'
      + '<button type="submit">Create User</button></form>';

    document.getElementById("create-user-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      const payload = Object.fromEntries(new FormData(e.currentTarget).entries());
      try {
        await apiFetch("/api/v1/auth/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        setMessage("create-user-msg", "ok", "User created successfully.");
      } catch (err) {
        setMessage("create-user-msg", "err", err.message);
      }
    });
  }
  async function renderUserBankDetails() {
    const state = {
      items: [],
      editingUserId: '',
      total: 0,
      limit: 25,
      offset: 0,
      search: '',
      isLoading: false,
    };
    let searchTimer = null;

    contentPanel.innerHTML = '<h2>Bank Details</h2><p class="muted">Manage bank details for super admins and doctors.</p>'
      + '<p id="bank-details-msg"></p>'
      + '<div class="form-row"><label for="bank-details-search">Search User</label><input id="bank-details-search" placeholder="Username, role, bank, IFSC, rate"></div>'
      + '<div class="link-row" style="justify-content:space-between; align-items:center; margin-top:10px">'
      + '<p class="muted" id="bank-details-total" style="margin:0">Showing 0-0 of 0 users</p>'
      + '<div class="link-row" style="align-items:center"><label for="bank-page-size" class="muted" style="margin:0">Rows:</label><select id="bank-page-size" style="width:auto"><option value="25">25</option><option value="50">50</option><option value="100">100</option></select></div>'
      + '</div>'
      + '<div class="table-wrap"><table><thead><tr><th>User</th><th>Role</th><th>Holder</th><th>Bank</th><th>Account</th><th>Rate</th><th>IFSC</th><th>UPI</th><th>Active</th><th>Updated By</th><th>Updated At</th><th>Action</th></tr></thead><tbody id="bank-details-tbody"><tr><td colspan="12">Loading...</td></tr></tbody></table></div>'
      + '<div class="link-row" style="justify-content:flex-end; margin-top:10px">'
      + '<button type="button" class="btn-soft" id="bank-page-prev">Previous</button>'
      + '<span class="muted" id="bank-page-info" style="min-width:120px; text-align:center">Page 0 of 0</span>'
      + '<button type="button" class="btn-soft" id="bank-page-next">Next</button>'
      + '</div>'
      + '<div id="bank-edit-modal" class="modal-backdrop">'
      + '<div class="modal-card wide" role="dialog" aria-modal="true" aria-labelledby="bank-edit-modal-title">'
      + '<div class="modal-header"><h3 id="bank-edit-modal-title">Edit Bank Details</h3><button type="button" class="btn-soft" id="bank-modal-close">Close</button></div>'
      + '<p class="muted" id="bank-modal-user-label">User: -</p>'
      + '<p id="bank-modal-msg"></p>'
      + '<form id="bank-details-form">'
      + '<div class="grid-2">'
      + '<div class="form-row"><label for="bank-account-holder">Account Holder Name</label><input id="bank-account-holder" maxlength="255"></div>'
      + '<div class="form-row"><label for="bank-name">Bank Name</label><input id="bank-name" maxlength="255"></div>'
      + '<div class="form-row"><label for="bank-branch">Branch Name</label><input id="bank-branch" maxlength="255"></div>'
      + '<div class="form-row"><label for="bank-account-number">Account Number</label><input id="bank-account-number" maxlength="64"></div>'
      + '<div class="form-row"><label for="bank-rate">Rate</label><input id="bank-rate" maxlength="64" placeholder="e.g. 500 or 2%"></div>'
      + '<div class="form-row"><label for="bank-ifsc">IFSC Code</label><input id="bank-ifsc" maxlength="32"></div>'
      + '<div class="form-row"><button type="button" class="btn-soft" id="bank-ifsc-verify">Verify IFSC</button><p id="bank-ifsc-msg" class="muted" style="margin:8px 0 0"></p></div>'
      + '<div class="form-row"><label for="bank-upi">UPI ID</label><input id="bank-upi" maxlength="255"></div>'
      + '<div class="form-row"><label for="bank-is-active"><input id="bank-is-active" type="checkbox" checked style="width:auto; margin-right:8px;">Bank Details Active</label></div>'
      + '</div>'
      + '<div class="form-row"><label for="bank-notes">Notes</label><textarea id="bank-notes" maxlength="2000" placeholder="Optional notes"></textarea></div>'
      + '<div class="link-row">'
      + '<button type="submit" id="bank-modal-save-btn">Save Bank Details</button>'
      + '<button type="button" class="btn-soft" id="bank-form-reset">Reset</button>'
      + '</div>'
      + '</form>'
      + '</div>'
      + '</div>';

    const tbody = document.getElementById('bank-details-tbody');
    const totalEl = document.getElementById('bank-details-total');
    const searchEl = document.getElementById('bank-details-search');
    const pageSizeEl = document.getElementById('bank-page-size');
    const pagePrevBtn = document.getElementById('bank-page-prev');
    const pageNextBtn = document.getElementById('bank-page-next');
    const pageInfoEl = document.getElementById('bank-page-info');

    const modalEl = document.getElementById('bank-edit-modal');
    const modalUserLabelEl = document.getElementById('bank-modal-user-label');
    const modalMsgEl = document.getElementById('bank-modal-msg');
    const form = document.getElementById('bank-details-form');
    const closeBtn = document.getElementById('bank-modal-close');
    const resetBtn = document.getElementById('bank-form-reset');
    const saveBtn = document.getElementById('bank-modal-save-btn');
    const ifscVerifyBtn = document.getElementById('bank-ifsc-verify');
    const ifscMsgEl = document.getElementById('bank-ifsc-msg');

    function setModalMessage(type, text) {
      if (!modalMsgEl) return;
      modalMsgEl.className = type ? ('msg ' + type) : '';
      modalMsgEl.textContent = text || '';
    }

    function setIfscStatus(message, isError) {
      if (!ifscMsgEl) return;
      const text = String(message || '').trim();
      if (!text) {
        ifscMsgEl.className = 'muted';
        ifscMsgEl.textContent = '';
        return;
      }
      ifscMsgEl.className = isError ? 'msg err' : 'msg ok';
      ifscMsgEl.textContent = text;
    }

    function setFormValues(item) {
      const row = item || {};
      state.editingUserId = String((row && row.user_id) || '').trim();
      document.getElementById('bank-account-holder').value = String(row.account_holder_name || '');
      document.getElementById('bank-name').value = String(row.bank_name || '');
      document.getElementById('bank-branch').value = String(row.branch_name || '');
      document.getElementById('bank-account-number').value = String(row.account_number || '');
      document.getElementById('bank-rate').value = String(row.payment_rate || '');
      document.getElementById('bank-ifsc').value = String(row.ifsc_code || '');
      document.getElementById('bank-upi').value = String(row.upi_id || '');
      document.getElementById('bank-notes').value = String(row.notes || '');
      document.getElementById('bank-is-active').checked = row.bank_is_active !== false;
      setIfscStatus('', false);
    }

    function resetFormToCurrentItem() {
      const userId = String(state.editingUserId || '').trim();
      const target = state.items.find(function (it) { return String(it && it.user_id || '') === userId; });
      if (target) {
        setFormValues(target);
      } else {
        form.reset();
        document.getElementById('bank-is-active').checked = true;
        setIfscStatus('', false);
      }
      setModalMessage('', '');
    }

    function openModal(item) {
      const row = item || {};
      const username = String(row.username || '-').trim() || '-';
      const role = String(row.role || '-').trim() || '-';
      setFormValues(row);
      modalUserLabelEl.textContent = 'User: ' + username + ' (' + role + ')';
      setModalMessage('', '');
      modalEl.classList.add('open');
    }

    function closeModal() {
      modalEl.classList.remove('open');
      setModalMessage('', '');
      setIfscStatus('', false);
    }

    function renderRows(items) {
      const rows = (items || []).map(function (item) {
        const id = String((item && item.user_id) || '').trim();
        return '<tr>'
          + '<td>' + escapeHtml(String(item && item.username ? item.username : '-')) + '</td>'
          + '<td>' + escapeHtml(formatRoleLabelGlobal(item && item.role ? item.role : '')) + '</td>'
          + '<td>' + escapeHtml(String(item && item.account_holder_name ? item.account_holder_name : '-')) + '</td>'
          + '<td>' + escapeHtml(String(item && item.bank_name ? item.bank_name : '-')) + '</td>'
          + '<td>' + escapeHtml(String(item && item.account_number ? item.account_number : '-')) + '</td>'
          + '<td>' + escapeHtml(String(item && item.payment_rate ? item.payment_rate : '-')) + '</td>'
          + '<td>' + escapeHtml(String(item && item.ifsc_code ? item.ifsc_code : '-')) + '</td>'
          + '<td>' + escapeHtml(String(item && item.upi_id ? item.upi_id : '-')) + '</td>'
          + '<td>' + statusChip(item && item.bank_is_active === false ? 'inactive' : 'active') + '</td>'
          + '<td>' + escapeHtml(String(item && item.updated_by ? item.updated_by : '-')) + '</td>'
          + '<td>' + escapeHtml(formatDateTime(item && item.updated_at ? item.updated_at : '')) + '</td>'
          + '<td><button type="button" class="btn-soft bank-edit-btn" data-user-id="' + escapeHtml(id) + '">Edit</button></td>'
          + '</tr>';
      }).join('');
      tbody.innerHTML = rows || '<tr><td colspan="12">No users found.</td></tr>';

      Array.from(contentPanel.querySelectorAll('.bank-edit-btn')).forEach(function (btn) {
        btn.addEventListener('click', function () {
          const userId = String(btn.getAttribute('data-user-id') || '').trim();
          if (!userId) return;
          const target = state.items.find(function (it) { return String(it && it.user_id || '') === userId; });
          if (!target) return;
          openModal(target);
        });
      });
    }

    function renderPager() {
      const total = Math.max(0, Number(state.total || 0));
      const limit = Math.max(1, Number(state.limit || 25));
      const offset = Math.max(0, Number(state.offset || 0));
      const pageNumber = total > 0 ? Math.floor(offset / limit) + 1 : 0;
      const totalPages = total > 0 ? Math.ceil(total / limit) : 0;
      const start = total > 0 ? offset + 1 : 0;
      const end = total > 0 ? Math.min(offset + state.items.length, total) : 0;

      totalEl.textContent = 'Showing ' + start + '-' + end + ' of ' + total + ' users';
      pageInfoEl.textContent = 'Page ' + pageNumber + ' of ' + totalPages;
      pagePrevBtn.disabled = state.isLoading || offset <= 0;
      pageNextBtn.disabled = state.isLoading || (offset + limit >= total);
      pageSizeEl.value = String(limit);
    }

    async function loadData(resetOffset) {
      if (resetOffset) state.offset = 0;
      state.isLoading = true;
      renderPager();
      try {
        const params = [
          'limit=' + encodeURIComponent(String(state.limit)),
          'offset=' + encodeURIComponent(String(state.offset)),
        ];
        const searchTerm = String(state.search || '').trim();
        if (searchTerm) params.push('search=' + encodeURIComponent(searchTerm));

        const payload = await apiFetch('/api/v1/auth/user-bank-details?' + params.join('&'));
        state.items = Array.isArray(payload && payload.items) ? payload.items : [];
        state.total = Number(payload && payload.total ? payload.total : 0);
        state.limit = Number(payload && payload.limit ? payload.limit : state.limit);
        state.offset = Number(payload && payload.offset ? payload.offset : state.offset);

        renderRows(state.items);
      } finally {
        state.isLoading = false;
        renderPager();
      }
    }

    searchEl.addEventListener('input', function () {
      state.search = String(searchEl.value || '').trim();
      if (searchTimer) window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(function () {
        loadData(true);
      }, 350);
    });

    pageSizeEl.addEventListener('change', function () {
      const next = Number(pageSizeEl.value || 25);
      state.limit = next > 0 ? next : 25;
      loadData(true);
    });

    pagePrevBtn.addEventListener('click', function () {
      state.offset = Math.max(0, Number(state.offset || 0) - Number(state.limit || 25));
      loadData(false);
    });

    pageNextBtn.addEventListener('click', function () {
      const nextOffset = Number(state.offset || 0) + Number(state.limit || 25);
      if (nextOffset >= Number(state.total || 0)) return;
      state.offset = nextOffset;
      loadData(false);
    });

    if (ifscVerifyBtn) {
      ifscVerifyBtn.addEventListener('click', async function () {
        const ifscInput = document.getElementById('bank-ifsc');
        const rawIfsc = String((ifscInput && ifscInput.value) || '').toUpperCase().replace(/\s+/g, '').trim();
        if (!rawIfsc) {
          setIfscStatus('Please enter IFSC code first.', true);
          return;
        }

        ifscVerifyBtn.disabled = true;
        setIfscStatus('Verifying IFSC...', false);
        try {
          const verify = await apiFetch('/api/v1/auth/ifsc/verify/' + encodeURIComponent(rawIfsc));
          if (ifscInput) ifscInput.value = String((verify && verify.ifsc_code) || rawIfsc).trim();
          const bankName = String((verify && verify.bank_name) || '').trim();
          const branchName = String((verify && verify.branch_name) || '').trim();
          if (bankName) document.getElementById('bank-name').value = bankName;
          if (branchName) document.getElementById('bank-branch').value = branchName;
          setIfscStatus('IFSC verified: ' + (bankName || '-') + ' / ' + (branchName || '-'), false);
        } catch (err) {
          setIfscStatus(err && err.message ? err.message : 'IFSC verification failed.', true);
        } finally {
          ifscVerifyBtn.disabled = false;
        }
      });
    }

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      const userId = String(state.editingUserId || '').trim();
      if (!userId) {
        setModalMessage('err', 'Please select a user from table first.');
        return;
      }

      const payload = {
        account_holder_name: String(document.getElementById('bank-account-holder').value || '').trim(),
        bank_name: String(document.getElementById('bank-name').value || '').trim(),
        branch_name: String(document.getElementById('bank-branch').value || '').trim(),
        account_number: String(document.getElementById('bank-account-number').value || '').trim(),
        payment_rate: String(document.getElementById('bank-rate').value || '').trim(),
        ifsc_code: String(document.getElementById('bank-ifsc').value || '').trim(),
        upi_id: String(document.getElementById('bank-upi').value || '').trim(),
        notes: String(document.getElementById('bank-notes').value || '').trim(),
        is_active: !!document.getElementById('bank-is-active').checked,
      };

      try {
        if (saveBtn) saveBtn.disabled = true;
        setModalMessage('', 'Saving bank details...');
        await apiFetch('/api/v1/auth/user-bank-details/' + encodeURIComponent(userId), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        await loadData(false);

        const target = state.items.find(function (it) { return String(it && it.user_id || '') === userId; });
        if (target) {
          setFormValues(target);
          const username = String(target.username || '-').trim() || '-';
          const role = String(target.role || '-').trim() || '-';
          modalUserLabelEl.textContent = 'User: ' + username + ' (' + role + ')';
        }

        setModalMessage('ok', 'Bank details saved successfully.');
        setMessage('bank-details-msg', 'ok', 'Bank details updated.');
      } catch (err) {
        setModalMessage('err', err && err.message ? err.message : 'Failed to save bank details.');
      } finally {
        if (saveBtn) saveBtn.disabled = false;
      }
    });

    if (resetBtn) {
      resetBtn.addEventListener('click', function () {
        resetFormToCurrentItem();
      });
    }

    if (closeBtn) {
      closeBtn.addEventListener('click', closeModal);
    }

    if (modalEl) {
      modalEl.addEventListener('click', function (e) {
        if (e.target === modalEl) closeModal();
      });
    }

    await loadData(true);
  }

  function renderChangePassword() {
    contentPanel.innerHTML = '<h2>Change My Password</h2><p class="muted">Policy: minimum 8 chars with uppercase, lowercase and number.</p>'
      + '<p id="change-pass-msg"></p><form id="change-pass-form">'
      + '<div class="form-row"><label>Current Password</label><input type="password" name="current_password" required></div>'
      + '<div class="form-row"><label>New Password</label><input type="password" name="new_password" required></div>'
      + '<div class="form-row"><label>Confirm Password</label><input type="password" name="confirm_password" required></div>'
      + '<button type="submit">Change Password</button></form>';

    document.getElementById("change-pass-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      const payload = Object.fromEntries(new FormData(e.currentTarget).entries());
      try {
        await apiFetch("/api/v1/auth/change-password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        setMessage("change-pass-msg", "ok", "Password changed successfully.");
      } catch (err) {
        setMessage("change-pass-msg", "err", err.message);
      }
    });
  }

  function renderResetUserPassword() {
    contentPanel.innerHTML = '<h2>Reset Any User Password</h2><p class="muted">Super admin only.</p>'
      + '<p id="reset-pass-msg"></p><form id="reset-pass-form">'
      + '<div class="form-row"><label>Username</label><input name="username" required></div>'
      + '<div class="form-row"><label>Role</label><select name="role"><option value="user">User</option><option value="doctor">Doctor</option><option value="super_admin">Super Admin</option><option value="auditor">Auditor</option></select></div>'
      + '<div class="form-row"><label>New Password</label><input type="password" name="new_password" required></div>'
      + '<button type="submit">Reset Password</button></form>';

    document.getElementById("reset-pass-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      const payload = Object.fromEntries(new FormData(e.currentTarget).entries());
      try {
        await apiFetch("/api/v1/auth/users/reset-password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        setMessage("reset-pass-msg", "ok", "Password reset successfully.");
      } catch (err) {
        setMessage("reset-pass-msg", "err", err.message);
      }
    });
  }

  async function renderAssignCases() {
    const doctors = await fetchDoctors();
    const doctorFilterOptions = '<option value="">All Doctors</option>'
      + doctors.map((d) => '<option value="' + escapeHtml(d) + '">' + escapeHtml(d) + '</option>').join('');
    const assignDoctorOptions = '<option value="">Select Doctor</option>'
      + doctors.map((d) => '<option value="' + escapeHtml(d) + '">' + escapeHtml(d) + '</option>').join('');

    const state = {
      page: 1,
      pageSize: 25,
      total: 0,
    };

    contentPanel.innerHTML = '<section class="claim-status-panel">'
      + '<h2 class="claim-status-title">Claim Document Status</h2>'
      + '<form id="claim-status-filter-form" class="claim-status-filters">'
      + '<div class="claim-filter-group"><label for="claim-search">Search Claim</label><input id="claim-search" name="search_claim" placeholder="Claim ID"></div>'
      + '<div class="claim-filter-group"><label for="claim-allotment-date">Allotment Date</label><input id="claim-allotment-date" type="date" name="allotment_date"></div>'
      + '<div class="claim-filter-group"><label for="claim-status-filter">Filter</label><select id="claim-status-filter" name="status_filter">'
      + '<option value="all">All Claims</option>'
      + '<option value="pending">Pending</option>'
      + '<option value="ready_for_assignment">Ready For Assignment</option>'
      + '<option value="waiting_for_documents">Awaiting Documents</option>'
      + '<option value="in_review">In Review</option>'
      + '<option value="needs_qc">Needs QC</option>'
      + '<option value="completed">Completed</option>'
      + '</select></div>'
      + '<div class="claim-filter-group"><label for="claim-doctor-filter">Doctor Filter</label><select id="claim-doctor-filter" name="doctor_filter">' + doctorFilterOptions + '</select></div>'
      + '<div class="claim-filter-action"><button type="submit" class="claim-apply-btn">Apply</button></div>'
      + '</form>'
      + '<div class="claim-status-toolbar">'
      + '<div class="claim-filter-group"><label for="claim-assign-doctor">Assign Doctor</label><select id="claim-assign-doctor" name="assign_doctor">' + assignDoctorOptions + '</select></div>'
      + '<button type="button" id="claim-bulk-assign-btn">Assign Selected Cases</button>'
      + '</div>'
      + '<p id="assign-msg"></p>'
      + '<p class="muted claim-total" id="claim-status-total">Total claims: 0</p>'
      + '<div class="table-wrap claim-status-table-wrap"><table><thead><tr>'
      + '<th class="claim-select-col"><input id="claim-select-all" type="checkbox"></th>'
      + '<th><button type="button" class="table-sort-btn" data-assign-sort="claim_id">Claim ID</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-assign-sort="treatment_type">Treatment Type</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-assign-sort="assigned_doctor">Assigned Doctor</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-assign-sort="allotment_date">Allotment Date</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-assign-sort="status">Status</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-assign-sort="documents">Documents</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-assign-sort="last_upload">Last Upload</button></th>'
      + '<th>Action</th>'
      + '</tr></thead><tbody id="claim-status-tbody"><tr><td colspan="9">Loading...</td></tr></tbody></table></div>'
      + '<div class="claim-pagination">'
      + '<div class="claim-pagination__left"><label for="claim-page-size">Rows</label><select id="claim-page-size"><option value="10">10</option><option value="25" selected>25</option><option value="50">50</option><option value="100">100</option></select></div>'
      + '<div class="claim-pagination__info" id="claim-page-info">Showing 0-0 of 0</div>'
      + '<div class="claim-pagination__actions"><button type="button" class="btn-soft" id="claim-prev-page">Previous</button><button type="button" class="btn-soft" id="claim-next-page">Next</button></div>'
      + '</div>'
      + '</section>';

    const form = document.getElementById('claim-status-filter-form');
    const tbody = document.getElementById('claim-status-tbody');
    const totalEl = document.getElementById('claim-status-total');
    const doctorFilterSelect = document.getElementById('claim-doctor-filter');
    const assignDoctorSelect = document.getElementById('claim-assign-doctor');
    const bulkAssignBtn = document.getElementById('claim-bulk-assign-btn');
    const selectAllEl = document.getElementById('claim-select-all');
    const pageSizeEl = document.getElementById('claim-page-size');
    const prevBtn = document.getElementById('claim-prev-page');
    const nextBtn = document.getElementById('claim-next-page');
    const pageInfoEl = document.getElementById('claim-page-info');
    const assignSortButtons = Array.from(contentPanel.querySelectorAll('button[data-assign-sort]'));
    const assignSortState = { key: 'allotment_date', dir: 'desc' };
    const assignSortConfig = {
      claim_id: { index: 1, type: 'number', defaultDir: 'asc' },
      treatment_type: { index: 2, type: 'text', defaultDir: 'asc' },
      assigned_doctor: { index: 3, type: 'text', defaultDir: 'asc' },
      allotment_date: { index: 4, type: 'date', defaultDir: 'desc' },
      status: { index: 5, type: 'text', defaultDir: 'asc' },
      documents: { index: 6, type: 'number', defaultDir: 'desc' },
      last_upload: { index: 7, type: 'date', defaultDir: 'desc' },
    };

    assignSortButtons.forEach(function (btn) {
      btn.setAttribute('data-sort-label', String(btn.textContent || '').trim());
    });

    function formatAssignedDoctor(value) {
      return String(value || '').split(',').map((s) => s.trim()).filter(Boolean)[0] || '';
    }

    function formatTreatmentType(value) {
      const raw = String(value || '').trim();
      const normalized = raw.toLowerCase();
      if (!normalized) return '';
      if (normalized === 'surgical') return 'Surgical';
      if (normalized === 'non_surgical' || normalized === 'non-surgical' || normalized === 'nonsurgical') return 'Non-Surgical';
      return raw;
    }

    function getSelectedClaimIds() {
      return Array.from(tbody.querySelectorAll('input[data-claim-select]:checked'))
        .map((el) => String(el.value || '').trim())
        .filter(Boolean);
    }

    function updatePaginationUi() {
      const total = Number(state.total || 0);
      const startRow = total === 0 ? 0 : ((state.page - 1) * state.pageSize + 1);
      const endRow = Math.min(state.page * state.pageSize, total);
      pageInfoEl.textContent = 'Showing ' + startRow + '-' + endRow + ' of ' + total;

      prevBtn.disabled = state.page <= 1;
      nextBtn.disabled = endRow >= total;
    }

    function parseAssignSortValue(text, type) {
      const raw = String(text || '').trim();
      if (!raw) return type === 'text' ? '' : 0;
      if (type === 'number') {
        const m = raw.match(/-?\d+(?:\.\d+)?/);
        const n = m ? Number(m[0]) : Number(raw.replace(/[^\d.-]+/g, ''));
        return Number.isFinite(n) ? n : 0;
      }
      if (type === 'date') {
        const ts = Date.parse(raw);
        if (!Number.isNaN(ts)) return ts;
        const m = raw.match(/^(\d{1,2})[-/](\d{1,2})[-/](\d{4})/);
        if (m) return Date.UTC(Number(m[3]), Number(m[2]) - 1, Number(m[1]));
        return 0;
      }
      return raw.toLowerCase();
    }

    function updateAssignSortButtonsUi() {
      assignSortButtons.forEach(function (btn) {
        const key = String(btn.getAttribute('data-assign-sort') || '').trim();
        const baseLabel = String(btn.getAttribute('data-sort-label') || '').trim();
        const active = key && key === assignSortState.key;
        btn.classList.toggle('is-active', !!active);
        btn.textContent = active ? (baseLabel + (assignSortState.dir === 'asc' ? ' ^' : ' v')) : baseLabel;
      });
    }

    function applyAssignSort() {
      const cfg = assignSortConfig[assignSortState.key];
      if (!cfg || !tbody) {
        updateAssignSortButtonsUi();
        return;
      }
      const rows = Array.from(tbody.querySelectorAll('tr')).filter(function (tr) {
        return tr && tr.children && tr.children.length >= 9;
      });
      if (!rows.length) {
        updateAssignSortButtonsUi();
        return;
      }

      const dirMult = assignSortState.dir === 'asc' ? 1 : -1;
      const decorated = rows.map(function (row, idx) {
        const cell = row.children[cfg.index];
        const value = parseAssignSortValue(cell ? cell.textContent : '', cfg.type);
        return { row: row, idx: idx, value: value };
      });

      decorated.sort(function (a, b) {
        let cmp = 0;
        if (typeof a.value === 'number' && typeof b.value === 'number') {
          cmp = a.value === b.value ? 0 : (a.value < b.value ? -1 : 1);
        } else {
          cmp = String(a.value).localeCompare(String(b.value), undefined, { numeric: true, sensitivity: 'base' });
        }
        if (cmp === 0) cmp = a.idx - b.idx;
        return cmp * dirMult;
      });

      decorated.forEach(function (entry) {
        tbody.appendChild(entry.row);
      });
      updateAssignSortButtonsUi();
    }

    async function assignClaims(claimIds) {
      const doctorId = String(assignDoctorSelect.value || '').trim();
      if (!doctorId) {
        setMessage('assign-msg', 'err', 'Select one doctor in Assign Doctor.');
        return;
      }
      if (!claimIds.length) {
        setMessage('assign-msg', 'err', 'Select at least one claim.');
        return;
      }

      let successCount = 0;
      const failed = [];

      for (const claimId of claimIds) {
        try {
          await apiFetch('/api/v1/claims/' + encodeURIComponent(claimId) + '/assign', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ assigned_doctor_id: doctorId, status: 'in_review' }),
          });
          successCount += 1;
        } catch (_err) {
          failed.push(claimId);
        }
      }

      if (!failed.length) {
        setMessage('assign-msg', 'ok', successCount + ' case(s) assigned to ' + doctorId + '.');
      } else {
        setMessage('assign-msg', 'err', successCount + ' assigned, ' + failed.length + ' failed.');
      }

      await loadRows();
    }

    async function loadRows(resetPage) {
      if (resetPage) state.page = 1;

      const searchClaim = String(document.getElementById('claim-search').value || '').trim();
      const allotmentDate = String(document.getElementById('claim-allotment-date').value || '').trim();
      const statusFilter = String(document.getElementById('claim-status-filter').value || 'all').trim();
      const doctorFilter = String(doctorFilterSelect.value || '').trim();

      const params = new URLSearchParams();
      if (searchClaim) params.set('search_claim', searchClaim);
      if (allotmentDate) params.set('allotment_date', allotmentDate);
      if (statusFilter) params.set('status_filter', statusFilter);
      if (doctorFilter) params.set('doctor_filter', doctorFilter);
      params.set('exclude_completed_uploaded', 'true');
      params.set('exclude_withdrawn', 'true');
      params.set('limit', String(state.pageSize));
      params.set('offset', String((state.page - 1) * state.pageSize));

      const result = await apiFetch('/api/v1/user-tools/claim-document-status?' + params.toString());
      state.total = Number(result.total || 0);
      totalEl.textContent = 'Total claims: ' + String(state.total);

      const rows = (result.items || []).map((c) => {
        const assignedDoctor = formatAssignedDoctor(c.assigned_doctor_id);
        const treatmentType = formatTreatmentType(c.treatment_type || c.claim_type);
        return '<tr>'
          + '<td class="claim-select-col"><input type="checkbox" data-claim-select value="' + escapeHtml(c.id) + '"></td>'
          + '<td>' + escapeHtml(c.external_claim_id || '-') + '</td>'
          + '<td>' + escapeHtml(treatmentType || '-') + '</td>'
          + '<td>' + escapeHtml(assignedDoctor || '-') + '</td>'
          + '<td>' + escapeHtml(formatDateOnly(c.allotment_date)) + '</td>'
          + '<td>' + statusChip(formatStatusText(c.status_display || c.status)) + '</td>'
          + '<td>' + escapeHtml(String(c.documents || 0)) + '</td>'
          + '<td>' + escapeHtml(formatDateTime(c.last_upload)) + '</td>'
          + '<td><button type="button" class="btn-soft claim-status-action-btn" data-assign-claim="' + escapeHtml(c.id) + '">Assign</button></td>'
          + '</tr>';
      }).join('');

      tbody.innerHTML = rows || '<tr><td colspan="9">No claims found for selected filter.</td></tr>';
      selectAllEl.checked = false;

      tbody.querySelectorAll('button[data-assign-claim]').forEach((btn) => {
        btn.addEventListener('click', async function () {
          const claimId = String(this.getAttribute('data-assign-claim') || '').trim();
          if (!claimId) return;
          await assignClaims([claimId]);
        });
      });

      applyAssignSort();
      updatePaginationUi();
    }

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      setMessage('assign-msg', '', '');
      try {
        await loadRows(true);
      } catch (err) {
        setMessage('assign-msg', 'err', err.message);
      }
    });

    bulkAssignBtn.addEventListener('click', async function () {
      setMessage('assign-msg', '', '');
      await assignClaims(getSelectedClaimIds());
    });

    selectAllEl.addEventListener('change', function () {
      const checked = !!selectAllEl.checked;
      tbody.querySelectorAll('input[data-claim-select]').forEach((el) => {
        el.checked = checked;
      });
    });

    pageSizeEl.addEventListener('change', async function () {
      state.pageSize = Number(this.value || 25);
      await loadRows(true);
    });

    prevBtn.addEventListener('click', async function () {
      if (state.page <= 1) return;
      state.page -= 1;
      await loadRows(false);
    });

    nextBtn.addEventListener('click', async function () {
      const maxPage = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
      if (state.page >= maxPage) return;
      state.page += 1;
      await loadRows(false);
    });

    assignSortButtons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        const key = String(this.getAttribute('data-assign-sort') || '').trim();
        const cfg = assignSortConfig[key];
        if (!cfg) return;
        if (assignSortState.key === key) {
          assignSortState.dir = assignSortState.dir === 'asc' ? 'desc' : 'asc';
        } else {
          assignSortState.key = key;
          assignSortState.dir = cfg.defaultDir || 'asc';
        }
        applyAssignSort();
      });
    });

    updateAssignSortButtonsUi();

    try {
      await loadRows(false);
    } catch (err) {
      setMessage('assign-msg', 'err', err.message);
      tbody.innerHTML = '<tr><td colspan="9">Failed to load claims.</td></tr>';
      updatePaginationUi();
    }
  }
  function isKycIdentityDocName(value) {
    const name = String(value || '').toLowerCase();
    if (!name) return false;
    return /(aadhar|aadhaar|\bkyc\b|pan\s*card|\bvoter\s*id\b|passport|driving\s*license|\bid\s*proof\b|identity\s*proof|\be-?kyc\b|\bckyc\b|frm[-_ ]?id)/i.test(name);
  }

  function isLikelyOrgName(value) {
    const v = String(value || '').trim();
    if (!v) return false;
    return /(hospital|clinic|diagnostic|laboratory|\blab\b|society|insurance|limited|\bltd\b|llp|plaza)/i.test(v);
  }

  async function runCasePreparationPipeline(claimUuid, actorId, options) {
    const opts = options || {};
    const force = !!opts.force;
    const preferOpenAI = !!opts.preferOpenAI;
    const strictOpenAI = !!opts.strictOpenAI;
    const extractionProviderRaw = String(opts.extractionProvider || 'openai').trim().toLowerCase();
    const extractionProvider = ['openai'].includes(extractionProviderRaw)
      ? extractionProviderRaw
      : 'openai';
    const extractionProviderLabel = extractionProvider === 'openai' ? 'VerifAI' : extractionProvider;
    const allowAutoFallback = false;
    const onProgress = typeof opts.onProgress === 'function' ? opts.onProgress : function () {};
    const onLog = typeof opts.onLog === 'function' ? opts.onLog : function () {};
    const batchThresholdBytes = Number(opts.batchThresholdBytes || (8 * 1024 * 1024));
    const batchSize = Math.max(1, Number(opts.batchSize || 4));

    const docsResult = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/documents?limit=200&offset=0');
    const docs = Array.isArray(docsResult && docsResult.items) ? docsResult.items : [];

    let extractedCount = 0;
    let skippedCount = 0;
    let failedCount = 0;
    let processedCount = 0;

    onLog('Found ' + String(docs.length) + ' documents for processing.');
    onLog('Extraction mode: provider=' + extractionProviderLabel + ', files > ' + formatBytes(batchThresholdBytes) + ' run in single mode, others run in batches of ' + String(batchSize) + ', auto fallback=' + (allowAutoFallback ? 'enabled' : 'disabled') + '.');

    async function processDoc(doc, forceSingleMode) {
      const row = doc || {};
      const docId = String(row.id || '').trim();
      const docName = String(row.file_name || docId || ('Document ' + String(processedCount + 1)));
      const sizeBytes = Number(row.file_size_bytes || 0);
      if (!docId) return;

      processedCount += 1;
      onProgress(processedCount, docs.length, docName);

      if (isKycIdentityDocName(docName)) {
        skippedCount += 1;
        onLog('Skipped KYC/ID document from clinical extraction: ' + docName);
        return;
      }

      if (forceSingleMode) {
        onLog('Single-file mode (>8MB): ' + docName + ' (' + formatBytes(sizeBytes) + ')');
      }

      try {
        if (!force) {
          const existing = await apiFetch('/api/v1/documents/' + encodeURIComponent(docId) + '/extractions?limit=1&offset=0');
          const alreadyExtracted = Number(existing && existing.total ? existing.total : 0) > 0;
          if (alreadyExtracted) {
            skippedCount += 1;
            onLog('Skipped existing extraction: ' + docName);
            return;
          }
        }

        let extracted = false;
        let lastError = null;

        if (extractionProvider !== 'auto') {
          try {
            const extractionResult = await apiFetch('/api/v1/documents/' + encodeURIComponent(docId) + '/extract', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ provider: extractionProvider, actor_id: actorId, force_refresh: !!force }),
            });
            extracted = true;
            const fallbackMeta = extractionResult
              && extractionResult.raw_response
              && extractionResult.raw_response.openai_rate_limit_fallback
              ? extractionResult.raw_response.openai_rate_limit_fallback
              : null;
            if (fallbackMeta) {
              onLog('OpenAI rate-limited; extracted with ' + String(fallbackMeta.used_fallback_provider || 'fallback') + ': ' + docName);
            } else {
              onLog('Extracted with ' + extractionProviderLabel + ': ' + docName);
            }
          } catch (err) {
            lastError = err;
            onLog(extractionProviderLabel + ' extraction failed for: ' + docName + ' (' + String(err && err.message ? err.message : err) + ')');
          }
        }

        if (!extracted && force && preferOpenAI && extractionProvider === 'auto') {
          try {
            const openaiExtract = await apiFetch('/api/v1/documents/' + encodeURIComponent(docId) + '/extract', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ provider: 'openai', actor_id: actorId, force_refresh: !!force }),
            });
            extracted = true;
            onLog('Extracted with VerifAI: ' + docName);
            if (openaiExtract && openaiExtract.raw_response) {
              const rawJson = JSON.stringify(openaiExtract.raw_response);
              onLog('VerifAI JSON response: ' + String(rawJson.length > 4000 ? rawJson.slice(0, 4000) + ' ...[truncated]' : rawJson));
            }
          } catch (err) {
            lastError = err;
            if (strictOpenAI) {
              onLog('VerifAI extraction failed (strict mode): ' + docName + ' (' + String(err && err.message ? err.message : err) + ')');
            } else {
              onLog('VerifAI extraction fallback to auto for: ' + docName + ' (' + String(err && err.message ? err.message : err) + ')');
            }
          }
        }

        if (!extracted && allowAutoFallback && !(strictOpenAI && force && preferOpenAI && extractionProvider === 'auto')) {
          try {
            await apiFetch('/api/v1/documents/' + encodeURIComponent(docId) + '/extract', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ provider: 'auto', actor_id: actorId, force_refresh: !!force }),
            });
            extracted = true;
            onLog('Extracted with auto pipeline: ' + docName);
          } catch (err) {
            lastError = err;
          }
        }

        if (extracted) {
          extractedCount += 1;
        } else {
          failedCount += 1;
          onLog('Extraction failed: ' + docName + ' (' + String(lastError && lastError.message ? lastError.message : lastError) + ')');
        }
      } catch (err) {
        failedCount += 1;
        onLog('Processing failed: ' + docName + ' (' + String(err && err.message ? err.message : err) + ')');
      }
    }

    async function flushBatch(batchDocs) {
      if (!batchDocs.length) return;
      await Promise.all(batchDocs.map(function (doc) {
        return processDoc(doc, false);
      }));
    }

    let pendingBatch = [];
    for (let idx = 0; idx < docs.length; idx += 1) {
      const doc = docs[idx] || {};
      const sizeBytes = Number(doc.file_size_bytes || 0);
      const isLarge = Number.isFinite(sizeBytes) && sizeBytes > batchThresholdBytes;

      if (isLarge) {
        await flushBatch(pendingBatch);
        pendingBatch = [];
        await processDoc(doc, true);
      } else {
        pendingBatch.push(doc);
        if (pendingBatch.length >= batchSize) {
          const currentBatch = pendingBatch;
          pendingBatch = [];
          await flushBatch(currentBatch);
        }
      }
    }
    await flushBatch(pendingBatch);

    let checklist = null;
    let checklistError = null;
    try {
      checklist = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/checklist/evaluate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ actor_id: actorId, force_source_refresh: !!force }),
      });
      onLog('Checklist evaluation completed. Recommendation: ' + String((checklist && checklist.recommendation) || 'unknown'));
      const mergedReviewMeta = checklist && checklist.source_summary && checklist.source_summary.openai_merged_review
        ? checklist.source_summary.openai_merged_review
        : null;
      if (mergedReviewMeta && mergedReviewMeta.used) {
        onLog(
          'Merged AI medical-audit review applied. Admission required: '
            + String(mergedReviewMeta.admission_required || 'uncertain')
            + ', confidence: ' + String(mergedReviewMeta.confidence || 0)
        );
      } else if (mergedReviewMeta && mergedReviewMeta.error) {
        onLog('Merged AI medical-audit review fallback: ' + String(mergedReviewMeta.error));
      }
    } catch (err) {
      checklistError = err;
      onLog('Checklist evaluation failed: ' + String(err && err.message ? err.message : err));
    }

    return {
      docsTotal: docs.length,
      extractedCount,
      skippedCount,
      failedCount,
      checklist,
      checklistError,
    };
  }
  async function renderDoctorAssignedCases() {
    const state = { page: 1, pageSize: 20, total: 0 };
    const me = await apiFetch('/api/v1/auth/me');

    contentPanel.innerHTML = '<section class="claim-status-panel">'
      + '<h2 class="claim-status-title">Assigned Cases</h2>'
      + '<p class="muted doctor-assigned-rule">Showing assigned cases excluding completed and withdrawn status.</p>'
      + '<form id="doctor-assigned-filter-form" class="claim-status-filters doctor-assigned-filters">'
      + '<div class="claim-filter-group"><label for="doctor-claim-search">Search Claim</label><input id="doctor-claim-search" name="search_claim" placeholder="Claim ID"></div>'
      + '<div class="claim-filter-group"><label for="doctor-allotment-date">Allotment Date</label><input id="doctor-allotment-date" type="date" name="allotment_date"></div>'
      + '<div class="claim-filter-action"><button type="submit" class="claim-apply-btn">Apply</button></div>'
      + '</form>'
      + '<p id="doctor-assigned-msg"></p>'
      + '<p class="muted claim-total" id="doctor-assigned-total">Total claims: 0</p>'
      + '<div class="table-wrap claim-status-table-wrap"><table><thead><tr>'
      + '<th>Claim ID</th><th>Treatment Type</th><th>Status</th><th>Documents</th><th>Last Upload</th><th>Assigned At</th><th>Allotment Date</th><th>Final Status</th><th>Action</th>'
      + '</tr></thead><tbody id="doctor-assigned-tbody"><tr><td colspan="9">Loading...</td></tr></tbody></table></div>'
      + '<div class="claim-pagination">'
      + '<div class="claim-pagination__left"><label for="doctor-page-size">Rows</label><select id="doctor-page-size"><option value="10">10</option><option value="20" selected>20</option><option value="50">50</option></select></div>'
      + '<div class="claim-pagination__info" id="doctor-page-info">Showing 0-0 of 0</div>'
      + '<div class="claim-pagination__actions"><button type="button" class="btn-soft" id="doctor-prev-page">Previous</button><button type="button" class="btn-soft" id="doctor-next-page">Next</button></div>'
      + '</div>'

      + '</section>';

    const form = document.getElementById('doctor-assigned-filter-form');
    const tbody = document.getElementById('doctor-assigned-tbody');
    const totalEl = document.getElementById('doctor-assigned-total');
    const pageSizeEl = document.getElementById('doctor-page-size');
    const prevBtn = document.getElementById('doctor-prev-page');
    const nextBtn = document.getElementById('doctor-next-page');
    const pageInfoEl = document.getElementById('doctor-page-info');

    function updatePaginationUi() {
      const total = Number(state.total || 0);
      const startRow = total === 0 ? 0 : ((state.page - 1) * state.pageSize + 1);
      const endRow = Math.min(state.page * state.pageSize, total);
      pageInfoEl.textContent = 'Showing ' + startRow + '-' + endRow + ' of ' + total;
      prevBtn.disabled = state.page <= 1;
      nextBtn.disabled = endRow >= total;
    }

    function renderFinalStatus(raw) {
      const normalized = String(raw || 'Pending').replace(/<br\s*\/?\s*>/gi, '\n');
      return escapeHtml(normalized).replace(/\n/g, '<br>');
    }
    async function openCaseDetail(claimUuid, externalClaimId, searchClaim, allotmentDate, triggerButton) {
      const claimId = String(externalClaimId || '').trim();
      const claimKey = String(claimUuid || '').trim();
      if (!claimKey) {
        setMessage('doctor-assigned-msg', 'err', 'Claim key not available for opening detail page.');
        return;
      }

      const detailParams = new URLSearchParams();
      detailParams.set('claim_uuid', claimKey);
      detailParams.set('claim_id', claimId);
      detailParams.set('search_claim', String(searchClaim || ''));
      detailParams.set('allotment_date', String(allotmentDate || ''));
      const detailUrl = '/qc/doctor/case-detail?' + detailParams.toString();

      if (triggerButton) {
        triggerButton.disabled = true;
        triggerButton.textContent = 'Opening...';
      }
      window.location.href = detailUrl;
    }
    async function pickDoctorStatus(defaultValue) {
      return await new Promise((resolve) => {
        const backdrop = document.createElement('div');
        backdrop.className = 'modal-backdrop open';
        backdrop.innerHTML = ''
          + '<div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="doctor-status-modal-title">'
          + '<div class="modal-header"><h3 id="doctor-status-modal-title">Change Status</h3></div>'
          + '<p class="muted">Select new status.</p>'
          + '<div class="form-row"><label for="doctor-status-select">Status</label>'
          + '<select id="doctor-status-select"><option value="pending">Pending</option><option value="completed">Completed</option></select></div>'
          + '<div class="link-row"><button type="button" id="doctor-status-save">Save</button><button type="button" class="btn-soft" id="doctor-status-cancel">Cancel</button></div>'
          + '</div>';
        document.body.appendChild(backdrop);

        const selectEl = backdrop.querySelector('#doctor-status-select');
        const saveBtn = backdrop.querySelector('#doctor-status-save');
        const cancelBtn = backdrop.querySelector('#doctor-status-cancel');
        const fallbackValue = defaultValue === 'completed' ? 'completed' : 'pending';
        if (selectEl) {
          selectEl.value = fallbackValue;
          selectEl.focus();
        }

        let closed = false;
        function closeWith(value) {
          if (closed) return;
          closed = true;
          backdrop.remove();
          resolve(value);
        }

        if (saveBtn) {
          saveBtn.addEventListener('click', function () {
            closeWith(String(selectEl && selectEl.value ? selectEl.value : fallbackValue));
          });
        }
        if (cancelBtn) {
          cancelBtn.addEventListener('click', function () {
            closeWith('');
          });
        }
        backdrop.addEventListener('click', function (event) {
          if (event.target === backdrop) closeWith('');
        });
        backdrop.addEventListener('keydown', function (event) {
          if (event.key === 'Escape') {
            event.preventDefault();
            closeWith('');
          }
          if (event.key === 'Enter') {
            event.preventDefault();
            closeWith(String(selectEl && selectEl.value ? selectEl.value : fallbackValue));
          }
        });
      });
    }

    async function changeStatus(claimId, currentStatusRaw) {
      const currentStatus = String(currentStatusRaw || '').trim().toLowerCase();
      const defaultStatus = currentStatus === 'completed' ? 'completed' : 'pending';
      const selectedStatus = await pickDoctorStatus(defaultStatus);
      if (!selectedStatus) return;

      const statusPayload = selectedStatus === 'completed' ? 'completed' : 'in_review';
      try {
        await apiFetch('/api/v1/claims/' + encodeURIComponent(claimId) + '/status', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: statusPayload }),
        });
        setMessage('doctor-assigned-msg', 'ok', 'Status updated to ' + formatStatusText(selectedStatus) + '.');
        await loadRows(false);
      } catch (err) {
        setMessage('doctor-assigned-msg', 'err', err.message);
      }
    }

    async function loadRows(resetPage) {
      if (resetPage) state.page = 1;

      const searchClaim = String(document.getElementById('doctor-claim-search').value || '').trim();
      const allotmentDate = String(document.getElementById('doctor-allotment-date').value || '').trim();

      const params = new URLSearchParams();
      if (searchClaim) params.set('search_claim', searchClaim);
      if (allotmentDate) params.set('allotment_date', allotmentDate);
      params.set('status_filter', 'all');
      params.set('exclude_completed', 'true');
      params.set('exclude_withdrawn', 'true');
      params.set('exclude_tagged', 'true');
      if (me && me.username) params.set('doctor_filter', me.username);
      params.set('sort_order', 'asc');
      params.set('limit', String(state.pageSize));
      params.set('offset', String((state.page - 1) * state.pageSize));

      const result = await apiFetch('/api/v1/user-tools/claim-document-status?' + params.toString());
      state.total = Number(result.total || 0);
      totalEl.textContent = 'Total claims: ' + String(state.total);

      const sortedItems = sortClaimsByAllotmentDateFirst((result.items || []), true);
      const rows = sortedItems.map((c) => {
        const treatmentType = resolveTreatmentType(c);
        return '<tr>'
          + '<td>' + escapeHtml(c.external_claim_id || '-') + '</td>'
          + '<td>' + escapeHtml(treatmentType || '-') + '</td>'
          + '<td>' + statusChip(formatStatusText(c.status_display || c.status)) + '</td>'
          + '<td>' + escapeHtml(String(c.documents || 0)) + '</td>'
          + '<td>' + escapeHtml(formatDateTime(c.last_upload)) + '</td>'
          + '<td>' + escapeHtml(formatDateTime(c.assigned_at)) + '</td>'
          + '<td>' + escapeHtml(formatDateOnly(c.allotment_date)) + '</td>'
          + '<td class="doctor-final-status">' + renderFinalStatus(c.final_status) + '</td>'
          + '<td><div class="doctor-case-actions">'
          + '<button type="button" class="btn-soft" data-open-case="' + escapeHtml(c.external_claim_id || '') + '" data-open-claim-id="' + escapeHtml(c.id || '') + '">Open Case</button>'
          + '<button type="button" class="btn-soft" data-change-status="' + escapeHtml(c.id) + '" data-current-status="' + escapeHtml(c.status || '') + '">Change Status</button>'
          + '</div></td>'
          + '</tr>';
      }).join('');

      tbody.innerHTML = rows || '<tr><td colspan="9">No assigned claims found.</td></tr>';

      tbody.querySelectorAll('button[data-open-case]').forEach((btn) => {
        btn.addEventListener('click', async function () {
          await openCaseDetail(
            String(this.getAttribute('data-open-claim-id') || ''),
            String(this.getAttribute('data-open-case') || ''),
            searchClaim,
            allotmentDate,
            this
          );
        });
      });

      tbody.querySelectorAll('button[data-change-status]').forEach((btn) => {
        btn.addEventListener('click', async function () {
          await changeStatus(
            String(this.getAttribute('data-change-status') || ''),
            String(this.getAttribute('data-current-status') || '')
          );
        });
      });

      updatePaginationUi();
    }

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      setMessage('doctor-assigned-msg', '', '');
      try {
        await loadRows(true);
      } catch (err) {
        setMessage('doctor-assigned-msg', 'err', err.message);
      }
    });

    pageSizeEl.addEventListener('change', async function () {
      state.pageSize = Number(this.value || 20);
      await loadRows(true);
    });

    prevBtn.addEventListener('click', async function () {
      if (state.page <= 1) return;
      state.page -= 1;
      await loadRows(false);
    });

    nextBtn.addEventListener('click', async function () {
      const maxPage = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
      if (state.page >= maxPage) return;
      state.page += 1;
      await loadRows(false);
    });

    try {
      await loadRows(true);
    } catch (err) {
      setMessage('doctor-assigned-msg', 'err', err.message);
      tbody.innerHTML = '<tr><td colspan="8">Failed to load assigned cases.</td></tr>';
      updatePaginationUi();
    }
  }
  async function renderDoctorCaseDetail() {
    const me = await apiFetch('/api/v1/auth/me');
    const routeParams = new URLSearchParams(window.location.search);
    const claimUuid = String(routeParams.get('claim_uuid') || '').trim();
    const routeClaimId = String(routeParams.get('claim_id') || '').trim();
    const backSearchClaim = String(routeParams.get('search_claim') || '').trim();
    const backAllotmentDate = String(routeParams.get('allotment_date') || '').trim();
    const backPageParam = String(routeParams.get('back_page') || '').trim();
    const routeInfo = parseRoute();
    const activeRouteRole = String((routeInfo && routeInfo.routeRole) || (me && me.role) || 'doctor').trim();
    const isAuditorRole = activeRouteRole === 'auditor' || !!(me && me.role === 'auditor');
    const backPage = backPageParam || (activeRouteRole === 'user' ? 'upload-document' : (isAuditorRole ? 'audit-claims' : 'assigned-cases'));
    const preferredReportSourceParam = String(routeParams.get('report_source') || 'doctor').trim().toLowerCase();
    let preferredReportSource = (preferredReportSourceParam === 'system' || preferredReportSourceParam === 'doctor') ? preferredReportSourceParam : 'doctor';

    if (!claimUuid) {
      contentPanel.innerHTML = '<section class="claim-status-panel">'
        + '<h2 class="claim-status-title">Case Detail</h2>'
        + '<p class="msg err">Missing claim id in URL. Open a case from Assigned Cases.</p>'
        + '<div class="link-row"><a class="btn-soft" href="/qc/' + escapeHtml(activeRouteRole) + '/' + escapeHtml(backPage) + '">Back</a></div>'
        + '</section>';
      return;
    }

    contentPanel.innerHTML = '<section class="claim-status-panel case-detail-panel">'
      + '<div class="case-detail-header">'
      + '<div><h2 class="claim-status-title">Case Detail</h2><p class="muted">Detailed view with extraction, AI analysis and document list.</p></div>'
      + '<div class="case-detail-header__actions">'
      + '<a class="btn-soft" href="/qc/' + escapeHtml(activeRouteRole) + '/' + escapeHtml(backPage) + '">Back</a>'
      + '</div></div>'
      + '<p id="case-detail-msg"></p>'
      + '<div class="table-wrap claim-status-table-wrap"><table><tbody id="case-detail-summary"></tbody></table></div>'
      + '<h3 style="margin-top:16px;">Case Documents</h3>'
      + '<div class="link-row case-detail-actions">'
      + '<button type="button" id="case-analyze-ai">Analyze Admission Need (VerifAI)</button>'
      + '<button type="button" class="btn-soft" id="case-force-ai">Force VerifAI Analyzer</button>'
      + '<button type="button" class="btn-soft" id="case-diagnosis-checklist">Generate Diagnosis Checklist</button>'
      + '<button type="button" id="case-generate-report">Generate Report</button>'
      + '<button type="button" class="btn-soft" id="case-save-report">Save Report</button>'
      + '<button type="button" class="btn-soft" id="case-change-status">Mark Completed</button>'
      + '<button type="button" class="btn-soft" id="case-send-back">Send Back To Doctor</button>'
      + '</div>'
      + '<div id="case-diagnosis-checklist-result" class="case-diagnosis-checklist-result muted">Diagnosis checklist is not generated yet.</div>'
      + '<div class="table-wrap view-documents-table-wrap"><table><thead><tr><th>File Name</th><th>Parse Status</th><th>Uploaded By</th><th>Uploaded At</th><th>Action</th></tr></thead><tbody id="case-detail-docs"></tbody></table></div>'

      + '<h3 style="margin-top:16px;">Generated Report</h3>'
      + '<div id="case-generated-report" class="case-generated-report muted">Click Generate Report to build latest report.</div>'
      + '<h3 style="margin-top:16px;">Batch Job Log</h3>'
      + '<pre id="case-detail-log" class="case-detail-log">Ready.</pre>'
      + '</section>'
      + '<section id="case-report-full-view" class="claim-status-panel case-report-full-view" style="display:none;">'
      + '<div class="case-detail-header">'
      + '<div><h2 class="claim-status-title">Full Report</h2><p class="muted">Full claim investigation report in same screen.</p></div>'
      + '<div class="case-detail-header__actions">'
      + '<button type="button" class="btn-soft" id="case-report-back">Back to Case Detail</button>'
      + '<button type="button" id="case-save-report-full">Save Report</button>'
      + '</div></div>'
      + '<div id="case-report-full-body" class="case-report-full-body"></div>'
      + '</section>';

    const summaryEl = document.getElementById('case-detail-summary');
    const docsEl = document.getElementById('case-detail-docs');
    const generatedReportEl = document.getElementById('case-generated-report');
    const logEl = document.getElementById('case-detail-log');
    const fullReportViewEl = document.getElementById('case-report-full-view');
    const fullReportBodyEl = document.getElementById('case-report-full-body');

    const analyzeBtn = document.getElementById('case-analyze-ai');
    const forceBtn = document.getElementById('case-force-ai');
    const diagnosisChecklistBtn = document.getElementById('case-diagnosis-checklist');
    const diagnosisChecklistResultEl = document.getElementById('case-diagnosis-checklist-result');
    const reportBtn = document.getElementById('case-generate-report');
    const saveReportBtn = document.getElementById('case-save-report');
    const saveReportFullBtn = document.getElementById('case-save-report-full');
    const backFromFullBtn = document.getElementById('case-report-back');
    const statusBtn = document.getElementById('case-change-status');
    const sendBackBtn = document.getElementById('case-send-back');
    const actionButtons = [analyzeBtn, forceBtn, diagnosisChecklistBtn, reportBtn, saveReportBtn, saveReportFullBtn, statusBtn, sendBackBtn].filter(Boolean);

    if (isAuditorRole) {
      [analyzeBtn, forceBtn, diagnosisChecklistBtn, reportBtn, saveReportBtn, saveReportFullBtn, statusBtn].forEach(function (btn) {
        if (btn) btn.remove();
      });
      if (diagnosisChecklistResultEl) diagnosisChecklistResultEl.remove();
    } else if (sendBackBtn) {
      sendBackBtn.remove();
    }

    function appendLog(text) {
      const ts = new Date().toLocaleTimeString();
      const line = '[' + ts + '] ' + String(text || '');
      if (!logEl.textContent || logEl.textContent === 'Ready.') {
        logEl.textContent = line;
      } else {
        logEl.textContent += '\n' + line;
      }
      logEl.scrollTop = logEl.scrollHeight;
    }

    function setActionDisabled(disabled) {
      actionButtons.forEach((btn) => {
        if (btn) btn.disabled = !!disabled;
      });
    }

    function renderDiagnosisChecklistResult(payload) {
      if (!diagnosisChecklistResultEl) return;
      if (!payload || typeof payload !== 'object') {
        diagnosisChecklistResultEl.className = 'case-diagnosis-checklist-result muted';
        diagnosisChecklistResultEl.textContent = 'Diagnosis checklist is not generated yet.';
        return;
      }

      const symptoms = Array.isArray(payload.symptoms) ? payload.symptoms.filter(Boolean) : [];
      const drugChoices = Array.isArray(payload.drug_choices) ? payload.drug_choices.filter(Boolean) : [];
      const investigations = Array.isArray(payload.investigation_findings) ? payload.investigation_findings.filter(Boolean) : [];
      const references = Array.isArray(payload.references) ? payload.references : [];
      const sourceText = String(payload.source || '').trim() || 'unknown';
      const modelText = String(payload.model_name || '').trim();
      const cacheLabel = payload.from_cache ? 'cached' : 'fresh';
      const diagnosisLabel = String(payload.diagnosis_name || '-').trim() || '-';
      const cautionNotes = String(payload.caution_notes || '').trim();

      function renderList(items, emptyText) {
        if (!Array.isArray(items) || items.length === 0) {
          return '<li class="muted">' + escapeHtml(emptyText) + '</li>';
        }
        return items.slice(0, 12).map(function (item) {
          return '<li>' + escapeHtml(String(item || '')) + '</li>';
        }).join('');
      }

      function renderReferences(refs) {
        if (!Array.isArray(refs) || refs.length === 0) {
          return '<li class="muted">No references provided</li>';
        }
        return refs.slice(0, 8).map(function (ref) {
          if (!ref || typeof ref !== 'object') return '';
          const title = String(ref.title || ref.url || 'Reference').trim();
          const url = String(ref.url || '').trim();
          if (!url || !/^https?:\/\//i.test(url)) {
            return '<li>' + escapeHtml(title) + '</li>';
          }
          return '<li><a href="' + escapeHtml(url) + '" target="_blank" rel="noopener">' + escapeHtml(title) + '</a></li>';
        }).filter(Boolean).join('') || '<li class="muted">No references provided</li>';
      }

      diagnosisChecklistResultEl.className = 'case-diagnosis-checklist-result';
      diagnosisChecklistResultEl.innerHTML = '<div class="case-diagnosis-checklist-result__head">'
        + '<strong>Diagnosis Checklist: ' + escapeHtml(diagnosisLabel) + '</strong>'
        + '<span class="muted">Source: ' + escapeHtml(sourceText) + ' (' + escapeHtml(cacheLabel) + ')' + (modelText ? (' | Model: ' + escapeHtml(modelText)) : '') + '</span>'
        + '</div>'
        + '<div class="case-diagnosis-checklist-result__grid">'
        + '<section><h4>Symptoms</h4><ul>' + renderList(symptoms, 'No symptom points available') + '</ul></section>'
        + '<section><h4>Drug Choices</h4><ul>' + renderList(drugChoices, 'No drug choices available') + '</ul></section>'
        + '<section><h4>Likely Investigation Findings</h4><ul>' + renderList(investigations, 'No investigation points available') + '</ul></section>'
        + '<section><h4>References</h4><ul>' + renderReferences(references) + '</ul></section>'
        + '</div>'
        + (cautionNotes ? ('<p class="muted">' + escapeHtml(cautionNotes) + '</p>') : '');
    }

    async function resolveDiagnosisForChecklist() {
      const claimIdText = String((currentClaim && currentClaim.id) || claimUuid || '').trim();
      if (!claimIdText) return '';
      try {
        const structured = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimIdText) + '/structured-data?auto_generate=true&use_llm=true');
        const diagnosis = sanitizeReportText((structured && structured.diagnosis) || '');
        if (diagnosis && diagnosis !== '-') return diagnosis;
      } catch (_err) {
        // ignore and fallback to manual input
      }
      return '';
    }

    async function runDiagnosisChecklistGeneration() {
      if (!diagnosisChecklistBtn) return;
      setActionDisabled(true);
      setMessage('case-detail-msg', '', 'Generating diagnosis checklist...');
      appendLog('Diagnosis checklist generation started.');
      try {
        let diagnosisText = await resolveDiagnosisForChecklist();
        if (!diagnosisText) {
          diagnosisText = String(window.prompt('Enter diagnosis for checklist generation:', '') || '').trim();
        }
        if (!diagnosisText) {
          setMessage('case-detail-msg', 'err', 'Diagnosis is required for checklist generation.');
          appendLog('Diagnosis checklist generation skipped: diagnosis missing.');
          return;
        }

        const payload = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/checklist/diagnosis-template', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            diagnosis: diagnosisText,
            actor_id: (me && me.username) ? me.username : 'doctor-ui',
            force_refresh: false,
          }),
        });
        renderDiagnosisChecklistResult(payload);
        const cacheText = payload && payload.from_cache ? 'loaded from DB cache' : 'generated and saved to DB';
        setMessage('case-detail-msg', 'ok', 'Diagnosis checklist ' + cacheText + '.');
        appendLog('Diagnosis checklist ready for ' + String((payload && payload.diagnosis_name) || diagnosisText) + ' (' + cacheText + ').');
      } catch (err) {
        setMessage('case-detail-msg', 'err', err.message || 'Diagnosis checklist generation failed.');
        appendLog('Diagnosis checklist generation failed: ' + String(err && err.message ? err.message : err));
      } finally {
        setActionDisabled(false);
      }
    }

    function summaryRow(label, value) {
      return '<tr><th class="case-detail-key">' + escapeHtml(label) + '</th><td>' + value + '</td></tr>';
    }

    function asTextCell(value) {
      const normalized = String(value == null || value === '' ? '-' : value).replace(/<br\s*\/?\s*>/gi, '\n');
      return escapeHtml(normalized).replace(/\n/g, '<br>');
    }

    function stripHtmlTags(value) {
      return String(value == null ? '' : value).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
    }

    function isJsonLikeText(value) {
      const t = String(value == null ? '' : value).trim();
      if (!t) return false;
      if ((t[0] === '{' && t[t.length - 1] === '}') || (t[0] === '[' && t[t.length - 1] === ']')) return true;
      if (t.indexOf('\"extracted_entities\"') >= 0 || t.indexOf('\"evidence_refs\"') >= 0 || t.indexOf('\"raw_response\"') >= 0) return true;
      if ((t.indexOf('{') >= 0 || t.indexOf('[') >= 0) && /\"[A-Za-z0-9_\- ]+\"\s*:/.test(t)) return true;
      return false;
    }

    function sanitizeReportText(value) {
      const raw = String(value == null ? '' : value).replace(/\r/g, '\n');
      const seen = new Set();
      const cleanedLines = raw.split('\n').map(function (line) {
        const t = String(line || '').trim();
        if (!t) return '';
        if (isJsonLikeText(t)) return '';
        if (/[{}\[\]]/.test(t) && /\"[^\"]+\"\s*:/.test(t)) return '';

        // Remove storage-path/log noise like:
        // /proclaim-20260106/xxx.pdf-7088-17.02.2026 08:53:19
        if (/^\/[A-Za-z0-9._\/-]+\.(pdf|jpg|jpeg|png|tif|tiff)(?:-[0-9]+)?(?:-\d{1,2}[.\/-]\d{1,2}[.\/-]\d{2,4}\s+\d{1,2}:\d{2}(?::\d{2})?)?$/i.test(t)) return '';
        if (/^s3:\/\//i.test(t)) return '';
        if (/^(?:https?:\/\/)?[^\s]+\.(pdf|jpg|jpeg|png|tif|tiff)(?:\?.*)?$/i.test(t)) return '';

        if (seen.has(t)) return '';
        seen.add(t);
        return t;
      }).filter(Boolean);
      return cleanedLines.join('\n').trim();
    }

    function sanitizeConclusionText(value) {
      const base = sanitizeReportText(value);
      if (!base) return '';
      const seen = new Set();
      const lines = base.split('\n').map(function (line) {
        const t = String(line || '').trim();
        if (!t) return '';

        // Drop JSON fragment lines leaking from raw extraction payloads.
        if (/^[\{\}\[\]],?$/.test(t)) return '';
        if (/^"[^"]+"\s*:\s*.*,?$/.test(t)) return '';
        if (/^(?:type|field|snippet|confidence|raw_response|analysis_snapshot|extracted_entities|evidence_refs)\s*:/i.test(t)) return '';
        if (/"(?:type|field|snippet|confidence|raw_response|analysis_snapshot|extracted_entities|evidence_refs)"\s*:/i.test(t)) return '';
        if (/[{}\[\]]/.test(t) && /"[^"]+"\s*:/.test(t)) return '';
        if (/bill\s*amount\s*:\s*.*\|\s*investigations\s*:/i.test(t)) return '';
        if (/^\s*bill\s*amount\s*:/i.test(t)) return '';
        if (/^(?:reject|query|approve)\s+triggers\s*:/i.test(t)) return '';
        if (/\bml\s*signal\s*:/i.test(t)) return '';
        if (/^r\d{3}(?:\s*,\s*r\d{3})*$/i.test(t)) return '';

        if (seen.has(t)) return '';
        seen.add(t);
        return t;
      }).filter(Boolean);
      return lines.join('\n').trim();
    }

    function isWeakConclusionText(value) {
      const t = sanitizeConclusionText(value).toLowerCase();
      if (!t) return true;
      if (t.length < 55) return true;
      if (/^(surgery\s*\/\s*procedure\s*performed|details\s+of\s+medication\s+administered|ipd\s+medicine\s+bill|patient\s+name|hospital\b|admitting\s+dr\b|total\s*[:-])/i.test(t)) return true;
      if (/^(admission\s+date|discharge\s+date|claim\s+no\.?|company\s+name)\b/i.test(t)) return true;
      if (/^(?:openai_(?:claim_rules|diagnosis_criteria)\s+(?:reject|query|approve|admissible|inadmissible)\s*)+$/i.test(t)) return true;
      if (/^based on available records, inpatient admission from /i.test(t) && /medicine evidence:|daily tpr summary:/i.test(t)) return true;
      if (/therefore, admission and treatment are not justified with current evidence\.?$/i.test(t)) return true;
      return false;
    }

    function summarizeForConclusion(value, maxLen) {
      const t = sanitizeConclusionText(value);
      if (!t || t === '-') return '';
      const limit = Number(maxLen || 260);
      if (!Number.isFinite(limit) || limit <= 20) return t;
      return t.length > limit ? (t.slice(0, limit - 3).trim() + '...') : t;
    }

    function normalizeConclusionCompare(value) {
      return sanitizeConclusionText(value)
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, ' ')
        .trim();
    }

    function mergeRuleWithLearning(ruleText, learningText, prefix, maxLen) {
      const rule = sanitizeConclusionText(ruleText);
      const learning = sanitizeConclusionText(learningText);
      if (!rule) return learning;
      if (!learning) return rule;

      const ruleNorm = normalizeConclusionCompare(rule);
      const learningNorm = normalizeConclusionCompare(learning);
      if (!ruleNorm || !learningNorm) return rule;
      if (ruleNorm === learningNorm) return rule;
      if (ruleNorm.indexOf(learningNorm) >= 0 || learningNorm.indexOf(ruleNorm) >= 0) return rule;

      const learningShort = summarizeForConclusion(learning, maxLen || 220);
      if (!learningShort) return rule;

      const label = sanitizeReportText(prefix || '').trim();
      if (!label) {
        const normalizedLearning = learningShort.replace(/^(?:auditor\s+learning|learning\s+update|learning\s+note)\s*[:\-]\s*/i, '').trim();
        if (!normalizedLearning) return rule;
        const joiner = /[.!?]$/.test(rule) ? ' ' : '. ';
        return rule + joiner + normalizedLearning;
      }

      return rule + '\n' + label + ': ' + learningShort;
    }

    function normalizeDoctorNameCandidate(value) {
      const raw = sanitizeReportText(value);
      if (!raw || raw === '-') return '';

      let text = String(raw).split('\n')[0].trim();
      if (!text) return '';
      text = text.replace(/^(?:treating\s*doctor(?:\s*name)?|consult(?:ant|ing)\s*doctor|attending\s*doctor|doctor(?:\s*name)?)\s*[:\-]\s*/i, '');

      const drMatch = text.match(/\bDr\.?\s*[A-Za-z][A-Za-z .'-]{1,80}/i);
      if (drMatch) text = String(drMatch[0] || '').trim();

      text = text
        .replace(/\s*\b(?:reg(?:istration)?(?:\s*no|\s*number)?|mci|nmc|dmc|medical council)\b[\s:.\-#]*[A-Za-z0-9\/\-]+.*$/i, '')
        .replace(/\s{2,}/g, ' ')
        .trim();

      if (!text) return '';
      if (text.length < 3 || text.length > 90) return '';
      if (isLikelyDoctorRegNo(text)) return '';
      if (/\b(?:hospital|clinic|nursing\s*home|medical\s*centre|medical\s*center|diagnostic|laboratory)\b/i.test(text)) return '';
      return text;
    }

    function extractTreatingDoctorName(extractionPairs) {
      const exactAliases = [
        'treating_doctor',
        'treating_doctor_name',
        'doctor_name',
        'attending_doctor',
        'consultant_doctor',
        'consulting_doctor',
        'primary_doctor'
      ].map(normalizeKey);
      const candidates = [];
      (Array.isArray(extractionPairs) ? extractionPairs : []).forEach(function (pair) {
        const keyNorm = normalizeKey(pair && pair.key ? pair.key : '');
        if (!keyNorm || !exactAliases.includes(keyNorm)) return;
        const textValue = sanitizeReportText(pair && pair.value ? pair.value : '');
        if (!textValue) return;
        const firstLine = String(textValue).split('\n')[0].trim();
        if (!firstLine) return;
        if (firstLine.length > 120) return;
        if (/[{}\[\]]/.test(firstLine) && /"[^"]+"\s*:/.test(firstLine)) return;
        if (/bill\s*amount\s*:/i.test(firstLine)) return;
        const normalized = normalizeDoctorNameCandidate(firstLine);
        if (!normalized) return;
        if (!candidates.includes(normalized)) candidates.push(normalized);
      });
      return candidates.length > 0 ? candidates[0] : '-';
    }

    function normalizeEntityText(value) {
      const t = sanitizeReportText(value).toLowerCase();
      if (!t) return '';
      return t
        .replace(/\b\d{1,3}\s*y\/[mfo]\b/g, ' ')
        .replace(/\b\d{1,3}\s*years?\b/g, ' ')
        .replace(/[^a-z0-9]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    }

    function isSameEntityText(a, b) {
      const x = normalizeEntityText(a);
      const y = normalizeEntityText(b);
      if (!x || !y) return false;
      if (x === y) return true;
      if (x.length >= 6 && y.indexOf(x) >= 0) return true;
      if (y.length >= 6 && x.indexOf(y) >= 0) return true;
      return false;
    }



    function looksLikeHospitalAddressText(value) {
      const text = sanitizeReportText(value);
      if (!text || text === '-') return false;
      const low = text.toLowerCase();
      if (/\b(?:hospital\s*address|full\s*postal\s*address|address\s*of\s*hospital|hospital\s*addr(?:ess)?)\b/.test(low)) return true;
      if (/^(?:add(?:ress)?\s*[:\-])/.test(low)) return true;
      const addressTokenHits = (low.match(/\b(?:road|rd|street|st|lane|ln|nagar|colony|city|district|state|pin|pincode|zip|plot|floor|building|plaza|near|opp|opposite|shop|apartment|apt|flat|unit|society|chsl|complex|tower|wing|sector|block|east|west|north|south|taluka|tehsil)\b/g) || []).length;
      if (/\b(?:shop|apartment|apt|flat|unit)\s*no\.?\s*\d+\b/.test(low) && /\b\d{6}\b/.test(low)) return true;
      if (addressTokenHits >= 2 && (/\d{3,}/.test(low) || /\b(?:pin|pincode|zip)\b/.test(low))) return true;
      if (addressTokenHits >= 1 && (low.match(/,/g) || []).length >= 2 && /\b\d{6}\b/.test(low)) return true;
      return false;
    }

    function cleanHospitalDisplayName(value, fallbackHospitalName) {
      const raw = sanitizeReportText(value);
      const fallback = sanitizeReportText(fallbackHospitalName);
      if (!raw || raw === '-') return fallback || '-';
      const match = raw.match(/([A-Za-z][A-Za-z .&'-]{2,90}\b(?:Hospital|Clinic|Nursing Home|Medical Centre|Medical Center|Research Centre|Research Center))/i);
      if (match && match[1]) return sanitizeReportText(match[1]);
      if (looksLikeHospitalAddressText(raw)) {
        if (fallback && fallback !== '-' && !isSameEntityText(fallback, raw)) return fallback;
        return '-';
      }
      return raw;
    }

    function extractHospitalNameFromNarrative(extractionPairs, insuredName) {
      const sources = extractTextByAliases(
        extractionPairs,
        ['hospital_name', 'hospital', 'provider_hospital', 'treating_hospital', 'hospital_city_name', 'facility_name', 'institution_name', 'provider_name', 'clinical_findings', 'major_diagnostic_finding', 'summary'],
        30
      );
      const candidates = [];
      const pattern = /([A-Za-z][A-Za-z .&'-]{2,90}\b(?:Hospital|Clinic|Nursing Home|Medical Centre|Medical Center|Research Centre|Research Center))/ig;
      sources.forEach(function (src) {
        const text = sanitizeReportText(src);
        if (!text) return;
        let m;
        while ((m = pattern.exec(text)) !== null) {
          const c = sanitizeReportText(m[1]);
          if (!c) continue;
          if (isSameEntityText(c, insuredName)) continue;
          if (!isLikelyOrgName(c)) continue;
          if (!candidates.includes(c)) candidates.push(c);
        }
      });
      return candidates.length ? candidates[0] : '';
    }

    function extractDoctorNameFromNarrative(extractionPairs, insuredName, hospitalName) {
      const sources = extractTextByAliases(
        extractionPairs,
        ['treating_doctor', 'treating_doctor_name', 'doctor_name', 'attending_doctor', 'consultant_doctor', 'consulting_doctor', 'primary_doctor', 'clinical_findings', 'major_diagnostic_finding', 'summary'],
        30
      );
      const candidates = [];
      const doctorPattern = /\b(?:Dr\.?\s*[A-Za-z][A-Za-z .'-]{2,70})\b/g;
      sources.forEach(function (src) {
        const text = sanitizeReportText(src);
        if (!text) return;
        let m;
        while ((m = doctorPattern.exec(text)) !== null) {
          const c = normalizeDoctorNameCandidate(m[0]);
          if (!c) continue;
          if (isSameEntityText(c, insuredName)) continue;
          if (isSameEntityText(c, hospitalName)) continue;
          if (isLikelyOrgName(c)) continue;
          if (!candidates.includes(c)) candidates.push(c);
        }
      });
      return candidates.length ? candidates[0] : '';
    }

    function normalizeDoctorRegNoCandidate(value) {
      const text = sanitizeReportText(value);
      if (!text || text === '-') return '';
      let out = text.replace(/\s+/g, ' ').trim();

      let m = out.match(/\b(?:reg(?:istration)?\.?\s*no\.?|mci(?:\s*reg\.?\s*no\.?)?|nmc(?:\s*reg\.?\s*no\.?)?|rmc(?:\s*reg\.?\s*no\.?)?|dmc(?:\s*reg\.?\s*no\.?)?)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\/-]{2,40})\b/i);
      if (m && m[1]) out = m[1];

      m = out.match(/\b(?:mci|nmc|rmc|dmc)\s*[:\-]?\s*([A-Za-z0-9\/-]{3,40})\b/i);
      if (m && m[1]) out = m[1];

      out = out.replace(/^[^A-Za-z0-9]+/, '').replace(/[^A-Za-z0-9]+$/, '').trim();
      if (!/\d/.test(out)) return '';
      if (out.length > 40) return '';
      return out;
    }
    function isLikelyDoctorRegNo(value) {
      const t = sanitizeReportText(value);
      if (!t) return false;
      if (!/\d/.test(t)) return false;
      if (/\b(reg|registration|mci|nmc|dmc|medical council)\b/i.test(t)) return true;
      if (/[A-Za-z]{1,8}[\/-]\d{3,}/.test(t)) return true;
      if (/\d{4,}/.test(t)) return true;
      return false;
    }

    function extractDoctorRegistrationFromNarrative(extractionPairs) {
      const sources = extractTextByAliases(
        extractionPairs,
        [
          'treating_doctor_registration_number', 'doctor_registration_number', 'registration_no', 'registration_number',
          'mci_reg_no', 'nmc_reg_no', 'clinical_findings', 'major_diagnostic_finding', 'summary'
        ],
        40
      );
      const candidates = [];
      const patterns = [
        /\b(?:reg(?:istration)?\.?\s*no\.?|mci(?:\s*reg\.?\s*no\.?)?|nmc(?:\s*reg\.?\s*no\.?)?|rmc(?:\s*reg\.?\s*no\.?)?|dmc(?:\s*reg\.?\s*no\.?)?)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\/-]{2,40})\b/ig,
        /\b(?:mci|nmc|rmc|dmc)\s*[:\-]?\s*([A-Za-z0-9\/-]{3,40})\b/ig,
        /\b(\d{2,4}[\/-]\d{2,4}[\/-]\d{2,8})\b/g
      ];

      sources.forEach(function (src) {
        const sourceText = sanitizeReportText(src);
        if (!sourceText) return;
        patterns.forEach(function (re) {
          re.lastIndex = 0;
          let m;
          while ((m = re.exec(sourceText)) !== null) {
            const raw = (m && (m[1] || m[0])) ? String(m[1] || m[0]) : '';
            const c = normalizeDoctorRegNoCandidate(raw);
            if (!c) continue;
            if (!isLikelyDoctorRegNo(c)) continue;
            if (!candidates.includes(c)) candidates.push(c);
          }
        });
      });
      return candidates.length ? candidates[0] : '';
    }
    function nl2brEsc(value) {
      const cleaned = sanitizeReportText(value);
      return escapeHtml(cleaned || '-').replace(/\r?\n/g, '<br>');
    }

    function normalizeKey(value) {
      return String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    }

    function toFlatText(value) {
      if (value == null) return '';
      if (typeof value === 'string') return value.trim();
      if (typeof value === 'number' || typeof value === 'boolean') return String(value);
      if (Array.isArray(value)) {
        return value.map(toFlatText).filter(Boolean).join(', ');
      }
      if (typeof value === 'object') {
        const lines = [];
        Object.keys(value).forEach(function (k) {
          const out = toFlatText(value[k]);
          if (out) lines.push(k + ': ' + out);
        });
        return lines.join(' | ');
      }
      return '';
    }

    function appendEntityPairsFromObject(entityObj, targetPairs, maxPairs) {
      if (!entityObj || typeof entityObj !== 'object' || !Array.isArray(targetPairs)) return;
      const limit = Math.max(1, Number(maxPairs || 200));
      const startSize = targetPairs.length;
      const seen = new Set();
      const blockedKeyNormFragments = [
        'rawresponse',
        'textpreview',
        'ocrtext',
        'fulltext',
        'documenttext',
        'analysissnapshot',
        'mergeddocumenttext',
        'fullocr',
      ];

      function pushPair(path, rawValue) {
        if (targetPairs.length >= startSize + limit) return;
        const key = String(Array.isArray(path) && path.length ? path.join('_') : 'value').trim();
        const keyNorm = normalizeKey(key);
        if (!keyNorm) return;
        if (blockedKeyNormFragments.some(function (frag) { return keyNorm.indexOf(frag) >= 0; })) return;

        const cleanedValue = sanitizeReportText(rawValue);
        if (!cleanedValue) return;
        if (cleanedValue.length > 700) return;

        const dedupe = keyNorm + '|' + cleanedValue.toLowerCase();
        if (seen.has(dedupe)) return;
        seen.add(dedupe);
        targetPairs.push({ key: key, value: cleanedValue });
      }

      function walk(node, path, depth) {
        if (targetPairs.length >= startSize + limit) return;
        if (node == null) return;
        if (depth > 6) return;

        if (Array.isArray(node)) {
          node.forEach(function (item, index) {
            if (item == null) return;
            if (typeof item === 'object') {
              walk(item, path.concat(String(index)), depth + 1);
              return;
            }
            pushPair(path, item);
          });
          return;
        }

        if (typeof node === 'object') {
          Object.keys(node).forEach(function (k) {
            const keyText = String(k || '').trim();
            if (!keyText) return;
            walk(node[k], path.concat(keyText), depth + 1);
          });
          return;
        }

        pushPair(path, node);
      }

      walk(entityObj, [], 0);
    }
    function formatDdMmYyyy(value) {
      if (!value) return '-';
      const dt = new Date(value);
      if (Number.isNaN(dt.getTime())) {
        const rawText = String(value).trim();
        if (/^\d{4}-\d{2}-\d{2}$/.test(rawText)) {
          const parts = rawText.split('-');
          return parts[2] + '-' + parts[1] + '-' + parts[0];
        }
        return rawText || '-';
      }
      const d = String(dt.getDate()).padStart(2, '0');
      const m = String(dt.getMonth() + 1).padStart(2, '0');
      const y = String(dt.getFullYear());
      return d + '-' + m + '-' + y;
    }
    function toIsoDateOrEmpty(value) {
      const raw = sanitizeReportText(value);
      if (!raw || raw === '-') return '';
      const firstLine = String(raw).split('\n')[0].trim();
      if (!firstLine) return '';

      const isoMatch = firstLine.match(/\b(\d{4})-(\d{2})-(\d{2})\b/);
      if (isoMatch) return isoMatch[0];

      const dmyMatch = firstLine.match(/\b(\d{1,2})[\/\.\-](\d{1,2})[\/\.\-](\d{2,4})\b/);
      if (dmyMatch) {
        const day = String(Number(dmyMatch[1]) || 0).padStart(2, '0');
        const month = String(Number(dmyMatch[2]) || 0).padStart(2, '0');
        let yearNum = Number(dmyMatch[3]) || 0;
        if (yearNum < 100) yearNum += 2000;
        if (yearNum > 0 && Number(month) >= 1 && Number(month) <= 12 && Number(day) >= 1 && Number(day) <= 31) {
          return String(yearNum) + '-' + month + '-' + day;
        }
      }

      const parsed = new Date(firstLine);
      if (Number.isNaN(parsed.getTime())) return '';
      const y = String(parsed.getFullYear());
      const m = String(parsed.getMonth() + 1).padStart(2, '0');
      const d = String(parsed.getDate()).padStart(2, '0');
      return y + '-' + m + '-' + d;
    }

    function firstValidDateFromAliases(extractionPairs, aliases) {
      const values = extractTextByAliases(extractionPairs, aliases, 10);
      for (let i = 0; i < values.length; i += 1) {
        const iso = toIsoDateOrEmpty(values[i]);
        if (iso) return iso;
      }
      return '';
    }
    function resolveReportDateForDisplay(primaryRawValue, extractionPairs, aliases) {
      const primaryRaw = sanitizeReportText(primaryRawValue);
      const isoFromPrimary = toIsoDateOrEmpty(primaryRaw);
      if (isoFromPrimary) {
        return { iso: isoFromPrimary, display: formatDdMmYyyy(isoFromPrimary) };
      }
      if (primaryRaw && primaryRaw !== '-') {
        return { iso: '', display: primaryRaw };
      }
      const extractedIso = firstValidDateFromAliases(extractionPairs, aliases || []);
      if (extractedIso) {
        return { iso: extractedIso, display: formatDdMmYyyy(extractedIso) };
      }
      return { iso: '', display: '-' };
    }
    function computeLengthOfStay(admissionDate, dischargeDate) {
      if (!admissionDate || !dischargeDate) return '-';
      const a = new Date(admissionDate);
      const d = new Date(dischargeDate);
      if (Number.isNaN(a.getTime()) || Number.isNaN(d.getTime())) return '-';
      const diffMs = d.getTime() - a.getTime();
      if (diffMs < 0) return '-';
      const days = Math.max(1, Math.round(diffMs / 86400000));
      return String(days) + ' day(s)';
    }

    function isDateLikeFragment(value) {
      const t = String(value || '').trim();
      if (!t) return false;
      return /^\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}$/.test(t) || /^\d{4}[\/\-.]\d{1,2}[\/\-.]\d{1,2}$/.test(t);
    }

    function isNumericFragment(value) {
      const t = String(value || '').trim();
      if (!t) return false;
      return /^[<>]?\d+(?:\.\d+)?$/.test(t);
    }

    function isUnitFragment(value) {
      const t = String(value || '').trim();
      if (!t) return false;
      return /^(?:%|mg%|gm%|g\/dl|mg\/dl|mm\/hr|cells\/cu\s*mm|u\/l|iu\/l|mmol\/l|ng\/ml)$/i.test(t);
    }

    function isLikelyInvestigationTestName(value) {
      const t = String(value || '').trim();
      if (!t) return false;
      if (t.length < 2 || t.length > 48) return false;
      if (/[|:]/.test(t)) return false;
      if (isDateLikeFragment(t) || isNumericFragment(t) || isUnitFragment(t)) return false;
      if (!/[A-Za-z]/.test(t)) return false;
      return true;
    }

    function normalizeInvestigationRows(lines) {
      const source = Array.isArray(lines) ? lines : [];
      const compact = [];
      const seen = new Set();
      const current = { test: '', value: '', unit: '', date: '' };

      function lineFingerprint(value) {
        let t = String(value || '').toLowerCase();
        t = t.replace(/\btest\s*:\s*/g, '');
        t = t.replace(/\blab\s*:\s*/g, '');
        t = t.replace(/\bdate\s*:\s*\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}\b/g, '');
        t = t.replace(/\bdate\s*:\s*\d{4}[\/\-.]\d{1,2}[\/\-.]\d{1,2}\b/g, '');
        t = t.replace(/[^a-z0-9]+/g, '');
        return t;
      }

      function isNoiseLine(value) {
        const t = String(value || '').trim();
        if (!t) return true;
        if (/^(na|n\/a|not available|none|nil|-|\.)$/i.test(t)) return true;
        if (/^date\s*:\s*[\w:\/\-. ]+$/i.test(t)) return true;
        if (/^(?:doa|dod)\s*[:\-]/i.test(t)) return true;
        if (/^(?:add\s*:|address\b|patient\s*name\b|hospital\b|consultant\s*name\b|admit(?:ting|ing)\s*dr\b|name\s+of\s+the\s+manager\b|signatures?\b)/i.test(t)) return true;
        if (looksLikeHospitalAddressText(t)) return true;
        if (/\b(?:details?\s+of\s+medication|follow\s*up\s+recommendation|follow\s*up(?:\s*to)?|review\s+after|in\s+case\s+of\s+emergency|call\s+us|contact\s*(?:us|no)?|helpline|ipd\s+medicine\s+bill|mobile(?:\s*number)?|phone(?:\s*number)?)\b/i.test(t)) return true;
        if (/(?:\+?91[\s\-]*)?[6-9]\d{2}[\s\-]?\d{3}[\s\-]?\d{4}\b/.test(t)) return true;
        return false;
      }

      function pushLine(text) {
        let cleaned = sanitizeReportText(text).replace(/^[\-?]+\s*/, '').trim();
        if (!cleaned) return;
        cleaned = cleaned.replace(/^\s*test\s*:\s*/i, '').trim();
        if (isNoiseLine(cleaned)) return;
        const dedupeKey = lineFingerprint(cleaned) || cleaned.toLowerCase();
        if (seen.has(dedupeKey)) return;
        seen.add(dedupeKey);
        compact.push(cleaned);
      }

      function flushCurrent() {
        if (current.test && (current.value || current.unit || current.date)) {
          let line = current.test;
          if (current.value || current.unit) {
            line += ' | Value: ' + String(current.value || '-');
            if (current.unit) line += ' ' + current.unit;
          }
          if (current.date) line += ' | Date: ' + current.date;
          pushLine(line);
        }
        current.test = '';
        current.value = '';
        current.unit = '';
        current.date = '';
      }

      source.forEach(function (raw) {
        let textLine = sanitizeReportText(raw).replace(/^[\-?]+\s*/, '').trim();
        if (!textLine) return;
        textLine = textLine.replace(/^\s*test\s*:\s*/i, '').trim();
        if (isNoiseLine(textLine)) return;

        if (/[|:]/.test(textLine) && /\d/.test(textLine) && /[A-Za-z]/.test(textLine)) {
          flushCurrent();
          pushLine(textLine);
          return;
        }

        if (isDateLikeFragment(textLine)) {
          if (!current.date) current.date = textLine;
          return;
        }
        if (isUnitFragment(textLine)) {
          if (!current.unit) current.unit = textLine;
          return;
        }
        if (isNumericFragment(textLine)) {
          if (!current.value) current.value = textLine;
          return;
        }
        if (isLikelyInvestigationTestName(textLine)) {
          if (current.test && (current.value || current.unit || current.date)) {
            flushCurrent();
          }
          current.test = textLine;
          return;
        }

        flushCurrent();
        pushLine(textLine);
      });

      flushCurrent();

      if (compact.length > 0) return compact;

      return source.map(function (line) {
        return sanitizeReportText(line).replace(/^[\-?]+\s*/, '').replace(/^\s*test\s*:\s*/i, '').trim();
      }).filter(function (line) {
        return line && !isNoiseLine(line);
      });
    }

    function formatInvestigationListForReport(lines, emptyMessage) {
      const unique = [];
      normalizeInvestigationRows(lines).forEach(function (line) {
        const text = sanitizeReportText(line);
        if (!text) return;
        if (/^(na|n\/a|not available|none|nil|-|\.)$/i.test(text)) return;
        if (!unique.includes(text)) unique.push(text);
      });
      if (unique.length === 0) return emptyMessage;
      return '- ' + unique.join('\n- ');
    }
    function normalizeDatePartsToIso(yearRaw, monthRaw, dayRaw) {
      const yearNum = Number(yearRaw);
      const monthNum = Number(monthRaw);
      const dayNum = Number(dayRaw);
      if (!Number.isFinite(yearNum) || !Number.isFinite(monthNum) || !Number.isFinite(dayNum)) return '';
      if (yearNum < 1900 || yearNum > 2100) return '';
      if (monthNum < 1 || monthNum > 12) return '';
      if (dayNum < 1 || dayNum > 31) return '';
      const y = String(yearNum);
      const m = String(monthNum).padStart(2, '0');
      const d = String(dayNum).padStart(2, '0');
      return y + '-' + m + '-' + d;
    }

    function extractIsoDateFromText(value) {
      const text = String(value || '').trim();
      if (!text) return '';
      const isoMatch = text.match(/\b(20\d{2}|19\d{2})[\/.\-](\d{1,2})[\/.\-](\d{1,2})\b/);
      if (isoMatch) {
        return normalizeDatePartsToIso(isoMatch[1], isoMatch[2], isoMatch[3]);
      }
      const dmyMatch = text.match(/\b(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2,4})\b/);
      if (!dmyMatch) return '';
      let first = Number(dmyMatch[1]) || 0;
      let second = Number(dmyMatch[2]) || 0;
      let yearNum = Number(dmyMatch[3]) || 0;
      if (yearNum < 100) yearNum += 2000;
      let day = first;
      let month = second;
      if (first <= 12 && second > 12) {
        day = second;
        month = first;
      }
      return normalizeDatePartsToIso(yearNum, month, day);
    }

    function normalizeMeasurementValue(value, minAllowed, maxAllowed) {
      const num = Number(value);
      if (!Number.isFinite(num)) return NaN;
      if (num < minAllowed || num > maxAllowed) return NaN;
      return num;
    }

    function extractVitalsFromLine(value) {
      const text = String(value || '');
      const out = { temps: [], pulses: [], bps: [] };
      let m;

      const tempRegex = /\b(?:temp(?:erature)?|t)\b\s*[:=\-]?\s*([0-9]{2,3}(?:\.[0-9]{1,2})?)\s*(?:ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â°?\s*[CF])?/ig;
      while ((m = tempRegex.exec(text)) !== null) {
        const t = normalizeMeasurementValue(m[1], 30, 110);
        if (Number.isFinite(t)) out.temps.push(t);
      }

      const pulseRegex = /\b(?:pulse|pr|heart\s*rate|hr)\b\s*[:=\-]?\s*([0-9]{2,3})\b/ig;
      while ((m = pulseRegex.exec(text)) !== null) {
        const p = normalizeMeasurementValue(m[1], 20, 260);
        if (Number.isFinite(p)) out.pulses.push(p);
      }

      const labeledBpRegex = /\b(?:bp|b\/p|blood\s*pressure)\b\s*[:=\-]?\s*([0-9]{2,3})\s*[\/\-]\s*([0-9]{2,3})\b/ig;
      while ((m = labeledBpRegex.exec(text)) !== null) {
        const s = normalizeMeasurementValue(m[1], 50, 280);
        const d = normalizeMeasurementValue(m[2], 30, 180);
        if (Number.isFinite(s) && Number.isFinite(d)) out.bps.push({ s: s, d: d });
      }

      if (out.bps.length === 0) {
        const genericBpRegex = /\b([0-9]{2,3})\s*[\/\-]\s*([0-9]{2,3})\b/g;
        const isLikelyBpLine = /\b(?:mmhg|systolic|diastolic|tpr|vital|bp|b\/p|blood\s*pressure)\b/i.test(text)
          || /^\s*\d{2,3}\s*[\/\-]\s*\d{2,3}\s*$/.test(text);
        if (isLikelyBpLine) {
          while ((m = genericBpRegex.exec(text)) !== null) {
            const s = normalizeMeasurementValue(m[1], 50, 280);
            const d = normalizeMeasurementValue(m[2], 30, 180);
            if (Number.isFinite(s) && Number.isFinite(d)) out.bps.push({ s: s, d: d });
          }
        }
      }

      return out;
    }

    function formatMinMax(values, decimals) {
      if (!Array.isArray(values) || values.length === 0) return '-';
      let min = values[0];
      let max = values[0];
      for (let i = 1; i < values.length; i += 1) {
        const n = values[i];
        if (n < min) min = n;
        if (n > max) max = n;
      }
      const d = Number.isFinite(decimals) ? decimals : 0;
      const left = d > 0 ? min.toFixed(d) : String(Math.round(min));
      const right = d > 0 ? max.toFixed(d) : String(Math.round(max));
      return left + ' - ' + right;
    }

    function buildDailyTprSummary(extractionPairs, evidenceLines) {
      const sourceTexts = [];
      const aliasSource = extractTextByAliases(
        extractionPairs,
        [
          'tpr',
          'vitals',
          'vital_signs',
          'temperature',
          'pulse',
          'bp',
          'blood_pressure',
          'nursing_chart',
          'nurse_notes',
          'clinical_findings',
          'major_diagnostic_finding',
          'all_investigation_reports_with_values',
          'date_wise_investigation_reports',
          'investigation_finding_in_details'
        ],
        180
      );
      aliasSource.forEach(function (text) {
        const cleaned = sanitizeReportText(text);
        if (cleaned) sourceTexts.push(cleaned);
      });
      (Array.isArray(evidenceLines) ? evidenceLines : []).slice(0, 200).forEach(function (line) {
        const cleaned = sanitizeReportText(line);
        if (cleaned) sourceTexts.push(cleaned);
      });

      const allLines = [];
      sourceTexts.forEach(function (block) {
        String(block || '').split(/\r?\n/).forEach(function (line) {
          const t = String(line || '').trim();
          if (!t) return;
          if (t.length > 320) return;
          allLines.push(t);
        });
      });

      const dayMap = new Map();
      let lastDateIso = '';
      allLines.forEach(function (line) {
        const dateIso = extractIsoDateFromText(line);
        if (dateIso) lastDateIso = dateIso;
        const looksLikeVitals = /\b(?:temp|temperature|pulse|pr|heart\s*rate|hr|bp|b\/p|blood\s*pressure|tpr|mmhg)\b/i.test(line)
          || /\b\d{2,3}\s*[\/\-]\s*\d{2,3}\b/.test(line);
        if (!looksLikeVitals) return;
        const vitals = extractVitalsFromLine(line);
        if (vitals.temps.length === 0 && vitals.pulses.length === 0 && vitals.bps.length === 0) return;
        const targetDate = dateIso || lastDateIso;
        if (!targetDate) return;
        if (!dayMap.has(targetDate)) {
          dayMap.set(targetDate, { temps: [], pulses: [], bpSys: [], bpDia: [] });
        }
        const bucket = dayMap.get(targetDate);
        vitals.temps.forEach(function (n) { bucket.temps.push(n); });
        vitals.pulses.forEach(function (n) { bucket.pulses.push(n); });
        vitals.bps.forEach(function (bp) {
          bucket.bpSys.push(bp.s);
          bucket.bpDia.push(bp.d);
        });
      });

      if (dayMap.size === 0) return 'No date-wise TPR (Temp/Pulse/BP) values extracted.';

      const rows = Array.from(dayMap.keys()).sort().map(function (iso) {
        const bucket = dayMap.get(iso);
        const tempPart = bucket.temps.length
          ? ('Temp min/max: ' + formatMinMax(bucket.temps, 1))
          : 'Temp: -';
        const pulsePart = bucket.pulses.length
          ? ('Pulse min/max: ' + formatMinMax(bucket.pulses, 0))
          : 'Pulse: -';
        const bpPart = (bucket.bpSys.length && bucket.bpDia.length)
          ? ('BP min/max: '
            + formatMinMax(bucket.bpSys, 0).split(' - ')[0] + '/' + formatMinMax(bucket.bpDia, 0).split(' - ')[0]
            + ' to '
            + formatMinMax(bucket.bpSys, 0).split(' - ')[1] + '/' + formatMinMax(bucket.bpDia, 0).split(' - ')[1])
          : 'BP: -';
        return '- ' + formatDdMmYyyy(iso) + ' | ' + tempPart + ' | ' + pulsePart + ' | ' + bpPart;
      });

      return rows.slice(0, 20).join('\n');
    }

    function buildMedicineEvidenceSummary(primaryMedicineText, extractionPairs) {
      const allTexts = [];
      const primary = sanitizeReportText(primaryMedicineText);
      if (primary && primary !== '-') allTexts.push(primary);
      extractTextByAliases(extractionPairs, ['treatment_medicines', 'medicine_used', 'medicines', 'medications', 'prescription', 'drug_chart', 'rx'], 40).forEach(function (text) {
        const cleaned = sanitizeReportText(text);
        if (cleaned && cleaned !== '-') allTexts.push(cleaned);
      });
      const unique = [];
      const seen = new Set();

      function medicineFingerprint(value) {
        let t = String(value || '').toLowerCase();
        t = t.replace(/\b(?:inj|injection|tab|tablet|cap|capsule|syp|syrup)\b\.?/g, '');
        t = t.replace(/\b\d+(?:\.\d+)?\s*(?:mg|gm|g|ml|mcg|iu|units?)\b/g, '');
        t = t.replace(/\b(?:iv|im|po|od|bd|tid|qid|hs|stat|tsp|tbsp)\b/g, '');
        t = t.replace(/[^a-z0-9]+/g, '');
        return t;
      }

      allTexts.forEach(function (block) {
        String(block || '').split(/\r?\n|;/).forEach(function (line) {
          const cleaned = sanitizeReportText(line).replace(/^[\-ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¢\d.()]+\s*/, '').trim();
          if (!cleaned || cleaned === '-') return;
          if (!/[A-Za-z]/.test(cleaned)) return;
          if (/^(?:list\s+of\s+medicines|details?\s+of\s+medication|ipd\s+medicine\s+bill)/i.test(cleaned)) return;
          const key = medicineFingerprint(cleaned) || cleaned.toLowerCase();
          if (seen.has(key)) return;
          seen.add(key);
          unique.push(cleaned);
        });
      });
      if (unique.length === 0) return 'No medicine list extracted.';
      return '- ' + unique.slice(0, 25).join('\n- ');
    }

    function buildCompactTreatmentSummary(value, maxItems) {
      const text = sanitizeReportText(value);
      if (!text || text === '-' || /no medicine list extracted/i.test(text)) return 'supportive in-patient treatment';

      const limit = Number(maxItems || 4);
      const keep = [];
      const seen = new Set();

      function medKey(raw) {
        let t = String(raw || '').toLowerCase();
        t = t.replace(/\b(?:inj|injection|tab|tablet|cap|capsule|syp|syrup)\b\.?/g, '');
        t = t.replace(/\b\d+(?:\.\d+)?\s*(?:mg|gm|g|ml|mcg|iu|units?)\b/g, '');
        t = t.replace(/\b(?:iv|im|po|od|bd|tid|qid|hs|stat|tsp|tbsp)\b/g, '');
        t = t.replace(/[^a-z0-9]+/g, '');
        return t;
      }

      String(text).split(/\r?\n|;/).forEach(function (chunk) {
        String(chunk || '').split(/\s*,\s*/).forEach(function (item) {
          const cleaned = sanitizeReportText(item).replace(/^[-\d.()\s]+/, '').trim();
          if (!cleaned || cleaned === '-') return;
          if (/^(?:list\s+of\s+medicines|details?\s+of\s+medication|ipd\s+medicine\s+bill)$/i.test(cleaned)) return;
          const key = medKey(cleaned) || cleaned.toLowerCase();
          if (!key || seen.has(key)) return;
          seen.add(key);
          keep.push(cleaned);
        });
      });

      if (!keep.length) return 'supportive in-patient treatment';
      const short = keep.slice(0, Math.max(1, limit));
      const summary = short.join(', ');
      return keep.length > short.length ? (summary + ', and other supportive medications') : summary;
    }

    function parseAgeYears(value) {
      const text = sanitizeReportText(value);
      if (!text || text === '-') return '';
      const match = text.match(/\b(\d{1,3})\b/);
      if (!match) return '';
      const years = Number(match[1]);
      if (!Number.isFinite(years) || years <= 0 || years > 120) return '';
      return String(Math.trunc(years));
    }

    function parseGenderWord(value) {
      const text = sanitizeReportText(value).toLowerCase();
      if (!text || text === '-') return '';
      if (text === 'm' || /\b(?:male|man|boy)\b/i.test(text)) return 'man';
      if (text === 'f' || /\b(?:female|woman|girl)\b/i.test(text)) return 'woman';
      return '';
    }

    function buildConclusionPatientPhrase(ageValue, genderValue) {
      const ageYears = parseAgeYears(ageValue);
      if (ageYears) return ageYears + 'yr old patient';
      const genderWord = parseGenderWord(genderValue);
      if (genderWord === 'man') return 'Male patient';
      if (genderWord === 'woman') return 'Female patient';
      return 'Patient';
    }

    function isChecklistRuleSource(value) {
      const src = String(value || '').trim().toLowerCase();
      return src.indexOf('openai_claim_rules') === 0 || src.indexOf('openai_diagnosis_criteria') === 0;
    }

    function extractRuleCodeFromText(value) {
      const text = sanitizeReportText(value).toUpperCase();
      if (!text) return '';
      const match = text.match(/\bR\d{3}\b/);
      return match ? match[0] : '';
    }

    function extractRuleCodeFromEntry(entry) {
      if (!entry || typeof entry !== 'object') return '';
      const candidates = [
        entry.code,
        entry.rule_id,
        entry.name,
        entry.title,
        entry.note,
        entry.summary,
        entry.reason
      ];
      for (let i = 0; i < candidates.length; i += 1) {
        const code = extractRuleCodeFromText(candidates[i]);
        if (code) return code;
      }
      return '';
    }
    function extractAntibioticNamesForConclusion(medicineText, highEndSignal, maxItems) {
      const combined = [
        sanitizeReportText(medicineText),
        sanitizeReportText(highEndSignal)
      ].filter(Boolean).join('\n');
      if (!combined) return '';

      const antibioticPattern = /(meropenem|imipenem|ertapenem|doripenem|piperacillin|tazobactam|pip\s*-?\s*taz|ceftriaxone|cefotaxime|cefoperazone|sulbactam|cefepime|ceftazidime|amikacin|linezolid|colistin|teicoplanin|vancomycin|tigecycline|azithromycin|levofloxacin|ofloxacin|ciprofloxacin|amox(?:i|y)clav|amoxycillin|monocef)/i;
      const normalized = [];
      const seen = new Set();
      const limit = Number(maxItems || 3);

      String(combined).split(/\r?\n|;|,/).forEach(function (chunk) {
        const cleaned = sanitizeReportText(chunk)
          .replace(/^[-\d.()\s]+/, '')
          .replace(/\b(?:inj|injection|tab|tablet|cap|capsule|syp|syrup|iv|im|po|od|bd|tid|qid|hs|stat)\b\.?/gi, ' ')
          .replace(/\b\d+(?:\.\d+)?\s*(?:mg|gm|g|ml|mcg|iu|units?)\b/gi, ' ')
          .replace(/\s+/g, ' ')
          .trim();
        if (!cleaned) return;
        if (!antibioticPattern.test(cleaned)) return;
        const key = cleaned.toLowerCase().replace(/[^a-z0-9]+/g, '');
        if (!key || seen.has(key)) return;
        seen.add(key);
        normalized.push(cleaned);
      });

      if (!normalized.length) {
        if (/high\s*-?end\s*antibiotic|meropenem/i.test(combined)) return 'high-end antibiotic therapy';
        return '';
      }

      return normalized.slice(0, Math.max(1, limit)).join(', ');
    }
    function buildRuleStyleConclusion(decision, context) {
      const info = context || {};
      const patientPhrase = buildConclusionPatientPhrase(info.age, info.gender);
      const complaints = summarizeForConclusion(info.complaints || '', 180) || 'unspecified complaints';
      const diagnosis = summarizeForConclusion(info.diagnosis || '', 130) || 'unspecified diagnosis';
      const deranged = summarizeForConclusion(info.deranged || '', 220) || 'no significant deranged values documented';
      const reason = summarizeForConclusion(info.reason || '', 360) || 'clinical justification could not be derived from the available records';
      const decisionUpper = String(decision || '').trim().toUpperCase();
      const reasonLabel = decisionUpper === 'ADMISSIBLE' ? 'approval' : (decisionUpper === 'QUERY' ? 'query' : 'rejection');
      return sanitizeConclusionText(
        patientPhrase + ' with chief complaint of ' + complaints
        + ', diagnosis of ' + diagnosis
        + ', and deranged investigation report of ' + deranged + '. '
        + 'Reason for ' + reasonLabel + ': ' + reason + '.'
      );
    }

    function hasFractureContextForConclusion(context) {
      const info = context || {};
      const corpus = [info.diagnosis, info.complaints, info.findings, info.treatments, info.medicineEvidence].map(function (x) {
        return sanitizeConclusionText(x || '');
      }).join(' ');
      if (!corpus) return false;
      return /\b(?:fracture|orif|k[\s-]?wire|kwire|hairline|undisplaced|displaced|fixation)\b/i.test(corpus);
    }

    function hasProcedureContextForConclusion(context) {
      const info = context || {};
      const corpus = [info.diagnosis, info.complaints, info.findings, info.treatments, info.medicineEvidence].map(function (x) {
        return sanitizeConclusionText(x || '');
      }).join(' ');
      if (!corpus) return false;
      return /\b(?:surgery|surgical|procedure|operation|operative|post\s*op|postoperative|ablation|ligation|stripping|stent|angioplasty|lscs|orif|fixation)\b/i.test(corpus);
    }

    function isRuleCodeRelevantForContext(code, context) {
      const key = String(code || '').trim().toUpperCase();
      if (!/^R\d{3}$/.test(key)) return true;
      if (key === 'R002' || key === 'R009' || key === 'R010' || key === 'R011') {
        return hasFractureContextForConclusion(context);
      }
      return true;
    }

    function isConclusionObservationRelevant(text, context) {
      const line = sanitizeConclusionText(text || '');
      if (!line) return false;
      const fractureLike = /\b(?:fracture|orif|k[\s-]?wire|kwire|hairline|undisplaced|displaced|fixation)\b/i.test(line);
      if (fractureLike && !hasFractureContextForConclusion(context)) return false;
      const orphanSurgicalGap = /^surgical\s+indication\.?$/i.test(line) || /^procedure\s+note\/?bill\.?$/i.test(line);
      const normalized = line.toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
      const looseSurgicalGap = /\b(?:surgical\s+indication|fracture\s+description|procedure\s+note(?:\s*bill)?|orif|k\s*wire|kwire)\b/i.test(normalized);
      if ((orphanSurgicalGap || looseSurgicalGap) && !hasProcedureContextForConclusion(context) && !hasFractureContextForConclusion(context)) return false;
      if (normalized.split(' ').filter(Boolean).length <= 2 && /\b(?:indication|description)\b/i.test(normalized)) return false;
      return true;
    }

    function filterTriggeredReasonTextForContext(reasonText, context) {
      const chunks = String(reasonText || '')
        .split(/[;\n]+/)
        .map(function (x) {
          return sanitizeConclusionText(x)
            .replace(/^(?:key\s*gaps?|pending\s*clarifications?|checklist\s*observations?|missing\s*evidence)\s*:\s*/i, '')
            .replace(/[\s.;:,-]+$/, '')
            .trim();
        })
        .filter(Boolean)
        .filter(function (x) { return isConclusionObservationRelevant(x, context); });
      return chunks.join('; ');
    }

    function buildTriggeredRuleSummary(checklistRows, context) {
      const rows = Array.isArray(checklistRows) ? checklistRows : [];
      const out = [];
      const seen = new Set();
      rows.forEach(function (entry) {
        if (!(entry && entry.triggered)) return;
        const src = String(entry.source || '').trim().toLowerCase();
        if (!isChecklistRuleSource(src)) return;

        const code = extractRuleCodeFromEntry(entry);
        if (code === 'OPENAI_MERGED_REVIEW') return;
        if (!isRuleCodeRelevantForContext(code, context || {})) return;
        const name = sanitizeConclusionText(entry.name || entry.title || '').replace(/\bR\d{3}\b\s*[-: ]*/gi, '').replace(/\s+/g, ' ').trim();
        const decisionRaw = String(entry.decision || entry.status || '').trim().toUpperCase();
        const decisionLabel = (decisionRaw === 'APPROVE' || decisionRaw === 'REJECT' || decisionRaw === 'QUERY')
          ? decisionRaw
          : '';

        const base = (name || 'Clinical rule').trim();
        if (!base) return;
        const detail = (base + (decisionLabel ? (' [' + decisionLabel + ']') : '')).trim();
        const key = detail.toLowerCase();
        if (!key || seen.has(key)) return;
        seen.add(key);
        out.push(detail);
      });
      return out.slice(0, 6).join('; ');
    }

    function getRuleConclusionLineByCode(code, context) {
      const key = String(code || '').trim().toUpperCase();
      const info = context || {};
      const antibioticNames = summarizeForConclusion(
        extractAntibioticNamesForConclusion(info.medicineEvidence || '', info.highEndSignal || '', 3),
        90
      );
      const abxLabel = antibioticNames || 'Meropenem/high-end antibiotic';

      const MAP = {
        R001: 'Use of ' + abxLabel + ' is not supported by sepsis markers or objective evidence of severe infection; therefore, this rule is triggered.',
        R005: 'The diagnosis of sepsis is not substantiated as relevant sepsis markers, culture reports, and infection work-up are not adequately documented.',
        R003: 'Pneumonia is not adequately established in view of negative/non-supportive imaging, absence of culture evidence, and unjustified use of ' + abxLabel + '.',
        R004: 'UTI management is not sufficiently supported because urine culture/sensitivity correlation is absent or not aligned with the prescribed treatment.',
        R002: 'The indication for ORIF is not justified as records do not demonstrate displaced, unstable, or otherwise surgically indicated fracture morphology.',
        R006: abxLabel + ' administration is not supported by abnormal vitals, laboratory markers, or other objective evidence of serious infection.',
        R009: 'The fracture described appears hairline/minimally severe in nature, and the necessity for surgical fixation is not established from available records.',
        R010: 'Available imaging/clinical records suggest stable or undisplaced fracture, for which ORIF/K-wire fixation lacks adequate justification.',
        R011: 'Fracture diagnosis and related billing are inadequately supported due to absence of relevant X-ray evidence and/or corresponding bill support.',
        R007: 'The claim is not admissible as the treating Ayurvedic facility\'s accreditation/registration documents are not available for verification.',
        R008: 'History suggestive of alcoholism in the context of chronic liver disease materially impacts claim assessment and triggers this exclusion/review rule.',
        R013: 'UTI treatment with ' + abxLabel + ' is supported by culture and sensitivity evidence; therefore, the treatment is considered justified under this rule.',
        R014: 'Though the bill amount is below the usual threshold, the treatment flow is not consistent with simple OPD management, and the override is applicable.',
        R015: 'The claim falls under high-bill maternity/LSCS/neonatal jaundice override criteria and is therefore considered under the applicable exception pathway.',
        R016: 'Sepsis justification requires a combination of abnormal vitals, inflammatory/infective markers, and culture support; absence of these elements weakens the diagnosis.'
      };
      return MAP[key] || '';
    }
    function getDecisionOutcomeForConclusion(finalDecision) {
      const d = String(finalDecision || '').trim().toUpperCase();
      if (d === 'ADMISSIBLE') return { verdict: 'APPROVED', terminalSentence: 'Therefore, the claim is admissible.' };
      if (d === 'INADMISSIBLE') return { verdict: 'REJECTED', terminalSentence: 'Therefore, the claim is recommended for rejection.' };
      return { verdict: 'QUERIED', terminalSentence: 'Therefore, the claim is kept under query.' };
    }

    function shouldSuppressRuleDrivenConclusion(context) {
      const claimKey = String((context && (context.externalClaimId || context.claimId || '')) || '').trim();
      return claimKey === '140420939';
    }

    function buildRuleDrivenConclusion(checklistRows, context) {
      const rows = Array.isArray(checklistRows) ? checklistRows : [];
      const info = context || {};
      const patientPhrase = buildConclusionPatientPhrase(info.age, info.gender);
      const complaints = summarizeForConclusion(info.complaints || '', 180) || 'unspecified complaints';
      const diagnosis = summarizeForConclusion(info.diagnosis || '', 130) || 'unspecified diagnosis';
      const deranged = summarizeForConclusion(info.deranged || '', 180) || 'no significant deranged values documented';
      const reason = summarizeForConclusion(info.reason || '', 320) || 'clinical justification could not be derived from the available records';
      const decisionUpper = String(info.finalDecision || '').trim().toUpperCase();
      const reasonLabel = decisionUpper === 'ADMISSIBLE' ? 'approval' : (decisionUpper === 'QUERY' ? 'query' : 'rejection');

      const clinicalSummary = sanitizeConclusionText(
        patientPhrase + ' with chief complaint of ' + complaints
        + ', diagnosis of ' + diagnosis
        + ', and deranged investigation report of ' + deranged
      );
      const introLine = sanitizeConclusionText(
        info.baseConclusion
        || (clinicalSummary + '. Reason for ' + reasonLabel + ': ' + reason + '.')
      );

      if (shouldSuppressRuleDrivenConclusion(info)) {
        return introLine;
      }

      const seenCodes = new Set();
      const ruleLinesOnly = [];
      rows.forEach(function (entry) {
        if (!(entry && entry.triggered)) return;
        const src = String(entry.source || '').trim().toLowerCase();
        if (!isChecklistRuleSource(src)) return;

        const code = extractRuleCodeFromEntry(entry);
        if (/^R\d{3}$/.test(code) && !isRuleCodeRelevantForContext(code, info)) return;
        if (/^R\d{3}$/.test(code)) {
          if (seenCodes.has(code)) return;
          seenCodes.add(code);
          const mappedLine = sanitizeConclusionText(getRuleConclusionLineByCode(code, info))
            .replace(/\bR\d{3}\b\s*[-:]\s*/gi, '')
            .trim();
          if (mappedLine) {
            ruleLinesOnly.push(mappedLine);
            return;
          }
        }

        const fallbackLine = sanitizeConclusionText(entry.note || entry.why_triggered || entry.summary || entry.reason || '')
          .replace(/\bR\d{3}\b\s*[-:]\s*/gi, '')
          .trim();
        if (fallbackLine && isConclusionObservationRelevant(fallbackLine, info)) ruleLinesOnly.push(fallbackLine);
      });

      if (ruleLinesOnly.length === 0) return introLine;

      const outcome = getDecisionOutcomeForConclusion(info.finalDecision);
      const rawGaps = filterTriggeredReasonTextForContext(info.triggeredReasonText || '', info).split(';').map(function (x) {
        return sanitizeConclusionText(x).replace(/\bR\d{3}\b\s*[-:]\s*/gi, '').trim();
      }).filter(Boolean);
      const gapItems = rawGaps.slice(0, 3);
      const gapText = gapItems.length >= 3
        ? (gapItems[0] + ', ' + gapItems[1] + ', and ' + gapItems[2])
        : (gapItems.length === 2 ? (gapItems[0] + ' and ' + gapItems[1]) : (gapItems[0] || 'key clinical documentation gaps'));

      const finalParagraph = sanitizeConclusionText(
        'Conclusion: The claim has been reviewed against applicable clinical rules. '
        + 'On examination of the available documents, it is noted that ' + clinicalSummary + '. '
        + 'However, ' + gapText + ' were observed. '
        + 'Therefore, one or more clinical rules are triggered. '
        + 'Based on the submitted evidence and medical necessity assessment, '
        + outcome.terminalSentence
      );

      return sanitizeConclusionText(introLine + '\n' + ruleLinesOnly.join('\n') + '\n' + finalParagraph) || introLine;
    }
    function buildExactReasonText(decision, context) {
      const info = context || {};
      const diagnosis = summarizeForConclusion(info.diagnosis || '', 120) || 'the diagnosed condition';
      const deranged = summarizeForConclusion(info.deranged || '', 140);
      const filteredTriggerText = filterTriggeredReasonTextForContext(info.triggeredReasonText || '', info);
      const triggerText = summarizeForConclusion(String(filteredTriggerText || '').replace(/\bR\d{3}\b\s*[-:]\s*/gi, ''), 220);
      const triggerTextClean = String(triggerText || '').replace(/[\s.;:,-]+$/, '');
      const triggerTextFinal = isConclusionObservationRelevant(triggerTextClean, info) ? triggerTextClean : '';
      const triggeredRuleSummary = summarizeForConclusion(String(info.triggeredRuleSummary || '').replace(/\bR\d{3}\b\s*[-:]\s*/gi, ''), 220);
      const compactTreatment = buildCompactTreatmentSummary(info.medicineEvidence || '', 4);
      const medEvidence = summarizeForConclusion(compactTreatment, 140);
      const tprEvidence = summarizeForConclusion(String(info.tprSummary || '').replace(/^-+\s*/gm, '').replace(/\n+/g, '; '), 180);
      const windowText = String(info.admissionWindowText || '-');
      const losText = String(info.lengthOfStay || '-');

      const supportBits = [];
      if (deranged && !/no deranged investigation values found/i.test(deranged)) {
        supportBits.push('deranged investigations (' + summarizeForConclusion(deranged, 90) + ')');
      }
      if (medEvidence && !/supportive in-patient treatment/i.test(medEvidence)) {
        supportBits.push('in-patient treatment records');
      }
      if (tprEvidence && !/no date-wise tpr/i.test(tprEvidence)) {
        supportBits.push('vital/TPR trend');
      }

      const supportText = supportBits.join(', ');
      const reasons = [];

      if (decision === 'INADMISSIBLE') {
        reasons.push('Admission during ' + windowText + ' (LOS: ' + losText + ') for ' + diagnosis + ' is not sufficiently justified on submitted records.');
        if (triggerTextFinal) {
          reasons.push('Key gaps: ' + triggerTextFinal + '.');
        } else if (triggeredRuleSummary) {
          reasons.push('Triggered clinical checks indicate documentation gaps.');
        }
      } else if (decision === 'ADMISSIBLE') {
        reasons.push('Admission during ' + windowText + ' (LOS: ' + losText + ') is medically justified for ' + diagnosis + ' based on submitted records.');
        if (supportText) reasons.push('Supportive evidence includes ' + supportText + '.');
      } else {
        reasons.push('Available records for ' + diagnosis + ' during ' + windowText + ' are insufficient for final admissibility decision.');
        if (triggerTextFinal) {
          reasons.push('Pending clarifications: ' + triggerTextFinal + '.');
        } else if (triggeredRuleSummary) {
          reasons.push('Triggered clinical checks require additional documentation.');
        }
      }

      return sanitizeConclusionText(reasons.join(' ')) || 'Reason could not be generated from extracted evidence.';
    }
    function mapFinalRecommendationDecision(rawRecommendation, admissionRequired) {
      const rec = String(rawRecommendation || '').trim().toLowerCase();
      const admission = String(admissionRequired || '').trim().toLowerCase();

      if (
        rec.indexOf('inadmissible') >= 0
        || rec.indexOf('reject') >= 0
        || rec.indexOf('not justified') >= 0
      ) {
        return 'INADMISSIBLE';
      }
      if (
        rec.indexOf('admissible') >= 0
        || rec.indexOf('approve') >= 0
        || rec.indexOf('justified') >= 0
      ) {
        return 'ADMISSIBLE';
      }
      if (
        rec.indexOf('query') >= 0
        || rec.indexOf('manual') >= 0
        || rec.indexOf('need_more_evidence') >= 0
        || rec.indexOf('uncertain') >= 0
      ) {
        return 'QUERY';
      }

      if (admission === 'yes' || admission === 'true' || admission === '1') return 'ADMISSIBLE';
      if (admission === 'no' || admission === 'false' || admission === '0') return 'INADMISSIBLE';
      return 'QUERY';
    }
    function extractTextByAliases(extractionPairs, aliases, limit) {
      const list = [];
      const aliasList = aliases.map(normalizeKey);
      extractionPairs.forEach(function (pair) {
        const keyNorm = normalizeKey(pair && pair.key ? pair.key : '');
        if (!keyNorm) return;
        const matched = aliasList.some(function (alias) {
          return keyNorm === alias || keyNorm.indexOf(alias) >= 0 || alias.indexOf(keyNorm) >= 0;
        });
        if (!matched) return;
        const textValue = sanitizeReportText(pair && pair.value ? pair.value : '');
        if (!textValue) return;
        if (!list.includes(textValue)) list.push(textValue);
      });
      if (typeof limit === 'number' && limit > 0) return list.slice(0, limit);
      return list;
    }

    function pickFirstValidValue(values, validator) {
      const list = Array.isArray(values) ? values : [];
      for (let i = 0; i < list.length; i += 1) {
        const cleaned = sanitizeReportText(list[i]);
        if (!cleaned) continue;
        if (typeof validator === 'function' && !validator(cleaned)) continue;
        return cleaned;
      }
      return '';
    }

    function isLikelyMimeType(value) {
      return /^[a-z]+\/[a-z0-9.+-]+$/i.test(String(value || '').trim());
    }

    function isLikelyDateTimeOnly(value) {
      const t = String(value || '').trim();
      if (!t) return false;
      if (/^\d{1,2}[.\-\/]\d{1,2}[.\-\/]\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$/.test(t)) return true;
      if (/^\d{4}[.\-\/]\d{1,2}[.\-\/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$/.test(t)) return true;
      if (/^\d{1,2}:\d{2}(?::\d{2})?$/.test(t)) return true;
      return false;
    }

    function cleanCompanyNameValue(value) {
      const t = sanitizeReportText(value);
      if (!t) return '';
      if (/^(rightworks|verifai)$/i.test(t)) return '';
      if (isLikelyMimeType(t)) return '';
      if (isLikelyDateTimeOnly(t)) return '';
      return t;
    }

    function cleanClaimTypeValue(value) {
      const t = sanitizeReportText(value);
      if (!t) return '';
      if (isLikelyMimeType(t)) return '';
      if (isLikelyDateTimeOnly(t)) return '';
      if (/^[a-z0-9._-]+\.pdf$/i.test(t)) return '';
      if (/^(application|image|text)\/[a-z0-9.+-]+$/i.test(t)) return '';
      return t;
    }

    function cleanDiagnosisValue(value) {
      const t = sanitizeReportText(value);
      if (!t) return '';
      if (isLikelyDateTimeOnly(t)) return '';
      if (/^[0-9.,:\-\/\s]+$/.test(t)) return '';
      if (/^\d+\s*$/.test(t)) return '';
      if (isLikelyMimeType(t)) return '';
      return t;
    }

    function extractDiagnosisFromNarrativeBlocks(values) {
      const list = Array.isArray(values) ? values : [];
      for (let i = 0; i < list.length; i += 1) {
        const lines = sanitizeReportText(list[i]).split('\n').map(function (x) { return String(x || '').trim(); }).filter(Boolean);
        for (let j = 0; j < lines.length; j += 1) {
          if (!/final\s+diagnosis|diagnosis/i.test(lines[j])) continue;
          for (let k = j + 1; k < Math.min(lines.length, j + 5); k += 1) {
            const cand = cleanDiagnosisValue(lines[k]);
            if (!cand) continue;
            if (/^case\s+history|reason\s+for\s+admission|treatment|operative/i.test(cand)) continue;
            return cand;
          }
        }
      }
      return '';
    }

    function trimClinicalTextAtStopLabels(value) {
      const text = String(value || '').trim();
      if (!text) return '';
      const stopMatch = text.match(/([\s\S]*?)(?=\b(?:major\s+diagnostic\s+finding|clinical\s+findings?|provisional\s+diagnostics?|final\s+diagnosis|patient\s+name|hospital|admit(?:ting|ing)\s*dr|consultant\s+name|reg(?:istration)?\.?\s*no|total|final\s+payment|surgery|procedure\s+performed|details\s+of\s+medication|ipd\s+medicine\s+bill|medicine\s+name|claimed|name\s+of\s+the\s+establishment|full\s+postal\s+address|name\s+of\s+the\s+manager)\b|$)/i);
      return sanitizeReportText((stopMatch && stopMatch[1]) ? stopMatch[1] : text);
    }

    function extractChiefComplaintsFromNarrative(value) {
      const text = sanitizeReportText(value);
      if (!text || text === '-') return '';
      const patterns = [
        /(?:^|\b)chief\s*complaints?(?:\s*at\s*admission)?\s*[:\-]\s*([\s\S]+)/i,
        /(?:^|\b)complaints?\s*[:\-]\s*([\s\S]+)/i,
        /(?:^|\b)symptoms?\s*include\s*[:\-]?\s*([\s\S]+)/i
      ];
      for (let i = 0; i < patterns.length; i += 1) {
        const match = text.match(patterns[i]);
        if (!match || !match[1]) continue;
        const cleaned = trimClinicalTextAtStopLabels(match[1]).replace(/^[\s,;:.\-]+/, '').trim();
        if (cleaned && cleaned !== '-') return cleaned;
      }
      return '';
    }

    function cleanMajorDiagnosticFindingText(value, chiefComplaintsText) {
      let text = sanitizeReportText(value);
      if (!text || text === '-') return '';

      text = text.replace(/^\s*major\s+diagnostic\s+finding\s*[:\-]?\s*/i, '');
      text = text.replace(/^\s*clinical\s+findings?\s*[:\-]?\s*/i, '');

      const chiefText = sanitizeReportText(chiefComplaintsText);
      if (chiefText && chiefText !== '-') {
        const escapedChief = chiefText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        if (escapedChief) text = text.replace(new RegExp(escapedChief, 'ig'), ' ');
      }
      text = text.replace(/chief\s*complaints?(?:\s*at\s*admission)?\s*[:\-]?/ig, '');

      text = text.replace(/\b(?:patient\s+name|name\s+of\s+the\s+establishment|full\s+postal\s+address|add\s*:|admit(?:ting|ing)\s*dr|consultant(?:\s+name)?|reg(?:istration)?\.?\s*no|ipd\s+medicine\s+bill|medicine\s+name|received\s+with\s+thanks|total\s*(?:sum)?|final\s+payment|claimed|date\s+of\s+admission|date\s+of\s+discharge)\b[\s\S]*$/i, '');

      const lineFiltered = String(text || '').split(/\r?\n/).map(function (line) {
        return String(line || '').trim();
      }).filter(function (line) {
        if (!line) return false;
        if (/^(patient\s+name|hospital\b|name\s+of\s+the\s+establishment|full\s+postal\s+address|add\s*:|admit(?:ting|ing)\s*dr|consultant(?:\s+name)?|reg(?:istration)?\.?\s*no|ipd\s+medicine\s+bill|medicine\s+name|received\s+with\s+thanks|total\b|final\s+payment|claimed\b|date\s+of\s+admission|date\s+of\s+discharge)/i.test(line)) return false;
        if (/^name\s*[:\-]?\s*[a-z]/i.test(line)) return false;
        if (/^(?:dr\.?\s+|doctor\s+|consultant\s+dr\.?\s*)/i.test(line)) return false;
        if (/\b(?:mbbs|md|ms|dm|mch|bams|bhms|bums|dnb|fnb|rmc\s*\d+)\b/i.test(line)) return false;
        if (/\b(?:follow\s*up|in\s+case\s+of\s+emergency|call\s+us|mobile\s*no|phone\s*no)\b/i.test(line)) return false;
        const addressTokenHits = (line.match(/\b(?:road|rd|street|st|lane|ln|nagar|colony|city|district|state|pin|pincode|zip|plot|floor|building|plaza|near|opp|opposite|shop|apartment|apt|flat|unit|society|chsl|complex|tower|wing|sector|block|east|west|north|south|taluka|tehsil|vihar|chowk)\b/ig) || []).length;
        const commaCount = (line.match(/,/g) || []).length;
        const clinicalTokenHits = (line.match(/\b(?:pain|swelling|fever|cough|breathlessness|vomiting|diarrhea|fracture|sepsis|infection|uti|pneumonia|tachycardia|hypotension|hypertension|bp|pulse|spo2|temperature|rbc|wbc|platelet|creatinine|sodium|potassium|diagnosis|edema|tenderness|discharge)\b/ig) || []).length;
        if (addressTokenHits >= 1 && clinicalTokenHits === 0 && /\b(?:road|rd|street|st|lane|ln|nagar|vihar|chowk|colony|sector|block|city|district|state|near|opp|opposite)\b/i.test(line)) return false;
        if (/\bhospital\b/i.test(line) && (addressTokenHits >= 2 || commaCount >= 2)) return false;
        if (addressTokenHits >= 3 && (commaCount >= 1 || /\b\d{6}\b/.test(line))) return false;
        return true;
      });

      return sanitizeReportText(lineFiltered.join('\n'));
    }
    function firstNonEmpty() {
      for (let i = 0; i < arguments.length; i += 1) {
        const candidate = sanitizeReportText(arguments[i]);
        if (candidate) return candidate;
      }
      return '-';
    }

    function buildLegacyReportHtml(generatedAt, doctorName, extractionPairs, evidenceLines) {
      const legacyPayloadRaw = (function () {
        const raw = currentStatusItem && currentStatusItem.legacy_payload;
        if (raw && typeof raw === 'object') return raw;
        if (typeof raw === 'string') {
          try {
            const parsed = JSON.parse(raw);
            if (parsed && typeof parsed === 'object') return parsed;
          } catch (_err) {
            // ignore legacy payload parse errors
          }
        }
        return {};
      }());
      const legacyPayload = {};
      Object.keys(legacyPayloadRaw).forEach(function (k) {
        const normKey = normalizeKey(k);
        if (!normKey) return;
        legacyPayload[normKey] = legacyPayloadRaw[k];
      });
      function legacyValue() {
        for (let i = 0; i < arguments.length; i += 1) {
          const keyNorm = normalizeKey(arguments[i]);
          if (!keyNorm) continue;
          if (!Object.prototype.hasOwnProperty.call(legacyPayload, keyNorm)) continue;
          const out = sanitizeReportText(legacyPayload[keyNorm]);
          if (out && out !== '-') return out;
        }
        return '';
      }

      const claimId = firstNonEmpty(
        legacyValue('claim_id', 'external_claim_id'),
        currentClaim && currentClaim.external_claim_id,
        routeClaimId,
        '-'
      );
      const companyName = 'Medi Assist Insurance TPA Pvt. Ltd.';
      const claimTypeFromExtraction = pickFirstValidValue(
        extractTextByAliases(extractionPairs, ['claim_type', 'case_type'], 6),
        cleanClaimTypeValue
      );
      const claimType = firstNonEmpty(
        claimTypeFromExtraction,
        legacyValue('claim_type', 'case_type', 'type'),
        '-'
      );
      const insuredFromExtraction = pickFirstValidValue(
        extractTextByAliases(extractionPairs, ['name', 'insured', 'beneficiary', 'patient_name', 'policy_holder_name'], 8),
        function (value) { return !isLikelyOrgName(value); }
      );
      const insured = firstNonEmpty(
        insuredFromExtraction,
        legacyValue('benef_name', 'patient_name', 'insured', 'name'),
        currentClaim && currentClaim.patient_name,
        '-'
      );
      const benefAge = firstNonEmpty(
        extractTextByAliases(extractionPairs, ['benef_age', 'patient_age', 'age'], 1)[0],
        legacyValue('benef_age', 'patient_age', 'age'),
        '-'
      );
      const benefGender = firstNonEmpty(
        extractTextByAliases(extractionPairs, ['benef_gender', 'patient_gender', 'gender', 'sex'], 1)[0],
        legacyValue('benef_gender', 'patient_gender', 'gender', 'sex'),
        '-'
      );
      const insuredCombined = (function () {
        const name = String(insured || '').trim();
        const age = String(benefAge || '').trim();
        const gender = String(benefGender || '').trim();
        if (!name) return '-';
        const agePart = age && age !== '-' ? (' ' + age + 'y') : '';
        const genderPart = gender && gender !== '-' ? ('/' + gender.charAt(0).toUpperCase()) : '';
        return (name + agePart + genderPart).trim() || name;
      }());
      let hospital = firstNonEmpty(
        extractTextByAliases(extractionPairs, ['hospital_name', 'hospital', 'provider_hospital', 'treating_hospital', 'hospital_city_name', 'facility_name', 'institution_name', 'provider_name'], 1)[0],
        legacyValue('hospital_name', 'hospital', 'provider_name', 'provider_hospital', 'provider_hospital_name', 'treating_hospital', 'hospital_city_name', 'facility_name', 'institution_name'),
        currentStatusItem && currentStatusItem.hospital_name,
        (currentClaim && Array.isArray(currentClaim.tags) && currentClaim.tags.length > 3) ? currentClaim.tags[3] : '',
        '-'
      );
      const narrativeHospital = extractHospitalNameFromNarrative(extractionPairs, insured);
      if (narrativeHospital && (hospital === '-' || isSameEntityText(hospital, insured) || !isLikelyOrgName(hospital))) {
        hospital = narrativeHospital;
      }
      if (isSameEntityText(hospital, insured) || !isLikelyOrgName(hospital)) {
        hospital = narrativeHospital || '-';
      }
      hospital = cleanHospitalDisplayName(hospital, narrativeHospital || '');

      const treatingDoctorFromExtraction = normalizeDoctorNameCandidate(extractTreatingDoctorName(extractionPairs));
      let treatingDoctor = normalizeDoctorNameCandidate(firstNonEmpty(
        (treatingDoctorFromExtraction && treatingDoctorFromExtraction !== '-') ? treatingDoctorFromExtraction : '',
        extractTextByAliases(extractionPairs, ['treating_doctor', 'treating_doctor_name', 'doctor_name', 'attending_doctor', 'consultant_doctor'], 1)[0],
        legacyValue('treating_doctor', 'treating_doctor_name', 'doctor_name', 'attending_doctor', 'consultant_doctor'),
        '-'
      )) || '-';
      const narrativeDoctor = normalizeDoctorNameCandidate(extractDoctorNameFromNarrative(extractionPairs, insured, hospital));
      if (narrativeDoctor && (treatingDoctor === '-' || isSameEntityText(treatingDoctor, insured) || isSameEntityText(treatingDoctor, hospital) || isLikelyOrgName(treatingDoctor))) {
        treatingDoctor = narrativeDoctor;
      }
      if (isSameEntityText(treatingDoctor, insured) || isSameEntityText(treatingDoctor, hospital) || isLikelyOrgName(treatingDoctor)) {
        treatingDoctor = narrativeDoctor || '-';
      }

      let treatingDoctorRegistration = normalizeDoctorRegNoCandidate(firstNonEmpty(
        extractTextByAliases(extractionPairs, ['treating_doctor_registration_number', 'doctor_registration_number', 'registration_no', 'registration_number', 'mci_reg_no', 'nmc_reg_no'], 1)[0],
        legacyValue('treating_doctor_registration_number', 'doctor_registration_number', 'registration_no', 'registration_number', 'mci_reg_no', 'nmc_reg_no'),
        '-'
      ));
      const narrativeDoctorRegistration = normalizeDoctorRegNoCandidate(extractDoctorRegistrationFromNarrative(extractionPairs));
      if ((!isLikelyDoctorRegNo(treatingDoctorRegistration)
          || isSameEntityText(treatingDoctorRegistration, insured)
          || isSameEntityText(treatingDoctorRegistration, hospital)
          || isSameEntityText(treatingDoctorRegistration, treatingDoctor))
          && narrativeDoctorRegistration) {
        treatingDoctorRegistration = narrativeDoctorRegistration;
      }
      if (!isLikelyDoctorRegNo(treatingDoctorRegistration)
          || isSameEntityText(treatingDoctorRegistration, insured)
          || isSameEntityText(treatingDoctorRegistration, hospital)
          || isSameEntityText(treatingDoctorRegistration, treatingDoctor)) {
        treatingDoctorRegistration = '-';
      }
      const treatmentMedicines = firstNonEmpty(
        extractTextByAliases(extractionPairs, ['treatment_medicines', 'medicine_used', 'medicines', 'medications', 'prescription', 'drug_chart', 'rx'], 25).join('\n'),
        '-'
      );
      const medicineEvidenceText = buildMedicineEvidenceSummary(treatmentMedicines, extractionPairs);
      const admissionDateImported = firstNonEmpty(
        currentStatusItem && currentStatusItem.doa_date,
        legacyValue('doa_date', 'doa', 'doa date', 'date_of_admission', 'date of admission', 'admission_date', 'admission date'),
        currentStatusItem && currentStatusItem.date_of_admission,
        extractTextByAliases(extractionPairs, ['admission_date', 'date_of_admission', 'doa', 'doa_date'], 1)[0],
        '-'
      );
      const dischargeDateImported = firstNonEmpty(
        currentStatusItem && currentStatusItem.dod_date,
        legacyValue('dod_date', 'dod', 'dod date', 'date_of_discharge', 'date of discharge', 'discharge_date', 'discharge date'),
        currentStatusItem && currentStatusItem.date_of_discharge,
        extractTextByAliases(extractionPairs, ['discharge_date', 'date_of_discharge', 'dod', 'dod_date'], 1)[0],
        '-'
      );
      const admissionDateResolved = resolveReportDateForDisplay(
        admissionDateImported,
        extractionPairs,
        ['admission_date', 'date_of_admission', 'doa']
      );
      const dischargeDateResolved = resolveReportDateForDisplay(
        dischargeDateImported,
        extractionPairs,
        ['discharge_date', 'date_of_discharge', 'dod']
      );
      const diagnosisFromExtraction = pickFirstValidValue(
        extractTextByAliases(extractionPairs, ['diagnosis', 'final_diagnosis', 'provisional_diagnosis', 'primary_diagnosis', 'diagnoses'], 10),
        cleanDiagnosisValue
      );
      const diagnosisFromNarrative = extractDiagnosisFromNarrativeBlocks(
        extractTextByAliases(extractionPairs, ['major_diagnostic_finding', 'clinical_findings', 'summary'], 8)
      );
      const diagnosis = firstNonEmpty(
        diagnosisFromExtraction,
        diagnosisFromNarrative,
        legacyValue('diagnosis', 'final_diagnosis', 'provisional_diagnosis', 'primary_diagnosis'),
        '-'
       );
      const majorFindingRawText = firstNonEmpty(
        extractTextByAliases(extractionPairs, ['major_diagnostic_finding', 'clinical_findings', 'diagnostic_finding', 'lab_findings', 'clinical'], 5).join('\n'),
        diagnosis,
        '-'
      );
      const chiefComplaintsDirect = sanitizeReportText(extractTextByAliases(extractionPairs, ['chief_complaints', 'chief_complaint', 'presenting_complaints', 'complaints'], 3).join('\n'));
      const chiefComplaintsFromNarrative = extractChiefComplaintsFromNarrative(majorFindingRawText);
      const chiefComplaints = firstNonEmpty(chiefComplaintsDirect, chiefComplaintsFromNarrative, '-');
      const majorFinding = firstNonEmpty(
        cleanMajorDiagnosticFindingText(majorFindingRawText, chiefComplaints),
        diagnosis,
        '-'
      );
      const alcoholHistory = firstNonEmpty(extractTextByAliases(extractionPairs, ['alcoholism_history', 'alcohol_history', 'alcohol'], 1)[0], '-');
      const claimAmountFromExtraction = pickFirstValidValue(
        extractTextByAliases(extractionPairs, ['claimed_amount', 'claim_amount', 'amount_claimed', 'bill_amount'], 8),
        function (value) {
          const t = String(value || '').trim();
          if (!t) return false;
          if (isLikelyDateTimeOnly(t)) return false;
          const digits = t.replace(/[^0-9.]/g, '');
          const num = Number(digits);
          if (!Number.isFinite(num)) return true;
          return num >= 100;
        }
      );
      const claimedAmount = firstNonEmpty(
        legacyValue('claim_amount', 'claimed_amount', 'amount_claimed', 'bill_amount'),
        claimAmountFromExtraction,
        '-'
      );

      const allInvestigationRows = normalizeInvestigationRows(extractTextByAliases(
        extractionPairs,
        ['all_investigation_reports_with_values', 'all_investigation_report_lines', 'investigation_finding_in_details', 'investigation', 'lab_results', 'test_results', 'hematology', 'biochemistry', 'deranged_investigation'],
        60
      ));
      const dateWiseRows = allInvestigationRows.filter(function (line) {
        return /\b\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}\b/.test(line) || /\b\d{4}[\/\-]\d{2}[\/\-]\d{2}\b/.test(line);
      });
      const derangedRows = allInvestigationRows.filter(function (line) {
        return /\b(high|low|elevated|decreased|abnormal|deranged)\b/i.test(line);
      });

      const filteredEvidenceLines = (evidenceLines || []).filter(function (line) {
        const t = String(line || '').trim();
        if (!t) return false;
        if ((t.indexOf('{') === 0 || t.indexOf('[') === 0) && (t.indexOf('\"extracted_entities\"') >= 0 || t.indexOf('\"evidence_refs\"') >= 0)) return false;
        return t.length <= 700;
      });
      const clinicalFindingCandidates = extractTextByAliases(extractionPairs, ['clinical_findings', 'summary', 'hospital_finding', 'major_diagnostic_finding'], 25)
        .concat(filteredEvidenceLines.slice(0, 20));
      const clinicalFindingsText = (function () {
        const keep = [];
        const seen = new Set();
        clinicalFindingCandidates.forEach(function (block) {
          String(block || '').split(/\r?\n/).forEach(function (line) {
            const t = sanitizeReportText(line).replace(/^[\-ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¢]+\s*/, '').trim();
            if (!t) return;
            if (/^(?:patient\s*name|hospital\b|add\s*:|address\b|doa\b|dod\b|doa[_\s-]*date\b|dod[_\s-]*date\b|date\s*:|time\s*:|total\b|claimed\b|name\s+of\s+the\s+manager\b|signatures?\b|admission\s*date\b|date\s+of\s+admission\b|discharge\s*date\b|date\s+of\s+discharge\b|consultant\s+name\b|admit(?:ting|ing)\s*dr\b)/i.test(t)) return;
            if (/\b(?:treating\s*doctor|treating_doctor|doctor\s*reg(?:istration)?(?:\s*no|\s*number)?|treating_doctor_registration_number|date\s*of\s*admission|admission\s*date|date\s*of\s*discharge|discharge\s*date)\b/i.test(t)) return;
            if (/\b(?:details?\s+of\s+medication|follow\s*up\s+recommendation|follow\s*up(?:\s*to)?|review\s+after|in\s+case\s+of\s+emergency|call\s+us|contact\s*(?:us|no)?|helpline|ipd\s+medicine\s+bill|medicine\s+name|mobile(?:\s*number)?|phone(?:\s*number)?)\b/i.test(t)) return;
            if (/(?:\+?91[\s\-]*)?[6-9]\d{2}[\s\-]?\d{3}[\s\-]?\d{4}\b/.test(t)) return;
            if (/\b(?:hospital\s*address|full\s*postal\s*address|address\s*of\s*hospital|hospital\s*addr(?:ess)?)\b/i.test(t)) return;
            const commaCount = (t.match(/,/g) || []).length;
            const addressTokenHits = (t.match(/\b(?:road|rd|street|st|lane|ln|nagar|colony|city|district|state|pin|pincode|zip|plot|floor|building|plaza|near|opp|opposite|shop|apartment|apt|flat|unit|society|chsl|complex|tower|wing|sector|block|east|west|north|south|taluka|tehsil|vihar|chowk|village|gaon)\b/ig) || []).length;
            const clinicalTokenHits = (t.match(/\b(?:pain|swelling|fever|cough|breathlessness|vomiting|diarrhea|fracture|sepsis|infection|uti|pneumonia|tachycardia|hypotension|hypertension|bp|pulse|spo2|temperature|rbc|wbc|platelet|creatinine|sodium|potassium|diagnosis|edema|tenderness|discharge)\b/ig) || []).length;
            if (addressTokenHits >= 1 && clinicalTokenHits === 0 && /\b(?:road|rd|street|st|lane|ln|nagar|vihar|chowk|colony|sector|block|city|district|state|near|opp|opposite)\b/i.test(t)) return;
            if (/\b(?:shop|apartment|apt|flat|unit)\s*no\.?\s*\d+\b/i.test(t) && /\b\d{6}\b/.test(t)) return;
            if (/\bhospital\b/i.test(t) && commaCount >= 2 && addressTokenHits >= 1) return;
            if (addressTokenHits >= 2 && (/\d{3,}/.test(t) || /\b(?:pin|pincode|zip)\b/i.test(t))) return;
            if (addressTokenHits >= 1 && commaCount >= 2) return;
            const k = t.toLowerCase();
            if (seen.has(k)) return;
            seen.add(k);
            keep.push(t);
          });
        });
        return keep.length ? keep.slice(0, 24).join('\n') : 'No clinical findings extracted.';
      }());
      const allInvestigationText = firstNonEmpty(
        formatInvestigationListForReport(allInvestigationRows, 'No investigation reports available.'),
        'No investigation reports available.'
      );
      const dateWiseText = firstNonEmpty(
        formatInvestigationListForReport(dateWiseRows, 'No date-wise investigation reports available.'),
        'No date-wise investigation reports available.'
      );
      const derangedText = firstNonEmpty(
        formatInvestigationListForReport(derangedRows, 'No deranged investigation values found.'),
        'No deranged investigation values found.'
      );
      const tprDailySummaryText = buildDailyTprSummary(extractionPairs, evidenceLines);
      const rawDecisionSignal = firstNonEmpty(
        currentChecklistLatest && currentChecklistLatest.recommendation,
        stripHtmlTags(currentStatusItem && currentStatusItem.final_status),
        extractTextByAliases(extractionPairs, ['final_recommendation', 'recommendation'], 1)[0],
        'manual_review'
      );
      const admissionRequired = firstNonEmpty(
        extractTextByAliases(extractionPairs, ['admission_required', 'medically_required'], 1)[0],
        'uncertain'
      );
      const finalRecommendationDecision = mapFinalRecommendationDecision(rawDecisionSignal, admissionRequired);
      const admissionDecisionLabel = finalRecommendationDecision === 'ADMISSIBLE'
        ? 'Justified'
        : (finalRecommendationDecision === 'INADMISSIBLE' ? 'Not Justified' : 'Query');

      const checklistRows = Array.isArray(currentChecklistLatest && currentChecklistLatest.checklist)
        ? currentChecklistLatest.checklist
        : [];
      const complaintsForConclusion = summarizeForConclusion(chiefComplaints, 180);
      const findingsForConclusion = summarizeForConclusion(majorFinding !== '-' ? majorFinding : clinicalFindingsText, 260);
      const derangedForConclusion = summarizeForConclusion(derangedText, 220);
      const diagnosisForConclusion = summarizeForConclusion(diagnosis, 120) || 'the reported diagnosis';
      const treatmentForConclusion = buildCompactTreatmentSummary(medicineEvidenceText, 4);
      const tprForConclusion = summarizeForConclusion(tprDailySummaryText, 240);
      const admissionWindowText = String(admissionDateResolved.display || '-') + ' to ' + String(dischargeDateResolved.display || '-');
      const highEndSignal = summarizeForConclusion(extractTextByAliases(extractionPairs, ['high_end_antibiotic_for_rejection'], 1)[0], 220);
      const conclusionRuleContext = {
        diagnosis: diagnosisForConclusion,
        complaints: complaintsForConclusion,
        findings: findingsForConclusion,
        treatments: treatmentForConclusion,
        medicineEvidence: medicineEvidenceText
      };
      const checklistReporting = (currentChecklistLatest && currentChecklistLatest.source_summary && currentChecklistLatest.source_summary.reporting && typeof currentChecklistLatest.source_summary.reporting === 'object')
        ? currentChecklistLatest.source_summary.reporting
        : {};
      const checklistConclusionText = sanitizeConclusionText(checklistReporting.conclusion || '');
      const structuredConclusionText = sanitizeConclusionText(extractTextByAliases(extractionPairs, ['detailed_conclusion', 'conclusion', 'decision'], 2).join('\n'));
      const checklistRecommendationTextRaw = sanitizeConclusionText(checklistReporting.recommendation_text || '');
      const checklistRecommendationText = /\b(recommended\s+for\s+rejection|reject(?:ion|ed)?|inadmissible)\b/i.test(checklistRecommendationTextRaw)
        ? 'Claim is kept in query/pending clarification. Please provide required clinical evidence for final decision.'
        : checklistRecommendationTextRaw;
      const structuredRecommendationRaw = sanitizeConclusionText(extractTextByAliases(extractionPairs, ['recommendation_text', 'recommendation'], 2).join('\n'));
      const structuredRecommendationText = /\b(recommended\s+for\s+rejection|reject(?:ion|ed)?|inadmissible)\b/i.test(structuredRecommendationRaw)
        ? 'Claim is kept in query/pending clarification. Please provide required clinical evidence for final decision.'
        : structuredRecommendationRaw;
      const triggeredConditionNotesRaw = checklistRows
        .filter(function (entry) {
          if (!(entry && entry.triggered)) return false;
          const src = String(entry.source || "").trim().toLowerCase();
          if (!isChecklistRuleSource(src)) return false;
          const code = extractRuleCodeFromEntry(entry);
          if (/^R\d{3}$/.test(code) && !isRuleCodeRelevantForContext(code, conclusionRuleContext)) return false;
          return true;
        })
        .reduce(function (out, entry) {
          const note = sanitizeConclusionText(entry.note || entry.why_triggered || entry.summary || entry.reason || '')
            .replace(/\bOPENAI_MERGED_REVIEW\b/ig, '')
            .replace(/\b[Rr]\d{3}\b\s*[-:]\s*/g, '')
            .replace(/\bDX\d{3}\b\s*[-:]\s*/g, '')
            .replace(/\bMissing evidence\s*:\s*/ig, '');
          const cleanedNote = sanitizeConclusionText(note);
          if (cleanedNote
              && !/^(openai_claim_rules|openai_diagnosis_criteria)\b/i.test(cleanedNote)
              && !/openai_merged_review|merged\s+document\s+ai\s+medical\s+audit/i.test(cleanedNote)
              && !/^(reject|query|approve|admissible|inadmissible)$/i.test(cleanedNote)) {
            out.push(cleanedNote);
          }

          const missingList = Array.isArray(entry.missing_evidence) ? entry.missing_evidence : [];
          missingList.forEach(function (missing) {
            const cleanedMissing = sanitizeConclusionText(String(missing || ''));
            if (cleanedMissing) out.push(cleanedMissing);
          });
          return out;
        }, []);
      const seenTriggered = new Set();
      const triggeredConditionNotes = triggeredConditionNotesRaw.filter(function (item) {
        const key = String(item || '').trim().toLowerCase();
        if (!key || seenTriggered.has(key)) return false;
        seenTriggered.add(key);
        return true;
      }).slice(0, 6);
      let triggeredReasonText = triggeredConditionNotes.join('; ');
      let triggeredRuleSummaryText = buildTriggeredRuleSummary(checklistRows);
      const lengthOfStay = computeLengthOfStay(admissionDateResolved.iso, dischargeDateResolved.iso);
      triggeredReasonText = filterTriggeredReasonTextForContext(triggeredReasonText, conclusionRuleContext);
      triggeredRuleSummaryText = buildTriggeredRuleSummary(checklistRows, conclusionRuleContext);
      const rejectDetailedFallback = sanitizeConclusionText(
        'The patient appears to have been managed for ' + diagnosisForConclusion + ' during ' + admissionWindowText + ' (LOS: ' + lengthOfStay + '). '
        + 'Current records need additional clinical correlation before final admissibility decision. '
        + (complaintsForConclusion ? ('Chief complaints: ' + summarizeForConclusion(complaintsForConclusion, 120) + '. ') : '')
        + (findingsForConclusion ? ('Clinical findings: ' + summarizeForConclusion(findingsForConclusion, 150) + '. ') : '')
        + (derangedForConclusion && !/no deranged investigation values found/i.test(derangedForConclusion) ? ('Deranged investigations: ' + summarizeForConclusion(derangedForConclusion, 120) + '. ') : '')
        + (highEndSignal ? ('Antibiotic clarification: ' + summarizeForConclusion(highEndSignal, 140) + '. ') : '')
        + (triggeredReasonText ? ('Required clarifications: ' + summarizeForConclusion(triggeredReasonText, 160) + '. ') : '')
        + 'Please upload supporting clinical evidence for final decision.'
      );
      const queryDetailedFallback = sanitizeConclusionText(
        'Current records are insufficient for a final admissibility decision. '
        + (complaintsForConclusion ? ('Chief complaints: ' + summarizeForConclusion(complaintsForConclusion, 120) + '. ') : '')
        + (findingsForConclusion ? ('Clinical findings: ' + summarizeForConclusion(findingsForConclusion, 140) + '. ') : '')
        + (highEndSignal ? ('Antibiotic clarification required: ' + summarizeForConclusion(highEndSignal, 140) + '. ') : '')
        + (triggeredReasonText ? ('Required clarifications: ' + summarizeForConclusion(triggeredReasonText, 160) + '. ') : '')
        + 'Please provide missing medical evidence for final decision.'
      );
      let conclusionText = firstNonEmpty(
        checklistConclusionText,
        structuredConclusionText,
        sanitizeConclusionText(extractTextByAliases(extractionPairs, ['detailed_conclusion', 'conclusion', 'decision'], 2).join('\n')),
        sanitizeConclusionText(stripHtmlTags(currentStatusItem && currentStatusItem.final_status)),
        (finalRecommendationDecision === 'QUERY') ? queryDetailedFallback : '',
        'Claim reviewed based on available documents and clinical course.'
      );
      if (isWeakConclusionText(conclusionText)) {
        conclusionText = firstNonEmpty(
          checklistConclusionText,
          structuredConclusionText,
          (finalRecommendationDecision === 'INADMISSIBLE') ? rejectDetailedFallback : '',
          (finalRecommendationDecision === 'QUERY') ? queryDetailedFallback : '',
          (finalRecommendationDecision !== 'ADMISSIBLE') ? queryDetailedFallback : '',
          sanitizeConclusionText(stripHtmlTags(currentStatusItem && currentStatusItem.final_status)),
          conclusionText,
          'Claim reviewed based on available documents and clinical course.'
        );
      }
      if (checklistConclusionText && structuredConclusionText) {
        conclusionText = mergeRuleWithLearning(conclusionText, structuredConclusionText, '', 220);
      }
      let recommendationText = firstNonEmpty(checklistRecommendationText, structuredRecommendationText, '');
      if (!recommendationText) {
        if (finalRecommendationDecision === 'ADMISSIBLE') {
          recommendationText = 'Claim is payable.';
        } else if (finalRecommendationDecision === 'INADMISSIBLE') {
          recommendationText = 'Claim is kept in query/pending clarification. Please provide required clinical evidence for final decision.';
        } else {
          recommendationText = 'Claim is kept in query. Please provide desired information/documents.';
        }
      }
      const exactReasonText = buildExactReasonText(finalRecommendationDecision, {
        diagnosis: diagnosisForConclusion,
        complaints: complaintsForConclusion,
        findings: findingsForConclusion,
        treatments: treatmentForConclusion,
        deranged: derangedForConclusion,
        triggeredReasonText: triggeredReasonText,
        triggeredRuleSummary: triggeredRuleSummaryText,
        medicineEvidence: medicineEvidenceText,
        tprSummary: tprDailySummaryText,
        admissionWindowText: admissionWindowText,
        lengthOfStay: lengthOfStay
      });
      const ruleStyleConclusionText = buildRuleStyleConclusion(finalRecommendationDecision, {
        age: benefAge,
        gender: benefGender,
        complaints: complaintsForConclusion,
        diagnosis: diagnosisForConclusion,
        treatments: treatmentForConclusion,
        deranged: derangedForConclusion,
        reason: exactReasonText
      });
      const ruleDrivenConclusionText = buildRuleDrivenConclusion(checklistRows, {
        age: benefAge,
        gender: benefGender,
        complaints: complaintsForConclusion,
        diagnosis: diagnosisForConclusion,
        treatments: treatmentForConclusion,
        deranged: derangedForConclusion,
        reason: exactReasonText,
        finalDecision: finalRecommendationDecision,
        triggeredReasonText: triggeredReasonText,
        highEndSignal: highEndSignal,
        medicineEvidence: medicineEvidenceText,
        baseConclusion: ruleStyleConclusionText,
        externalClaimId: String((currentClaim && currentClaim.external_claim_id) || routeClaimId || '')
      });
      const preferredConclusionText = firstNonEmpty(ruleDrivenConclusionText, ruleStyleConclusionText, '');
      if (preferredConclusionText) {
        conclusionText = sanitizeConclusionText(preferredConclusionText) || preferredConclusionText;
      }

      const auditorLearningText = summarizeForConclusion(
        String(currentStatusItem && currentStatusItem.auditor_learning ? currentStatusItem.auditor_learning : '').replace(/\bR\d{3}\b\s*[-:]\s*/gi, ''),
        260
      );
      if (auditorLearningText) {
        conclusionText = mergeRuleWithLearning(conclusionText, auditorLearningText, '', 260);
      }

      const generatedMeta = firstNonEmpty(generatedAt, '-');
      const doctorMeta = firstNonEmpty(doctorName, '-');
      const treatingDoctorDisplay = (treatingDoctor && treatingDoctor !== '-')
        ? ((treatingDoctorRegistration && treatingDoctorRegistration !== '-')
            ? (treatingDoctor + ' (Registration No: ' + treatingDoctorRegistration + ')')
            : treatingDoctor)
        : '-';

      return '<h1 class="title">HEALTH CLAIM ASSESSMENT SHEET</h1>'
        + '<div class="meta">Generated: ' + escapeHtml(generatedMeta) + ' | Doctor: ' + escapeHtml(doctorMeta) + '</div>'
        + '<table class="t"><tbody>'
        + '<tr><th>COMPANY NAME</th><td>' + escapeHtml(companyName) + '</td></tr>'
        + '<tr><th>CLAIM NO.</th><td>' + escapeHtml(claimId) + '</td></tr>'
        + '<tr><th>CLAIM TYPE</th><td>' + escapeHtml(claimType) + '</td></tr>'
        + '<tr><th>INSURED</th><td>' + escapeHtml(insuredCombined) + '</td></tr>'
        + '<tr><th>HOSPITAL</th><td>' + escapeHtml(hospital) + '</td></tr>'
        + '<tr><th>TREATING DOCTOR</th><td>' + escapeHtml(treatingDoctorDisplay) + '</td></tr>'
        + '<tr><th>TREATING DOCTOR REGISTRATION NUMBER</th><td>' + escapeHtml(treatingDoctorRegistration) + '</td></tr>'
        + '<tr><th>ADMISSION</th><td>' + escapeHtml(admissionDateResolved.display) + '</td></tr>'
        + '<tr><th>DISCHARGE</th><td>' + escapeHtml(dischargeDateResolved.display) + '</td></tr>'
        + '<tr><th>LENGTH OF STAY</th><td>' + escapeHtml(lengthOfStay) + '</td></tr>'
        + '<tr><th>DIAGNOSIS</th><td><strong>' + escapeHtml(diagnosis) + '</strong></td></tr>'
        + '<tr><th>CHIEF COMPLAINTS AT ADMISSION</th><td>' + nl2brEsc(chiefComplaints) + '</td></tr>'
        + '<tr><th>MAJOR DIAGNOSTIC FINDING (ADMISSION / DURING STAY)</th><td>' + nl2brEsc(majorFinding) + '</td></tr>'
        + '<tr><th>ALCOHOLISM HISTORY</th><td>' + escapeHtml(alcoholHistory) + '</td></tr>'
        + '<tr><th>CLAIMED AMOUNT</th><td>' + escapeHtml(claimedAmount) + '</td></tr>'
        + '</tbody></table>'
        + '<div class="sec">CLINICAL FINDINGS</div>'
        + '<table class="t"><tbody><tr><td>' + nl2brEsc(clinicalFindingsText) + '</td></tr></tbody></table>'
        + '<div class="sec">ALL INVESTIGATION REPORTS</div>'
        + '<table class="t"><tbody><tr><td>' + nl2brEsc(allInvestigationText) + '</td></tr></tbody></table>'
        + '<div class="sec">DATE-WISE INVESTIGATION REPORTS</div>'
        + '<table class="t"><tbody><tr><td>' + nl2brEsc(dateWiseText) + '</td></tr></tbody></table>'
        + '<div class="sec">DERANGED INVESTIGATION REPORTS</div>'
        + '<table class="t"><tbody><tr><td>' + nl2brEsc(derangedText) + '</td></tr></tbody></table>'
        + '<div class="sec">DAILY TPR CHART (MIN/MAX)</div>'
        + '<table class="t"><tbody><tr><td>' + nl2brEsc(tprDailySummaryText) + '</td></tr></tbody></table>'
        + '<div class="sec">MEDICINE EVIDENCE USED</div>'
        + '<table class="t"><tbody><tr><td>' + nl2brEsc(medicineEvidenceText) + '</td></tr></tbody></table>'
        + '<div class="sec">CONCLUSION AND RECOMMENDATION</div>'
        + '<table class="t"><tbody>'
        + '<tr><th>Admission Required</th><td>' + escapeHtml(admissionDecisionLabel) + '</td></tr>'
        + '<tr><th>Final Recommendation</th><td>' + escapeHtml(finalRecommendationDecision) + '</td></tr>'
        + '<tr><th>Conclusion</th><td>' + nl2brEsc(conclusionText) + '</td></tr>'
        + '<tr><th>Recommendation</th><td>' + nl2brEsc(recommendationText) + '</td></tr>'
        + '</tbody></table>';
    }

    function buildReportPairsFromStructuredData(data) {
      const extractionPairs = [];
      const evidenceLines = [];
      if (!data || typeof data !== 'object') {
        return { extractionPairs: extractionPairs, evidenceLines: evidenceLines };
      }
      function addPair(key, value) {
        const textValue = sanitizeReportText(value);
        if (!textValue || textValue === '-') return;
        extractionPairs.push({ key: key, value: textValue });
      }

      addPair('company_name', data.company_name);
      addPair('claim_type', data.claim_type);
      addPair('name', data.insured_name);
      addPair('hospital_name', cleanHospitalDisplayName(data.hospital_name, ''));
      addPair('treating_doctor', data.treating_doctor);
      addPair('treating_doctor_registration_number', data.treating_doctor_registration_number);
      addPair('admission_date', data.doa);
      addPair('discharge_date', data.dod);
      addPair('diagnosis', data.diagnosis);
      addPair('chief_complaints', data.complaints);
      addPair('clinical_findings', data.findings);
      addPair('claimed_amount', data.claim_amount);
      addPair('detailed_conclusion', data.conclusion);
      addPair('recommendation', data.recommendation);
      addPair('high_end_antibiotic_for_rejection', data.high_end_antibiotic_for_rejection);

      const invText = String(data.investigation_finding_in_details || '').trim();
      if (invText && invText !== '-') {
        normalizeInvestigationRows(invText.split(/\r?\n/)).forEach(function (line) {
          const t = sanitizeReportText(line);
          if (t) extractionPairs.push({ key: 'all_investigation_reports_with_values', value: t });
        });
      }

      const medsText = String(data.medicine_used || '').trim();
      if (medsText && medsText !== '-') {
        extractionPairs.push({ key: 'treatment_medicines', value: medsText });
      }

      const recommendationText = String(data.recommendation || '').toLowerCase();
      let finalRec = 'QUERY';
      if (/rejection|reject|inadmissible|not\s+justified/.test(recommendationText)) finalRec = 'INADMISSIBLE';
      else if (/payable|admissible|approve/.test(recommendationText)) finalRec = 'ADMISSIBLE';
      addPair('final_recommendation', finalRec);
      addPair('admission_required', finalRec === 'INADMISSIBLE' ? 'no' : (finalRec === 'ADMISSIBLE' ? 'yes' : 'uncertain'));

      const deranged = sanitizeReportText(data.deranged_investigation || '');
      if (deranged && deranged !== '-') {
        normalizeInvestigationRows(deranged.split(/\r?\n/)).forEach(function (line) {
          const t = sanitizeReportText(line);
          if (t) extractionPairs.push({ key: 'all_investigation_reports_with_values', value: t });
        });
      }

      const abxSignal = sanitizeReportText(data.high_end_antibiotic_for_rejection || '');
      if (abxSignal && abxSignal !== '-') evidenceLines.push('High-end antibiotic check: ' + abxSignal);
      return { extractionPairs: extractionPairs, evidenceLines: evidenceLines };
    }

    async function buildLegacyReportHtmlFromLatestData() {
      const extractionPairs = [];
      const evidenceLines = [];
      const claimUuid = String((currentClaim && currentClaim.id) || routeClaimUuid || '').trim();
      if (claimUuid) {
        try {
          appendLog('Generating structured claim fields...');
          const structured = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/structured-data', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ use_llm: true, force_refresh: true }),
          });
          if (structured && typeof structured === 'object') {
            const mapped = buildReportPairsFromStructuredData(structured);
            if (Array.isArray(mapped.extractionPairs) && mapped.extractionPairs.length > 0) {
              appendLog('Structured fields ready (' + String(structured.source || 'heuristic') + ').');
              return buildLegacyReportHtml(new Date().toLocaleString(), String((me && me.username) || ''), mapped.extractionPairs, mapped.evidenceLines || []);
            }
          }
        } catch (_structuredErr) {
          appendLog('Structured field build unavailable; falling back to extraction history.');
        }
      }
      if (Array.isArray(currentDocs) && currentDocs.length > 0) {
        appendLog('Collecting full parse details from extraction history...');
      }

      await Promise.all((currentDocs || []).map(async function (doc) {
        const docId = String((doc && doc.id) || '').trim();
        const fileName = String((doc && doc.file_name) || '').trim();
        if (!docId) return;
        if (isKycIdentityDocName(fileName)) return;
        try {
          const history = await apiFetch('/api/v1/documents/' + encodeURIComponent(docId) + '/extractions?limit=1&offset=0');
          const latest = Array.isArray(history && history.items) && history.items.length > 0 ? history.items[0] : null;
          if (!latest) return;

          const entities = latest.extracted_entities && typeof latest.extracted_entities === 'object'
            ? latest.extracted_entities
            : {};

          const exclusionText = [
            entities.document_name,
            entities.text_preview,
            entities.kyc_exclusion_reason,
            entities.text_source,
          ].map(function (x) { return String(x || ''); }).join(' ');
          if (entities.kyc_excluded || isKycIdentityDocName(exclusionText)) return;

          const focusedName = toFlatText(entities.name || entities.patient_name || entities.beneficiary || '');
          if (focusedName && !isLikelyOrgName(focusedName)) extractionPairs.push({ key: 'name', value: focusedName });

          const focusedDiagnosis = toFlatText(entities.diagnosis || entities.final_diagnosis || '');
          if (focusedDiagnosis) extractionPairs.push({ key: 'diagnosis', value: focusedDiagnosis });

          const focusedClinical = toFlatText(entities.clinical_findings || entities.major_diagnostic_finding || entities.summary || '');
          if (focusedClinical) extractionPairs.push({ key: 'clinical_findings', value: focusedClinical });

          const focusedConclusion = toFlatText(entities.detailed_conclusion || entities.conclusion || entities.rationale || '');
          if (focusedConclusion) extractionPairs.push({ key: 'detailed_conclusion', value: focusedConclusion });

          const focusedInvestigations = entities.all_investigation_reports_with_values || entities.all_investigation_report_lines || entities.investigation_reports || [];
          if (Array.isArray(focusedInvestigations)) {
            focusedInvestigations.forEach(function (row) {
              if (row && typeof row === 'object') {
                const line = toFlatText(row.line || row.text || row.value || row.result || '');
                if (line) extractionPairs.push({ key: 'all_investigation_reports_with_values', value: line });
              } else {
                const line = toFlatText(row);
                if (line) extractionPairs.push({ key: 'all_investigation_reports_with_values', value: line });
              }
            });
          }

          const scalarFields = [
            'company_name', 'insurance_company', 'insurer', 'tpa',
            'claim_type', 'case_type', 'hospital_name', 'hospital',
            'treating_doctor', 'doctor_name', 'attending_doctor',
            'admission_date', 'date_of_admission', 'doa',
            'discharge_date', 'date_of_discharge', 'dod',
            'chief_complaints', 'chief_complaint', 'presenting_complaints',
            'major_diagnostic_finding', 'alcoholism_history', 'alcohol_history',
            'claim_amount', 'claimed_amount', 'bill_amount',
            'recommendation', 'final_recommendation',
            'admission_required', 'medically_required'
          ];
          scalarFields.forEach(function (fieldName) {
            const val = toFlatText(entities[fieldName]);
            if (val) extractionPairs.push({ key: fieldName, value: val });
          });
          appendEntityPairsFromObject(entities, extractionPairs, 250);

          const refs = Array.isArray(latest.evidence_refs) ? latest.evidence_refs : [];
          refs.forEach(function (entry) {
            if (!entry || typeof entry !== 'object') return;
            const snippet = toFlatText(entry.snippet || entry.text || entry.value || entry.note || '');
            if (!snippet) return;

            const looksLikeRawJson = (snippet.indexOf('{') === 0 || snippet.indexOf('[') === 0)
              && (snippet.indexOf('\"extracted_entities\"') >= 0 || snippet.indexOf('\"evidence_refs\"') >= 0);
            if (!looksLikeRawJson && !evidenceLines.includes(snippet)) {
              evidenceLines.push(snippet);
            }

            if (/\d/.test(snippet) && /\b(hb|hgb|hemoglobin|wbc|rbc|platelet|mcv|mch|mchc|rdw|creatinine|urea|bun|sodium|potassium|bilirubin|sgot|sgpt|alt|ast|urine|glucose)\b/i.test(snippet)) {
              extractionPairs.push({ key: 'all_investigation_reports_with_values', value: snippet });
            }
          });
        } catch (_err) {
          // Keep report generation resilient even when one document history call fails.
        }
      }));

      return buildLegacyReportHtml(new Date().toLocaleString(), String((me && me.username) || ''), extractionPairs, evidenceLines);
    }

    let currentClaim = null;
    let currentDocs = [];
    let currentChecklistLatest = { found: false, checklist: [] };
    let currentStatusItem = {};
    let hasGeneratedReport = false;
    let latestGeneratedReportHtml = '';
    function renderGeneratedReport() {
      if (!generatedReportEl) return;
      if (!hasGeneratedReport || !currentClaim) {
        generatedReportEl.className = 'case-generated-report muted';
        generatedReportEl.innerHTML = 'Click Generate Report to build latest report.';
        return;
      }

      generatedReportEl.className = 'case-generated-report';
      const finalHtml = latestGeneratedReportHtml || buildLegacyReportHtml(
        new Date().toLocaleString(),
        String((me && me.username) || ''),
        [],
        []
      );
      generatedReportEl.innerHTML = finalHtml;

      // Keep full-view editor in sync so hidden stale editor cannot override fresh extraction output.
      if (fullReportBodyEl) {
        const editor = fullReportBodyEl.querySelector('#report-editor');
        if (editor) editor.innerHTML = finalHtml;
      }
    }
    function syncReportFromEditor() {
      if (!fullReportBodyEl) return false;
      const editor = fullReportBodyEl.querySelector('#report-editor');
      if (!editor) return false;
      const editedHtml = String(editor.innerHTML || '').trim();
      if (!editedHtml) return false;
      latestGeneratedReportHtml = editedHtml;
      hasGeneratedReport = true;
      if (generatedReportEl) {
        generatedReportEl.className = 'case-generated-report';
        generatedReportEl.innerHTML = editedHtml;
      }
      return true;
    }

    function getCurrentReportHtml() {
      // Sync only when full editor is visible; hidden editor might contain old report HTML.
      if (fullReportViewEl && fullReportViewEl.style.display !== 'none') {
        syncReportFromEditor();
      }
      return String(latestGeneratedReportHtml || (generatedReportEl ? generatedReportEl.innerHTML : '') || '').trim();
    }
    function setFullReportOpen(open) {
      const mainPanel = contentPanel.querySelector('section.case-detail-panel');
      if (mainPanel) mainPanel.style.display = open ? 'none' : '';
      if (fullReportViewEl) fullReportViewEl.style.display = open ? '' : 'none';
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function openFullReportView() {
      const reportHtml = getCurrentReportHtml();
      if (!reportHtml) {
        setMessage('case-detail-msg', 'err', 'Generate report first.');
        return false;
      }

      if (fullReportBodyEl) {
        fullReportBodyEl.innerHTML = '<div class="sheet"><div id="report-editor" contenteditable="true">' + reportHtml + '</div></div>';
      }
      setFullReportOpen(true);
      appendLog('Full report opened in same page view.');
      return true;
    }
        function openReportInBrowserTab(reportHtml, targetWindow) {
      const html = String(reportHtml || "").trim();
      if (!html) return false;
      const claimUuidText = String(claimUuid || "").trim();
      const claimLabel = String((currentClaim && currentClaim.external_claim_id) || routeClaimId || "").trim();
      const actorIdText = String((me && me.username) || "doctor-ui").trim() || "doctor-ui";
      const title = claimLabel ? ("Claim Report - " + claimLabel) : "Claim Report";
      const draftKey = "qc_report_draft_" + String(Date.now()) + "_" + Math.random().toString(36).slice(2);
      const payload = {
        claim_uuid: claimUuidText,
        claim_id: claimLabel,
        actor_id: actorIdText,
        title: title,
        report_html: html,
        created_at: new Date().toISOString(),
      };
      const windowNamePayload = "qc_report_draft:" + JSON.stringify(payload);
      try {
        localStorage.setItem(draftKey, JSON.stringify(payload));
      } catch (_storageErr) {
        // Continue using window.name-based handoff as fallback.
      }
      const params = new URLSearchParams();
      params.set("draft_key", draftKey);
      params.set("claim_uuid", claimUuidText);
      params.set("claim_id", claimLabel);
      params.set("title", title);
      const url = "/qc/public/report-editor.html?" + params.toString();
      try {
        if (targetWindow && !targetWindow.closed) {
          try { targetWindow.name = windowNamePayload; } catch (_nameErr) {}
          targetWindow.location.href = url;
          try { targetWindow.focus(); } catch (_focusErr) {}
          return true;
        }
        const w = window.open(url, "_blank");
        if (!w) return false;
        try { w.name = windowNamePayload; } catch (_nameErr2) {}
        try { w.focus(); } catch (_focusErr2) {}
        return true;
      } catch (_openErr) {
        return false;
      }
    }

    async function grammarCheckReportHtmlForClaim(reportHtml, onLog) {
      const html = String(reportHtml || '').trim();
      if (!html) return html;

      const claimUuidText = String(claimUuid || '').trim();
      if (!claimUuidText) return html;

      try {
        const body = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuidText) + '/reports/grammar-check', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            report_html: html,
            actor_id: String((me && me.username) || 'doctor-ui'),
          }),
        });

        const correctedHtml = String(body && body.corrected_html ? body.corrected_html : '').trim();
        const changed = !!(body && body.changed);
        const correctedSegments = Number((body && body.corrected_segments) || 0);
        const checkedSegments = Number((body && body.checked_segments) || 0);
        const model = String((body && body.model) || '').trim();

        if (typeof onLog === 'function') {
          if (changed) {
            onLog('Grammar check applied: corrected ' + String(correctedSegments) + '/' + String(checkedSegments) + ' segment(s).' + (model ? (' Engine: ' + model) : ''));
          } else {
            onLog('Grammar check completed: no corrections needed.' + (model ? (' Engine: ' + model) : ''));
          }
        }

        return correctedHtml || html;
      } catch (err) {
        if (typeof onLog === 'function') {
          onLog('Grammar check skipped: ' + String(err && err.message ? err.message : err));
        }
        return html;
      }
    }

    function formatElapsedClock(elapsedMs) {
      const sec = Math.max(0, Math.floor(Number(elapsedMs || 0) / 1000));
      const mm = String(Math.floor(sec / 60)).padStart(2, '0');
      const ss = String(sec % 60).padStart(2, '0');
      return mm + ':' + ss;
    }

    function renderReportLoadingTab(tab, stageText, elapsedText) {
      if (!tab || tab.closed) return;
      const stage = String(stageText || 'Generating report...');
      const elapsed = String(elapsedText || '00:00');
      try {
        const docRef = tab.document;
        const statusEl = docRef.getElementById('report-gen-status');
        const timerEl = docRef.getElementById('report-gen-timer');
        if (statusEl && timerEl) {
          statusEl.textContent = stage;
          timerEl.textContent = 'Elapsed: ' + elapsed;
          return;
        }

        docRef.open();
        docRef.write('<!doctype html><html><head><meta charset="UTF-8"><title>Generating Report...</title>'
          + '<style>'
          + 'body{font-family:Arial,sans-serif;background:#f7f8fc;color:#1f2937;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}'
          + '.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:24px;min-width:340px;box-shadow:0 10px 30px rgba(0,0,0,.08)}'
          + '.row{display:flex;align-items:center;gap:10px}'
          + '.spinner{width:18px;height:18px;border:3px solid #d1d5db;border-top-color:#1d4ed8;border-radius:50%;animation:spin 1s linear infinite}'
          + '.stage{font-weight:600}'
          + '.timer{margin-top:10px;color:#6b7280;font-size:13px}'
          + '@keyframes spin{to{transform:rotate(360deg)}}'
          + '</style></head><body>'
          + '<div class="card"><div class="row"><div class="spinner"></div><div id="report-gen-status" class="stage">' + escapeHtml(stage) + '</div></div>'
          + '<div id="report-gen-timer" class="timer">Elapsed: ' + escapeHtml(elapsed) + '</div></div>'
          + '</body></html>');
        docRef.close();
      } catch (_err) {
        // Ignore tab rendering errors and continue report generation.
      }
    }

    async function checkExistingExtractionCoverage(docs, onProgress) {
      const rawDocs = Array.isArray(docs) ? docs : [];
      const eligibleDocs = rawDocs.filter(function (doc) {
        const docId = String((doc && doc.id) || '').trim();
        const docName = String((doc && doc.file_name) || '').trim();
        if (!docId) return false;
        return !isKycIdentityDocName(docName);
      });

      let withExtractionCount = 0;
      const missingDocs = [];
      let failedChecks = 0;

      for (let idx = 0; idx < eligibleDocs.length; idx += 1) {
        const doc = eligibleDocs[idx] || {};
        const docId = String(doc.id || '').trim();
        const docName = String(doc.file_name || ('Document ' + String(idx + 1)));
        if (typeof onProgress === 'function') onProgress(idx + 1, eligibleDocs.length, docName);

        try {
          const existing = await apiFetch('/api/v1/documents/' + encodeURIComponent(docId) + '/extractions?limit=1&offset=0');
          const hasExtraction = Number(existing && existing.total ? existing.total : 0) > 0;
          if (hasExtraction) {
            withExtractionCount += 1;
          } else {
            missingDocs.push(doc);
          }
        } catch (_err) {
          failedChecks += 1;
          missingDocs.push(doc);
        }
      }

      return {
        eligibleCount: eligibleDocs.length,
        withExtractionCount: withExtractionCount,
        missingDocs: missingDocs,
        failedChecks: failedChecks,
      };
    }
    async function saveCurrentReportToDb(reportSource, options) {
      if (!currentClaim) return;

      const opts = options || {};
      const silent = !!opts.silent;
      const manageBusy = opts.manageBusy !== false;
      let reportHtml = getCurrentReportHtml();

      if (!reportHtml) {
        appendLog('Current report HTML is empty. Attempting rebuild before save...');
        try {
          latestGeneratedReportHtml = await buildLegacyReportHtmlFromLatestData();
          renderGeneratedReport();
          reportHtml = getCurrentReportHtml();
        } catch (rebuildErr) {
          appendLog('Rebuild before save failed: ' + String(rebuildErr && rebuildErr.message ? rebuildErr.message : rebuildErr));
        }
      }

      if (!reportHtml) {
        try {
          const loaded = await loadSavedReportBySource(preferredReportSource || 'doctor', true);
          if (!loaded) await loadSavedReportBySource('system', true);
          reportHtml = getCurrentReportHtml();
        } catch (_loadErr) {
          // ignore and surface final message below
        }
      }

      if (!reportHtml) {
        if (!silent) setMessage('case-detail-msg', 'err', 'Generate report first, then save.');
        return;
      }
      if (!hasGeneratedReport) hasGeneratedReport = true;

      if (manageBusy) setActionDisabled(true);
      const targetSource = (String(reportSource || 'doctor').toLowerCase() === 'system') ? 'system' : 'doctor';
      if (!silent) setMessage('case-detail-msg', '', 'Saving ' + targetSource + ' report to DB...');
      appendLog('Save Report started.' + (silent ? ' (silent mode)' : ''));
      try {
        const saved = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/reports/html', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            report_html: reportHtml,
            report_status: 'draft',
            actor_id: (me && me.username) ? me.username : 'doctor-ui',
            report_source: targetSource,
          }),
        });
        if (!silent) setMessage('case-detail-msg', 'ok', 'Report saved in DB (' + targetSource + '). Version: ' + String(saved.version_no || '1'));
        appendLog('Report saved in DB (' + targetSource + '). version=' + String(saved.version_no || '1') + ', size=' + String(saved.html_size || 0) + ' bytes');
      } catch (err) {
        if (!silent) setMessage('case-detail-msg', 'err', err.message || 'Failed to save report.');
        appendLog('Save Report failed: ' + String(err && err.message ? err.message : err));
        throw err;
      } finally {
        if (manageBusy) setActionDisabled(false);
      }
    }

    async function loadSavedReportBySource(source, silentIfMissing, options) {
      const targetSource = (String(source || 'doctor').toLowerCase() === 'system') ? 'system' : 'doctor';
      try {
        const opts = options || {};
        const allowStale = opts.allowStale !== false;
        const payload = await apiFetch('/api/v1/user-tools/completed-reports/' + encodeURIComponent(claimUuid) + '/latest-html?source=' + encodeURIComponent(targetSource));
        const html = normalizeHealthClaimReportTitle(String(payload && payload.report_html ? payload.report_html : '').trim());
        if (!html) throw new Error('No saved report HTML found for this claim and source.');

        const reportCreatedAtMs = Date.parse(String((payload && payload.created_at) || ''));
        const checklistGeneratedAtMs = Date.parse(String((currentChecklistLatest && currentChecklistLatest.generated_at) || ''));
        const isSavedReportStale = Number.isFinite(reportCreatedAtMs)
          && Number.isFinite(checklistGeneratedAtMs)
          && checklistGeneratedAtMs > reportCreatedAtMs;

        if (isSavedReportStale) {
          if (!allowStale) {
            appendLog((targetSource === 'system' ? 'System' : 'Doctor') + ' saved report is older than latest checklist decision. Skipping stale report load.');
            return false;
          }
          appendLog((targetSource === 'system' ? 'System' : 'Doctor') + ' saved report is older than latest checklist decision. Loading stale report for manual edit/save.');
          if (!silentIfMissing) {
            setMessage('case-detail-msg', '', 'Loaded ' + (targetSource === 'system' ? 'system' : 'doctor') + ' report (older than latest checklist). You can edit and save it.');
          }
        }

        latestGeneratedReportHtml = html;
        hasGeneratedReport = true;
        preferredReportSource = targetSource;
        renderGeneratedReport();
        appendLog((targetSource === 'system' ? 'System' : 'Doctor') + ' saved report loaded.');
        if (!silentIfMissing) {
          setMessage('case-detail-msg', 'ok', (targetSource === 'system' ? 'System-generated' : 'Doctor-saved') + ' report loaded.');
        }
        return true;
      } catch (err) {
        const msg = String(err && err.message ? err.message : (err || ''));
        if (silentIfMissing && msg.toLowerCase().indexOf('no saved report html') >= 0) return false;
        if (!silentIfMissing) {
          setMessage('case-detail-msg', 'err', msg || 'Failed to load saved report.');
        }
        appendLog('Load saved report failed (' + targetSource + '): ' + (msg || 'unknown error'));
        return false;
      }
    }

    async function loadDetail() {
      setFullReportOpen(false);
      setMessage('case-detail-msg', '', 'Loading case details...');
      appendLog('Loading claim and document details.');

      const claim = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid));
      currentClaim = claim;

      const [docsResult, checklistLatest] = await Promise.all([
        apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/documents?limit=200&offset=0'),
        apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/checklist/latest').catch(function () {
          return { found: false, checklist: [] };
        }),
      ]);

      const statusParams = new URLSearchParams();
      statusParams.set('search_claim', String(claim.external_claim_id || routeClaimId || ''));
      if (me && me.role === 'doctor' && me.username) statusParams.set('doctor_filter', me.username);
      statusParams.set('status_filter', 'all');
      statusParams.set('limit', '50');
      statusParams.set('offset', '0');

      const statusResult = await apiFetch('/api/v1/user-tools/claim-document-status?' + statusParams.toString()).catch(function () {
        return { items: [] };
      });

      const statusItems = Array.isArray(statusResult && statusResult.items) ? statusResult.items : [];
      const statusItem = statusItems.find(function (item) {
        return String(item && item.id ? item.id : '') === claimUuid;
      }) || {};
      const detailRows = [];
      detailRows.push(summaryRow('Claim ID', asTextCell(claim.external_claim_id || routeClaimId || '-')));
      detailRows.push(summaryRow('Claim Date', asTextCell(formatDateOnly(claim.created_at))));
      detailRows.push(summaryRow('Beneficiary', asTextCell(claim.patient_name || '-')));
      detailRows.push(summaryRow('Patient Identifier', asTextCell(claim.patient_identifier || '-')));
      detailRows.push(summaryRow('Assigned Doctor', asTextCell(claim.assigned_doctor_id || '-')));
      detailRows.push(summaryRow('Allotment Date', asTextCell(formatDateOnly(statusItem.allotment_date || ''))));
      detailRows.push(summaryRow('Import DOA', asTextCell(formatDateOnly(statusItem.doa_date || ''))));
      detailRows.push(summaryRow('Import DOD', asTextCell(formatDateOnly(statusItem.dod_date || ''))));
      detailRows.push(summaryRow('Assigned At', asTextCell(formatDateTime(statusItem.assigned_at || ''))));
      detailRows.push(summaryRow('Documents', asTextCell(String(statusItem.documents || (docsResult.total || 0)))));
      detailRows.push(summaryRow('Last Upload', asTextCell(formatDateTime(statusItem.last_upload || ''))));
      detailRows.push(summaryRow('Current Status', asTextCell(formatStatusText(claim.status || '-'))));
      detailRows.push(summaryRow('Final Status', asTextCell(statusItem.final_status || (checklistLatest && checklistLatest.recommendation ? checklistLatest.recommendation : 'Pending'))));
      detailRows.push(summaryRow('Doctor Opinion', asTextCell(statusItem.opinion || '-')));
      detailRows.push(summaryRow('Priority', asTextCell(String(claim.priority || '-'))));
      detailRows.push(summaryRow('Source Channel', asTextCell(claim.source_channel || '-')));
      detailRows.push(summaryRow('Tags', asTextCell(Array.isArray(claim.tags) ? claim.tags.join(', ') : '-')));
      summaryEl.innerHTML = detailRows.join('');

      const docs = Array.isArray(docsResult && docsResult.items) ? docsResult.items : [];
      currentDocs = docs;
      currentChecklistLatest = checklistLatest || { found: false, checklist: [] };
      currentStatusItem = statusItem || {};
      const docRows = docs.map(function (doc) {
        return '<tr>'
          + '<td>' + escapeHtml(doc.file_name || '-') + '</td>'
          + '<td>' + statusChip(doc.parse_status || '-') + '</td>'
          + '<td>' + escapeHtml(doc.uploaded_by || '-') + '</td>'
          + '<td>' + escapeHtml(formatDateTime(doc.uploaded_at || '')) + '</td>'
          + '<td><button type="button" class="btn-soft" data-doc-open="' + escapeHtml(doc.id || '') + '">Open</button></td>'
          + '</tr>';
      }).join('');
      docsEl.innerHTML = docRows || '<tr><td colspan="5">No documents found for this claim.</td></tr>';

      docsEl.querySelectorAll('button[data-doc-open]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          const docId = String(this.getAttribute('data-doc-open') || '').trim();
          if (!docId) return;
          try {
            const dl = await apiFetch('/api/v1/documents/' + encodeURIComponent(docId) + '/download-url?expires_in=900');
            if (dl && dl.download_url) {
              window.open(dl.download_url, '_blank', 'noopener');
            }
          } catch (err) {
            setMessage('case-detail-msg', 'err', err.message || 'Failed to open document.');
          }
        });
      });
      const loadedPreferred = await loadSavedReportBySource(preferredReportSource, true, { allowStale: false });
      if (!loadedPreferred && preferredReportSource === 'doctor') {
        await loadSavedReportBySource('system', true, { allowStale: false });
      }
      renderGeneratedReport();
      setMessage('case-detail-msg', 'ok', 'Case detail loaded.');
      appendLog('Case detail loaded for claim ' + String(claim.external_claim_id || routeClaimId || claimUuid) + '.');
    }

    async function runPipelineAction(force, preferOpenAI, label, strictOpenAI, extraOptions) {
      const pipelineOptions = extraOptions || {};
      setActionDisabled(true);
      setMessage('case-detail-msg', '', label + ' in progress...');
      appendLog(label + ' started.');
      try {
        const summary = await runCasePreparationPipeline(claimUuid, (me && me.username) ? me.username : 'doctor-ui', {
          force: force,
          preferOpenAI: preferOpenAI,
          strictOpenAI: !!strictOpenAI,
          extractionProvider: pipelineOptions.extractionProvider || 'openai',
          allowAutoFallback: pipelineOptions.allowAutoFallback !== false,
          onProgress: function (index, total, fileName) {
            setMessage('case-detail-msg', '', label + ': processing ' + String(index) + '/' + String(total) + ' (' + String(fileName || 'document') + ')');
          },
          onLog: appendLog,
        });

        if (summary.checklistError) {
          setMessage(
            'case-detail-msg',
            'err',
            label + ' completed with checklist error. New extractions: ' + String(summary.extractedCount) + ', skipped: ' + String(summary.skippedCount) + ', failed: ' + String(summary.failedCount)
          );
        } else {
          setMessage(
            'case-detail-msg',
            'ok',
            label + ' completed. New extractions: ' + String(summary.extractedCount) + ', skipped: ' + String(summary.skippedCount) + ', failed: ' + String(summary.failedCount)
          );
        }

        await loadDetail();
      } catch (err) {
        setMessage('case-detail-msg', 'err', (err && err.message) ? err.message : (label + ' failed'));
        appendLog(label + ' failed: ' + String(err && err.message ? err.message : err));
      } finally {
        setActionDisabled(false);
      }
    }
    analyzeBtn.addEventListener('click', async function () {
      await runPipelineAction(false, true, 'Analyze Admission Need (VerifAI)', true, { extractionProvider: 'openai', allowAutoFallback: false });
    });

    forceBtn.addEventListener('click', async function () {
      await runPipelineAction(true, true, 'Force VerifAI Analyzer', true, { extractionProvider: 'openai', allowAutoFallback: false });
    });

    if (diagnosisChecklistBtn) {
      diagnosisChecklistBtn.addEventListener('click', async function () {
        await runDiagnosisChecklistGeneration();
      });
    }

    reportBtn.addEventListener('click', async function () {
      const reportTab = window.open('', '_blank');
      const startedAt = Date.now();
      let stageText = 'Checking existing AI response...';
      const renderProgress = function () {
        const elapsed = formatElapsedClock(Date.now() - startedAt);
        setMessage('case-detail-msg', '', stageText + ' (' + elapsed + ')');
        renderReportLoadingTab(reportTab, stageText, elapsed);
      };

      renderProgress();
      const timer = window.setInterval(renderProgress, 1000);
      hasGeneratedReport = true;
      setActionDisabled(true);
      appendLog('Generate Report started.');

      try {
        stageText = 'Refreshing latest decision...';
        renderProgress();
        try {
          const latestChecklist = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/checklist/latest');
          if (latestChecklist && latestChecklist.found) {
            currentChecklistLatest = latestChecklist;
            appendLog('Latest checklist decision refreshed: ' + String(latestChecklist.recommendation || 'unknown'));
          }
          appendLog('Running fresh checklist evaluation for report conclusion...');
          const evaluatedChecklist = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/checklist/evaluate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ actor_id: (me && me.username) ? me.username : 'doctor-ui', force_source_refresh: false }),
          });
          if (evaluatedChecklist && Array.isArray(evaluatedChecklist.checklist)) {
            currentChecklistLatest = evaluatedChecklist;
            appendLog('Checklist evaluation completed for report generation: ' + String(evaluatedChecklist.recommendation || 'unknown'));
          }
        } catch (refreshErr) {
          appendLog('Checklist refresh before report generation failed: ' + String(refreshErr && refreshErr.message ? refreshErr.message : refreshErr));
        }

        const docsForCoverage = Array.isArray(currentDocs) ? currentDocs : [];
        const coverage = await checkExistingExtractionCoverage(docsForCoverage, function (index, total, fileName) {
          stageText = 'Checking existing AI response: ' + String(index) + '/' + String(total) + ' (' + String(fileName || 'document') + ')';
          renderProgress();
        });

        appendLog('Existing extraction coverage: ' + String(coverage.withExtractionCount) + '/' + String(coverage.eligibleCount) + ' clinical document(s).');

        if ((coverage.missingDocs || []).length > 0) {
          const missing = Number((coverage.missingDocs || []).length || 0);
          const msg = 'Please generate response first. Missing AI response for ' + String(missing) + ' document(s). Click Analyze Admission Need (AI) first.';
          setMessage('case-detail-msg', 'err', msg);
          appendLog(msg);
          renderReportLoadingTab(reportTab, 'Please generate response first.', formatElapsedClock(Date.now() - startedAt));
          try {
            if (reportTab && !reportTab.closed) reportTab.close();
          } catch (_err) {
            // ignore
          }
          return;
        }

        appendLog('Existing AI response found for all clinical documents. Reusing extraction data for report.');

        stageText = 'Generating report from existing AI response...';
        renderProgress();
        latestGeneratedReportHtml = await buildLegacyReportHtmlFromLatestData();
        if (!String(latestGeneratedReportHtml || '').trim()) {
          appendLog('Generated report HTML was empty. Falling back to template report.');
          latestGeneratedReportHtml = buildLegacyReportHtml(
            new Date().toLocaleString(),
            String((me && me.username) || ''),
            [],
            []
          );
        }

        stageText = 'Finalizing report...';
        renderProgress();

        renderGeneratedReport();

        stageText = 'Saving report...';
        renderProgress();
        await saveCurrentReportToDb('system', { silent: true, manageBusy: false });

        const finalHtml = String(getCurrentReportHtml() || latestGeneratedReportHtml || '').trim();
        const opened = openReportInBrowserTab(finalHtml, reportTab || null);
        if (opened) {
          setMessage('case-detail-msg', 'ok', 'Report generated and opened in new tab.');
          appendLog('Report generated and opened in new tab.');
        } else {
          try {
            if (reportTab && !reportTab.closed) reportTab.close();
          } catch (_closeErr) {
            // ignore
          }
          setMessage('case-detail-msg', 'err', 'Could not open new tab. Please allow popups and click Generate Report again.');
          appendLog('Report open failed: popup blocked or tab unavailable.');
        }
      } catch (err) {
        const errMsg = String(err && err.message ? err.message : err || 'Report generation failed');
        setMessage('case-detail-msg', 'err', errMsg);
        appendLog('Generate Report failed: ' + errMsg);
        renderReportLoadingTab(reportTab, 'Report generation failed', formatElapsedClock(Date.now() - startedAt));
      } finally {
        window.clearInterval(timer);
        setActionDisabled(false);
      }
    });



    saveReportBtn.addEventListener('click', async function () {
      await saveCurrentReportToDb('doctor');
    });

    saveReportFullBtn.addEventListener('click', async function () {
      await saveCurrentReportToDb('doctor');
    });

    backFromFullBtn.addEventListener('click', function () {
      syncReportFromEditor();
      setFullReportOpen(false);
      appendLog('Returned from full report view to case detail.');
    });

    window.addEventListener('message', async function (event) {
      try {
        if (!event || event.origin !== window.location.origin) return;
        const payload = (event.data && typeof event.data === 'object') ? event.data : null;
        if (!payload || payload.type !== 'report-saved-from-tab') return;
        if (String(payload.claim_uuid || '') !== String(claimUuid || '')) return;
        appendLog('Report saved from new tab. Refreshing latest doctor report.');
        await loadSavedReportBySource('doctor', true);
        setMessage('case-detail-msg', 'ok', 'Report saved from new tab and refreshed here.');
      } catch (_syncErr) {
      }
    });
    if (sendBackBtn) {
      sendBackBtn.addEventListener('click', async function () {
        if (!currentClaim) return;
        const opinion = String(window.prompt('Enter auditor opinion to send this case back to doctor:', '') || '').trim();
        if (!opinion) {
          setMessage('case-detail-msg', 'err', 'Auditor opinion is required.');
          return;
        }

        setActionDisabled(true);
        setMessage('case-detail-msg', '', 'Sending case back to doctor...');
        appendLog('Send back to doctor started.');
        try {
          await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/status', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'in_review', note: opinion }),
          });
          setMessage('case-detail-msg', 'ok', 'Case sent back to doctor.');
          appendLog('Case sent back to doctor with auditor opinion.');
          await loadDetail();
        } catch (err) {
          setMessage('case-detail-msg', 'err', err.message || 'Send back failed.');
          appendLog('Send back failed: ' + String(err && err.message ? err.message : err));
        } finally {
          setActionDisabled(false);
        }
      });
    }

    statusBtn.addEventListener('click', async function () {
      if (!currentClaim) return;
      const target = 'completed';
      if (String(currentClaim.status || '').trim().toLowerCase() === 'completed') {
        setMessage('case-detail-msg', 'ok', 'Case is already Completed.');
        return;
      }

      setActionDisabled(true);
      setMessage('case-detail-msg', '', 'Updating status to Completed...');
      appendLog('Status update started: ' + target);
      try {
        await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/status', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: target }),
        });
        setMessage('case-detail-msg', 'ok', 'Status updated to Completed.');
        appendLog('Status updated to ' + target + '.');
        await loadDetail();
      } catch (err) {
        setMessage('case-detail-msg', 'err', err.message || 'Status update failed.');
        appendLog('Status update failed: ' + String(err && err.message ? err.message : err));
      } finally {
        setActionDisabled(false);
      }
    });

    try {
      await loadDetail();
    } catch (err) {
      setMessage('case-detail-msg', 'err', err.message || 'Failed to load case detail.');
      appendLog('Case detail load failed: ' + String(err && err.message ? err.message : err));
    }
  }

  async function renderUploadDocument() {
    const me = await apiFetch('/api/v1/auth/me');
    const doctors = await fetchDoctors();
    const doctorFilterOptions = '<option value="">All Doctors</option>'
      + doctors.map((d) => '<option value="' + escapeHtml(d) + '">' + escapeHtml(d) + '</option>').join('');
    const state = { page: 1, pageSize: 20, total: 0 };
    const viewState = { claimUuid: '', claimId: '', total: 0, sourceFiles: 0 };

    contentPanel.innerHTML = '<section class="claim-status-panel">'
      + '<h2 class="claim-status-title">Claim Document Status</h2>'
      + '<form id="upload-doc-filter-form" class="claim-status-filters">'
      + '<div class="claim-filter-group"><label for="upload-doc-search">Search Claim</label><input id="upload-doc-search" name="search_claim" placeholder="Claim ID"></div>'
      + '<div class="claim-filter-group"><label for="upload-doc-allotment-date">Allotment Date</label><input id="upload-doc-allotment-date" type="date" name="allotment_date"></div>'
      + '<div class="claim-filter-group"><label for="upload-doc-status-filter">Filter</label><select id="upload-doc-status-filter" name="status_filter">'
      + '<option value="all">All Claims</option>'
      + '<option value="pending">Pending</option>'
      + '<option value="ready_for_assignment">Ready For Assignment</option>'
      + '<option value="waiting_for_documents">Awaiting Documents</option>'
      + '<option value="in_review">In Review</option>'
      + '<option value="needs_qc">Needs QC</option>'
      + '<option value="completed">Completed</option>'
      + '</select></div>'
      + '<div class="claim-filter-group"><label for="upload-doc-document-upload-filter">Document Upload</label><select id="upload-doc-document-upload-filter" name="document_upload"><option value="all">All</option><option value="yes">Yes</option><option value="no">No</option></select></div>'
      + '<div class="claim-filter-group"><label for="upload-doc-doctor-filter">Doctor</label><select id="upload-doc-doctor-filter" name="doctor_filter">' + doctorFilterOptions + '</select></div>'
      + '<div class="claim-filter-action"><button type="submit" class="claim-apply-btn">Apply</button></div>'
      + '</form>'
      + '<p id="upload-doc-list-msg"></p>'
      + '<div class="table-wrap claim-status-table-wrap"><table><thead><tr>'
      + '<th><button type="button" class="table-sort-btn" data-upload-doc-sort="claim_id">CLAIM ID</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-upload-doc-sort="treatment_type">TREATMENT TYPE</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-upload-doc-sort="assigned_doctor">ASSIGNED DOCTOR</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-upload-doc-sort="allotment_date">ALLOTMENT DATE</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-upload-doc-sort="document_upload">DOCUMENT UPLOAD</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-upload-doc-sort="merge_summary">MERGE SUMMARY</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-upload-doc-sort="uploaded_by">UPLOADED BY</button></th>'
      + '<th><button type="button" class="table-sort-btn" data-upload-doc-sort="last_upload">LAST UPLOAD</button></th>'
      + '<th>ACTION</th>'
      + '</tr></thead><tbody id="upload-doc-tbody"><tr><td colspan="9">Loading...</td></tr></tbody></table></div>'
      + '<div class="claim-pagination">'
      + '<div class="claim-pagination__left"><label for="upload-doc-page-size">Rows</label><select id="upload-doc-page-size"><option value="10">10</option><option value="20" selected>20</option><option value="50">50</option></select></div>'
      + '<div class="claim-pagination__info" id="upload-doc-page-info">Showing 0-0 of 0</div>'
      + '<div class="claim-pagination__actions"><button type="button" class="btn-soft" id="upload-doc-prev-page">Previous</button><button type="button" class="btn-soft" id="upload-doc-next-page">Next</button></div>'
      + '</div>'
      + '<input id="upload-doc-file-input" type="file" style="display:none;" multiple>'
      + '<div id="upload-doc-modal" class="modal-backdrop">'
      + '<div class="modal-card wide upload-doc-modal-card" role="dialog" aria-modal="true" aria-labelledby="upload-doc-modal-title">'
      + '<div class="modal-header">'
      + '<h3 id="upload-doc-modal-title">Previously Uploaded Documents</h3>'
      + '<button type="button" class="btn-soft" id="upload-doc-modal-close">Close</button>'
      + '</div>'
      + '<p class="muted" id="upload-doc-modal-subtitle">Claim ID: -</p>'
      + '<p id="upload-doc-modal-msg"></p>'
      + '<div class="link-row upload-doc-modal-actions">'
      + '<button type="button" class="btn-soft" id="upload-doc-modal-select-all">Select All</button>'
      + '<button type="button" class="btn-soft" id="upload-doc-modal-clear">Clear Selection</button>'
      + '<button type="button" id="upload-doc-modal-delete">Delete Selected</button>'
      + '</div>'
      + '<div class="table-wrap view-documents-table-wrap"><table><thead><tr>'
      + '<th class="claim-select-col"><input id="upload-doc-modal-select-page" type="checkbox"></th>'
      + '<th>File Name</th><th>Merged Files</th><th>Size</th><th>Parse Status</th><th>Uploaded By</th><th>Uploaded At</th><th>Action</th>'
      + '</tr></thead><tbody id="upload-doc-modal-tbody"><tr><td colspan="8">Select a claim to view documents.</td></tr></tbody></table></div>'
      + '<p class="muted upload-doc-modal-count" id="upload-doc-modal-count">Total documents: 0 | Source files: 0 | Selected: 0</p>'
      + '</div>'
      + '</div>'
      + '</section>';

    const form = document.getElementById('upload-doc-filter-form');
    const tbody = document.getElementById('upload-doc-tbody');
    const fileInput = document.getElementById('upload-doc-file-input');
    const pageSizeEl = document.getElementById('upload-doc-page-size');
    const prevBtn = document.getElementById('upload-doc-prev-page');
    const nextBtn = document.getElementById('upload-doc-next-page');
    const pageInfoEl = document.getElementById('upload-doc-page-info');
    const uploadDocSortButtons = Array.from(contentPanel.querySelectorAll('button[data-upload-doc-sort]'));

    const modalEl = document.getElementById('upload-doc-modal');
    const modalSubtitleEl = document.getElementById('upload-doc-modal-subtitle');
    const modalMsgEl = document.getElementById('upload-doc-modal-msg');
    const modalTbody = document.getElementById('upload-doc-modal-tbody');
    const modalCountEl = document.getElementById('upload-doc-modal-count');
    const modalCloseBtn = document.getElementById('upload-doc-modal-close');
    const modalSelectAllBtn = document.getElementById('upload-doc-modal-select-all');
    const modalClearBtn = document.getElementById('upload-doc-modal-clear');
    const modalDeleteBtn = document.getElementById('upload-doc-modal-delete');
    const modalSelectPageEl = document.getElementById('upload-doc-modal-select-page');
    const uploadDocSortState = { key: 'allotment_date', dir: 'desc' };
    const uploadDocSortConfig = {
      claim_id: { index: 0, type: 'number', defaultDir: 'asc' },
      treatment_type: { index: 1, type: 'text', defaultDir: 'asc' },
      assigned_doctor: { index: 2, type: 'text', defaultDir: 'asc' },
      allotment_date: { index: 3, type: 'date', defaultDir: 'desc' },
      document_upload: { index: 4, type: 'bool', defaultDir: 'desc' },
      merge_summary: { index: 5, type: 'number', defaultDir: 'desc' },
      uploaded_by: { index: 6, type: 'text', defaultDir: 'asc' },
      last_upload: { index: 7, type: 'date', defaultDir: 'desc' },
    };

    uploadDocSortButtons.forEach(function (btn) {
      btn.setAttribute('data-sort-label', String(btn.textContent || '').trim());
    });

    function formatAssignedDoctor(value) {
      return String(value || '').split(',').map((s) => s.trim()).filter(Boolean)[0] || 'Unassigned';
    }

    function setModalMessage(type, text) {
      if (!modalMsgEl) return;
      modalMsgEl.className = type ? 'msg ' + type : '';
      modalMsgEl.textContent = text || '';
    }

    function isModalOpen() {
      return !!(modalEl && modalEl.classList.contains('open'));
    }

    function isModalBusy() {
      return !!(modalEl && modalEl.getAttribute('data-busy') === '1');
    }

    function setModalBusy(busy, text) {
      if (!modalEl) return;
      modalEl.setAttribute('data-busy', busy ? '1' : '0');

      const controls = [modalCloseBtn, modalSelectAllBtn, modalClearBtn, modalDeleteBtn, modalSelectPageEl];
      controls.forEach(function (el) {
        if (!el) return;
        el.disabled = !!busy;
      });

      if (typeof text === 'string' && text) {
        setModalMessage('', text);
      }

      if (!busy) {
        updateModalSelectionUi();
      }
    }

    function openModal() {
      if (!modalEl) return;
      modalEl.classList.add('open');
    }

    function closeModal() {
      if (!modalEl) return;
      if (isModalBusy()) {
        setModalMessage('', 'Upload in progress. Please wait...');
        return;
      }
      modalEl.classList.remove('open');
      setModalMessage('', '');
    }

    function getSelectedModalDocIds() {
      return Array.from(modalTbody.querySelectorAll('input[data-modal-doc-select]:checked'))
        .map((el) => String(el.value || '').trim())
        .filter(Boolean);
    }

    function updateModalSelectionUi() {
      const allCheckboxes = Array.from(modalTbody.querySelectorAll('input[data-modal-doc-select]'));
      const selected = getSelectedModalDocIds().length;
      const total = allCheckboxes.length;
      if (modalCountEl) {
        modalCountEl.textContent = 'Total documents: ' + String(viewState.total || total) + ' | Source files: ' + String(viewState.sourceFiles || 0) + ' | Selected: ' + String(selected);
      }
      if (modalDeleteBtn) {
        modalDeleteBtn.disabled = selected === 0;
      }
      if (modalSelectPageEl) {
        modalSelectPageEl.checked = total > 0 && selected === total;
        modalSelectPageEl.indeterminate = selected > 0 && selected < total;
      }
    }

    function updatePaginationUi() {
      const total = Number(state.total || 0);
      const startRow = total === 0 ? 0 : ((state.page - 1) * state.pageSize + 1);
      const endRow = Math.min(state.page * state.pageSize, total);
      pageInfoEl.textContent = 'Showing ' + startRow + '-' + endRow + ' of ' + total;
      prevBtn.disabled = state.page <= 1;
      nextBtn.disabled = endRow >= total;
    }

    function parseUploadDocSortValue(text, type) {
      const raw = String(text || '').trim();
      if (!raw) return type === 'text' ? '' : 0;
      if (type === 'number') {
        const match = raw.match(/-?\d+(?:\.\d+)?/);
        const num = match ? Number(match[0]) : Number(raw.replace(/[^\d.-]+/g, ''));
        return Number.isFinite(num) ? num : 0;
      }
      if (type === 'bool') {
        const v = raw.toLowerCase();
        if (v === 'yes') return 1;
        if (v === 'no') return 0;
        return v ? 1 : 0;
      }
      if (type === 'date') {
        const ts = Date.parse(raw);
        if (!Number.isNaN(ts)) return ts;
        const m = raw.match(/^(\d{1,2})[-/](\d{1,2})[-/](\d{4})/);
        if (m) return Date.UTC(Number(m[3]), Number(m[2]) - 1, Number(m[1]));
        return 0;
      }
      return raw.toLowerCase();
    }

    function updateUploadDocSortButtonsUi() {
      uploadDocSortButtons.forEach(function (btn) {
        const key = String(btn.getAttribute('data-upload-doc-sort') || '').trim();
        const baseLabel = String(btn.getAttribute('data-sort-label') || '').trim();
        const active = key && key === uploadDocSortState.key;
        btn.classList.toggle('is-active', !!active);
        btn.textContent = active ? (baseLabel + (uploadDocSortState.dir === 'asc' ? ' ^' : ' v')) : baseLabel;
      });
    }

    function applyUploadDocSort() {
      const cfg = uploadDocSortConfig[uploadDocSortState.key];
      if (!cfg || !tbody) {
        updateUploadDocSortButtonsUi();
        return;
      }
      const rows = Array.from(tbody.querySelectorAll('tr')).filter(function (tr) {
        return tr && tr.children && tr.children.length >= 9;
      });
      if (!rows.length) {
        updateUploadDocSortButtonsUi();
        return;
      }

      const dirMult = uploadDocSortState.dir === 'asc' ? 1 : -1;
      const decorated = rows.map(function (row, idx) {
        const cell = row.children[cfg.index];
        const value = parseUploadDocSortValue(cell ? cell.textContent : '', cfg.type);
        return { row: row, idx: idx, value: value };
      });

      decorated.sort(function (a, b) {
        let cmp = 0;
        if (typeof a.value === 'number' && typeof b.value === 'number') {
          cmp = a.value === b.value ? 0 : (a.value < b.value ? -1 : 1);
        } else {
          cmp = String(a.value).localeCompare(String(b.value), undefined, { numeric: true, sensitivity: 'base' });
        }
        if (cmp === 0) cmp = a.idx - b.idx;
        return cmp * dirMult;
      });

      decorated.forEach(function (entry) {
        tbody.appendChild(entry.row);
      });
      updateUploadDocSortButtonsUi();
    }

    async function openDocumentFile(docId) {
      const id = String(docId || '').trim();
      if (!id) return;
      try {
        const dl = await apiFetch('/api/v1/documents/' + encodeURIComponent(id) + '/download-url?expires_in=900');
        if (dl && dl.download_url) {
          window.open(dl.download_url, '_blank', 'noopener');
        }
      } catch (err) {
        setMessage('upload-doc-list-msg', 'err', err && err.message ? err.message : 'Failed to open document.');
      }
    }

    async function loadModalDocuments() {
      const claimKey = String(viewState.claimUuid || '').trim();
      if (!claimKey) {
        modalTbody.innerHTML = '<tr><td colspan="8">Select a claim to view documents.</td></tr>';
        viewState.total = 0;
        viewState.sourceFiles = 0;
        updateModalSelectionUi();
        return;
      }

      modalTbody.innerHTML = '<tr><td colspan="8">Loading uploaded documents...</td></tr>';
      try {
        const docsResult = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimKey) + '/documents?limit=200&offset=0');
        const docs = Array.isArray(docsResult && docsResult.items) ? docsResult.items : [];
        viewState.total = Number(docsResult && docsResult.total ? docsResult.total : docs.length);

        let sourceFileTotal = 0;
        const rows = docs.map(function (doc) {
          const meta = (doc && doc.metadata && typeof doc.metadata === 'object') ? doc.metadata : {};
          const mergedCount = Number(meta.merge_source_file_count || meta.merge_accepted_file_count || 0);
          const normalizedSourceCount = mergedCount > 0 ? mergedCount : 1;
          sourceFileTotal += normalizedSourceCount;

          const baseFileName = String(doc.file_name || '-');
          const outputSizeBytes = Number(doc.file_size_bytes || meta.merged_output_size_bytes || 0);
          const sourceSizeBytes = Number(meta.merged_source_total_size_bytes || 0);
          const mergeLabel = mergedCount > 0 ? ('Merged (' + String(mergedCount) + ')') : 'Single';
          const compressionNote = (mergedCount > 0 && sourceSizeBytes > 0)
            ? ('<div class="muted">Compressed: ' + escapeHtml(formatBytes(sourceSizeBytes)) + ' -> ' + escapeHtml(formatBytes(outputSizeBytes)) + '</div>')
            : '';
        return '<tr>'
            + '<td class="claim-select-col"><input type="checkbox" data-modal-doc-select value="' + escapeHtml(doc.id || '') + '"></td>'
            + '<td>' + escapeHtml(baseFileName) + compressionNote + '</td>'
            + '<td>' + escapeHtml(mergeLabel) + '</td>'
            + '<td>' + escapeHtml(formatBytes(outputSizeBytes)) + '</td>'
            + '<td>' + statusChip(doc.parse_status || '-') + '</td>'
            + '<td>' + escapeHtml(doc.uploaded_by || '-') + '</td>'
            + '<td>' + escapeHtml(formatDateTime(doc.uploaded_at || '')) + '</td>'
            + '<td><button type="button" class="btn-soft" data-upload-doc-open="' + escapeHtml(doc.id || '') + '">Open</button></td>'
            + '</tr>';
        }).join('');
        viewState.sourceFiles = sourceFileTotal;

        modalTbody.innerHTML = rows || '<tr><td colspan="8">No uploaded documents found for this claim.</td></tr>';

        modalTbody.querySelectorAll('input[data-modal-doc-select]').forEach(function (el) {
          el.addEventListener('change', updateModalSelectionUi);
        });

        modalTbody.querySelectorAll('button[data-upload-doc-open]').forEach(function (btn) {
          btn.addEventListener('click', async function () {
            await openDocumentFile(String(this.getAttribute('data-upload-doc-open') || ''));
          });
        });

        updateModalSelectionUi();
      } catch (err) {
        modalTbody.innerHTML = '<tr><td colspan="8">Failed to load uploaded documents.</td></tr>';
        viewState.total = 0;
        viewState.sourceFiles = 0;
        updateModalSelectionUi();
        setModalMessage('err', err && err.message ? err.message : 'Failed to load uploaded documents.');
      }
    }

    async function showUploadedDocuments(claimUuid, externalClaimId) {
      const claimKey = String(claimUuid || '').trim();
      const claimIdText = String(externalClaimId || '').trim() || '-';
      if (!claimKey) return;

      viewState.claimUuid = claimKey;
      viewState.claimId = claimIdText;
      modalSubtitleEl.textContent = 'Claim ID: ' + claimIdText;
      setModalMessage('', '');
      openModal();
      await loadModalDocuments();
    }

    async function deleteSelectedDocuments() {
      const claimKey = String(viewState.claimUuid || '').trim();
      const selectedIds = getSelectedModalDocIds();
      if (!claimKey) {
        setModalMessage('err', 'No claim selected.');
        return;
      }
      if (!selectedIds.length) {
        setModalMessage('err', 'Select at least one document to delete.');
        return;
      }

      const ok = window.confirm('Delete ' + String(selectedIds.length) + ' selected document(s)? This cannot be undone.');
      if (!ok) return;

      const prevDeleteText = modalDeleteBtn.textContent;
      modalDeleteBtn.disabled = true;
      modalDeleteBtn.textContent = 'Deleting...';
      setModalMessage('', 'Deleting selected documents...');

      try {
        const result = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimKey) + '/documents', {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            document_ids: selectedIds,
            actor_id: String((me && me.username) || 'ui-user'),
          }),
        });

        const deleted = Number(result && result.deleted ? result.deleted : 0);
        const failed = Number(result && result.failed ? result.failed : 0);
        const notFound = Number(result && result.not_found ? result.not_found : 0);

        setModalMessage('ok', 'Delete complete. Deleted: ' + String(deleted) + ', failed: ' + String(failed) + ', not found: ' + String(notFound) + '.');
        setMessage('upload-doc-list-msg', 'ok', 'Documents delete completed for claim ' + String(viewState.claimId || '-') + '.');

        await loadRows(false);
        await loadModalDocuments();
      } catch (err) {
        setModalMessage('err', err && err.message ? err.message : 'Delete failed.');
      } finally {
        modalDeleteBtn.textContent = prevDeleteText;
        updateModalSelectionUi();
      }
    }

    async function uploadFilesForClaim(claimId, files) {
      const claimKey = String(claimId || '').trim();
      if (!claimKey) return;
      let list = Array.from(files || []);
      if (!list.length) return;

      const masterDocPattern = /(all[\s._-]*documents?)/i;
      const masterDocs = list.filter(function (f) {
        return masterDocPattern.test(String((f && f.name) || ''));
      });
      if (masterDocs.length > 0 && list.length > masterDocs.length) {
        const keepMasterOnly = window.confirm('You selected ALL DOCUMENTS with other files. For smaller merged PDF, click OK to upload only ALL DOCUMENTS. Click Cancel to keep all files.');
        if (keepMasterOnly) {
          list = masterDocs;
          setMessage('upload-doc-list-msg', '', 'Smart compression: uploading only ALL DOCUMENTS file(s) to avoid duplicate pages.');
        }
      }

      const claimExternalId = String(fileInput.getAttribute('data-claim-external-id') || viewState.claimId || '').trim();
      if (!isModalOpen() || String(viewState.claimUuid || '') !== claimKey) {
        await showUploadedDocuments(claimKey, claimExternalId);
      }

      const previewNames = list.slice(0, 5).map(function (f) { return String(f && f.name ? f.name : 'file'); }).join(', ');
      const moreCount = Math.max(0, list.length - 5);
      setModalBusy(true, 'Uploading and merging ' + String(list.length) + ' file(s)...' + (previewNames ? ' [' + previewNames + (moreCount > 0 ? ', +' + String(moreCount) + ' more' : '') + ']' : ''));
      setMessage('upload-doc-list-msg', '', 'Uploading and merging ' + String(list.length) + ' file(s) into single PDF...');

      const fd = new FormData();
      list.forEach(function (file) {
        fd.append('files', file);
      });
      fd.append('uploaded_by', String((me && me.username) || 'ui-user'));
      fd.append('compression_mode', 'lossy');

      try {
        const mergedResult = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimKey) + '/documents/merged', {
          method: 'POST',
          body: fd,
        });

        const sourceCount = Number((mergedResult && mergedResult.source_file_count) || list.length || 0);
        const acceptedCount = Number((mergedResult && mergedResult.accepted_file_count) || 0);
        const skippedCount = Number((mergedResult && mergedResult.skipped_file_count) || 0);
        const mergedDocName = String((mergedResult && mergedResult.document && mergedResult.document.file_name) || 'merged_document.pdf');
        const mergeProfile = String((mergedResult && mergedResult.document && mergedResult.document.metadata && mergedResult.document.metadata.merge_profile) || 'standard');
        const sourceSizeBytes = Number((mergedResult && mergedResult.merged_source_total_size_bytes) || 0);
        const outputSizeBytes = Number((mergedResult && mergedResult.merged_output_size_bytes)
          || (mergedResult && mergedResult.document && mergedResult.document.file_size_bytes)
          || 0);
        const savedSizeBytes = Number((mergedResult && mergedResult.merged_saved_size_bytes) || Math.max(0, sourceSizeBytes - outputSizeBytes));
        const lowSavings = sourceSizeBytes > 0 && outputSizeBytes >= (sourceSizeBytes * 0.98);

        const successText = 'Merged upload complete. Source: ' + String(sourceCount)
          + ', accepted: ' + String(acceptedCount)
          + ', skipped: ' + String(skippedCount)
          + '. Saved as: ' + mergedDocName + ' (' + String(acceptedCount) + ')'
          + ', Size: ' + formatBytes(outputSizeBytes)
          + (sourceSizeBytes > 0 ? (', Compressed: ' + formatBytes(sourceSizeBytes) + ' -> ' + formatBytes(outputSizeBytes) + ' (saved ' + formatBytes(savedSizeBytes) + ')') : '')
          + ', Mode: ' + mergeProfile
          + (lowSavings ? '. Note: Source files are already PDF/compressed, so additional compression is limited.' : '');

        setMessage('upload-doc-list-msg', 'ok', successText);
        setModalMessage('ok', successText);
      } catch (err) {
        const msg = err && err.message ? err.message : 'Merge upload failed.';
        setMessage('upload-doc-list-msg', 'err', msg);
        setModalMessage('err', msg);
      } finally {
        setModalBusy(false);
      }

      await loadRows(false);
      await loadModalDocuments();
    }

    async function queueAiBatch(claimUuid, buttonEl) {
      const claimKey = String(claimUuid || '').trim();
      if (!claimKey) return;

      const btn = buttonEl;
      const oldText = btn ? btn.textContent : '';
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Queued...';
      }

      try {
        const summary = await runCasePreparationPipeline(claimKey, String((me && me.username) || 'user-ui'), {
          force: false,
          preferOpenAI: false,
          strictOpenAI: false,
        });
        setMessage(
          'upload-doc-list-msg',
          'ok',
          'Queue AI Batch done. New extractions: ' + String(summary.extractedCount || 0)
            + ', skipped: ' + String(summary.skippedCount || 0)
            + ', failed: ' + String(summary.failedCount || 0)
        );
      } catch (err) {
        setMessage('upload-doc-list-msg', 'err', err && err.message ? err.message : 'Queue AI Batch failed.');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = oldText || 'Queue AI Batch';
        }
        await loadRows(false);
      }
    }

    async function loadRows(resetPage) {
      if (resetPage) state.page = 1;

      const searchClaim = String(document.getElementById('upload-doc-search').value || '').trim();
      const allotmentDate = String(document.getElementById('upload-doc-allotment-date').value || '').trim();
      const statusFilter = String(document.getElementById('upload-doc-status-filter').value || 'all').trim();
      const doctorFilter = String(document.getElementById('upload-doc-doctor-filter').value || '').trim();
      const documentUploadFilter = String(document.getElementById('upload-doc-document-upload-filter').value || 'all').trim().toLowerCase();

      const params = new URLSearchParams();
      if (searchClaim) params.set('search_claim', searchClaim);
      if (allotmentDate) params.set('allotment_date', allotmentDate);
      if (statusFilter) params.set('status_filter', statusFilter);
      if (doctorFilter) params.set('doctor_filter', doctorFilter);
      if (documentUploadFilter && documentUploadFilter !== 'all') params.set('document_upload', documentUploadFilter);
      params.set('exclude_completed_uploaded', 'true');
      params.set('exclude_withdrawn', 'true');
      params.set('limit', String(state.pageSize));
      params.set('offset', String((state.page - 1) * state.pageSize));

      const result = await apiFetch('/api/v1/user-tools/claim-document-status?' + params.toString());
      state.total = Number(result.total || 0);

      const rows = (result.items || []).map((c) => {
        const treatmentType = resolveTreatmentType(c);
        const documentsCount = Number(c.documents || 0);
        const sourceFilesCount = Number(c.source_files || documentsCount || 0);
        const canQueue = documentsCount > 0;
        const lastUploadedBy = String(c.last_uploaded_by || '').trim();
        const mergeSummary = documentsCount <= 0
          ? '0'
          : (String(documentsCount) + ' merged doc(s) from ' + String(sourceFilesCount) + ' file(s)');
        return '<tr>'
          + '<td>' + escapeHtml(c.external_claim_id || '-') + '</td>'
          + '<td>' + escapeHtml(treatmentType || '-') + '</td>'
          + '<td>' + escapeHtml(formatAssignedDoctor(c.assigned_doctor_id)) + '</td>'
          + '<td>' + escapeHtml(formatDateOnly(c.allotment_date)) + '</td>'
          + '<td>' + escapeHtml(documentsCount > 0 ? 'Yes' : 'No') + '</td>'
          + '<td>' + escapeHtml(mergeSummary) + '</td>'
          + '<td>' + escapeHtml(lastUploadedBy || '-') + '</td>'
          + '<td>' + escapeHtml(formatDateTime(c.last_upload)) + '</td>'
          + '<td><div class="doctor-case-actions">'
            + '<button type="button" class="btn-soft" data-upload-claim="' + escapeHtml(c.id || '') + '" data-upload-claim-id="' + escapeHtml(c.external_claim_id || '') + '">Upload</button>'
            + '<button type="button" class="btn-soft" data-view-claim="' + escapeHtml(c.id || '') + '" data-view-claim-id="' + escapeHtml(c.external_claim_id || '') + '">View</button>'
            + '<button type="button" class="btn-soft" data-queue-claim="' + escapeHtml(c.id || '') + '"' + (canQueue ? '' : ' disabled') + '>Queue AI Batch</button>'
          + '</div></td>'
          + '</tr>';
      }).join('');

      tbody.innerHTML = rows || '<tr><td colspan="9">No claims found for selected filter.</td></tr>';

      tbody.querySelectorAll('button[data-upload-claim]').forEach((btn) => {
        btn.addEventListener('click', async function () {
          const claimId = String(this.getAttribute('data-upload-claim') || '').trim();
          const claimExternalId = String(this.getAttribute('data-upload-claim-id') || '').trim();
          if (!claimId) return;
          fileInput.setAttribute('data-claim-id', claimId);
          fileInput.setAttribute('data-claim-external-id', claimExternalId);
          fileInput.value = '';

          await showUploadedDocuments(claimId, claimExternalId);
          setModalMessage('', 'Select files now. Upload will run in this modal.');
          fileInput.click();
        });
      });

      tbody.querySelectorAll('button[data-view-claim]').forEach((btn) => {
        btn.addEventListener('click', async function () {
          await showUploadedDocuments(
            String(this.getAttribute('data-view-claim') || ''),
            String(this.getAttribute('data-view-claim-id') || '')
          );
        });
      });

      tbody.querySelectorAll('button[data-queue-claim]').forEach((btn) => {
        btn.addEventListener('click', async function () {
          const claimId = String(this.getAttribute('data-queue-claim') || '').trim();
          await queueAiBatch(claimId, this);
        });
      });

      applyUploadDocSort();
      updatePaginationUi();
    }

    fileInput.addEventListener('change', async function () {
      const claimId = String(fileInput.getAttribute('data-claim-id') || '').trim();
      const files = fileInput.files;
      await uploadFilesForClaim(claimId, files);
    });

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      setMessage('upload-doc-list-msg', '', '');
      try {
        await loadRows(true);
      } catch (err) {
        setMessage('upload-doc-list-msg', 'err', err.message);
      }
    });

    pageSizeEl.addEventListener('change', async function () {
      state.pageSize = Number(this.value || 20);
      await loadRows(true);
    });

    prevBtn.addEventListener('click', async function () {
      if (state.page <= 1) return;
      state.page -= 1;
      await loadRows(false);
    });

    nextBtn.addEventListener('click', async function () {
      const maxPage = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
      if (state.page >= maxPage) return;
      state.page += 1;
      await loadRows(false);
    });

    uploadDocSortButtons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        const key = String(this.getAttribute('data-upload-doc-sort') || '').trim();
        const cfg = uploadDocSortConfig[key];
        if (!cfg) return;
        if (uploadDocSortState.key === key) {
          uploadDocSortState.dir = uploadDocSortState.dir === 'asc' ? 'desc' : 'asc';
        } else {
          uploadDocSortState.key = key;
          uploadDocSortState.dir = cfg.defaultDir || 'asc';
        }
        applyUploadDocSort();
      });
    });

    updateUploadDocSortButtonsUi();

    if (modalCloseBtn) {
      modalCloseBtn.addEventListener('click', closeModal);
    }

    if (modalEl) {
      modalEl.addEventListener('click', function (e) {
        if (e.target === modalEl) closeModal();
      });
    }

    if (modalSelectPageEl) {
      modalSelectPageEl.addEventListener('change', function () {
        const checked = !!modalSelectPageEl.checked;
        modalTbody.querySelectorAll('input[data-modal-doc-select]').forEach(function (el) {
          el.checked = checked;
        });
        updateModalSelectionUi();
      });
    }

    if (modalSelectAllBtn) {
      modalSelectAllBtn.addEventListener('click', function () {
        modalTbody.querySelectorAll('input[data-modal-doc-select]').forEach(function (el) {
          el.checked = true;
        });
        updateModalSelectionUi();
      });
    }

    if (modalClearBtn) {
      modalClearBtn.addEventListener('click', function () {
        modalTbody.querySelectorAll('input[data-modal-doc-select]').forEach(function (el) {
          el.checked = false;
        });
        updateModalSelectionUi();
      });
    }

    if (modalDeleteBtn) {
      modalDeleteBtn.addEventListener('click', async function () {
        await deleteSelectedDocuments();
      });
    }

    try {
      await loadRows(true);
    } catch (err) {
      setMessage('upload-doc-list-msg', 'err', err.message);
      tbody.innerHTML = '<tr><td colspan="7">Failed to load claim document status.</td></tr>';
      updatePaginationUi();
    }
  }

  async function renderUploadExcel() {
    contentPanel.innerHTML = '<h2>Upload Excel</h2><p class="muted">Upload case data in xlsx/csv/sql format.</p>'
      + '<p id="upload-excel-msg"></p><form id="upload-excel-form">'
      + '<div class="form-row"><label>Select File</label><input type="file" name="file" accept=".xlsx,.csv,.sql" required></div>'
      + '<button type="submit">Upload Excel</button></form>'
      + '<div id="upload-excel-rejected-wrap" style="display:none;margin-top:10px;">'
      + '<a id="upload-excel-download-rejected" href="#" download>Download Error Excel</a>'
      + '</div>'
      + '<div id="upload-excel-overlay" style="display:none;position:fixed;inset:0;background:rgba(12, 24, 45, 0.35);z-index:9999;align-items:center;justify-content:center;">'
      + '<div style="background:#fff;border-radius:12px;padding:16px 18px;box-shadow:0 10px 30px rgba(0,0,0,0.2);font-weight:600;color:#1c2b3a;">Uploading file, please wait...</div>'
      + '</div>';

    const rejectedWrap = document.getElementById('upload-excel-rejected-wrap');
    const downloadRejectedLink = document.getElementById('upload-excel-download-rejected');
    const overlayEl = document.getElementById('upload-excel-overlay');
    let rejectedDownloadObjectUrl = '';

    function clearRejectedLink() {
      if (rejectedDownloadObjectUrl) {
        try { URL.revokeObjectURL(rejectedDownloadObjectUrl); } catch (_err) {}
        rejectedDownloadObjectUrl = '';
      }
      if (downloadRejectedLink) {
        downloadRejectedLink.removeAttribute('href');
        downloadRejectedLink.removeAttribute('download');
      }
    }

    function base64ToBytes(base64Text) {
      const clean = String(base64Text || '').replace(/\s+/g, '');
      if (!clean) return new Uint8Array(0);
      const binary = atob(clean);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      return bytes;
    }

    function prepareRejectedLink(base64Content, fileName) {
      const bytes = base64ToBytes(base64Content);
      if (!bytes.length) throw new Error('Empty rejected Excel content.');
      const blob = new Blob(
        [bytes],
        { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }
      );
      clearRejectedLink();
      rejectedDownloadObjectUrl = URL.createObjectURL(blob);
      if (downloadRejectedLink) {
        downloadRejectedLink.href = rejectedDownloadObjectUrl;
        downloadRejectedLink.download = String(fileName || 'rejected_rows.xlsx');
      }
    }

    function showUploadOverlay() {
      if (!overlayEl) return;
      overlayEl.style.display = 'flex';
    }

    function hideUploadOverlay() {
      if (!overlayEl) return;
      overlayEl.style.display = 'none';
    }

    document.getElementById("upload-excel-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      const fd = new FormData(e.currentTarget);
      const submitBtn = e.currentTarget ? e.currentTarget.querySelector('button[type="submit"]') : null;
      if (submitBtn) submitBtn.disabled = true;
      showUploadOverlay();
      try {
        const result = await apiFetch('/api/v1/user-tools/upload-excel', { method: 'POST', body: fd });
        const rejectedCount = Number((result && result.rejected_count) || 0);
        const uploadedCount = Number(
          (result && result.uploaded)
          || ((Number(result && result.inserted) || 0) + (Number(result && result.updated) || 0))
          || 0
        );
        setMessage(
          "upload-excel-msg",
          "ok",
          'Upload complete. Total: ' + result.total_rows + ', uploaded: ' + uploadedCount + ', rejected: ' + rejectedCount
        );
        if (rejectedWrap && downloadRejectedLink) {
          if (rejectedCount > 0) {
            try {
              prepareRejectedLink(
                String(result && result.rejected_excel_base64 ? result.rejected_excel_base64 : ''),
                String(result && result.rejected_excel_filename ? result.rejected_excel_filename : 'rejected_rows.xlsx')
              );
              rejectedWrap.style.display = '';
            } catch (_linkErr) {
              clearRejectedLink();
              rejectedWrap.style.display = 'none';
            }
          } else {
            clearRejectedLink();
            rejectedWrap.style.display = 'none';
          }
        }
      } catch (err) {
        if (rejectedWrap && downloadRejectedLink) {
          clearRejectedLink();
          rejectedWrap.style.display = 'none';
        }
        setMessage("upload-excel-msg", "err", err.message);
      } finally {
        hideUploadOverlay();
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  async function renderCompletedReports(initialStatusFilter) {
    const me = await apiFetch('/api/v1/auth/me');
    const canManageUpload = !!(me && (me.role === 'super_admin' || me.role === 'user'));
    const canAuditQc = !!(me && (me.role === 'super_admin' || me.role === 'auditor'));
    const canChangeStatus = !!(me && (me.role === 'super_admin' || me.role === 'user'));
    const route = parseRoute();
    const routePage = String((route && route.page) || '').trim();
    const routeRole = String((route && route.routeRole) || '').trim();
    const auditOnlyRole = routeRole === 'auditor' || !!(me && me.role === 'auditor');
    const isAuditorAuditClaimsPage = auditOnlyRole && routePage === 'audit-claims';
    const isAuditorDashboardPage = auditOnlyRole && routePage === 'dashboard';
    const shouldExcludeTaggedForAuditor = isAuditorAuditClaimsPage || isAuditorDashboardPage;
    const hideQcAndSystemActions = routeRole === 'user' && routePage === 'completed-not-uploaded';

    const doctors = await fetchDoctors();
    const doctorFilterOptions = '<option value="">All Doctors</option>'
      + doctors.map((d) => '<option value="' + escapeHtml(d) + '">' + escapeHtml(d) + '</option>').join('');

    const completedReportsFilterStorageKey = 'qc_completed_reports_filters_v1:' + String(routeRole || '-') + ':' + String(routePage || '-');
    const validStatusFilters = ['all', 'pending', 'uploaded'];
    const validQcFilters = ['all', 'yes', 'no'];

    function readCompletedReportFilters() {
      try {
        const raw = sessionStorage.getItem(completedReportsFilterStorageKey);
        if (!raw) return {};
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === 'object' ? parsed : {};
      } catch (_err) {
        return {};
      }
    }

    function writeCompletedReportFilters(payload) {
      try {
        const data = payload && typeof payload === 'object' ? payload : {};
        sessionStorage.setItem(completedReportsFilterStorageKey, JSON.stringify(data));
      } catch (_err) {
      }
    }

    const savedFilters = readCompletedReportFilters();
    const savedStatus = String(savedFilters.status || '').trim().toLowerCase();
    const savedQc = String(savedFilters.qc || '').trim().toLowerCase();
    const savedPageSize = Number(savedFilters.page_size || 0);

    const state = {
      page: 1,
      pageSize: (savedPageSize === 10 || savedPageSize === 20 || savedPageSize === 50) ? savedPageSize : 20,
      total: 0,
      status: validStatusFilters.includes(savedStatus)
        ? savedStatus
        : ((initialStatusFilter === 'uploaded' || initialStatusFilter === 'all') ? initialStatusFilter : 'pending'),
      qc: validQcFilters.includes(savedQc)
        ? savedQc
        : ((initialStatusFilter === 'uploaded' || initialStatusFilter === 'all') ? 'all' : 'no'),
    };
    if (auditOnlyRole) {
      state.qc = 'no';
    }
    const defaultTaggingMap = {
      Genuine: ['Hospitalization verified and found to be genuine'],
      Fraudulent: [
        'Non cooperation of Hospital / patient during investigation',
        'Circumstantial evidence suggesting of possible fraud',
        'Infalted bills',
        'OPD to IPD conversion',
      ],
    };
    let taggingSubtaggingMap = defaultTaggingMap;
    const rowsByClaim = new Map();
    let auditorAllotmentGate = {
      enforced: false,
      minDateTs: Number.NaN,
      minDateLabel: '',
    };

    function sameAllotmentDay(aTs, bTs) {
      if (!Number.isFinite(aTs) || !Number.isFinite(bTs)) return false;
      const a = new Date(aTs);
      const b = new Date(bTs);
      return a.getFullYear() === b.getFullYear()
        && a.getMonth() === b.getMonth()
        && a.getDate() === b.getDate();
    }

    function isAuditorClaimLocked(row) {
      if (!auditOnlyRole) return false;
      if (!auditorAllotmentGate.enforced || !Number.isFinite(auditorAllotmentGate.minDateTs)) return false;
      const rowTs = parseAllotmentDateValue(row && row.allotment_date);
      return !sameAllotmentDay(rowTs, auditorAllotmentGate.minDateTs);
    }

    contentPanel.innerHTML = '<section class="claim-status-panel">'
      + '<h2 class="claim-status-title">Completed Reports</h2>'
      + '<p class="muted">View completed case reports in HTML and export to Word/PDF.</p>'
      + '<form id="completed-reports-filter-form" class="claim-status-toolbar">'
      + '<div class="claim-filter-group"><label for="completed-reports-status-filter">Filter</label><select id="completed-reports-status-filter" name="status_filter">'
      + '<option value="all">All</option><option value="pending">Pending</option><option value="uploaded">Uploaded</option>'
      + '</select></div>'
      + '<div class="claim-filter-group"><label for="completed-reports-qc-filter">QC</label><select id="completed-reports-qc-filter" name="qc_filter">'
      + '<option value="all">All</option><option value="yes">Yes</option><option value="no">No</option>'
      + '</select></div>'
      + '<div class="claim-filter-group"><label for="completed-reports-doctor-filter">Doctor</label><select id="completed-reports-doctor-filter" name="doctor_filter">' + doctorFilterOptions + '</select></div>'
      + '<div class="claim-filter-group"><label for="completed-reports-search">Search Claim</label><input id="completed-reports-search" name="search_claim" placeholder="Claim ID"></div>'
      + '<div class="claim-filter-group"><label for="completed-reports-allotment-date">Allotment Date</label><input id="completed-reports-allotment-date" type="date" name="allotment_date"></div>'
      + '<div class="claim-filter-action"><button type="submit" class="claim-apply-btn">Apply</button></div>'
      + '</form>'
      + '<p id="completed-reports-msg"></p>'
      + '<div class="table-wrap claim-status-table-wrap"><table><thead><tr>'
      + '<th>CLAIM ID</th><th>DOCTOR</th><th>COMPLETED AT</th><th>REPORT STATUS ^</th><th>QC</th><th>TAGGING</th><th>SUBTAGGING</th><th>OPINION</th><th>AI REPORTS</th><th>ACTION</th>'
      + '</tr></thead><tbody id="completed-reports-tbody"><tr><td colspan="10">Loading...</td></tr></tbody></table></div>'
      + '<div class="claim-pagination">'
      + '<div class="claim-pagination__left"><label for="completed-reports-page-size">Rows</label><select id="completed-reports-page-size"><option value="10">10</option><option value="20" selected>20</option><option value="50">50</option></select></div>'
      + '<div class="claim-pagination__info" id="completed-reports-page-info">Showing 0-0 of 0</div>'
      + '<div class="claim-pagination__actions"><button type="button" class="btn-soft" id="completed-reports-prev-page">Previous</button><button type="button" class="btn-soft" id="completed-reports-next-page">Next</button></div>'
      + '</div>'
      + '<div id="completed-doc-modal" class="modal-backdrop">'
      + '<div class="modal-card wide" role="dialog" aria-modal="true" aria-labelledby="completed-doc-modal-title">'
      + '<div class="modal-header"><h3 id="completed-doc-modal-title">View Documents</h3><button type="button" class="btn-soft" id="completed-doc-modal-close">Close</button></div>'
      + '<p class="muted" id="completed-doc-modal-subtitle">Claim ID: -</p>'
      + '<p id="completed-doc-modal-msg"></p>'
      + '<div class="table-wrap view-documents-table-wrap"><table><thead><tr><th>File Name</th><th>Parse Status</th><th>Uploaded By</th><th>Uploaded At</th><th>Action</th></tr></thead><tbody id="completed-doc-modal-tbody"><tr><td colspan="5">Select a claim to view documents.</td></tr></tbody></table></div>'
      + '</div>'
      + '</div>'
      + '<div id="completed-upload-modal" class="modal-backdrop">'
      + '<div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="completed-upload-modal-title">'
      + '<div class="modal-header"><h3 id="completed-upload-modal-title">Update Uploaded Format</h3><button type="button" class="btn-soft" id="completed-upload-modal-close">Close</button></div>'
      + '<p class="muted" id="completed-upload-modal-claim-label">Claim ID: -</p>'
      + '<p id="completed-upload-modal-msg"></p>'
      + '<form id="completed-upload-form">'
      + '<input type="hidden" id="completed-upload-claim-uuid">'
      + '<div class="form-row"><label for="completed-upload-status">Uploaded Format Status</label><select id="completed-upload-status" required><option value="uploaded">Uploaded</option></select></div>'
      + '<div class="form-row"><label for="completed-upload-tagging">Tagging</label><select id="completed-upload-tagging" required><option value="">Select Tagging</option><option value="Genuine">Genuine</option><option value="Fraudulent">Fraudulent</option></select></div>'
      + '<div class="form-row"><label for="completed-upload-subtagging">Subtagging</label><select id="completed-upload-subtagging" required><option value="">Select Subtagging</option></select></div>'
      + '<div class="form-row"><label for="completed-upload-opinion">Opinion</label><input id="completed-upload-opinion" type="text" placeholder="Enter opinion" maxlength="4000" required></div>'
      + '<div class="link-row"><button type="submit" id="completed-upload-save-btn">Save</button></div>'
      + '</form>'
      + '</div>'
      + '</div>'
      + '<div id="completed-qc-modal" class="modal-backdrop">'
      + '<div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="completed-qc-modal-title">'
      + '<div class="modal-header"><h3 id="completed-qc-modal-title">Update QC</h3><button type="button" class="btn-soft" id="completed-qc-modal-close">Close</button></div>'
      + '<p class="muted" id="completed-qc-modal-claim-label">Claim ID: -</p>'
      + '<p id="completed-qc-modal-msg"></p>'
      + '<form id="completed-qc-form">'
      + '<input type="hidden" id="completed-qc-claim-uuid">'
      + '<div class="form-row"><label for="completed-qc-status">QC Status</label><select id="completed-qc-status" required><option value="yes">Yes</option><option value="no">No</option></select></div>'
      + '<div class="link-row"><button type="submit" id="completed-qc-save-btn">Save</button></div>'
      + '</form>'
      + '</div>'
      + '</div>'
      + '<div id="completed-report-editor-modal" class="modal-backdrop">'
      + '<div class="modal-card wide" role="dialog" aria-modal="true" aria-labelledby="completed-report-editor-title">'
      + '<div class="modal-header"><h3 id="completed-report-editor-title">Edit Report HTML</h3><button type="button" class="btn-soft" id="completed-report-editor-close">Close</button></div>'
      + '<p class="muted" id="completed-report-editor-claim-label">Claim ID: -</p>'
      + '<p id="completed-report-editor-msg"></p>'
      + '<div class="form-row"><label>Report Content</label><div id="completed-report-editor-body" contenteditable="true" style="min-height:360px;max-height:65vh;overflow:auto;border:1px solid #d5dde8;border-radius:10px;background:#fff;padding:12px;"></div></div>'
      + '<div class="link-row"><button type="button" id="completed-report-editor-save">Save Report</button></div>'
      + '</div>'
      + '</div>'      + '</section>';

    const form = document.getElementById('completed-reports-filter-form');
    const tbody = document.getElementById('completed-reports-tbody');
    const pageSizeEl = document.getElementById('completed-reports-page-size');
    const prevBtn = document.getElementById('completed-reports-prev-page');
    const nextBtn = document.getElementById('completed-reports-next-page');
    const pageInfoEl = document.getElementById('completed-reports-page-info');
    const statusFilterEl = document.getElementById('completed-reports-status-filter');
    const qcFilterEl = document.getElementById('completed-reports-qc-filter');
    const doctorFilterEl = document.getElementById('completed-reports-doctor-filter');
    const searchFilterEl = document.getElementById('completed-reports-search');
    const allotmentDateEl = document.getElementById('completed-reports-allotment-date');

    const docsModalEl = document.getElementById('completed-doc-modal');
    const docsModalCloseBtn = document.getElementById('completed-doc-modal-close');
    const docsModalSubtitleEl = document.getElementById('completed-doc-modal-subtitle');
    const docsModalMsgEl = document.getElementById('completed-doc-modal-msg');
    const docsModalTbody = document.getElementById('completed-doc-modal-tbody');

    const uploadModalEl = document.getElementById('completed-upload-modal');
    const uploadModalCloseBtn = document.getElementById('completed-upload-modal-close');
    const uploadModalClaimLabelEl = document.getElementById('completed-upload-modal-claim-label');
    const uploadModalMsgEl = document.getElementById('completed-upload-modal-msg');
    const uploadForm = document.getElementById('completed-upload-form');
    const uploadClaimUuidEl = document.getElementById('completed-upload-claim-uuid');
    const uploadStatusEl = document.getElementById('completed-upload-status');
    const uploadTaggingEl = document.getElementById('completed-upload-tagging');
    const uploadSubtaggingEl = document.getElementById('completed-upload-subtagging');
    const uploadOpinionEl = document.getElementById('completed-upload-opinion');
    const uploadSaveBtn = document.getElementById('completed-upload-save-btn');

    const qcModalEl = document.getElementById('completed-qc-modal');
    const qcModalCloseBtn = document.getElementById('completed-qc-modal-close');
    const qcModalClaimLabelEl = document.getElementById('completed-qc-modal-claim-label');
    const qcModalMsgEl = document.getElementById('completed-qc-modal-msg');
    const qcForm = document.getElementById('completed-qc-form');
    const qcClaimUuidEl = document.getElementById('completed-qc-claim-uuid');
    const qcStatusEl = document.getElementById('completed-qc-status');
    const qcSaveBtn = document.getElementById('completed-qc-save-btn');

    const reportEditorModalEl = document.getElementById('completed-report-editor-modal');
    const reportEditorCloseBtn = document.getElementById('completed-report-editor-close');
    const reportEditorClaimLabelEl = document.getElementById('completed-report-editor-claim-label');
    const reportEditorMsgEl = document.getElementById('completed-report-editor-msg');
    const reportEditorBodyEl = document.getElementById('completed-report-editor-body');
    const reportEditorSaveBtn = document.getElementById('completed-report-editor-save');
    let reportEditorCurrentRow = null;
    let reportEditorCurrentSource = 'doctor';

    statusFilterEl.value = state.status;
    if (qcFilterEl) qcFilterEl.value = state.qc;
    if (pageSizeEl) pageSizeEl.value = String(state.pageSize);
    if (searchFilterEl) searchFilterEl.value = String(savedFilters.search_claim || '').trim();
    if (allotmentDateEl) allotmentDateEl.value = String(savedFilters.allotment_date || '').trim();
    if (doctorFilterEl) {
      const doctorSaved = String(savedFilters.doctor_filter || '').trim();
      const hasDoctorOption = !!Array.from(doctorFilterEl.options || []).find(function (opt) {
        return String(opt && opt.value || '') === doctorSaved;
      });
      doctorFilterEl.value = hasDoctorOption ? doctorSaved : '';
    }

    if (auditOnlyRole) {
      if (qcFilterEl) {
        qcFilterEl.value = 'no';
        qcFilterEl.disabled = true;
        const qcFilterGroup = qcFilterEl.closest('.claim-filter-group');
        if (qcFilterGroup) qcFilterGroup.style.display = 'none';
      }
    }

    function formatAssignedDoctor(value) {
      return String(value || '').split(',').map((s) => s.trim()).filter(Boolean)[0] || 'Unassigned';
    }

    function normalizeTagging(value) {
      const normalized = String(value || '').trim().toLowerCase();
      if (normalized === 'genuine') return 'Genuine';
      if (normalized === 'fraudulent' || normalized === 'fraudlent') return 'Fraudulent';
      return '';
    }
    function normalizeDisplayText(value) {
      const text = String(value || '').trim();
      if (!text) return '';
      if (/^(na|n\/a|not available|none|nil|null|-|\.)$/i.test(text)) return '';
      return text;
    }

    function normalizeOpinionText(value) {
      const base = normalizeDisplayText(value);
      if (!base) return '';
      const stripped = String(base)
        .replace(/<br\s*\/?>/gi, ' ')
        .replace(/<[^>]*>/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
      if (!stripped) return '';

      let clean = stripped
        .replace(/trigger\s*remarks?\s*:?/ig, ' ')
        .replace(/amber\s*flag\s*entities\s*in\s*this\s*claim\s*:?/ig, ' ')
        .replace(/red\s*flag\s*entities\s*in\s*this\s*claim\s*:?/ig, ' ')
        .replace(/look\s*alike\s*based\s*:?/ig, ' ')
        .replace(/market\s*intelligence\s*:?/ig, ' ')
        .replace(/hospital\s*not\s*verified\s*:?/ig, ' ')
        .replace(/past\s*claims\s*with\s*similar\s*attributes[^|:]*:?/ig, ' ')
        .replace(/\s*\|\s*/g, ', ')
        .replace(/\s{2,}/g, ' ')
        .trim();

      if (!clean) clean = stripped;
      return clean;
    }

    function previewOpinion(text) {
      const value = normalizeOpinionText(text);
      if (!value) return '-';
      const maxLen = 40;
      return value.length > maxLen ? (value.slice(0, maxLen) + '...') : value;
    }

    function setDocsModalMessage(type, text) {
      if (!docsModalMsgEl) return;
      docsModalMsgEl.className = type ? 'msg ' + type : '';
      docsModalMsgEl.textContent = text || '';
    }

    function setUploadModalMessage(type, text) {
      if (!uploadModalMsgEl) return;
      uploadModalMsgEl.className = type ? 'msg ' + type : '';
      uploadModalMsgEl.textContent = text || '';
    }

    function setQcModalMessage(type, text) {
      if (!qcModalMsgEl) return;
      qcModalMsgEl.className = type ? 'msg ' + type : '';
      qcModalMsgEl.textContent = text || '';
    }

    function openDocsModal() {
      if (!docsModalEl) return;
      docsModalEl.classList.add('open');
    }

    function closeDocsModal() {
      if (!docsModalEl) return;
      docsModalEl.classList.remove('open');
      setDocsModalMessage('', '');
    }

    function openUploadModal() {
      if (!uploadModalEl) return;
      uploadModalEl.classList.add('open');
    }

    function closeUploadModal() {
      if (!uploadModalEl) return;
      uploadModalEl.classList.remove('open');
      setUploadModalMessage('', '');
    }

    function openQcModal() {
      if (!qcModalEl) return;
      qcModalEl.classList.add('open');
    }

    function closeQcModal() {
      if (!qcModalEl) return;
      qcModalEl.classList.remove('open');
      setQcModalMessage('', '');
    }

    function setSubtaggingOptions(taggingValue, selectedSubtagging) {
      const key = normalizeTagging(taggingValue);
      const options = Array.isArray(taggingSubtaggingMap[key]) ? taggingSubtaggingMap[key] : [];
      uploadSubtaggingEl.innerHTML = '<option value="">Select Subtagging</option>';
      options.forEach(function (opt) {
        const option = document.createElement('option');
        option.value = String(opt);
        option.textContent = String(opt);
        uploadSubtaggingEl.appendChild(option);
      });
      if (selectedSubtagging && options.includes(selectedSubtagging)) {
        uploadSubtaggingEl.value = selectedSubtagging;
      } else if (options.length === 1) {
        uploadSubtaggingEl.value = String(options[0]);
      }
    }

    function updatePaginationUi() {
      const total = Number(state.total || 0);
      const startRow = total === 0 ? 0 : ((state.page - 1) * state.pageSize + 1);
      const endRow = Math.min(state.page * state.pageSize, total);
      pageInfoEl.textContent = 'Showing ' + startRow + '-' + endRow + ' of ' + total;
      prevBtn.disabled = state.page <= 1;
      nextBtn.disabled = endRow >= total;
    }

    async function openDocumentFile(docId) {
      const id = String(docId || '').trim();
      if (!id) return;
      try {
        const dl = await apiFetch('/api/v1/documents/' + encodeURIComponent(id) + '/download-url?expires_in=900');
        if (dl && dl.download_url) {
          window.open(dl.download_url, '_blank', 'noopener');
        }
      } catch (err) {
        setMessage('completed-reports-msg', 'err', err && err.message ? err.message : 'Failed to open document.');
      }
    }

    async function showClaimDocuments(claimUuid, externalClaimId) {
      const claimKey = String(claimUuid || '').trim();
      if (!claimKey) return;

      docsModalSubtitleEl.textContent = 'Claim ID: ' + (String(externalClaimId || '').trim() || '-');
      setDocsModalMessage('', '');
      docsModalTbody.innerHTML = '<tr><td colspan="5">Loading documents...</td></tr>';
      openDocsModal();

      try {
        const docsResult = await apiFetch('/api/v1/claims/' + encodeURIComponent(claimKey) + '/documents?limit=200&offset=0');
        const docs = Array.isArray(docsResult && docsResult.items) ? docsResult.items : [];
        const rows = docs.map(function (doc) {
        return '<tr>'
            + '<td>' + escapeHtml(doc.file_name || '-') + '</td>'
            + '<td>' + statusChip(doc.parse_status || '-') + '</td>'
            + '<td>' + escapeHtml(doc.uploaded_by || '-') + '</td>'
            + '<td>' + escapeHtml(formatDateTime(doc.uploaded_at || '')) + '</td>'
            + '<td><button type="button" class="btn-soft" data-completed-open-doc="' + escapeHtml(doc.id || '') + '">Open</button></td>'
            + '</tr>';
        }).join('');

        docsModalTbody.innerHTML = rows || '<tr><td colspan="5">No documents uploaded for this claim.</td></tr>';
        docsModalTbody.querySelectorAll('button[data-completed-open-doc]').forEach(function (btn) {
          btn.addEventListener('click', async function () {
            await openDocumentFile(String(this.getAttribute('data-completed-open-doc') || ''));
          });
        });
      } catch (err) {
        docsModalTbody.innerHTML = '<tr><td colspan="5">Failed to load documents.</td></tr>';
        setDocsModalMessage('err', err && err.message ? err.message : 'Failed to load documents.');
      }
    }

    function openUploadStatusModal(row) {
      const claimUuid = String(row.claim_uuid || '').trim();
      const externalClaimId = String(row.external_claim_id || '').trim();
      if (!claimUuid) return;

      uploadClaimUuidEl.value = claimUuid;
      uploadModalClaimLabelEl.textContent = 'Claim ID: ' + (externalClaimId || '-');
      uploadStatusEl.value = 'uploaded';
      const normalizedTagging = normalizeTagging(normalizeDisplayText(row.tagging));
      uploadTaggingEl.value = normalizedTagging;
      setSubtaggingOptions(normalizedTagging, normalizeDisplayText(row.subtagging));
      uploadOpinionEl.value = normalizeOpinionText(row.opinion);
      setUploadModalMessage('', '');
      openUploadModal();
    }

    function openQcStatusModal(row) {
      const claimUuid = String(row.claim_uuid || '').trim();
      const externalClaimId = String(row.external_claim_id || '').trim();
      if (!claimUuid) return;
      qcClaimUuidEl.value = claimUuid;
      qcModalClaimLabelEl.textContent = 'Claim ID: ' + (externalClaimId || '-');
      qcStatusEl.value = String(row.qc_status || 'no').toLowerCase() === 'yes' ? 'yes' : 'no';
      setQcModalMessage('', '');
      openQcModal();
    }


    function setReportEditorMessage(type, text) {
      if (!reportEditorMsgEl) return;
      reportEditorMsgEl.className = type ? 'msg ' + type : '';
      reportEditorMsgEl.textContent = text || '';
    }

    function openReportEditorModal() {
      if (!reportEditorModalEl) return;
      reportEditorModalEl.classList.add('open');
    }

    function closeReportEditorModal() {
      if (!reportEditorModalEl) return;
      reportEditorModalEl.classList.remove('open');
      setReportEditorMessage('', '');
      reportEditorCurrentRow = null;
      reportEditorCurrentSource = 'doctor';
      if (reportEditorBodyEl) reportEditorBodyEl.innerHTML = '';
      if (reportEditorClaimLabelEl) reportEditorClaimLabelEl.textContent = 'Claim ID: -';
    }

    function normalizeHealthClaimReportTitle(html) {
      const value = String(html || '');
      if (!value) return '';
      return value.replace(/HEALTH CLAIM INVESTIGATION REPORT/g, 'HEALTH CLAIM ASSESSMENT SHEET');
    }

    function openAuditorQcWorkspace(row) {
      const claimUuid = String(row && row.claim_uuid ? row.claim_uuid : '').trim();
      const claimId = String(row && row.external_claim_id ? row.external_claim_id : '').trim();
      if (!claimUuid) {
        setMessage('completed-reports-msg', 'err', 'Claim key missing.');
        return;
      }
      const params = new URLSearchParams();
      params.set('claim_uuid', claimUuid);
      params.set('claim_id', claimId);
      params.set('rev', String(Date.now()));
      const url = '/qc/public/auditor-qc.html?' + params.toString();
      const popup = window.open(url, '_blank', 'width=1500,height=900,resizable=yes,scrollbars=yes');
      if (!popup) {
        window.location.href = url;
      }
    }

    async function openEditableReport(row, reportSource) {
      const claimUuid = String(row && row.claim_uuid ? row.claim_uuid : '').trim();
      const claimId = String(row && row.external_claim_id ? row.external_claim_id : '').trim();
      if (!claimUuid) return;

      reportEditorCurrentRow = row;
      reportEditorCurrentSource = (String(reportSource || 'doctor').toLowerCase() === 'system') ? 'system' : 'doctor';
      if (reportEditorClaimLabelEl) {
        reportEditorClaimLabelEl.textContent = 'Claim ID: ' + (claimId || '-');
      }
      if (reportEditorBodyEl) {
        reportEditorBodyEl.innerHTML = '<p class="muted">Loading report...</p>';
      }
      setReportEditorMessage('', '');
      openReportEditorModal();

      try {
        const payload = await apiFetch('/api/v1/user-tools/completed-reports/' + encodeURIComponent(claimUuid) + '/latest-html?source=' + encodeURIComponent(reportEditorCurrentSource));
        const html = normalizeHealthClaimReportTitle(String(payload && payload.report_html ? payload.report_html : '').trim());
        if (!html) {
          throw new Error('No saved report HTML found for this source.');
        }
        if (reportEditorBodyEl) {
          reportEditorBodyEl.innerHTML = html;
        }
      } catch (err) {
        if (reportEditorBodyEl) reportEditorBodyEl.innerHTML = '';
        setReportEditorMessage('err', err && err.message ? err.message : 'Failed to load report HTML.');
      }
    }

    async function saveEditableReport() {
      const row = reportEditorCurrentRow;
      const claimUuid = String(row && row.claim_uuid ? row.claim_uuid : '').trim();
      if (!claimUuid || !reportEditorBodyEl) return;

      const html = normalizeHealthClaimReportTitle(String(reportEditorBodyEl.innerHTML || '').trim());
      if (!html) {
        setReportEditorMessage('err', 'Report content is empty.');
        return;
      }

      const oldText = reportEditorSaveBtn ? reportEditorSaveBtn.textContent : '';
      if (reportEditorSaveBtn) {
        reportEditorSaveBtn.disabled = true;
        reportEditorSaveBtn.textContent = 'Saving...';
      }
      setReportEditorMessage('', 'Saving report...');

      try {
        await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/reports/html', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            report_html: html,
            report_status: 'draft',
            report_source: reportEditorCurrentSource,
          }),
        });
        setReportEditorMessage('ok', 'Report saved successfully.');
        setMessage('completed-reports-msg', 'ok', 'Report updated for claim ' + String(row.external_claim_id || '-') + '.');
        await loadRows(false);
      } catch (err) {
        setReportEditorMessage('err', err && err.message ? err.message : 'Failed to save report.');
      } finally {
        if (reportEditorSaveBtn) {
          reportEditorSaveBtn.disabled = false;
          reportEditorSaveBtn.textContent = oldText || 'Save Report';
        }
      }
    }
    function openCaseDetail(row, reportSource) {
      const claimUuid = String(row.claim_uuid || '').trim();
      if (!claimUuid) return;
      const route = parseRoute();
      const role = String((route && route.routeRole) || 'user').trim() || 'user';
      const params = new URLSearchParams();
      params.set('claim_uuid', claimUuid);
      params.set('claim_id', String(row.external_claim_id || ''));
      const backPage = auditOnlyRole ? 'audit-claims' : (initialStatusFilter === 'uploaded' ? 'completed-uploaded' : 'completed-not-uploaded');
      params.set('back_page', backPage);
      params.set('search_claim', String(document.getElementById('completed-reports-search').value || '').trim());
      params.set('allotment_date', String(document.getElementById('completed-reports-allotment-date').value || '').trim());
      const src = (String(reportSource || 'doctor').toLowerCase() === 'system') ? 'system' : 'doctor';
      params.set('report_source', src);
      window.location.href = '/qc/' + encodeURIComponent(role) + '/case-detail?' + params.toString();
    }

    async function fetchBestSavedReport(claimUuid) {
      const id = String(claimUuid || '').trim();
      if (!id) throw new Error('Claim key missing.');
      const sources = ['doctor', 'system', 'any'];
      let lastErr = null;
      for (let i = 0; i < sources.length; i += 1) {
        const src = sources[i];
        try {
          const payload = await apiFetch('/api/v1/user-tools/completed-reports/' + encodeURIComponent(id) + '/latest-html?source=' + encodeURIComponent(src));
          const html = normalizeHealthClaimReportTitle(String(payload && payload.report_html ? payload.report_html : '').trim());
          if (html) return Object.assign({}, payload || {}, { report_html: html });
        } catch (err) {
          lastErr = err;
        }
      }
      if (lastErr && lastErr.message) throw lastErr;
      throw new Error('No saved report HTML found for this claim.');
    }

    async function exportWord(row) {
      const claimUuid = String(row.claim_uuid || '').trim();
      if (!claimUuid) return;
      const payload = await fetchBestSavedReport(claimUuid);
      const html = normalizeHealthClaimReportTitle(String(payload && payload.report_html ? payload.report_html : '').trim());
      if (!html) throw new Error('No saved report HTML found for this claim.');
      const wrapped = '<html><head><meta charset="UTF-8"><title>Claim Report</title></head><body>' + html + '</body></html>';
      const blob = new Blob([wrapped], { type: 'application/msword;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'claim_report_' + String(row.external_claim_id || 'report') + '.doc';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
    }

    let pdfLibsPromise = null;
    function loadExternalScript(src) {
      return new Promise(function (resolve, reject) {
        const existing = document.querySelector('script[data-ext-src="' + src + '"]');
        if (existing) {
          if (existing.getAttribute('data-loaded') === 'yes') {
            resolve();
            return;
          }
          existing.addEventListener('load', function () { resolve(); }, { once: true });
          existing.addEventListener('error', function () { reject(new Error('Failed to load ' + src)); }, { once: true });
          return;
        }
        const s = document.createElement('script');
        s.src = src;
        s.async = true;
        s.defer = true;
        s.setAttribute('data-ext-src', src);
        s.addEventListener('load', function () {
          s.setAttribute('data-loaded', 'yes');
          resolve();
        }, { once: true });
        s.addEventListener('error', function () {
          reject(new Error('Failed to load ' + src));
        }, { once: true });
        document.head.appendChild(s);
      });
    }

    async function ensurePdfLibraries() {
      if (window.jspdf && window.jspdf.jsPDF && window.html2canvas) return;
      if (!pdfLibsPromise) {
        pdfLibsPromise = (async function () {
          await loadExternalScript('https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js');
          await loadExternalScript('https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js');
          if (!(window.jspdf && window.jspdf.jsPDF && window.html2canvas)) {
            throw new Error('PDF libraries unavailable in browser.');
          }
        }());
      }
      await pdfLibsPromise;
    }

    function buildPdfRenderNode(html) {
      const host = document.createElement('div');
      host.style.position = 'fixed';
      host.style.left = '0';
      host.style.top = '0';
      host.style.width = '520px';
      host.style.background = '#ffffff';
      host.style.padding = '0';
      host.style.opacity = '0.001';
      host.style.pointerEvents = 'none';
      host.style.zIndex = '2147483647';

      const safeHtml = String(html || '')
        .replace(/<script[\s\S]*?<\/script>/gi, '')
        .replace(/<style[\s\S]*?<\/style>/gi, '');

      const style = document.createElement('style');
      style.textContent = ''
        + '.pdf-wrap,.pdf-wrap *{box-sizing:border-box;}'
        + '.pdf-wrap{font-family:Arial,sans-serif;color:#111;background:#fff;line-height:1.3;letter-spacing:0;word-spacing:0;font-size:11px;}'
        + '.pdf-wrap{width:520px;max-width:520px;margin:0 auto;padding:0 8px;}'
        + '.pdf-wrap table{border-collapse:collapse;width:100%;}'
        + '.pdf-wrap tr{break-inside:auto;page-break-inside:auto;}'
        + '.pdf-wrap th,.pdf-wrap td,.pdf-wrap p,.pdf-wrap div,.pdf-wrap span{white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;overflow:visible !important;text-align:left !important;text-justify:auto !important;}'
        + '.pdf-wrap p,.pdf-wrap ul,.pdf-wrap ol{margin:0 0 6px 0;padding:0;}'
        + '.pdf-wrap li{margin:0 0 4px 18px;padding:0;}'
        + '.pdf-wrap th,.pdf-wrap td{border:1px solid #222;padding:8px;vertical-align:top;font-size:10px !important;line-height:1.25 !important;}'
        + '.pdf-wrap th{text-align:left;background:#f0d9d2;width:38%;min-width:220px;font-weight:700;}'
        + '.pdf-wrap .sec{font-weight:700;margin:12px 0 8px;background:#f3ece9;padding:6px 8px;border:1px solid #ddd;break-inside:avoid;page-break-after:avoid;}'
        + '.pdf-wrap .title{display:block;text-align:center !important;font-size:16px !important;line-height:1.2 !important;letter-spacing:.1px;margin:4px 0 6px;font-weight:700;}'
        + '.pdf-wrap .meta{display:block;text-align:right !important;max-width:480px;margin-left:auto;font-size:10px !important;line-height:1.2 !important;color:#333;margin-bottom:8px;padding-right:8px;white-space:normal !important;overflow-wrap:anywhere;word-break:break-word;}'
        + '.pdf-wrap .pdf-signature-section{margin:18px 0 6px;padding-top:10px;border-top:1px solid #bbb;page-break-inside:avoid;break-inside:avoid;text-align:right !important;}'
        + '.pdf-wrap .pdf-signature-label{font-size:10px;color:#333;margin:0 0 6px 0;text-align:right !important;}'
        + '.pdf-wrap .pdf-signature-image{display:inline-block;max-width:180px;max-height:90px;width:auto;height:auto;object-fit:contain;}';

      const wrap = document.createElement('div');
      wrap.className = 'pdf-wrap';
      wrap.innerHTML = safeHtml;

      const signatureSection = document.createElement('div');
      signatureSection.className = 'pdf-signature-section';
      signatureSection.innerHTML = ''
        + '<img class="pdf-signature-image" src="/qc/public/assets/report-signature.png" alt="Signature" crossorigin="anonymous" />';
      wrap.appendChild(signatureSection);

      host.appendChild(style);
      host.appendChild(wrap);
      return host;
    }
    function renderHtmlToPdf(doc, element, options) {
      return new Promise(function (resolve, reject) {
        try {
          doc.html(element, Object.assign({}, options || {}, {
            callback: function (pdfDoc) { resolve(pdfDoc); },
          }));
        } catch (err) {
          reject(err);
        }
      });
    }

    function saveBlobAsFile(blob, fileName) {
      if (!blob) return false;
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = fileName;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
      return true;
    }

    function waitForRenderImages(rootNode, timeoutMs) {
      const root = rootNode || document;
      const maxWait = Number(timeoutMs || 4000);
      const images = Array.from(root.querySelectorAll('img'));
      if (!images.length) return Promise.resolve();

      return Promise.all(images.map(function (img) {
        if (img && img.complete && Number(img.naturalWidth || 0) > 0) return Promise.resolve();
        return new Promise(function (resolve) {
          let done = false;
          const finish = function () {
            if (done) return;
            done = true;
            resolve();
          };
          const timer = window.setTimeout(finish, maxWait);
          img.addEventListener('load', function () {
            window.clearTimeout(timer);
            finish();
          }, { once: true });
          img.addEventListener('error', function () {
            window.clearTimeout(timer);
            finish();
          }, { once: true });
        });
      })).then(function () { return; });
    }

    async function exportPdfUsingHtmlRenderer(host, fileName, renderWidthPx) {
      const jsPDFCtor = window.jspdf.jsPDF;
      const pdf = new jsPDFCtor({
        orientation: 'p',
        unit: 'pt',
        format: 'a4',
        compress: true,
      });

      const pageWidth = pdf.internal.pageSize.getWidth();
      const marginPt = 20;
      const contentWidthPt = Math.max(380, pageWidth - (marginPt * 2));

      await renderHtmlToPdf(pdf, host, {
        margin: [marginPt, marginPt, marginPt, marginPt],
        autoPaging: 'text',
        width: contentWidthPt,
        windowWidth: Math.max(500, Math.ceil(renderWidthPx || 520)),
        html2canvas: {
          scale: 1,
          useCORS: true,
          backgroundColor: '#ffffff',
          logging: false,
        },
        pagebreak: {
          mode: ['css', 'legacy'],
          avoid: ['.sec'],
        },
      });

      const blob = pdf.output('blob');
      if (!blob || Number(blob.size || 0) < 6000) return false;
      return saveBlobAsFile(blob, fileName);
    }

    async function exportPdfUsingCanvasFallback(host, fileName) {
      const canvas = await window.html2canvas(host, {
        scale: 1.8,
        useCORS: true,
        backgroundColor: '#ffffff',
        logging: false,
      });

      const jsPDFCtor = window.jspdf.jsPDF;
      const pdf = new jsPDFCtor({ orientation: 'p', unit: 'pt', format: 'a4', compress: true });
      const pageWidth = pdf.internal.pageSize.getWidth();
      const pageHeight = pdf.internal.pageSize.getHeight();
      const margin = 20;
      const contentWidth = pageWidth - (margin * 2);
      const contentHeight = pageHeight - (margin * 2);
      const imgData = canvas.toDataURL('image/png');
      const imgWidth = contentWidth;
      const imgHeight = (canvas.height * imgWidth) / canvas.width;

      let heightLeft = imgHeight;
      let position = margin;
      pdf.addImage(imgData, 'PNG', margin, position, imgWidth, imgHeight, undefined, 'FAST');
      heightLeft -= contentHeight;

      while (heightLeft > 0) {
        position = margin - (imgHeight - heightLeft);
        pdf.addPage();
        pdf.addImage(imgData, 'PNG', margin, position, imgWidth, imgHeight, undefined, 'FAST');
        heightLeft -= contentHeight;
      }

      const blob = pdf.output('blob');
      if (!blob || Number(blob.size || 0) < 3000) {
        throw new Error('PDF generation failed.');
      }
      saveBlobAsFile(blob, fileName);
      return true;
    }

    async function exportPdf(row) {
      const claimUuid = String(row.claim_uuid || '').trim();
      if (!claimUuid) return;

      const payload = await fetchBestSavedReport(claimUuid);
      const html = normalizeHealthClaimReportTitle(String(payload && payload.report_html ? payload.report_html : '').trim());
      if (!html) throw new Error('No saved report HTML found for this claim.');

      await ensurePdfLibraries();

      const fileName = 'claim_report_' + String(row.external_claim_id || 'report') + '.pdf';
      const host = buildPdfRenderNode(html);
      document.body.appendChild(host);

      try {
        await new Promise(function (resolve) { requestAnimationFrame(function () { requestAnimationFrame(resolve); }); });
        await waitForRenderImages(host, 5000);

        const renderWidthPx = Math.max(500, Math.ceil((host && host.scrollWidth) ? host.scrollWidth : 520));

        let saved = false;
        try {
          saved = await exportPdfUsingHtmlRenderer(host, fileName, renderWidthPx);
        } catch (_primaryErr) {
          saved = false;
        }

        if (!saved) {
          await exportPdfUsingCanvasFallback(host, fileName);
        }
      } finally {
        if (host && host.parentNode) host.parentNode.removeChild(host);
      }
    }
    async function changeClaimStatus(row, targetStatus, successMessage) {
      const claimUuid = String(row.claim_uuid || '').trim();
      if (!claimUuid) return;
      await apiFetch('/api/v1/claims/' + encodeURIComponent(claimUuid) + '/status', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: targetStatus }),
      });
      setMessage('completed-reports-msg', 'ok', successMessage + ' for claim ' + String(row.external_claim_id || '-') + '.');
      await loadRows(false);
    }

    async function loadRows(resetPage) {
      if (resetPage) state.page = 1;

      const searchClaim = String(searchFilterEl && searchFilterEl.value ? searchFilterEl.value : '').trim();
      const allotmentDate = String(allotmentDateEl && allotmentDateEl.value ? allotmentDateEl.value : '').trim();
      const statusFilterRaw = String(statusFilterEl && statusFilterEl.value ? statusFilterEl.value : state.status).trim();
      const qcFilterRaw = String(qcFilterEl && qcFilterEl.value ? qcFilterEl.value : (state.qc || 'no')).trim();
      const statusFilter = statusFilterRaw;
      const qcFilter = auditOnlyRole ? 'no' : qcFilterRaw;
      const doctorFilter = String(doctorFilterEl && doctorFilterEl.value ? doctorFilterEl.value : '').trim();

      const params = new URLSearchParams();
      if (searchClaim) params.set('search_claim', searchClaim);
      if (allotmentDate) params.set('allotment_date', allotmentDate);
      if (statusFilter) params.set('status_filter', statusFilter);
      if (qcFilter) params.set('qc_filter', qcFilter);
      if (doctorFilter) params.set('doctor_filter', doctorFilter);
      if (shouldExcludeTaggedForAuditor) params.set('exclude_tagged', 'true');
      params.set('limit', String(state.pageSize));
      params.set('offset', String((state.page - 1) * state.pageSize));
      state.status = statusFilter || state.status;
      state.qc = qcFilter || state.qc;
      writeCompletedReportFilters({
        status: state.status,
        qc: state.qc,
        doctor_filter: doctorFilter,
        search_claim: searchClaim,
        allotment_date: allotmentDate,
        page_size: state.pageSize,
      });

      if (auditOnlyRole) params.set('sort_order', 'allotment_asc');
      const earliestParams = new URLSearchParams(params.toString());
      earliestParams.set('limit', '1');
      earliestParams.set('offset', '0');

      const [result, earliestResult] = await Promise.all([
        apiFetch('/api/v1/user-tools/completed-reports?' + params.toString()),
        auditOnlyRole ? apiFetch('/api/v1/user-tools/completed-reports?' + earliestParams.toString()) : Promise.resolve(null),
      ]);
      state.total = Number(result && result.total ? result.total : 0);

      if (result && result.tagging_subtagging_options && typeof result.tagging_subtagging_options === 'object') {
        taggingSubtaggingMap = result.tagging_subtagging_options;
      }

      const rawItems = Array.isArray(result && result.items) ? result.items : [];
      const items = auditOnlyRole ? sortClaimsByAllotmentDateFirst(rawItems, true) : rawItems;
      if (auditOnlyRole) {
        const earliestItem = (earliestResult && Array.isArray(earliestResult.items) && earliestResult.items.length)
          ? earliestResult.items[0]
          : null;
        const minDateTs = parseAllotmentDateValue(earliestItem && earliestItem.allotment_date);
        auditorAllotmentGate = {
          enforced: Number.isFinite(minDateTs),
          minDateTs: minDateTs,
          minDateLabel: Number.isFinite(minDateTs) ? formatDateOnly(new Date(minDateTs)) : '',
        };
      } else {
        auditorAllotmentGate = { enforced: false, minDateTs: Number.NaN, minDateLabel: '' };
      }

      rowsByClaim.clear();
      items.forEach(function (r) {
        rowsByClaim.set(String(r.claim_uuid || ''), r || {});
      });

      const rows = items.map(function (r) {
        const claimUuid = String(r.claim_uuid || '');
        const taggingText = normalizeDisplayText(r.tagging);
        const subtaggingText = normalizeDisplayText(r.subtagging);
        const opinionText = normalizeOpinionText(r.opinion);
        const opinionDisplay = previewOpinion(opinionText);
        const qcText = String(r.qc_status || 'no').toLowerCase() === 'yes' ? 'Yes' : 'No';
        const hasReportHtml = !!r.report_html_available;
        const hasDoctorReport = !!r.doctor_report_html_available || (hasReportHtml && String(r.latest_report_source || 'doctor') !== 'system');
        const hasSystemReport = !!r.system_report_html_available;
        const hasAnyReport = hasDoctorReport || hasSystemReport || hasReportHtml;
        const canOpenDoctorHtml = auditOnlyRole ? true : hasDoctorReport;
        const isLockedForAudit = isAuditorClaimLocked(r);
        const auditLockTitle = isLockedForAudit
          ? ('Process allotment date ' + String(auditorAllotmentGate.minDateLabel || '-') + ' claim(s) first.')
          : '';
        const primaryButtonsHtml = auditOnlyRole
          ? ('<button type="button" data-completed-view-html="' + escapeHtml(claimUuid) + '"'
            + (canOpenDoctorHtml && !isLockedForAudit ? '' : ' disabled')
            + (auditLockTitle ? (' title="' + escapeHtml(auditLockTitle) + '"') : '')
            + '>View Doctor HTML</button>')
          : ('<button type="button" data-completed-view-html="' + escapeHtml(claimUuid) + '"' + (hasDoctorReport ? '' : ' disabled') + '>View Doctor HTML</button>'
            + '<button type="button" data-completed-view-doc="' + escapeHtml(claimUuid) + '" data-completed-view-claim-id="' + escapeHtml(r.external_claim_id || '') + '">View Document</button>');

        let overflowButtonsHtml = '';
        if (auditOnlyRole) {
        } else {
          overflowButtonsHtml += '<button type="button" data-completed-export-word="' + escapeHtml(claimUuid) + '"' + (hasAnyReport ? '' : ' disabled') + '>Export Word</button>';
          overflowButtonsHtml += '<button type="button" data-completed-export-pdf="' + escapeHtml(claimUuid) + '"' + (hasAnyReport ? '' : ' disabled') + '>Export PDF</button>';
          overflowButtonsHtml += '<button type="button" data-completed-pending="' + escapeHtml(claimUuid) + '">Pending</button>';
          overflowButtonsHtml += '<button type="button" data-completed-withdraw="' + escapeHtml(claimUuid) + '">Withdraw</button>';
          overflowButtonsHtml += '<button type="button" class="btn-soft" data-completed-open-upload="' + escapeHtml(claimUuid) + '">Update Upload</button>';
        }

        const moreMenuHtml = overflowButtonsHtml
          ? ('<details class="action-menu"><summary>More</summary><div class="action-menu-list">' + overflowButtonsHtml + '</div></details>')
          : '';

        const actionButtonsHtml = '<div class="action-row action-row-primary">' + primaryButtonsHtml + moreMenuHtml + '</div>';
        return '<tr>'
          + '<td>' + escapeHtml(r.external_claim_id || '-') + '</td>'
          + '<td>' + escapeHtml(formatAssignedDoctor(r.assigned_doctor_id)) + '</td>'
          + '<td>' + escapeHtml(formatDateTime(r.completed_at || r.updated_at || '')) + '</td>'
          + '<td>' + escapeHtml(formatStatusText(r.report_status || 'pending')) + '</td>'
          + '<td>' + escapeHtml(qcText) + '</td>'
          + '<td>' + escapeHtml(taggingText || '-') + '</td>'
          + '<td>' + escapeHtml(subtaggingText || '-') + '</td>'
          + '<td title="' + escapeHtml(opinionText) + '">' + escapeHtml(opinionDisplay) + '</td>'
          + '<td>' + escapeHtml(String(r.report_count || 0)) + '</td>'
          + '<td class="action-cell"><div class="action-buttons">'
          + actionButtonsHtml
          + '</div></td>'
          + '</tr>';
      }).join('');

      tbody.innerHTML = rows || '<tr><td colspan="10">No completed reports found.</td></tr>';

      if (!canManageUpload) {
        tbody.querySelectorAll('button[data-completed-open-upload]').forEach(function (btn) { btn.remove(); });
      }
      if (!canAuditQc) {
        tbody.querySelectorAll('button[data-completed-open-qc], button[data-completed-send-back]').forEach(function (btn) { btn.remove(); });
      }
      if (!canChangeStatus) {
        tbody.querySelectorAll('button[data-completed-pending], button[data-completed-withdraw]').forEach(function (btn) { btn.remove(); });
      }

      tbody.querySelectorAll('button[data-completed-view-html]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          const row = rowsByClaim.get(String(this.getAttribute('data-completed-view-html') || ''));
          if (!row) return;
          if (auditOnlyRole) {
            if (isAuditorClaimLocked(row)) {
              setMessage(
                'completed-reports-msg',
                'err',
                'Allotment-wise audit is enabled. Please complete allotment date '
                  + String(auditorAllotmentGate.minDateLabel || '-')
                  + ' claim(s) first.'
              );
              return;
            }
            openAuditorQcWorkspace(row);
            return;
          }
          const rowHasReportHtml = !!row.report_html_available;
          const rowHasDoctorReport = !!row.doctor_report_html_available || (rowHasReportHtml && String(row.latest_report_source || 'doctor') !== 'system');
          const rowHasSystemReport = !!row.system_report_html_available || rowHasReportHtml;
          try {
            if (rowHasDoctorReport) {
              await openEditableReport(row, 'doctor');
            } else if (rowHasSystemReport) {
              await openEditableReport(row, 'system');
            } else {
              throw new Error('No saved report HTML found for this claim.');
            }
          } catch (err) {
            setMessage('completed-reports-msg', 'err', err && err.message ? err.message : 'Failed to open doctor report.');
          }
        });
      });


      tbody.querySelectorAll('button[data-completed-view-system]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          const row = rowsByClaim.get(String(this.getAttribute('data-completed-view-system') || ''));
          if (!row) return;
          try {
            await openEditableReport(row, 'system');
          } catch (err) {
            setMessage('completed-reports-msg', 'err', err && err.message ? err.message : 'Failed to open system report.');
          }
        });
      });

      tbody.querySelectorAll('button[data-completed-view-doc]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          await showClaimDocuments(
            String(this.getAttribute('data-completed-view-doc') || ''),
            String(this.getAttribute('data-completed-view-claim-id') || '')
          );
        });
      });

      tbody.querySelectorAll('button[data-completed-export-word]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          try {
            const row = rowsByClaim.get(String(this.getAttribute('data-completed-export-word') || ''));
            if (!row) return;
            await exportWord(row);
          } catch (err) {
            setMessage('completed-reports-msg', 'err', err && err.message ? err.message : 'Export Word failed.');
          }
        });
      });

      tbody.querySelectorAll('button[data-completed-export-pdf]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          try {
            const row = rowsByClaim.get(String(this.getAttribute('data-completed-export-pdf') || ''));
            if (!row) return;
            await exportPdf(row);
          } catch (err) {
            setMessage('completed-reports-msg', 'err', err && err.message ? err.message : 'Export PDF failed.');
          }
        });
      });

      tbody.querySelectorAll('button[data-completed-pending]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          const row = rowsByClaim.get(String(this.getAttribute('data-completed-pending') || ''));
          if (!row) return;
          const ok = window.confirm('Set this claim to Pending?');
          if (!ok) return;
          try {
            await changeClaimStatus(row, 'waiting_for_documents', 'Claim moved to Pending');
          } catch (err) {
            setMessage('completed-reports-msg', 'err', err && err.message ? err.message : 'Pending update failed.');
          }
        });
      });

      tbody.querySelectorAll('button[data-completed-withdraw]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          const row = rowsByClaim.get(String(this.getAttribute('data-completed-withdraw') || ''));
          if (!row) return;
          const ok = window.confirm('Move this claim to Withdrawn?');
          if (!ok) return;
          try {
            await changeClaimStatus(row, 'withdrawn', 'Claim moved to Withdrawn');
          } catch (err) {
            setMessage('completed-reports-msg', 'err', err && err.message ? err.message : 'Withdraw failed.');
          }
        });
      });

      tbody.querySelectorAll('button[data-completed-open-upload]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          const row = rowsByClaim.get(String(this.getAttribute('data-completed-open-upload') || ''));
          if (row) openUploadStatusModal(row);
        });
      });

      tbody.querySelectorAll('button[data-completed-open-qc]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          const row = rowsByClaim.get(String(this.getAttribute('data-completed-open-qc') || ''));
          if (row) openQcStatusModal(row);
        });
      });

      tbody.querySelectorAll('button[data-completed-send-back]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          const row = rowsByClaim.get(String(this.getAttribute('data-completed-send-back') || ''));
          if (!row) return;
          const opinion = String(window.prompt('Enter auditor opinion to send this case back to doctor:', '') || '').trim();
          if (!opinion) {
            setMessage('completed-reports-msg', 'err', 'Auditor opinion is required.');
            return;
          }
          const ok = window.confirm('Send this case back to doctor?');
          if (!ok) return;
          try {
            await apiFetch('/api/v1/claims/' + encodeURIComponent(String(row.claim_uuid || '')) + '/status', {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ status: 'in_review', note: opinion }),
            });
            setMessage('completed-reports-msg', 'ok', 'Case sent back to doctor for claim ' + String(row.external_claim_id || '-') + '.');
            await loadRows(false);
          } catch (err) {
            setMessage('completed-reports-msg', 'err', err && err.message ? err.message : 'Send back failed.');
          }
        });
      });

      updatePaginationUi();
    }

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      setMessage('completed-reports-msg', '', '');
      try {
        await loadRows(true);
      } catch (err) {
        setMessage('completed-reports-msg', 'err', err && err.message ? err.message : 'Failed to load completed reports.');
      }
    });

    pageSizeEl.addEventListener('change', async function () {
      state.pageSize = Number(this.value || 20);
      await loadRows(true);
    });

    prevBtn.addEventListener('click', async function () {
      if (state.page <= 1) return;
      state.page -= 1;
      await loadRows(false);
    });

    nextBtn.addEventListener('click', async function () {
      const maxPage = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
      if (state.page >= maxPage) return;
      state.page += 1;
      await loadRows(false);
    });

    if (docsModalCloseBtn) {
      docsModalCloseBtn.addEventListener('click', closeDocsModal);
    }
    if (docsModalEl) {
      docsModalEl.addEventListener('click', function (e) {
        if (e.target === docsModalEl) closeDocsModal();
      });
    }

    if (uploadTaggingEl) {
      uploadTaggingEl.addEventListener('change', function () {
        setSubtaggingOptions(uploadTaggingEl.value, '');
      });
    }

    if (uploadModalCloseBtn) {
      uploadModalCloseBtn.addEventListener('click', closeUploadModal);
    }
    if (uploadModalEl) {
      uploadModalEl.addEventListener('click', function (e) {
        if (e.target === uploadModalEl) closeUploadModal();
      });
    }

    if (qcModalCloseBtn) {
      qcModalCloseBtn.addEventListener('click', closeQcModal);
    }
    if (qcModalEl) {
      qcModalEl.addEventListener('click', function (e) {
        if (e.target === qcModalEl) closeQcModal();
      });
    }

    if (reportEditorCloseBtn) {
      reportEditorCloseBtn.addEventListener('click', closeReportEditorModal);
    }
    if (reportEditorModalEl) {
      reportEditorModalEl.addEventListener('click', function (e) {
        if (e.target === reportEditorModalEl) closeReportEditorModal();
      });
    }
    if (reportEditorSaveBtn) {
      reportEditorSaveBtn.addEventListener('click', async function () {
        await saveEditableReport();
      });
    }

    if (uploadForm) {
      uploadForm.addEventListener('submit', async function (e) {
        e.preventDefault();

        const claimUuid = String(uploadClaimUuidEl.value || '').trim();
        const tagging = String(uploadTaggingEl.value || '').trim();
        const subtagging = String(uploadSubtaggingEl.value || '').trim();
        const opinion = String(uploadOpinionEl.value || '').trim();

        if (!claimUuid) {
          setUploadModalMessage('err', 'Invalid claim selected.');
          return;
        }
        if (!tagging || !subtagging || !opinion) {
          setUploadModalMessage('err', 'Tagging, Subtagging and Opinion are mandatory.');
          return;
        }

        const oldText = uploadSaveBtn.textContent;
        uploadSaveBtn.disabled = true;
        uploadSaveBtn.textContent = 'Saving...';
        setUploadModalMessage('', 'Saving upload status...');

        try {
          const result = await apiFetch('/api/v1/user-tools/completed-reports/' + encodeURIComponent(claimUuid) + '/upload-status', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              report_export_status: 'uploaded',
              tagging: tagging,
              subtagging: subtagging,
              opinion: opinion,
            }),
          });

          const claimLabel = String(result && result.external_claim_id ? result.external_claim_id : uploadModalClaimLabelEl.textContent.replace('Claim ID: ', ''));
          setMessage('completed-reports-msg', 'ok', 'Upload status updated to Uploaded for claim ' + claimLabel + '.');
          closeUploadModal();
          await loadRows(false);
        } catch (err) {
          setUploadModalMessage('err', err && err.message ? err.message : 'Failed to update upload status.');
        } finally {
          uploadSaveBtn.disabled = false;
          uploadSaveBtn.textContent = oldText || 'Save';
        }
      });
    }

    if (qcForm) {
      qcForm.addEventListener('submit', async function (e) {
        e.preventDefault();
        const claimUuid = String(qcClaimUuidEl.value || '').trim();
        const qcStatus = String(qcStatusEl.value || 'no').trim().toLowerCase();
        if (!claimUuid) {
          setQcModalMessage('err', 'Invalid claim selected.');
          return;
        }

        const oldText = qcSaveBtn.textContent;
        qcSaveBtn.disabled = true;
        qcSaveBtn.textContent = 'Saving...';
        setQcModalMessage('', 'Saving QC status...');

        try {
          const result = await apiFetch('/api/v1/user-tools/completed-reports/' + encodeURIComponent(claimUuid) + '/qc-status', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ qc_status: qcStatus }),
          });
          const claimLabel = String(result && result.external_claim_id ? result.external_claim_id : qcModalClaimLabelEl.textContent.replace('Claim ID: ', ''));
          setMessage('completed-reports-msg', 'ok', 'QC updated for claim ' + claimLabel + '.');
          closeQcModal();
          await loadRows(false);
        } catch (err) {
          setQcModalMessage('err', err && err.message ? err.message : 'Failed to update QC status.');
        } finally {
          qcSaveBtn.disabled = false;
          qcSaveBtn.textContent = oldText || 'Save';
        }
      });
    }

    detachCompletedReportsMessageListener();
    completedReportsMessageHandler = async function (event) {
      try {
        if (!event || event.origin !== window.location.origin) return;
        const payload = (event.data && typeof event.data === 'object') ? event.data : null;
        if (!payload) return;
        const eventType = String(payload.type || '').trim();
        if (eventType !== 'qc-updated' && eventType !== 'claim-status-updated' && eventType !== 'report-saved-from-tab') return;
        await loadRows(false);
      } catch (_err) {
      }
    };
    window.addEventListener('message', completedReportsMessageHandler);
    try {
      await loadRows(true);
    } catch (err) {
      setMessage('completed-reports-msg', 'err', err && err.message ? err.message : 'Failed to load completed reports.');
      tbody.innerHTML = '<tr><td colspan="10">Failed to load completed reports.</td></tr>';
      updatePaginationUi();
    }
  }

  async function renderExportData() {
    contentPanel.innerHTML = '<h2>Export Full Data</h2><p class="muted">Export filtered claims as CSV or Excel.</p>'
      + '<p id="export-msg"></p><form id="export-form" class="grid-2">'
      + '<div class="form-row"><label>From Date</label><input type="date" name="from_date"></div>'
      + '<div class="form-row"><label>To Date</label><input type="date" name="to_date"></div>'
      + '<div class="form-row"><label>Allotment Date</label><input type="date" name="allotment_date"></div>'
      + '<div class="form-row"><label>Format</label><select name="format"><option value="csv">CSV</option><option value="excel">Excel</option></select></div>'
      + '<div class="form-row" style="align-self:end"><button type="submit">Run Export</button></div>'
      + '</form><div id="export-preview"></div>';

    document.getElementById("export-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      const form = new FormData(e.currentTarget);
      const from = String(form.get('from_date') || '').trim();
      const to = String(form.get('to_date') || '').trim();
      const allotmentDate = String(form.get('allotment_date') || '').trim();
      const format = String(form.get('format') || 'csv').trim().toLowerCase();
      const normalizedFormat = (format === 'excel') ? 'excel' : 'csv';
      const qs = '?from_date=' + encodeURIComponent(from)
        + '&to_date=' + encodeURIComponent(to)
        + '&allotment_date=' + encodeURIComponent(allotmentDate)
        + '&format=' + encodeURIComponent(normalizedFormat);

      try {
        const isExcel = normalizedFormat === 'excel';
        const fileResult = await apiFetchFile('/api/v1/user-tools/export-full-data' + qs);
        const a = document.createElement('a');
        a.href = URL.createObjectURL(fileResult.blob);
        const fallbackName = isExcel ? 'user_full_data.xlsx' : 'user_full_data.csv';
        a.download = String(fileResult.filename || fallbackName);
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(a.href);
        setMessage('export-msg', 'ok', (isExcel ? 'Excel' : 'CSV') + ' exported successfully.');
        document.getElementById('export-preview').innerHTML = '';
      } catch (err) {
        setMessage('export-msg', 'err', err.message);
      }
    });
  }

  async function renderAllotmentDateWise() {
    contentPanel.innerHTML = '<h2>Allotment Date Wise Data</h2><p class="muted">Date-wise pending and completed (completed + uploaded) counts from legacy payload allocation date.</p>'
      + '<form id="allotment-form" class="grid-2">'
      + '<div class="form-row"><label>From Date</label><input type="date" name="from_date"></div>'
      + '<div class="form-row"><label>To Date</label><input type="date" name="to_date"></div>'
      + '<div class="form-row" style="align-self:end"><button type="submit">Apply Filter</button></div>'
      + '</form><div id="allotment-results"></div><div id="allotment-detail-results"></div>';

    function toInputDateLocal(value) {
      const dt = value instanceof Date ? value : new Date(value || '');
      if (Number.isNaN(dt.getTime())) return '';
      const year = dt.getFullYear();
      const month = String(dt.getMonth() + 1).padStart(2, '0');
      const day = String(dt.getDate()).padStart(2, '0');
      return year + '-' + month + '-' + day;
    }

    const allotmentFormEl = document.getElementById('allotment-form');
    const fromDateInputEl = allotmentFormEl ? allotmentFormEl.querySelector('input[name="from_date"]') : null;
    const toDateInputEl = allotmentFormEl ? allotmentFormEl.querySelector('input[name="to_date"]') : null;
    const allotmentResultsEl = document.getElementById('allotment-results');
    const allotmentDetailEl = document.getElementById('allotment-detail-results');

    const defaultToDateObj = new Date();
    const defaultFromDateObj = new Date(defaultToDateObj);
    defaultFromDateObj.setDate(defaultFromDateObj.getDate() - 9);
    const defaultFromDate = toInputDateLocal(defaultFromDateObj);
    const defaultToDate = toInputDateLocal(defaultToDateObj);

    if (fromDateInputEl && !String(fromDateInputEl.value || '').trim()) fromDateInputEl.value = defaultFromDate;
    if (toDateInputEl && !String(toDateInputEl.value || '').trim()) toDateInputEl.value = defaultToDate;

    function renderCountButton(count, bucket, options) {
      const numericCount = Number(count || 0);
      if (!(numericCount > 0)) return '<span class="muted">0</span>';
      const opts = options || {};
      const attrs = [];
      attrs.push('type="button"');
      attrs.push('class="allotment-count-link"');
      attrs.push('data-allotment-bucket="' + escapeHtml(bucket || 'all') + '"');
      if (opts.allotmentDate) attrs.push('data-allotment-date="' + escapeHtml(String(opts.allotmentDate || '')) + '"');
      if (opts.fromDate) attrs.push('data-from-date="' + escapeHtml(String(opts.fromDate || '')) + '"');
      if (opts.toDate) attrs.push('data-to-date="' + escapeHtml(String(opts.toDate || '')) + '"');
      return '<button ' + attrs.join(' ') + '>' + escapeHtml(String(numericCount)) + '</button>';
    }

    function detailTitleFor(bucket, allotmentDate, fromDate, toDate) {
      const label = bucket === 'completed' ? 'Completed (Completed + Uploaded)' : 'Pending';
      if (allotmentDate) {
        return label + ' claim list for allotment date ' + formatDateOnly(allotmentDate);
      }
      const fromLabel = fromDate ? formatDateOnly(fromDate) : '-';
      const toLabel = toDate ? formatDateOnly(toDate) : '-';
      return label + ' claim list for date range ' + fromLabel + ' to ' + toLabel;
    }

    async function loadDetailList(opts) {
      const options = opts || {};
      const bucket = String(options.bucket || 'all').trim().toLowerCase();
      const allotmentDate = String(options.allotmentDate || '').trim();
      const fromDate = String(options.fromDate || '').trim();
      const toDate = String(options.toDate || '').trim();

      const params = new URLSearchParams();
      params.set('bucket', bucket);
      if (allotmentDate) params.set('allotment_date', allotmentDate);
      if (fromDate) params.set('from_date', fromDate);
      if (toDate) params.set('to_date', toDate);
      params.set('limit', '5000');
      params.set('offset', '0');

      allotmentDetailEl.innerHTML = '<section class="claim-status-panel allotment-detail-panel"><p class="muted">Loading full list...</p></section>';

      const result = await apiFetch('/api/v1/user-tools/allotment-date-wise/claims?' + params.toString());
      const items = Array.isArray(result && result.items) ? result.items : [];
      const total = Number(result && result.total ? result.total : items.length);
      const rows = items.map(function (it) {
        return '<tr>'
          + '<td>' + escapeHtml(formatDateOnly(it.allotment_date || '')) + '</td>'
          + '<td>' + escapeHtml(it.external_claim_id || '-') + '</td>'
          + '<td>' + escapeHtml(it.patient_name || '-') + '</td>'
          + '<td>' + escapeHtml(it.assigned_doctor_id || '-') + '</td>'
          + '<td>' + escapeHtml(String(it.status || '-')) + '</td>'
          + '</tr>';
      }).join('');

      allotmentDetailEl.innerHTML = '<section class="claim-status-panel allotment-detail-panel">'
        + '<h3>' + escapeHtml(detailTitleFor(bucket, allotmentDate, fromDate, toDate)) + '</h3>'
        + '<p class="muted">Total Claims: <strong>' + escapeHtml(String(total)) + '</strong></p>'
        + '<div class="table-wrap"><table><thead><tr><th>Allotment Date</th><th>Claim ID</th><th>Patient Name</th><th>Assigned Doctor</th><th>Status</th></tr></thead><tbody>'
        + (rows || '<tr><td colspan="5">No records found.</td></tr>')
        + '</tbody></table></div>'
        + '<div class="link-row"><button type="button" class="btn-soft" id="allotment-detail-close">Close</button></div>'
        + '</section>';

      const closeBtn = document.getElementById('allotment-detail-close');
      if (closeBtn) {
        closeBtn.addEventListener('click', function () {
          allotmentDetailEl.innerHTML = '';
        });
      }
    }

    function attachAllotmentDetailHandlers() {
      if (!allotmentResultsEl) return;
      const countButtons = allotmentResultsEl.querySelectorAll('.allotment-count-link');
      countButtons.forEach(function (btn) {
        btn.addEventListener('click', async function () {
          const bucket = String(this.getAttribute('data-allotment-bucket') || 'all').trim().toLowerCase();
          const allotmentDate = String(this.getAttribute('data-allotment-date') || '').trim();
          const fromDate = String(this.getAttribute('data-from-date') || '').trim();
          const toDate = String(this.getAttribute('data-to-date') || '').trim();
          try {
            await loadDetailList({ bucket: bucket, allotmentDate: allotmentDate, fromDate: fromDate, toDate: toDate });
          } catch (err) {
            allotmentDetailEl.innerHTML = '<section class="claim-status-panel allotment-detail-panel"><p class="msg err">'
              + escapeHtml(err && err.message ? err.message : 'Failed to load detail list.')
              + '</p></section>';
          }
        });
      });
    }

    async function loadData(from, to) {
      const qs = '?from_date=' + encodeURIComponent(from || '') + '&to_date=' + encodeURIComponent(to || '');
      const result = await apiFetch('/api/v1/user-tools/allotment-date-wise' + qs);
      const items = result.items || [];
      let totalPending = 0;
      let totalCompleted = 0;
      let totalUploaded = 0;
      const rows = items.map(function (it) {
        totalPending += Number(it.pending_count || 0);
        totalCompleted += Number(it.completed_count || 0);
        totalUploaded += Number(it.uploaded_count || 0);
        const rowDate = String(it.allotment_date || '').trim();
        return '<tr>'
          + '<td>' + escapeHtml(rowDate || '-') + '</td>'
          + '<td>' + renderCountButton(it.pending_count, 'pending', { allotmentDate: rowDate }) + '</td>'
          + '<td>' + renderCountButton(it.completed_count, 'completed', { allotmentDate: rowDate }) + '</td>'
          + '</tr>';
      }).join('');

      allotmentResultsEl.innerHTML = '<p class="muted">Total Pending: '
        + renderCountButton(totalPending, 'pending', { fromDate: from, toDate: to })
        + ' | Total Completed (Completed + Uploaded): '
        + renderCountButton(totalCompleted, 'completed', { fromDate: from, toDate: to })
        + ' | Uploaded Included: <strong>' + escapeHtml(String(totalUploaded)) + '</strong></p>'
        + '<div class="table-wrap"><table><thead><tr><th>Allotment Date</th><th>Pending</th><th>Completed (Completed + Uploaded)</th></tr></thead><tbody>'
        + (rows || '<tr><td colspan="3">No records found.</td></tr>')
        + '</tbody></table></div>';

      attachAllotmentDetailHandlers();
    }

    document.getElementById('allotment-form').addEventListener('submit', async function (e) {
      e.preventDefault();
      const fd = new FormData(e.currentTarget);
      const fromDate = String(fd.get('from_date') || '');
      const toDate = String(fd.get('to_date') || '');
      allotmentDetailEl.innerHTML = '';
      try {
        await loadData(fromDate, toDate);
      } catch (err) {
        renderError(err.message);
      }
    });

    await loadData(defaultFromDate, defaultToDate);
  }
  async function renderStorageMaintenance() {
    const data = await apiFetch('/api/v1/admin/storage-maintenance');
    const bucketRows = (data.buckets || []).map((b) => '<tr><td>' + escapeHtml(b.bucket || 'unknown') + '</td><td>' + escapeHtml(String(b.count || 0)) + '</td></tr>').join('');

    contentPanel.innerHTML = '<h2>Storage Maintenance</h2><p class="muted">Read-only summary for document storage state.</p>'
      + '<div class="stats-grid">'
      + '<article class="stat-card"><div class="muted">Total Documents</div><div class="value">' + escapeHtml(String(data.total_documents || 0)) + '</div></article>'
      + '<article class="stat-card"><div class="muted">Total Size</div><div class="value">' + escapeHtml(formatBytes(data.total_bytes || 0)) + '</div></article>'
      + '<article class="stat-card"><div class="muted">Pending Parse</div><div class="value">' + escapeHtml(String((data.parse_status_counts || {}).pending || 0)) + '</div></article>'
      + '</div><div class="stats-grid" style="margin-top:12px;">'
      + '<article class="stat-card"><div class="muted">Processing</div><div class="value">' + escapeHtml(String((data.parse_status_counts || {}).processing || 0)) + '</div></article>'
      + '<article class="stat-card"><div class="muted">Succeeded</div><div class="value">' + escapeHtml(String((data.parse_status_counts || {}).succeeded || 0)) + '</div></article>'
      + '<article class="stat-card"><div class="muted">Failed</div><div class="value">' + escapeHtml(String((data.parse_status_counts || {}).failed || 0)) + '</div></article>'
      + '</div><h3 style="margin-top:18px">Bucket Split</h3>'
      + '<div class="table-wrap"><table><thead><tr><th>Bucket</th><th>Documents</th></tr></thead><tbody>'
      + (bucketRows || '<tr><td colspan="2">No bucket metadata found.</td></tr>')
      + '</tbody></table></div>';
  }

  async function renderRuleSuggestions() {
    const result = await apiFetch('/api/v1/admin/rule-suggestions?status_filter=all&limit=300');
    const items = result.items || [];

    const rows = items.map((s) => {
        return '<tr>'
        + '<td><code>' + escapeHtml(s.claim_id || '') + '</code></td>'
        + '<td>' + escapeHtml(s.suggestion_type || 'new_rule') + '</td>'
        + '<td><code>' + escapeHtml(s.target_rule_id || s.proposed_rule_id || '-') + '</code></td>'
        + '<td>' + escapeHtml(s.suggested_name || '-') + '</td>'
        + '<td>' + statusChip(s.suggested_decision || 'QUERY') + '</td>'
        + '<td>' + escapeHtml(String(s.generator_confidence || 0)) + '%</td>'
        + '<td>' + statusChip(s.status || 'pending') + '</td>'
        + '<td>'
        + (s.status === 'pending' ? '<button type="button" data-suggest-approve="' + s.id + '">Approve</button> <button type="button" class="btn-soft" data-suggest-reject="' + s.id + '">Reject</button>' : '<span class="muted">Reviewed</span>')
        + '</td></tr>'
        + '<tr><td colspan="8"><div class="muted" style="font-size:0.86rem;"><strong>Conditions:</strong> '
        + escapeHtml(s.suggested_conditions || '-')
        + '<br><strong>Reasoning:</strong> '
        + escapeHtml(s.generator_reasoning || '-')
        + '</div></td></tr>';
    }).join('');

    contentPanel.innerHTML = '<h2>Rule Suggestions</h2><p class="muted">Nightly generated suggestions from analysis history.</p>'
      + '<p id="suggest-msg"></p><div class="table-wrap"><table><thead><tr><th>Claim ID</th><th>Type</th><th>Rule Ref</th><th>Name</th><th>Decision</th><th>Confidence</th><th>Status</th><th>Action</th></tr></thead><tbody>'
      + (rows || '<tr><td colspan="8">No suggestions found.</td></tr>')
      + '</tbody></table></div>';

    contentPanel.querySelectorAll('button[data-suggest-approve]').forEach((btn) => {
      btn.addEventListener('click', async function () {
        const id = Number(this.getAttribute('data-suggest-approve'));
        try {
          await apiFetch('/api/v1/admin/rule-suggestions/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: 'approved' }) });
          setMessage('suggest-msg', 'ok', 'Suggestion approved.');
          await renderRuleSuggestions();
        } catch (err) {
          setMessage('suggest-msg', 'err', err.message);
        }
      });
    });

    contentPanel.querySelectorAll('button[data-suggest-reject]').forEach((btn) => {
      btn.addEventListener('click', async function () {
        const id = Number(this.getAttribute('data-suggest-reject'));
        try {
          await apiFetch('/api/v1/admin/rule-suggestions/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: 'rejected' }) });
          setMessage('suggest-msg', 'ok', 'Suggestion rejected.');
          await renderRuleSuggestions();
        } catch (err) {
          setMessage('suggest-msg', 'err', err.message);
        }
      });
    });
  }
  async function renderClaimRules() {
    const result = await apiFetch('/api/v1/admin/claim-rules?limit=500');
    const items = result.items || [];

    const rows = items.map((r) => {
        return '<tr>'
        + '<td><code>' + escapeHtml(r.rule_id) + '</code></td>'
        + '<td>' + escapeHtml(r.name) + '</td>'
        + '<td>' + statusChip(r.decision) + '</td>'
        + '<td>' + escapeHtml(r.severity) + '</td>'
        + '<td>' + escapeHtml(String(r.priority || 0)) + '</td>'
        + '<td>' + statusChip(r.is_active ? 'active' : 'inactive') + '</td>'
        + '<td><button type="button" data-rule-edit="' + r.id + '">Edit</button> '
        + '<button type="button" class="btn-soft" data-rule-toggle="' + r.id + '" data-rule-active="' + (r.is_active ? '1' : '0') + '">' + (r.is_active ? 'Disable' : 'Enable') + '</button> '
        + '<button type="button" data-rule-delete="' + r.id + '">Delete</button></td></tr>';
    }).join('');

    contentPanel.innerHTML = '<h2>Claim Rules</h2><p class="muted">Manage claim checklist rules.</p>'
      + '<p id="rule-msg"></p><div class="table-wrap" style="margin-bottom:12px;"><table><thead><tr><th>Rule ID</th><th>Name</th><th>Decision</th><th>Severity</th><th>Priority</th><th>Status</th><th>Action</th></tr></thead><tbody>'
      + (rows || '<tr><td colspan="7">No rules found.</td></tr>')
      + '</tbody></table></div>'
      + '<h3 id="rule-form-title">Create Rule</h3><form id="rule-form"><input type="hidden" name="row_id" value="">'
      + '<div class="grid-2">'
      + '<div class="form-row"><label>Rule ID</label><input name="rule_id" required></div>'
      + '<div class="form-row"><label>Name</label><input name="name" required></div>'
      + '<div class="form-row"><label>Decision</label><select name="decision"><option>APPROVE</option><option selected>QUERY</option><option>REJECT</option></select></div>'
      + '<div class="form-row"><label>Severity</label><select name="severity"><option>INFO</option><option selected>SOFT_QUERY</option><option>HARD_REJECT</option></select></div>'
      + '<div class="form-row"><label>Priority</label><input type="number" name="priority" value="999" min="1" max="9999"></div>'
      + '<div class="form-row"><label>Version</label><input name="version" value="1.0"></div>'
      + '</div>'
      + '<div class="form-row"><label>Scope (comma/new line)</label><textarea name="scope"></textarea></div>'
      + '<div class="form-row"><label>Conditions</label><textarea name="conditions" required></textarea></div>'
      + '<div class="form-row"><label>Remark Template</label><textarea name="remark_template"></textarea></div>'
      + '<div class="form-row"><label>Required Evidence (new line)</label><textarea name="required_evidence"></textarea></div>'
      + '<div class="form-row"><label><input type="checkbox" name="is_active" checked style="width:auto"> Active</label></div>'
      + '<div class="link-row"><button type="submit">Save Rule</button><button type="button" class="btn-soft" id="rule-reset-btn">New Rule</button></div></form>';

    const form = document.getElementById('rule-form');
    const title = document.getElementById('rule-form-title');

    function resetForm() {
      form.reset();
      form.row_id.value = '';
      form.priority.value = 999;
      form.version.value = '1.0';
      form.is_active.checked = true;
      title.textContent = 'Create Rule';
    }

    function fillForm(item) {
      form.row_id.value = String(item.id || '');
      form.rule_id.value = item.rule_id || '';
      form.name.value = item.name || '';
      form.decision.value = item.decision || 'QUERY';
      form.severity.value = item.severity || 'SOFT_QUERY';
      form.priority.value = item.priority || 999;
      form.version.value = item.version || '1.0';
      form.scope.value = listToTextarea(item.scope || []);
      form.conditions.value = item.conditions || '';
      form.remark_template.value = item.remark_template || '';
      form.required_evidence.value = listToTextarea(item.required_evidence || []);
      form.is_active.checked = !!item.is_active;
      title.textContent = 'Edit Rule';
    }

    document.getElementById('rule-reset-btn').addEventListener('click', resetForm);

    contentPanel.querySelectorAll('button[data-rule-edit]').forEach((btn) => {
      btn.addEventListener('click', function () {
        const id = Number(this.getAttribute('data-rule-edit'));
        const found = items.find((x) => x.id === id);
        if (found) fillForm(found);
      });
    });

    contentPanel.querySelectorAll('button[data-rule-toggle]').forEach((btn) => {
      btn.addEventListener('click', async function () {
        const id = Number(this.getAttribute('data-rule-toggle'));
        const active = this.getAttribute('data-rule-active') === '1';
        try {
          await apiFetch('/api/v1/admin/claim-rules/' + id + '/toggle?is_active=' + (!active), { method: 'PATCH' });
          setMessage('rule-msg', 'ok', 'Rule status updated.');
          await renderClaimRules();
        } catch (err) {
          setMessage('rule-msg', 'err', err.message);
        }
      });
    });

    contentPanel.querySelectorAll('button[data-rule-delete]').forEach((btn) => {
      btn.addEventListener('click', async function () {
        const id = Number(this.getAttribute('data-rule-delete'));
        if (!window.confirm('Delete this rule?')) return;
        try {
          await apiFetch('/api/v1/admin/claim-rules/' + id, { method: 'DELETE' });
          setMessage('rule-msg', 'ok', 'Rule deleted.');
          await renderClaimRules();
        } catch (err) {
          setMessage('rule-msg', 'err', err.message);
        }
      });
    });

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      const rowId = String(form.row_id.value || '').trim();
      const payload = {
        rule_id: String(form.rule_id.value || '').trim(),
        name: String(form.name.value || '').trim(),
        scope: parseListInput(form.scope.value),
        conditions: String(form.conditions.value || '').trim(),
        decision: String(form.decision.value || 'QUERY').trim(),
        remark_template: String(form.remark_template.value || '').trim(),
        required_evidence: parseListInput(form.required_evidence.value),
        severity: String(form.severity.value || 'SOFT_QUERY').trim(),
        priority: Number(form.priority.value || 999),
        is_active: !!form.is_active.checked,
        version: String(form.version.value || '1.0').trim() || '1.0',
      };

      try {
        if (rowId) {
          await apiFetch('/api/v1/admin/claim-rules/' + encodeURIComponent(rowId), { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
          setMessage('rule-msg', 'ok', 'Rule updated.');
        } else {
          await apiFetch('/api/v1/admin/claim-rules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
          setMessage('rule-msg', 'ok', 'Rule created.');
        }
        await renderClaimRules();
      } catch (err) {
        setMessage('rule-msg', 'err', err.message);
      }
    });
  }

  async function renderDiagnosisCriteria() {
    const result = await apiFetch('/api/v1/admin/diagnosis-criteria?limit=500');
    const items = result.items || [];

    const rows = items.map((r) => {
        return '<tr>'
        + '<td><code>' + escapeHtml(r.criteria_id) + '</code></td>'
        + '<td>' + escapeHtml(r.diagnosis_name) + '</td>'
        + '<td>' + statusChip(r.decision) + '</td>'
        + '<td>' + escapeHtml(r.severity) + '</td>'
        + '<td>' + statusChip(r.is_active ? 'active' : 'inactive') + '</td>'
        + '<td><button type="button" data-criteria-edit="' + r.id + '">Edit</button> '
        + '<button type="button" class="btn-soft" data-criteria-toggle="' + r.id + '" data-criteria-active="' + (r.is_active ? '1' : '0') + '">' + (r.is_active ? 'Disable' : 'Enable') + '</button> '
        + '<button type="button" data-criteria-delete="' + r.id + '">Delete</button></td></tr>';
    }).join('');

    contentPanel.innerHTML = '<h2>Diagnosis Criteria</h2><p class="muted">Manage diagnosis checklist criteria.</p>'
      + '<p id="criteria-msg"></p><div class="table-wrap" style="margin-bottom:12px;"><table><thead><tr><th>Code</th><th>Diagnosis</th><th>Decision</th><th>Severity</th><th>Status</th><th>Action</th></tr></thead><tbody>'
      + (rows || '<tr><td colspan="6">No criteria found.</td></tr>')
      + '</tbody></table></div>'
      + '<h3 id="criteria-form-title">Create Criteria</h3><form id="criteria-form"><input type="hidden" name="row_id" value="">'
      + '<div class="grid-2">'
      + '<div class="form-row"><label>Criteria ID</label><input name="criteria_id" required></div>'
      + '<div class="form-row"><label>Diagnosis Key</label><input name="diagnosis_key"></div>'
      + '<div class="form-row"><label>Diagnosis Name</label><input name="diagnosis_name" required></div>'
      + '<div class="form-row"><label>Decision</label><select name="decision"><option>APPROVE</option><option selected>QUERY</option><option>REJECT</option></select></div>'
      + '<div class="form-row"><label>Severity</label><select name="severity"><option selected>SOFT_QUERY</option><option>HARD_REJECT</option></select></div>'
      + '<div class="form-row"><label>Priority</label><input type="number" name="priority" value="999" min="1" max="9999"></div>'
      + '<div class="form-row"><label>Version</label><input name="version" value="1.0"></div>'
      + '</div>'
      + '<div class="form-row"><label>Aliases</label><textarea name="aliases"></textarea></div>'
      + '<div class="form-row"><label>Required Evidence</label><textarea name="required_evidence"></textarea></div>'
      + '<div class="form-row"><label>Remark Template</label><textarea name="remark_template"></textarea></div>'
      + '<div class="form-row"><label><input type="checkbox" name="is_active" checked style="width:auto"> Active</label></div>'
      + '<div class="link-row"><button type="submit">Save Criteria</button><button type="button" class="btn-soft" id="criteria-reset-btn">New Criteria</button></div></form>';

    const form = document.getElementById('criteria-form');
    const title = document.getElementById('criteria-form-title');

    function resetForm() {
      form.reset();
      form.row_id.value = '';
      form.priority.value = 999;
      form.version.value = '1.0';
      form.is_active.checked = true;
      title.textContent = 'Create Criteria';
    }

    function fillForm(item) {
      form.row_id.value = String(item.id || '');
      form.criteria_id.value = item.criteria_id || '';
      form.diagnosis_key.value = item.diagnosis_key || '';
      form.diagnosis_name.value = item.diagnosis_name || '';
      form.decision.value = item.decision || 'QUERY';
      form.severity.value = item.severity || 'SOFT_QUERY';
      form.priority.value = item.priority || 999;
      form.version.value = item.version || '1.0';
      form.aliases.value = listToTextarea(item.aliases || []);
      form.required_evidence.value = listToTextarea(item.required_evidence || []);
      form.remark_template.value = item.remark_template || '';
      form.is_active.checked = !!item.is_active;
      title.textContent = 'Edit Criteria';
    }

    document.getElementById('criteria-reset-btn').addEventListener('click', resetForm);

    contentPanel.querySelectorAll('button[data-criteria-edit]').forEach((btn) => {
      btn.addEventListener('click', function () {
        const id = Number(this.getAttribute('data-criteria-edit'));
        const found = items.find((x) => x.id === id);
        if (found) fillForm(found);
      });
    });

    contentPanel.querySelectorAll('button[data-criteria-toggle]').forEach((btn) => {
      btn.addEventListener('click', async function () {
        const id = Number(this.getAttribute('data-criteria-toggle'));
        const active = this.getAttribute('data-criteria-active') === '1';
        try {
          await apiFetch('/api/v1/admin/diagnosis-criteria/' + id + '/toggle?is_active=' + (!active), { method: 'PATCH' });
          setMessage('criteria-msg', 'ok', 'Criteria status updated.');
          await renderDiagnosisCriteria();
        } catch (err) {
          setMessage('criteria-msg', 'err', err.message);
        }
      });
    });

    contentPanel.querySelectorAll('button[data-criteria-delete]').forEach((btn) => {
      btn.addEventListener('click', async function () {
        const id = Number(this.getAttribute('data-criteria-delete'));
        if (!window.confirm('Delete this criteria?')) return;
        try {
          await apiFetch('/api/v1/admin/diagnosis-criteria/' + id, { method: 'DELETE' });
          setMessage('criteria-msg', 'ok', 'Criteria deleted.');
          await renderDiagnosisCriteria();
        } catch (err) {
          setMessage('criteria-msg', 'err', err.message);
        }
      });
    });

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      const rowId = String(form.row_id.value || '').trim();
      const payload = {
        criteria_id: String(form.criteria_id.value || '').trim(),
        diagnosis_key: String(form.diagnosis_key.value || '').trim() || null,
        diagnosis_name: String(form.diagnosis_name.value || '').trim(),
        aliases: parseListInput(form.aliases.value),
        required_evidence: parseListInput(form.required_evidence.value),
        decision: String(form.decision.value || 'QUERY').trim(),
        remark_template: String(form.remark_template.value || '').trim(),
        severity: String(form.severity.value || 'SOFT_QUERY').trim(),
        priority: Number(form.priority.value || 999),
        is_active: !!form.is_active.checked,
        version: String(form.version.value || '1.0').trim() || '1.0',
      };

      try {
        if (rowId) {
          await apiFetch('/api/v1/admin/diagnosis-criteria/' + encodeURIComponent(rowId), { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
          setMessage('criteria-msg', 'ok', 'Criteria updated.');
        } else {
          await apiFetch('/api/v1/admin/diagnosis-criteria', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
          setMessage('criteria-msg', 'ok', 'Criteria created.');
        }
        await renderDiagnosisCriteria();
      } catch (err) {
        setMessage('criteria-msg', 'err', err.message);
      }
    });
  }
  async function renderMedicines() {
    const state = renderMedicines._state || { page: 1, pageSize: 50, total: 0, search: '' };
    renderMedicines._state = state;

    contentPanel.innerHTML = '<h2>Medicines</h2><p class="muted">Medicine components and subclassification lookup.</p>'
      + '<form id="med-filter-form" class="claim-status-toolbar" style="margin-bottom:8px;">'
      + '<div class="claim-filter-group"><label for="med-search">Search</label><input id="med-search" name="search" placeholder="Medicine / component / subclass" value="' + escapeHtml(state.search || '') + '"></div>'
      + '<div class="claim-filter-action"><button type="submit" class="claim-apply-btn">Apply</button></div>'
      + '<div class="claim-filter-action"><button type="button" class="btn-soft" id="med-search-reset">Reset</button></div>'
      + '<div class="claim-filter-action"><button type="button" id="med-create-btn">New Medicine</button></div>'
      + '</form>'
      + '<p id="med-msg"></p>'
      + '<div class="table-wrap" style="margin-bottom:12px;"><table><thead><tr><th>Name</th><th>Components</th><th>Subclass</th><th>High-end Antibiotic</th><th>Source</th><th>Action</th></tr></thead><tbody id="med-tbody"><tr><td colspan="6">Loading...</td></tr></tbody></table></div>'
      + '<div class="claim-pagination" style="margin-bottom:12px;">'
      + '<div class="claim-pagination__left"><label for="med-page-size">Rows</label><select id="med-page-size"><option value="25">25</option><option value="50">50</option><option value="100">100</option><option value="200">200</option></select></div>'
      + '<div class="claim-pagination__info" id="med-page-info">Showing 0-0 of 0</div>'
      + '<div class="claim-pagination__actions"><button type="button" class="btn-soft" id="med-prev-page">Previous</button><button type="button" class="btn-soft" id="med-next-page">Next</button></div>'
      + '</div>';

    const filterForm = document.getElementById('med-filter-form');
    const tbody = document.getElementById('med-tbody');
    const pageInfoEl = document.getElementById('med-page-info');
    const pageSizeEl = document.getElementById('med-page-size');
    const prevBtn = document.getElementById('med-prev-page');
    const nextBtn = document.getElementById('med-next-page');
    const searchInput = document.getElementById('med-search');
    const searchResetBtn = document.getElementById('med-search-reset');
    const createBtn = document.getElementById('med-create-btn');

    let currentItems = [];
    pageSizeEl.value = String(state.pageSize || 50);

    function updatePaginationUi() {
      const total = Number(state.total || 0);
      const startRow = total === 0 ? 0 : ((state.page - 1) * state.pageSize + 1);
      const endRow = Math.min(state.page * state.pageSize, total);
      pageInfoEl.textContent = 'Showing ' + startRow + '-' + endRow + ' of ' + total;
      prevBtn.disabled = state.page <= 1;
      nextBtn.disabled = endRow >= total;
    }

    async function deleteMedicine(item) {
      if (!item || !item.id) return false;
      const medName = String(item.medicine_name || '').trim();
      const confirmed = window.confirm('Delete medicine "' + (medName || ('ID ' + String(item.id))) + '"?');
      if (!confirmed) return false;

      try {
        await apiFetch('/api/v1/admin/medicines/' + encodeURIComponent(String(item.id)), { method: 'DELETE' });
        setMessage('med-msg', 'ok', 'Medicine deleted.');
        if (currentItems.length === 1 && state.page > 1) state.page -= 1;
        await loadRows(false);
        return true;
      } catch (err) {
        setMessage('med-msg', 'err', err && err.message ? err.message : 'Failed to delete medicine.');
        return false;
      }
    }

    function openMedicineModal(item) {
      const isEdit = !!(item && item.id);
      const backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop open';
      backdrop.innerHTML = ''
        + '<div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="med-modal-title">'
        + '<div class="modal-header"><h3 id="med-modal-title">' + (isEdit ? 'Edit Medicine' : 'Create Medicine') + '</h3><button type="button" class="btn-soft" id="med-modal-close">Close</button></div>'
        + '<p id="med-modal-msg"></p>'
        + '<form id="med-modal-form">'
        + '<input type="hidden" name="row_id" value="' + escapeHtml(String((item && item.id) || '')) + '">'
        + '<div class="form-row"><label>Medicine Name</label><input name="medicine_name" required></div>'
        + '<div class="form-row"><label>Components</label><textarea name="components" required></textarea></div>'
        + '<div class="form-row"><label>Subclass</label><input name="subclassification" value="Supportive care"></div>'
        + '<div class="form-row"><label><input type="checkbox" name="is_high_end_antibiotic" style="width:auto"> High-end antibiotic</label></div>'
        + '<div class="link-row">'
        + '<button type="submit" id="med-modal-save-btn">Save Medicine</button>'
        + (isEdit ? '<button type="button" class="btn-soft" id="med-modal-delete-btn">Delete</button>' : '')
        + '<button type="button" class="btn-soft" id="med-modal-cancel-btn">Cancel</button>'
        + '</div>'
        + '</form>'
        + '</div>';
      document.body.appendChild(backdrop);

      const formEl = backdrop.querySelector('#med-modal-form');
      const closeBtn = backdrop.querySelector('#med-modal-close');
      const cancelBtn = backdrop.querySelector('#med-modal-cancel-btn');
      const saveBtn = backdrop.querySelector('#med-modal-save-btn');
      const deleteBtn = backdrop.querySelector('#med-modal-delete-btn');

      formEl.medicine_name.value = (item && item.medicine_name) ? item.medicine_name : '';
      formEl.components.value = (item && item.components) ? item.components : '';
      formEl.subclassification.value = (item && item.subclassification) ? item.subclassification : 'Supportive care';
      formEl.is_high_end_antibiotic.checked = !!(item && item.is_high_end_antibiotic);

      let closed = false;
      function closeModal() {
        if (closed) return;
        closed = true;
        document.removeEventListener('keydown', onEsc);
        backdrop.remove();
      }

      function onEsc(e) {
        if (e.key === 'Escape') closeModal();
      }

      document.addEventListener('keydown', onEsc);

      closeBtn.addEventListener('click', closeModal);
      cancelBtn.addEventListener('click', closeModal);
      backdrop.addEventListener('click', function (e) {
        if (e.target === backdrop) closeModal();
      });

      formEl.addEventListener('submit', async function (e) {
        e.preventDefault();
        const rowId = String(formEl.row_id.value || '').trim();
        const payload = {
          medicine_name: String(formEl.medicine_name.value || '').trim(),
          components: String(formEl.components.value || '').trim(),
          subclassification: String(formEl.subclassification.value || 'Supportive care').trim() || 'Supportive care',
          is_high_end_antibiotic: !!formEl.is_high_end_antibiotic.checked,
        };

        const controls = [saveBtn, cancelBtn, closeBtn, deleteBtn].filter(Boolean);
        controls.forEach((el) => { el.disabled = true; });
        setMessage('med-modal-msg', '', 'Saving...');

        try {
          if (rowId) {
            await apiFetch('/api/v1/admin/medicines/' + encodeURIComponent(rowId), {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            });
            setMessage('med-msg', 'ok', 'Medicine updated.');
          } else {
            await apiFetch('/api/v1/admin/medicines', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            });
            setMessage('med-msg', 'ok', 'Medicine created.');
          }

          closeModal();
          await loadRows(rowId ? false : true);
        } catch (err) {
          setMessage('med-modal-msg', 'err', err && err.message ? err.message : 'Failed to save medicine.');
          controls.forEach((el) => { el.disabled = false; });
        }
      });

      if (deleteBtn && item) {
        deleteBtn.addEventListener('click', async function () {
          const deleted = await deleteMedicine(item);
          if (deleted) closeModal();
        });
      }
    }

    function bindActionButtons() {
      contentPanel.querySelectorAll('button[data-med-edit]').forEach((btn) => {
        btn.addEventListener('click', function () {
          const id = Number(this.getAttribute('data-med-edit'));
          const found = currentItems.find((x) => Number(x.id) === id);
          if (found) openMedicineModal(found);
        });
      });

      contentPanel.querySelectorAll('button[data-med-delete]').forEach((btn) => {
        btn.addEventListener('click', async function () {
          const id = Number(this.getAttribute('data-med-delete'));
          const found = currentItems.find((x) => Number(x.id) === id);
          if (found) await deleteMedicine(found);
        });
      });
    }

    async function loadRows(resetPage) {
      if (resetPage) state.page = 1;
      state.search = String(searchInput.value || '').trim();
      tbody.innerHTML = '<tr><td colspan="6">Loading...</td></tr>';
      setMessage('med-msg', '', '');

      const params = new URLSearchParams();
      params.set('limit', String(state.pageSize));
      params.set('offset', String((state.page - 1) * state.pageSize));
      if (state.search) params.set('search', state.search);

      try {
        const result = await apiFetch('/api/v1/admin/medicines?' + params.toString());
        state.total = Number(result && result.total ? result.total : 0);
        currentItems = (result && Array.isArray(result.items)) ? result.items : [];

        const maxPage = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
        if (state.page > maxPage) {
          state.page = maxPage;
          return loadRows(false);
        }

        if (!currentItems.length) {
          tbody.innerHTML = '<tr><td colspan="6">No medicine rows found.</td></tr>';
          updatePaginationUi();
          return;
        }

        const rows = currentItems.map((m) => {
          return '<tr>'
            + '<td>' + escapeHtml(m.medicine_name || '') + '</td>'
            + '<td>' + escapeHtml(m.components || '') + '</td>'
            + '<td>' + escapeHtml(m.subclassification || '') + '</td>'
            + '<td>' + statusChip(m.is_high_end_antibiotic ? 'yes' : 'no') + '</td>'
            + '<td>' + escapeHtml(m.source || '') + '</td>'
            + '<td><div class="doctor-case-actions">'
            + '<button type="button" class="btn-soft" data-med-edit="' + escapeHtml(String(m.id || '')) + '">Edit</button>'
            + '<button type="button" class="btn-soft" data-med-delete="' + escapeHtml(String(m.id || '')) + '">Delete</button>'
            + '</div></td>'
            + '</tr>';
        }).join('');

        tbody.innerHTML = rows;
        bindActionButtons();
        updatePaginationUi();
      } catch (err) {
        state.total = 0;
        currentItems = [];
        tbody.innerHTML = '<tr><td colspan="6">Failed to load medicines.</td></tr>';
        updatePaginationUi();
        setMessage('med-msg', 'err', err && err.message ? err.message : 'Failed to load medicines.');
      }
    }

    createBtn.addEventListener('click', function () {
      openMedicineModal(null);
    });

    filterForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      await loadRows(true);
    });

    searchResetBtn.addEventListener('click', async function () {
      searchInput.value = '';
      await loadRows(true);
    });

    pageSizeEl.addEventListener('change', async function () {
      const selected = Number(pageSizeEl.value || 50);
      state.pageSize = selected > 0 ? selected : 50;
      await loadRows(true);
    });

    prevBtn.addEventListener('click', async function () {
      if (state.page <= 1) return;
      state.page -= 1;
      await loadRows(false);
    });

    nextBtn.addEventListener('click', async function () {
      const maxPage = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
      if (state.page >= maxPage) return;
      state.page += 1;
      await loadRows(false);
    });

    await loadRows(false);
  }

async function renderLegacyMigration() {
    contentPanel.innerHTML = '<h2>Legacy Migration Pipeline</h2><p class="muted">One-click migration from TeamRightWorks to VerifAI for claims/data only.</p>'
      + '<p id="legacy-sync-msg"></p>'
      + '<form id="legacy-sync-form" class="grid-2">'
      + '<div class="form-row"><label><input type="checkbox" id="legacy-sync-claims" checked style="width:auto"> Sync Claims/Data</label></div>'
      + '<div class="form-row"><label><input type="checkbox" id="legacy-sync-users" disabled style="width:auto"> User Sync Disabled</label></div>'
      + '<div class="form-row"><label><input type="checkbox" id="legacy-sync-raw" checked style="width:auto"> Raw Files Only (skip legacy report/extraction sync)</label></div>'
      + '<div class="form-row"><label>Status Filter</label><select id="legacy-sync-status"><option value="completed">Completed</option><option value="all">All</option><option value="in_review">In Review</option><option value="needs_qc">Needs QC</option><option value="withdrawn">Withdrawn</option></select></div>'
      + '<div class="form-row"><label>Batch Size</label><input id="legacy-sync-batch" type="number" value="200" min="1" max="500"></div>'
      + '<div class="form-row"><label>Max Batches</label><input id="legacy-sync-max" type="number" value="200" min="1" max="1000"></div>'
      + '<div class="form-row" style="align-self:end"><button type="submit" id="legacy-sync-start-btn">Start Migration</button></div>'
      + '</form>'
      + '<h3 style="margin-top:16px">Live Status</h3>'
      + '<div id="legacy-sync-status-box" class="table-wrap"><table><tbody><tr><td>No migration has started yet.</td></tr></tbody></table></div>';

    const form = document.getElementById('legacy-sync-form');
    const startBtn = document.getElementById('legacy-sync-start-btn');
    const statusBox = document.getElementById('legacy-sync-status-box');
    let pollTimer = null;

    function stopPolling() {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function renderJob(job) {
      if (!job) {
        statusBox.innerHTML = '<table><tbody><tr><td>No migration has started yet.</td></tr></tbody></table>';
        return;
      }

      const claims = (job.progress && job.progress.claims) || {};
      const users = (job.progress && job.progress.users) || {};
      const rawCleanup = (job.progress && job.progress.raw_cleanup) || {};
      const sampleErrors = (job.progress && Array.isArray(job.progress.sample_errors)) ? job.progress.sample_errors : [];
      const errorRows = sampleErrors.slice(0, 10).map((e) => {
        return '<tr><td>' + escapeHtml(e.claim_id || '-') + '</td><td>' + escapeHtml(e.error || '-') + '</td><td>' + escapeHtml(String(e.http_code || '-')) + '</td></tr>';
      }).join('');

      statusBox.innerHTML = '<table><tbody>'
        + '<tr><th style="width:260px">Job ID</th><td><code>' + escapeHtml(job.job_id || '-') + '</code></td></tr>'
        + '<tr><th>Status</th><td>' + statusChip(job.status || '-') + '</td></tr>'
        + '<tr><th>Phase</th><td>' + escapeHtml((job.progress && job.progress.phase) || '-') + '</td></tr>'
        + '<tr><th>Message</th><td>' + escapeHtml(job.message || '-') + '</td></tr>'
        + '<tr><th>Started By</th><td>' + escapeHtml(job.started_by || '-') + '</td></tr>'
        + '<tr><th>Started At</th><td>' + escapeHtml(formatDateTime(job.started_at || '')) + '</td></tr>'
        + '<tr><th>Finished At</th><td>' + escapeHtml(formatDateTime(job.finished_at || '')) + '</td></tr>'
        + '<tr><th>Claims Synced</th><td>Selected: ' + escapeHtml(String(claims.selected || 0)) + ', Success: ' + escapeHtml(String(claims.success || 0)) + ', Failed: ' + escapeHtml(String(claims.failed || 0)) + ', Batches: ' + escapeHtml(String(claims.batches || 0)) + '</td></tr>'
        + '<tr><th>Users Synced</th><td>Candidates: ' + escapeHtml(String(users.candidates || 0)) + ', Created: ' + escapeHtml(String(users.created || 0)) + ', Updated: ' + escapeHtml(String(users.updated || 0)) + ', Skipped: ' + escapeHtml(String(users.skipped || 0)) + ', Failed: ' + escapeHtml(String(users.failed || 0)) + '</td></tr>'
        + '<tr><th>Raw Cleanup</th><td>Enabled: ' + escapeHtml(String(!!rawCleanup.enabled)) + ', Claims: ' + escapeHtml(String(rawCleanup.claims_touched || 0)) + ', Reports Deleted: ' + escapeHtml(String(rawCleanup.report_versions_deleted || 0)) + ', Extractions Deleted: ' + escapeHtml(String(rawCleanup.document_extractions_deleted || 0)) + ', Docs Reset: ' + escapeHtml(String(rawCleanup.documents_reset || 0)) + '</td></tr>'
        + (job.error ? '<tr><th>Error</th><td>' + escapeHtml(job.error) + '</td></tr>' : '')
        + '</tbody></table>'
        + (errorRows ? '<h4 style="margin:10px 0 6px">Recent Sync Errors</h4><table><thead><tr><th>Claim ID</th><th>Error</th><th>HTTP</th></tr></thead><tbody>' + errorRows + '</tbody></table>' : '');
    }

    async function refreshStatus(jobId, autoContinue) {
      try {
        const qs = jobId ? ('?job_id=' + encodeURIComponent(jobId)) : '';
        const statusPayload = await apiFetch('/api/v1/admin/legacy-migration/status' + qs);
        const job = statusPayload && statusPayload.job ? statusPayload.job : null;
        renderJob(job);
        if (job && (job.status === 'queued' || job.status === 'running') && autoContinue) {
          pollTimer = setTimeout(function () {
            refreshStatus(String(job.job_id || jobId || ''), true);
          }, 2000);
        } else {
          stopPolling();
        }
      } catch (err) {
        stopPolling();
        setMessage('legacy-sync-msg', 'err', err && err.message ? err.message : 'Failed to fetch migration status.');
      }
    }

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      stopPolling();
      setMessage('legacy-sync-msg', '', '');

      const includeClaims = !!document.getElementById('legacy-sync-claims').checked;
      const rawFilesOnly = !!document.getElementById('legacy-sync-raw').checked;
      const statusFilter = String(document.getElementById('legacy-sync-status').value || 'completed');
      const batchSize = Number(document.getElementById('legacy-sync-batch').value || 200);
      const maxBatches = Number(document.getElementById('legacy-sync-max').value || 200);

      if (!includeClaims) {
        setMessage('legacy-sync-msg', 'err', 'Select claims/data to start migration.');
        return;
      }

      const oldText = startBtn.textContent;
      startBtn.disabled = true;
      startBtn.textContent = 'Starting...';

      try {
        const started = await apiFetch('/api/v1/admin/legacy-migration/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            include_claims: includeClaims,
            include_users: false,
            raw_files_only: rawFilesOnly,
            status_filter: statusFilter,
            batch_size: Math.max(1, Math.min(500, Math.floor(batchSize || 200))),
            max_batches: Math.max(1, Math.min(1000, Math.floor(maxBatches || 200))),
          }),
        });

        setMessage('legacy-sync-msg', 'ok', 'Migration started. Job ID: ' + String(started.job_id || '-'));
        await refreshStatus(String(started.job_id || ''), true);
      } catch (err) {
        setMessage('legacy-sync-msg', 'err', err && err.message ? err.message : 'Failed to start migration.');
      } finally {
        startBtn.disabled = false;
        startBtn.textContent = oldText || 'Start Migration';
      }
    });

    await refreshStatus('', false);
  }
  async function renderAIPrompt() {
    contentPanel.innerHTML = '<h2>AI Prompt</h2><p class="muted">Admin prompt configuration page from legacy QC is reserved here.</p>'
      + '<p class="msg ok">Navbar is aligned with QC admin pages. AI prompt backend module can be wired next.</p>';
  }

  async function renderWithdrawnClaims() {
    const doctors = await fetchDoctors();
    const doctorFilterOptions = '<option value="">All Doctors</option>'
      + doctors.map((d) => '<option value="' + escapeHtml(d) + '">' + escapeHtml(d) + '</option>').join('');

    const state = {
      page: 1,
      pageSize: 20,
      total: 0,
    };

    contentPanel.innerHTML = '<section class="claim-status-panel">'
      + '<h2 class="claim-status-title">Withdrawn Claims</h2>'
      + '<p class="muted">Search and review withdrawn claims.</p>'
      + '<form id="withdrawn-claims-filter-form" class="claim-status-toolbar">'
      + '<div class="claim-filter-group"><label for="withdrawn-claims-search">Search Claim</label><input id="withdrawn-claims-search" name="search_claim" placeholder="Claim ID"></div>'
      + '<div class="claim-filter-group"><label for="withdrawn-claims-allotment-date">Allotment Date</label><input id="withdrawn-claims-allotment-date" type="date" name="allotment_date"></div>'
      + '<div class="claim-filter-group"><label for="withdrawn-claims-doctor-filter">Doctor</label><select id="withdrawn-claims-doctor-filter" name="doctor_filter">' + doctorFilterOptions + '</select></div>'
      + '<div class="claim-filter-action"><button type="submit" class="claim-apply-btn">Apply</button></div>'
      + '<div class="claim-filter-action"><button type="button" class="btn-soft" id="withdrawn-claims-reset">Reset</button></div>'
      + '</form>'
      + '<p id="withdrawn-claims-msg"></p>'
      + '<div class="table-wrap claim-status-table-wrap"><table><thead><tr><th>CLAIM ID</th><th>DOCTOR</th><th>ALLOTMENT DATE</th><th>STATUS</th><th>UPDATED</th></tr></thead><tbody id="withdrawn-claims-tbody"><tr><td colspan="5">Loading...</td></tr></tbody></table></div>'
      + '<div class="claim-pagination">'
      + '<div class="claim-pagination__left"><label for="withdrawn-claims-page-size">Rows</label><select id="withdrawn-claims-page-size"><option value="10">10</option><option value="20" selected>20</option><option value="50">50</option></select></div>'
      + '<div class="claim-pagination__info" id="withdrawn-claims-page-info">Showing 0-0 of 0</div>'
      + '<div class="claim-pagination__actions"><button type="button" class="btn-soft" id="withdrawn-claims-prev-page">Previous</button><button type="button" class="btn-soft" id="withdrawn-claims-next-page">Next</button></div>'
      + '</div>'
      + '</section>';

    const form = document.getElementById('withdrawn-claims-filter-form');
    const tbody = document.getElementById('withdrawn-claims-tbody');
    const pageSizeEl = document.getElementById('withdrawn-claims-page-size');
    const prevBtn = document.getElementById('withdrawn-claims-prev-page');
    const nextBtn = document.getElementById('withdrawn-claims-next-page');
    const pageInfoEl = document.getElementById('withdrawn-claims-page-info');
    const resetBtn = document.getElementById('withdrawn-claims-reset');

    function formatAssignedDoctor(value) {
      return String(value || '').split(',').map((s) => s.trim()).filter(Boolean)[0] || '-';
    }

    function updatePaginationUi() {
      const total = Number(state.total || 0);
      const startRow = total === 0 ? 0 : ((state.page - 1) * state.pageSize + 1);
      const endRow = Math.min(state.page * state.pageSize, total);
      pageInfoEl.textContent = 'Showing ' + startRow + '-' + endRow + ' of ' + total;
      prevBtn.disabled = state.page <= 1;
      nextBtn.disabled = endRow >= total;
    }

    async function loadRows(resetPage) {
      if (resetPage) state.page = 1;
      setMessage('withdrawn-claims-msg', '', '');
      tbody.innerHTML = '<tr><td colspan="5">Loading...</td></tr>';

      const searchClaim = String(document.getElementById('withdrawn-claims-search').value || '').trim();
      const allotmentDate = String(document.getElementById('withdrawn-claims-allotment-date').value || '').trim();
      const doctorFilter = String(document.getElementById('withdrawn-claims-doctor-filter').value || '').trim();

      const params = new URLSearchParams();
      params.set('status_filter', 'withdrawn');
      if (searchClaim) params.set('search_claim', searchClaim);
      if (allotmentDate) params.set('allotment_date', allotmentDate);
      if (doctorFilter) params.set('doctor_filter', doctorFilter);
      params.set('limit', String(state.pageSize));
      params.set('offset', String((state.page - 1) * state.pageSize));

      try {
        const result = await apiFetch('/api/v1/user-tools/claim-document-status?' + params.toString());
        state.total = Number(result && result.total ? result.total : 0);

        const items = (result && Array.isArray(result.items)) ? result.items : [];
        if (!items.length) {
          tbody.innerHTML = '<tr><td colspan="5">No withdrawn claims found.</td></tr>';
          updatePaginationUi();
          return;
        }

        const rows = items.map(function (item) {
        return '<tr>'
            + '<td><code>' + escapeHtml(item.external_claim_id || '-') + '</code></td>'
            + '<td>' + escapeHtml(formatAssignedDoctor(item.assigned_doctor_id || '')) + '</td>'
            + '<td>' + escapeHtml(formatDateTime(item.allotment_date || '')) + '</td>'
            + '<td>' + statusChip(item.status_display || item.status || 'withdrawn') + '</td>'
            + '<td>' + escapeHtml(formatDateTime(item.updated_at || '')) + '</td>'
            + '</tr>';
        }).join('');

        tbody.innerHTML = rows;
        updatePaginationUi();
      } catch (err) {
        state.total = 0;
        tbody.innerHTML = '<tr><td colspan="5">Failed to load withdrawn claims.</td></tr>';
        updatePaginationUi();
        setMessage('withdrawn-claims-msg', 'err', err && err.message ? err.message : 'Failed to load withdrawn claims.');
      }
    }

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      await loadRows(true);
    });

    if (resetBtn) {
      resetBtn.addEventListener('click', async function () {
        document.getElementById('withdrawn-claims-search').value = '';
        document.getElementById('withdrawn-claims-allotment-date').value = '';
        document.getElementById('withdrawn-claims-doctor-filter').value = '';
        await loadRows(true);
      });
    }

    pageSizeEl.addEventListener('change', async function () {
      const selected = Number(pageSizeEl.value || 20);
      state.pageSize = selected > 0 ? selected : 20;
      await loadRows(true);
    });

    prevBtn.addEventListener('click', async function () {
      if (state.page <= 1) return;
      state.page -= 1;
      await loadRows(false);
    });

    nextBtn.addEventListener('click', async function () {
      const maxPage = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
      if (state.page >= maxPage) return;
      state.page += 1;
      await loadRows(false);
    });

    await loadRows(true);
  }

  async function renderPage(activeRole, page) {
    detachCompletedReportsMessageListener();
    if (page === 'dashboard') {
      if (activeRole === 'super_admin') return renderSuperAdminDashboard();
      if (activeRole === 'doctor') return renderDoctorDashboard();
      if (activeRole === 'auditor') return renderCompletedReports('all');
      return renderUserDashboard();
    }
    if (page === 'assigned-cases') return renderDoctorAssignedCases();
    if (page === 'case-detail') return renderDoctorCaseDetail();

    if (page === 'create-user') return renderCreateUser();
    if (page === 'change-password') return renderChangePassword();
    if (page === 'reset-user-password') return renderResetUserPassword();
    if (page === 'upload-excel') return renderUploadExcel();
    if (page === 'assign-cases') return renderAssignCases();
    if (page === 'upload-document') return renderUploadDocument();

    if (page === 'withdrawn-claims') return renderWithdrawnClaims();

    if (page === 'completed-not-uploaded') return renderCompletedReports('pending');
    if (page === 'completed-uploaded') return renderCompletedReports('uploaded');
    if (page === 'audit-claims') return renderCompletedReports('all');
    if (page === 'export-data') return renderExportData();
    if (page === 'allotment-date-wise') return renderAllotmentDateWise();
    if (page === 'bank-details') return renderUserBankDetails();
    if (page === 'payment-sheet') return renderPaymentSheet();

    if (page === 'claim-rules') return renderClaimRules();
    if (page === 'diagnosis-criteria') return renderDiagnosisCriteria();
    if (page === 'rule-suggestions') return renderRuleSuggestions();
    if (page === 'medicines') return renderMedicines();
    if (page === 'storage-maintenance') return renderStorageMaintenance();
    if (page === 'ai-prompt') return renderAIPrompt();
    if (page === 'legacy-sync') return renderLegacyMigration();

    contentPanel.innerHTML = '<h2>Not Found</h2><p class="muted">This QC module path is not mapped yet.</p>';
  }

  async function boot() {
    try {
      const me = await apiFetch('/api/v1/auth/me');
      const route = parseRoute();
      const actualRole = me.role;

      let activeRole = route.routeRole;
      if (!activeRole || !NAV[activeRole]) {
        activeRole = actualRole === 'super_admin' ? 'super_admin' : actualRole;
      }

      if (actualRole !== 'super_admin' && activeRole !== actualRole) {
        window.location.href = '/qc/' + actualRole + '/dashboard';
        return;
      }

      const allowedPages = (NAV[activeRole] || []).map((n) => n.page);
      const isRoleCaseDetailPage = route.page === 'case-detail' && (activeRole === 'doctor' || activeRole === 'user' || activeRole === 'super_admin' || activeRole === 'auditor');
      const page = (route.page && (allowedPages.includes(route.page) || isRoleCaseDetailPage)) ? route.page : 'dashboard';
      const navPage = page === 'case-detail'
        ? (activeRole === 'doctor' ? 'assigned-cases' : (activeRole === 'user' ? 'upload-document' : (activeRole === 'auditor' ? 'audit-claims' : 'dashboard')))
        : page;

      attachClaimSyncListeners();
      renderHeader(me, activeRole, page);
      renderNav(activeRole, navPage);
      await renderPage(activeRole, page);
    } catch (err) {
      renderError(err && err.message ? err.message : 'Failed to load workspace');
    }
  }

  boot();
})();








































































































































































































































































































