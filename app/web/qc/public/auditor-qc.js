(function () {
  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function setMessage(type, text) {
    const el = document.getElementById('qc-msg');
    if (!el) return;
    el.className = type ? ('msg ' + type) : '';
    el.textContent = text || '';
  }

  function formatDateTime(value) {
    if (!value) return '-';
    const dt = new Date(value);
    return Number.isNaN(dt.getTime()) ? String(value) : dt.toLocaleString();
  }

  function parseRouteParams() {
    const p = new URLSearchParams(window.location.search);
    return {
      claimUuid: String(p.get('claim_uuid') || '').trim(),
      claimId: String(p.get('claim_id') || '').trim(),
    };
  }

  function getToken() {
    return localStorage.getItem('qc_access_token') || '';
  }

  function clearAuthAndRedirect() {
    localStorage.removeItem('qc_access_token');
    localStorage.removeItem('qc_user');
    localStorage.removeItem('qc_acting_role');
    window.location.href = '/qc/login';
  }

  async function apiFetch(path, options) {
    const token = getToken();
    if (!token) {
      clearAuthAndRedirect();
      throw new Error('Not authenticated');
    }

    const opts = options || {};
    const headers = new Headers(opts.headers || {});
    headers.set('Authorization', 'Bearer ' + token);

    const resp = await fetch(path, { ...opts, headers });
    const raw = await resp.text();
    let body = null;
    try {
      body = raw ? JSON.parse(raw) : null;
    } catch (_err) {
      body = { detail: raw };
    }

    if (!resp.ok) {
      if (resp.status === 401) {
        clearAuthAndRedirect();
      }
      const detail = body && (body.detail || body.message) ? (body.detail || body.message) : ('HTTP ' + resp.status);
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }

    return body || {};
  }

  const params = parseRouteParams();
  if (!params.claimUuid) {
    setMessage('err', 'Missing claim_uuid.');
    throw new Error('Missing claim_uuid');
  }

  const claimMetaEl = document.getElementById('qc-claim-meta');
  const reportSourceEl = document.getElementById('qc-report-source');
  const docCountEl = document.getElementById('qc-doc-count');
  const reportEditorEl = document.getElementById('report-editor');
  const docSelectEl = document.getElementById('doc-select');
  const docPreviewBtn = document.getElementById('doc-preview-btn');
  const docOpenBtn = document.getElementById('doc-open-btn');
  const docFullscreenBtn = document.getElementById('doc-fullscreen-btn');
  const docSelectedNameEl = document.getElementById('doc-selected-name');
  const docPreviewContainerEl = document.getElementById('doc-preview-container');
  const docPreviewEl = document.getElementById('doc-preview');
  const reloadBtn = document.getElementById('qc-reload-btn');
  const sendBackBtn = document.getElementById('qc-send-back-btn');
  const runRulesBtn = document.getElementById('qc-run-rules-btn');
  const generateConclusionBtn = document.getElementById('qc-generate-conclusion-btn');
  const applyConclusionBtn = document.getElementById('qc-apply-conclusion-btn');
  const saveBtn = document.getElementById('qc-save-btn');
  const saveMarkBtn = document.getElementById('qc-save-mark-btn');
  const qcConfirmOverlayEl = document.getElementById('qc-confirm-overlay');
  const qcConfirmCancelBtn = document.getElementById('qc-confirm-cancel-btn');
  const qcConfirmProceedBtn = document.getElementById('qc-confirm-proceed-btn');
  const qcConfirmRatingEl = document.getElementById('qc-confirm-rating');
  const qcConfirmMsgEl = document.getElementById('qc-confirm-msg');
  const conclusionOnlyEl = document.getElementById('qc-conclusion-only');
  const layoutEl = document.getElementById('qc-layout');
  const splitterEl = document.getElementById('qc-splitter');

  let loadedSource = 'doctor';
  let currentDocs = [];
  let selectedDocId = '';
  let latestChecklistResult = null;
  const DOC_LOOKBACK_DAYS = 10;
  const SPLIT_STORAGE_KEY = 'qc_auditor_split_left_pct';
  const SPLIT_MIN_PERCENT = 35;
  const SPLIT_MAX_PERCENT = 70;

  function clampSplitPercent(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return 52;
    return Math.max(SPLIT_MIN_PERCENT, Math.min(SPLIT_MAX_PERCENT, num));
  }

  function applySplitPercent(value) {
    if (!layoutEl) return;
    const pct = clampSplitPercent(value);
    layoutEl.style.setProperty('--left-pane-width', String(pct) + '%');
    if (splitterEl) {
      splitterEl.setAttribute('aria-valuemin', String(SPLIT_MIN_PERCENT));
      splitterEl.setAttribute('aria-valuemax', String(SPLIT_MAX_PERCENT));
      splitterEl.setAttribute('aria-valuenow', String(Math.round(pct)));
    }
  }

  function getCurrentSplitPercent() {
    if (!layoutEl) return 52;
    const raw = String(layoutEl.style.getPropertyValue('--left-pane-width') || '').trim();
    if (raw && /%$/.test(raw)) {
      const pct = parseFloat(raw.replace('%', '').trim());
      if (Number.isFinite(pct)) return pct;
    }
    const computed = window.getComputedStyle(layoutEl).getPropertyValue('--left-pane-width');
    const pct = parseFloat(String(computed || '').replace('%', '').trim());
    return Number.isFinite(pct) ? pct : 52;
  }

  function saveSplitPercent(value) {
    try {
      localStorage.setItem(SPLIT_STORAGE_KEY, String(clampSplitPercent(value)));
    } catch (_err) {
    }
  }

  function loadSavedSplitPercent() {
    try {
      const raw = localStorage.getItem(SPLIT_STORAGE_KEY);
      const pct = parseFloat(String(raw || '').trim());
      if (Number.isFinite(pct)) {
        applySplitPercent(pct);
      }
    } catch (_err) {
    }
  }

  function isMobileStackedLayout() {
    return !!window.matchMedia && window.matchMedia('(max-width: 1100px)').matches;
  }

  function initPanelSplitter() {
    if (!layoutEl || !splitterEl) return;

    loadSavedSplitPercent();

    let dragging = false;

    function stopDragging(saveValue) {
      if (!dragging) return;
      dragging = false;
      splitterEl.classList.remove('dragging');
      document.body.classList.remove('qc-resizing');
      document.removeEventListener('pointermove', onPointerMove);
      document.removeEventListener('pointerup', onPointerUp);
      document.removeEventListener('pointercancel', onPointerUp);
      if (saveValue) saveSplitPercent(getCurrentSplitPercent());
    }

    function onPointerMove(ev) {
      if (!dragging || !layoutEl) return;
      const rect = layoutEl.getBoundingClientRect();
      if (!rect || rect.width <= 0) return;
      const pct = ((ev.clientX - rect.left) / rect.width) * 100;
      applySplitPercent(pct);
    }

    function onPointerUp() {
      stopDragging(true);
    }

    splitterEl.addEventListener('pointerdown', function (ev) {
      if (isMobileStackedLayout()) return;
      ev.preventDefault();
      dragging = true;
      splitterEl.classList.add('dragging');
      document.body.classList.add('qc-resizing');
      document.addEventListener('pointermove', onPointerMove);
      document.addEventListener('pointerup', onPointerUp);
      document.addEventListener('pointercancel', onPointerUp);
    });

    splitterEl.addEventListener('keydown', function (ev) {
      if (isMobileStackedLayout()) return;
      let target = getCurrentSplitPercent();
      if (ev.key === 'ArrowLeft') target -= 2;
      else if (ev.key === 'ArrowRight') target += 2;
      else if (ev.key === 'Home') target = SPLIT_MIN_PERCENT;
      else if (ev.key === 'End') target = SPLIT_MAX_PERCENT;
      else return;
      ev.preventDefault();
      applySplitPercent(target);
      saveSplitPercent(target);
    });

    window.addEventListener('resize', function () {
      if (!isMobileStackedLayout()) {
        applySplitPercent(getCurrentSplitPercent());
      }
    });

    applySplitPercent(getCurrentSplitPercent());
  }

  function filterDocumentsByRecentDays(items, lookbackDays) {
    const days = Number(lookbackDays || 0);
    if (!Number.isFinite(days) || days <= 0) return Array.isArray(items) ? items : [];
    const cutoff = Date.now() - (days * 24 * 60 * 60 * 1000);
    return (Array.isArray(items) ? items : []).filter(function (doc) {
      const raw = String((doc && doc.uploaded_at) || '').trim();
      const ts = Date.parse(raw);
      return Number.isFinite(ts) && ts >= cutoff;
    });
  }
  function sanitizeText(value) {
    const text = String(value == null ? '' : value).trim();
    if (!text) return '-';
    if (/^(na|n\/a|not available|none|nil|null|-|\.)$/i.test(text)) return '-';
    return text;
  }

  function escapeMultilineHtml(value) {
    return escapeHtml(String(value == null ? '' : value)).replace(/\r?\n/g, '<br>');
  }

  function normalizeHealthClaimReportTitle(html) {
    const value = String(html || '');
    if (!value) return '';
    return value.replace(/HEALTH CLAIM INVESTIGATION REPORT/g, 'HEALTH CLAIM ASSESSMENT SHEET');
  }

  function buildStructuredFallbackReportHtml(data) {
    const payload = data && typeof data === 'object' ? data : {};
    const generatedAt = new Date().toLocaleString();

    function row(label, value) {
      return '<tr><th style="width:32%;text-align:left;background:#e9dad4;border:1px solid #2f2f2f;padding:10px;font-size:18px;font-weight:700;">' + escapeHtml(label) + '</th>'
        + '<td style="border:1px solid #2f2f2f;padding:10px;font-size:18px;">' + escapeMultilineHtml(sanitizeText(value)) + '</td></tr>';
    }

    return '<div style="max-width:1100px;margin:0 auto;background:#fff;color:#111;padding:16px;">'
      + '<h1 style="margin:0 0 10px 0;text-align:center;font-size:42px;line-height:1.2;font-weight:800;">HEALTH CLAIM ASSESSMENT SHEET</h1>'
      + '<div style="text-align:right;color:#333;margin:0 0 12px 0;font-size:14px;">Generated: ' + escapeHtml(generatedAt) + ' | Doctor: -</div>'
      + '<table style="width:100%;border-collapse:collapse;table-layout:fixed;">'
      + row('COMPANY NAME', payload.company_name)
      + row('CLAIM NO.', payload.external_claim_id || params.claimId)
      + row('CLAIM TYPE', payload.claim_type)
      + row('INSURED', payload.insured_name)
      + row('HOSPITAL', payload.hospital_name)
      + row('TREATING DOCTOR', payload.treating_doctor)
      + row('TREATING DOCTOR REG. NO.', payload.treating_doctor_registration_number)
      + row('ADMISSION', payload.doa)
      + row('DISCHARGE', payload.dod)
      + row('DIAGNOSIS', payload.diagnosis)
      + row('CHIEF COMPLAINTS AT ADMISSION', payload.complaints)
      + row('MAJOR DIAGNOSTIC FINDING (ADMISSION / DURING STAY)', payload.findings)
      + row('ALL INVESTIGATION REPORTS', payload.investigation_finding_in_details)
      + row('MEDICINES USED', payload.medicine_used)
      + row('DERANGED INVESTIGATION', payload.deranged_investigation)
      + row('CLAIMED AMOUNT', payload.claim_amount)
      + row('CONCLUSION', payload.conclusion)
      + row('RECOMMENDATION', payload.recommendation)
      + '</table>'
      + '</div>';
  }

  async function loadStructuredFallbackReport() {
    try {
      const data = await apiFetch('/api/v1/claims/' + encodeURIComponent(params.claimUuid) + '/structured-data?auto_generate=true&use_llm=true');
      if (data && typeof data === 'object') return data;
    } catch (_err) {
    }

    try {
      const data = await apiFetch('/api/v1/claims/' + encodeURIComponent(params.claimUuid) + '/structured-data?auto_generate=true&use_llm=false');
      if (data && typeof data === 'object') return data;
    } catch (_err2) {
    }

    try {
      const claim = await apiFetch('/api/v1/claims/' + encodeURIComponent(params.claimUuid));
      if (claim && typeof claim === 'object') {
        return {
          external_claim_id: claim.external_claim_id || params.claimId,
          insured_name: claim.patient_name || '',
          company_name: 'Medi Assist Insurance TPA Pvt. Ltd.',
          claim_type: '',
          hospital_name: '',
          treating_doctor: '',
          treating_doctor_registration_number: '',
          doa: '',
          dod: '',
          diagnosis: '',
          complaints: '',
          findings: '',
          investigation_finding_in_details: '',
          medicine_used: '',
          deranged_investigation: '',
          claim_amount: '',
          conclusion: '',
          recommendation: '',
        };
      }
    } catch (_err3) {
    }

    return null;
  }

  if (claimMetaEl) {
    claimMetaEl.textContent = 'Claim: ' + (params.claimId || '-') + ' | UUID: ' + params.claimUuid;
  }

  function getDocById(docId) {
    const target = String(docId || '').trim();
    return currentDocs.find(function (doc) {
      return String((doc && doc.id) || '').trim() === target;
    }) || null;
  }

  function updateSelectedDocLabel(doc) {
    if (!docSelectedNameEl) return;
    if (!doc) {
      docSelectedNameEl.textContent = 'No document selected.';
      docSelectedNameEl.title = '';
      return;
    }
    const name = String(doc.file_name || '-');
    const stamp = formatDateTime(doc.uploaded_at || '');
    const label = name + ' | ' + stamp;
    docSelectedNameEl.textContent = label;
    docSelectedNameEl.title = label;
  }

  function buildPreviewUrl(url) {
    const raw = String(url || '').trim();
    if (!raw) return '';
    const marker = 'toolbar=1&view=FitH&zoom=page-fit';
    const hashIndex = raw.indexOf('#');
    if (hashIndex >= 0) {
      const hash = raw.slice(hashIndex + 1);
      if (/(^|&)(toolbar|view|zoom)=/i.test(hash)) return raw;
      return raw + (raw.endsWith('#') ? '' : '&') + marker;
    }
    return raw + '#' + marker;
  }

  function isFullscreenActive() {
    const fsEl = document.fullscreenElement || document.webkitFullscreenElement || null;
    return !!(fsEl && docPreviewContainerEl && fsEl === docPreviewContainerEl);
  }

  function updateFullscreenButtonLabel() {
    if (!docFullscreenBtn) return;
    docFullscreenBtn.textContent = isFullscreenActive() ? 'Exit Full' : 'Full Screen';
  }

  async function requestPreviewFullscreen() {
    if (!docPreviewContainerEl) throw new Error('Preview container unavailable.');
    if (docPreviewContainerEl.requestFullscreen) {
      await docPreviewContainerEl.requestFullscreen();
      return;
    }
    if (docPreviewContainerEl.webkitRequestFullscreen) {
      docPreviewContainerEl.webkitRequestFullscreen();
      return;
    }
    throw new Error('Fullscreen is not supported in this browser.');
  }

  async function exitPreviewFullscreen() {
    if (document.exitFullscreen) {
      await document.exitFullscreen();
      return;
    }
    if (document.webkitExitFullscreen) {
      document.webkitExitFullscreen();
      return;
    }
  }

  async function resolveDownloadUrl(docId) {
    const payload = await apiFetch('/api/v1/documents/' + encodeURIComponent(String(docId || '')) + '/download-url?expires_in=900');
    return String(payload && payload.download_url ? payload.download_url : '').trim();
  }

  async function previewSelectedDoc() {
    const doc = getDocById(selectedDocId);
    if (!doc) {
      updateSelectedDocLabel(null);
      if (docPreviewEl) docPreviewEl.src = 'about:blank';
      return;
    }
    updateSelectedDocLabel(doc);
    const url = await resolveDownloadUrl(doc.id);
    if (!url) throw new Error('No preview URL available');
    const previewUrl = buildPreviewUrl(url);
    if (docPreviewEl) docPreviewEl.src = previewUrl || url;
  }

  async function openSelectedDoc() {
    const doc = getDocById(selectedDocId);
    if (!doc) throw new Error('Please select a document first.');
    const url = await resolveDownloadUrl(doc.id);
    if (!url) throw new Error('No document URL available');
    window.open(buildPreviewUrl(url) || url, '_blank', 'noopener');
  }

  function renderDocDropdown() {
    if (!docSelectEl) return;
    const options = ['<option value="">Select document</option>'];
    currentDocs.forEach(function (doc, idx) {
      const id = String((doc && doc.id) || '').trim();
      if (!id) return;
      const name = escapeHtml(String(doc.file_name || ('Document ' + String(idx + 1))));
      options.push('<option value="' + escapeHtml(id) + '">' + name + '</option>');
    });
    docSelectEl.innerHTML = options.join('');

    const hasDocs = currentDocs.length > 0;
    docSelectEl.disabled = !hasDocs;
    if (docPreviewBtn) docPreviewBtn.disabled = !hasDocs;
    if (docOpenBtn) docOpenBtn.disabled = !hasDocs;
    if (docFullscreenBtn) docFullscreenBtn.disabled = !hasDocs;

    if (!hasDocs) {
      selectedDocId = '';
      updateSelectedDocLabel(null);
      if (docPreviewEl) docPreviewEl.src = 'about:blank';
      return;
    }

    selectedDocId = String((currentDocs[0] && currentDocs[0].id) || '').trim();
    docSelectEl.value = selectedDocId;
    updateSelectedDocLabel(getDocById(selectedDocId));
  }

  async function loadDocuments() {
    if (docCountEl) docCountEl.textContent = '...';

    try {
      const result = await apiFetch('/api/v1/claims/' + encodeURIComponent(params.claimUuid) + '/documents?limit=200&offset=0');
      const allDocs = Array.isArray(result && result.items) ? result.items : [];
      currentDocs = filterDocumentsByRecentDays(allDocs, DOC_LOOKBACK_DAYS);
      if (docCountEl) docCountEl.textContent = String(currentDocs.length);

      renderDocDropdown();
      if (currentDocs.length === 0) {
        setMessage('', 'No documents uploaded in last ' + String(DOC_LOOKBACK_DAYS) + ' days.');
      }
      if (currentDocs.length > 0) {
        try {
          await previewSelectedDoc();
        } catch (err) {
          setMessage('err', err && err.message ? err.message : 'Preview failed');
        }
      }
    } catch (err) {
      currentDocs = [];
      renderDocDropdown();
      if (docCountEl) docCountEl.textContent = '0';
      setMessage('err', err && err.message ? err.message : 'Failed to load documents');
    }
  }

  async function loadReportHtml() {
    if (reportEditorEl) reportEditorEl.innerHTML = '<p class="muted">Loading report...</p>';
    const sources = ['doctor', 'system', 'any'];

    for (let i = 0; i < sources.length; i += 1) {
      const src = sources[i];
      try {
        const payload = await apiFetch('/api/v1/user-tools/completed-reports/' + encodeURIComponent(params.claimUuid) + '/latest-html?source=' + encodeURIComponent(src));
        const html = normalizeHealthClaimReportTitle(String(payload && payload.report_html ? payload.report_html : '').trim());
        if (html) {
          loadedSource = String(payload.report_source || src || 'doctor').toLowerCase();
          if (reportSourceEl) reportSourceEl.textContent = 'loaded source: ' + loadedSource;
          reportEditorEl.innerHTML = html;
          syncConclusionOnlyFromReport();
          return;
        }
      } catch (_err) {
      }
    }

    const structuredFallback = await loadStructuredFallbackReport();
    if (structuredFallback) {
      loadedSource = 'structured';
      if (reportSourceEl) reportSourceEl.textContent = 'loaded source: structured';
      reportEditorEl.innerHTML = buildStructuredFallbackReportHtml(structuredFallback);
      syncConclusionOnlyFromReport();
      setMessage('', 'No saved report found. Auto-built report from extracted data. Please review and click Save HTML.');
      return;
    }

    loadedSource = 'doctor';
    if (reportSourceEl) reportSourceEl.textContent = 'loaded source: none';
    reportEditorEl.innerHTML = '<p class="muted">No saved report HTML found. You can paste/edit and save.</p>';
    syncConclusionOnlyFromReport();
  }

  function getActorId() {
    try {
      const raw = localStorage.getItem('qc_user') || '';
      if (!raw) return 'auditor-ui';
      const parsed = JSON.parse(raw);
      const username = String(parsed && parsed.username ? parsed.username : '').trim();
      return username || 'auditor-ui';
    } catch (_err) {
      return 'auditor-ui';
    }
  }

  function normalizeLabelKey(value) {
    return String(value == null ? '' : value).toLowerCase().replace(/[^a-z0-9]+/g, '');
  }

  function tidyInlineText(value) {
    const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim();
    return text;
  }

  function trimForConclusion(value, maxLen) {
    const text = tidyInlineText(value);
    const lim = Number(maxLen || 0);
    if (!text) return '';
    if (!Number.isFinite(lim) || lim <= 0 || text.length <= lim) return text;
    return text.slice(0, Math.max(20, lim - 1)).trim() + '...';
  }

  function parseEditorRows() {
    const rows = {};
    const html = String(reportEditorEl ? reportEditorEl.innerHTML : '').trim();
    if (!html) return rows;

    const wrap = document.createElement('div');
    wrap.innerHTML = html;
    wrap.querySelectorAll('tr').forEach(function (tr) {
      const th = tr.querySelector('th');
      const td = tr.querySelector('td');
      if (!th || !td) return;
      const key = normalizeLabelKey(th.textContent || '');
      const val = tidyInlineText(td.textContent || '');
      if (!key || !val || rows[key]) return;
      rows[key] = val;
    });
    return rows;
  }

  function pickRowValue(rows, aliases) {
    const map = rows && typeof rows === 'object' ? rows : {};
    const keys = Array.isArray(aliases) ? aliases : [];
    for (let i = 0; i < keys.length; i += 1) {
      const key = normalizeLabelKey(keys[i]);
      if (key && map[key]) return map[key];
    }
    return '';
  }

  function parseAgeYears(value) {
    const text = String(value || '');
    let m = text.match(/\b(\d{1,3})\s*(?:years?|yrs?|yr|y)\b/i);
    if (!m) m = text.match(/\b(\d{1,3})\b/);
    if (!m) return '';
    const years = Number(m[1]);
    if (!Number.isFinite(years) || years <= 0 || years > 120) return '';
    return String(Math.trunc(years));
  }

  function parseGenderWord(value) {
    const text = String(value || '').toLowerCase();
    if (!text) return '';
    if (/\b(?:male|man|boy|\bm\b)\b/i.test(text)) return 'man';
    if (/\b(?:female|woman|girl|\bf\b)\b/i.test(text)) return 'woman';
    return '';
  }

  function buildPatientPhrase(insuredText) {
    const age = parseAgeYears(insuredText);
    if (age) return age + 'yr old patient';
    const gender = parseGenderWord(insuredText);
    if (gender === 'man') return 'Male patient';
    if (gender === 'woman') return 'Female patient';
    return 'Patient';
  }

  function stripRulePrefixes(value) {
    let t = tidyInlineText(String(value || ''))
      .replace(/\bOPENAI_MERGED_REVIEW\b/ig, '')
      .replace(/\b[Rr]\d{3}\b\s*[-:]\s*/g, '')
      .replace(/\bDX\d{3}\b\s*[-:]\s*/g, '')
      .replace(/\bMissing evidence\s*:[^.;\n]*(?:[.;]|$)/ig, ' ')
      .replace(/\bGST guardrail triggered\b[^.;\n]*(?:[.;]|$)/ig, ' ')
      .replace(/\bChecklist condition matched and evidence pattern triggered\b[^.;\n]*(?:[.;]|$)/ig, ' ')
      .replace(/\bLearning signal\s*:[^.]*\.?/ig, '')
      .replace(/\btherefore,\s*one or more clinical rules are triggered\.?/ig, '')
      .replace(/\btherefore,\s*this rule is triggered\.?/ig, '')
      .replace(/\b(?:one or more\s+)?clinical rules?\s+are\s+triggered\.?/ig, '')
      .replace(/\bthis rule is triggered\.?/ig, '')
      .replace(/\bAuto-learned rule from auditor conclusion and extraction context\.?/ig, ' ')
      .replace(/\bSuper-admin approval required\.?/ig, ' ')
      .replace(/\bSuggested outcome\s*:\s*[A-Z_]+\.?/ig, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    if (/\bchecklist condition matched and evidence pattern triggered\b/i.test(t)) {
      return '';
    }
    if (/\bgst guardrail triggered\b/i.test(t)) {
      return '';
    }
    if (/\b(?:investigation_finding_in_details|auditor_conclusion|decision_conclusion)\b/i.test(t)) {
      return '';
    }
    if (/\b(?:investigation_finding_in_details|auditor_conclusion|decision_conclusion|complaints)(?:\s*,\s*(?:investigation_finding_in_details|auditor_conclusion|decision_conclusion|complaints))*$/i.test(t)) {
      return '';
    }
    return t;
  }

  function decisionReasonLabel(recommendation, recommendationText) {
    const rec = String(recommendation || '').trim().toUpperCase();
    const fallback = String(recommendationText || '').toLowerCase();
    if (rec === 'APPROVE' || rec === 'ADMISSIBLE' || /approve|admissible|payable/.test(fallback)) return 'approval';
    if (rec === 'QUERY' || /query|pending/.test(fallback)) return 'query';
    return 'rejection';
  }

  function extractChecklistRuleReason(checklist) {
    const reporting = checklist && checklist.source_summary && checklist.source_summary.reporting && typeof checklist.source_summary.reporting === 'object'
      ? checklist.source_summary.reporting
      : {};
    const fromReporting = stripRulePrefixes(reporting.conclusion || '');
    if (fromReporting) return fromReporting;

    const out = [];
    const seen = new Set();
    const rows = Array.isArray(checklist && checklist.checklist) ? checklist.checklist : [];
    rows.forEach(function (entry) {
      if (!(entry && entry.triggered)) return;
      const src = String(entry.source || '').trim().toLowerCase();
      if (src.indexOf('openai_claim_rules') !== 0 && src.indexOf('openai_diagnosis_criteria') !== 0) return;
      const note = stripRulePrefixes(entry.note || entry.why_triggered || entry.summary || entry.reason || '');
      const key = note.toLowerCase();
      if (!note || seen.has(key)) return;
      seen.add(key);
      out.push(note);
    });
    return out.slice(0, 3).join('; ');
  }

  async function runChecklistEvaluation(forceSourceRefresh) {
    const payload = await apiFetch('/api/v1/claims/' + encodeURIComponent(params.claimUuid) + '/checklist/evaluate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        actor_id: getActorId(),
        force_source_refresh: !!forceSourceRefresh,
      }),
    });
    latestChecklistResult = payload || null;
    return payload || {};
  }

  async function generateConclusionOnlyFromServer(options) {
    const html = String(reportEditorEl ? reportEditorEl.innerHTML : '').trim();
    if (!html) throw new Error('Report content is empty. Please load or generate report first.');

    const opts = options || {};
    const payload = await apiFetch('/api/v1/claims/' + encodeURIComponent(params.claimUuid) + '/reports/conclusion-only', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        report_html: html,
        actor_id: getActorId(),
        rerun_rules: !!opts.rerunRules,
        force_source_refresh: !!opts.forceSourceRefresh,
        use_ai: true,
      }),
    });

    const conclusion = String(payload && payload.conclusion ? payload.conclusion : '').trim();
    if (!conclusion) throw new Error('Conclusion generation returned empty output.');
    return payload || {};
  }

  function buildConclusionOnlyText(checklist) {
    const rows = parseEditorRows();
    const insured = pickRowValue(rows, ['INSURED', 'PATIENT', 'PATIENT DETAILS']);
    const diagnosis = trimForConclusion(pickRowValue(rows, ['DIAGNOSIS']), 180) || 'unspecified diagnosis';
    const complaints = trimForConclusion(pickRowValue(rows, ['CHIEF COMPLAINTS AT ADMISSION', 'CHIEF COMPLAINTS', 'CHIEF COMPLAINT']), 240) || 'unspecified complaints';
    const treatedWith = trimForConclusion(pickRowValue(rows, ['MEDICINE EVIDENCE USED', 'MEDICINES USED', 'TREATMENT']), 240) || 'supportive treatment';
    const deranged = trimForConclusion(pickRowValue(rows, ['DERANGED INVESTIGATION REPORTS', 'DERANGED INVESTIGATION']), 220) || 'no significant deranged values documented';
    const recommendationText = pickRowValue(rows, ['FINAL RECOMMENDATION', 'RECOMMENDATION']);
    const reasonLabel = decisionReasonLabel(checklist && checklist.recommendation, recommendationText);
    const ruleReason = trimForConclusion(extractChecklistRuleReason(checklist), 420) || 'clinical evidence is incomplete for final admissibility decision';
    const patientPhrase = buildPatientPhrase(insured);

    return stripRulePrefixes(
      patientPhrase + ' with chief complaint of ' + complaints
      + ', diagnosis of ' + diagnosis
      + ', treated with following ' + treatedWith
      + ', and deranged investigation report of ' + deranged + '. '
      + 'Reason for ' + reasonLabel + ': ' + ruleReason + '.'
    );
  }

  function syncConclusionOnlyFromReport() {
    if (!conclusionOnlyEl) return;
    const rows = parseEditorRows();
    const existing = pickRowValue(rows, ['CONCLUSION']);
    if (existing) {
      conclusionOnlyEl.value = stripRulePrefixes(existing);
    }
  }

  function applyConclusionToReport() {
    const text = tidyInlineText(conclusionOnlyEl ? conclusionOnlyEl.value : '');
    if (!text) throw new Error('Conclusion only text is empty.');
    if (!reportEditorEl) throw new Error('Report editor unavailable.');

    const wrap = document.createElement('div');
    wrap.innerHTML = String(reportEditorEl.innerHTML || '');
    let applied = false;
    wrap.querySelectorAll('tr').forEach(function (tr) {
      if (applied) return;
      const th = tr.querySelector('th');
      const td = tr.querySelector('td');
      if (!th || !td) return;
      if (normalizeLabelKey(th.textContent || '') !== 'conclusion') return;
      td.innerHTML = escapeMultilineHtml(text);
      applied = true;
    });

    if (!applied) {
      throw new Error('Conclusion row not found in report HTML.');
    }

    reportEditorEl.innerHTML = wrap.innerHTML;
  }
  function setBusy(busy) {
    if (saveBtn) saveBtn.disabled = !!busy;
    if (saveMarkBtn) saveMarkBtn.disabled = !!busy;
    if (qcConfirmCancelBtn) qcConfirmCancelBtn.disabled = !!busy;
    if (qcConfirmRatingEl) qcConfirmRatingEl.disabled = !!busy;
    if (qcConfirmProceedBtn) {
      if (busy) {
        qcConfirmProceedBtn.disabled = true;
      } else {
        updateQcConfirmProceedState();
      }
    }
    if (reloadBtn) reloadBtn.disabled = !!busy;
    if (sendBackBtn) sendBackBtn.disabled = !!busy;
    if (runRulesBtn) runRulesBtn.disabled = !!busy;
    if (generateConclusionBtn) generateConclusionBtn.disabled = !!busy;
    if (applyConclusionBtn) applyConclusionBtn.disabled = !!busy;
    if (conclusionOnlyEl) conclusionOnlyEl.disabled = !!busy;
    if (docSelectEl) docSelectEl.disabled = !!busy || currentDocs.length === 0;
    if (docPreviewBtn) docPreviewBtn.disabled = !!busy || currentDocs.length === 0;
    if (docOpenBtn) docOpenBtn.disabled = !!busy || currentDocs.length === 0;
    if (docFullscreenBtn) docFullscreenBtn.disabled = !!busy || currentDocs.length === 0;
  }

  function updateQcConfirmProceedState() {
    const rating = Number(qcConfirmRatingEl && qcConfirmRatingEl.value ? qcConfirmRatingEl.value : 0);
    const valid = Number.isFinite(rating) && rating >= 1 && rating <= 5;
    if (qcConfirmProceedBtn) qcConfirmProceedBtn.disabled = !valid;
    if (qcConfirmMsgEl) {
      qcConfirmMsgEl.textContent = valid ? '' : 'Select rating 1 to 5 to continue.';
    }
  }

  function toggleQcConfirmModal(show) {
    if (!qcConfirmOverlayEl) return;
    const open = !!show;
    qcConfirmOverlayEl.classList.toggle('open', open);
    qcConfirmOverlayEl.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (open) {
      if (qcConfirmRatingEl) qcConfirmRatingEl.value = '0';
      updateQcConfirmProceedState();
    }
  }

  async function saveDoctorHtml() {
    const html = normalizeHealthClaimReportTitle(String(reportEditorEl ? reportEditorEl.innerHTML : '').trim());
    if (!html) throw new Error('Report content is empty.');

    const payload = await apiFetch('/api/v1/claims/' + encodeURIComponent(params.claimUuid) + '/reports/html', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        report_html: html,
        report_status: 'draft',
        report_source: 'doctor',
      }),
    });

    return payload;
  }

  async function markQcDone() {
    const payload = await apiFetch('/api/v1/user-tools/completed-reports/' + encodeURIComponent(params.claimUuid) + '/qc-status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ qc_status: 'yes' }),
    });
    return payload;
  }
  async function sendBackToDoctor() {
    const opinion = String(window.prompt('Enter auditor opinion to send this case back to doctor:', '') || '').trim();
    if (!opinion) throw new Error('Auditor opinion is required.');

    const payload = await apiFetch('/api/v1/claims/' + encodeURIComponent(params.claimUuid) + '/status', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'in_review', note: opinion }),
    });
    return payload;
  }

  async function saveAuditorRating(rating) {
    const payload = await apiFetch('/api/v1/user-tools/completed-reports/' + encodeURIComponent(params.claimUuid) + '/auditor-rating', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating: Number(rating) }),
    });
    return payload;
  }

  async function runSaveAndMarkQcDone(rating) {
    setBusy(true);
    setMessage('', 'Saving report, marking QC done, and saving rating...');
    try {
      const saved = await saveDoctorHtml();
      const qcUpdated = await markQcDone();
      await saveAuditorRating(rating);
      const qcLabel = String(qcUpdated && qcUpdated.qc_status ? qcUpdated.qc_status : 'yes').toLowerCase() === 'yes' ? 'QC Yes' : 'QC No';
      setMessage('ok', 'Saved (v' + String(saved && saved.version_no ? saved.version_no : '-') + '), status changed to ' + qcLabel + ', and rating saved.');
      try {
        if (window.opener && window.opener !== window) {
          window.opener.postMessage({ type: 'qc-updated', claim_uuid: params.claimUuid, qc_status: String(qcUpdated && qcUpdated.qc_status ? qcUpdated.qc_status : 'yes') }, window.location.origin);
        }
      } catch (_err) {
      }
    } catch (err) {
      setMessage('err', err && err.message ? err.message : 'Save + Mark QC failed.');
    } finally {
      setBusy(false);
    }
  }

  if (docSelectEl) {
    docSelectEl.addEventListener('change', async function () {
      selectedDocId = String(docSelectEl.value || '').trim();
      try {
        await previewSelectedDoc();
      } catch (err) {
        setMessage('err', err && err.message ? err.message : 'Preview failed');
      }
    });
  }

  if (docPreviewBtn) {
    docPreviewBtn.addEventListener('click', async function () {
      try {
        await previewSelectedDoc();
      } catch (err) {
        setMessage('err', err && err.message ? err.message : 'Preview failed');
      }
    });
  }

  if (docOpenBtn) {
    docOpenBtn.addEventListener('click', async function () {
      try {
        await openSelectedDoc();
      } catch (err) {
        setMessage('err', err && err.message ? err.message : 'Open failed');
      }
    });
  }

  if (docFullscreenBtn) {
    docFullscreenBtn.addEventListener('click', async function () {
      try {
        if (!selectedDocId) throw new Error('Please select a document first.');
        await previewSelectedDoc();
        if (isFullscreenActive()) {
          await exitPreviewFullscreen();
        } else {
          await requestPreviewFullscreen();
        }
      } catch (err) {
        setMessage('err', err && err.message ? err.message : 'Full screen failed');
      }
    });
  }

  document.addEventListener('fullscreenchange', updateFullscreenButtonLabel);
  document.addEventListener('webkitfullscreenchange', updateFullscreenButtonLabel);

  if (reloadBtn) {
    reloadBtn.addEventListener('click', async function () {
      setMessage('', 'Reloading...');
      await Promise.all([loadReportHtml(), loadDocuments()]);
      setMessage('ok', 'Reloaded.');
    });
  }
  if (runRulesBtn) {
    runRulesBtn.addEventListener('click', async function () {
      setBusy(true);
      setMessage('', 'Running all clinical rules again...');
      try {
        const generated = await generateConclusionOnlyFromServer({ rerunRules: true, forceSourceRefresh: true });
        if (conclusionOnlyEl) conclusionOnlyEl.value = String(generated && generated.conclusion ? generated.conclusion : '').trim();
        const count = Number(generated && generated.triggered_rules_count ? generated.triggered_rules_count : 0);
        setMessage('ok', 'Rules re-evaluated. Conclusion generated from current report.' + (count > 0 ? (' Triggered rule count: ' + String(count) + '.') : ''));
      } catch (err) {
        setMessage('err', err && err.message ? err.message : 'Rule evaluation failed.');
      } finally {
        setBusy(false);
      }
    });
  }

  if (generateConclusionBtn) {
    generateConclusionBtn.addEventListener('click', async function () {
      setBusy(true);
      setMessage('', 'Generating conclusion only...');
      try {
        const generated = await generateConclusionOnlyFromServer({ rerunRules: false, forceSourceRefresh: false });
        if (conclusionOnlyEl) conclusionOnlyEl.value = String(generated && generated.conclusion ? generated.conclusion : '').trim();
        setMessage('ok', 'Conclusion only generated from current report. Review and click Apply To Report if needed.');
      } catch (err) {
        setMessage('err', err && err.message ? err.message : 'Conclusion generation failed.');
      } finally {
        setBusy(false);
      }
    });
  }

  if (applyConclusionBtn) {
    applyConclusionBtn.addEventListener('click', function () {
      try {
        applyConclusionToReport();
        setMessage('ok', 'Conclusion row updated in report editor.');
      } catch (err) {
        setMessage('err', err && err.message ? err.message : 'Could not apply conclusion.');
      }
    });
  }
  if (sendBackBtn) {
    sendBackBtn.addEventListener('click', async function () {
      setBusy(true);
      setMessage('', 'Sending case back to doctor...');
      try {
        const updated = await sendBackToDoctor();
        setMessage('ok', 'Case sent back to doctor successfully.');
        try {
          if (window.opener && window.opener !== window) {
            window.opener.postMessage({ type: 'claim-status-updated', claim_uuid: params.claimUuid, status: String(updated && updated.status ? updated.status : 'in_review') }, window.location.origin);
          }
        } catch (_err) {
        }
      } catch (err) {
        setMessage('err', err && err.message ? err.message : 'Send back failed.');
      } finally {
        setBusy(false);
      }
    });
  }

  if (saveBtn) {
    saveBtn.addEventListener('click', async function () {
      setBusy(true);
      setMessage('', 'Saving report...');
      try {
        const saved = await saveDoctorHtml();
        setMessage('ok', 'Saved. Report version: ' + String(saved && saved.version_no ? saved.version_no : '-'));
      } catch (err) {
        setMessage('err', err && err.message ? err.message : 'Save failed.');
      } finally {
        setBusy(false);
      }
    });
  }

  if (saveMarkBtn) {
    saveMarkBtn.addEventListener('click', function () {
      toggleQcConfirmModal(true);
    });
  }

  if (qcConfirmCancelBtn) {
    qcConfirmCancelBtn.addEventListener('click', function () {
      toggleQcConfirmModal(false);
    });
  }

  if (qcConfirmProceedBtn) {
    qcConfirmProceedBtn.addEventListener('click', async function () {
      const rating = Number(qcConfirmRatingEl && qcConfirmRatingEl.value ? qcConfirmRatingEl.value : 0);
      if (!Number.isFinite(rating) || rating < 1 || rating > 5) {
        updateQcConfirmProceedState();
        return;
      }
      toggleQcConfirmModal(false);
      await runSaveAndMarkQcDone(rating);
    });
  }

  if (qcConfirmRatingEl) {
    qcConfirmRatingEl.addEventListener('change', function () {
      updateQcConfirmProceedState();
    });
  }

  if (qcConfirmOverlayEl) {
    qcConfirmOverlayEl.addEventListener('click', function (event) {
      if (event && event.target === qcConfirmOverlayEl) {
        toggleQcConfirmModal(false);
      }
    });
  }

  document.addEventListener('keydown', function (event) {
    if (!event || event.key !== 'Escape') return;
    if (qcConfirmOverlayEl && qcConfirmOverlayEl.classList.contains('open')) {
      toggleQcConfirmModal(false);
    }
  });

  initPanelSplitter();

  Promise.all([loadReportHtml(), loadDocuments()]).then(function () {
    updateFullscreenButtonLabel();
    setMessage('ok', 'Ready.');
  }).catch(function (err) {
    setMessage('err', err && err.message ? err.message : 'Failed to load review workspace.');
  });
})();

















