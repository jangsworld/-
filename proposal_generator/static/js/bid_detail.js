/* =============================================
   bid_detail.js – All interactivity for the
   bid detail page (bid_detail.html)
   ============================================= */

/* BID_ID and ALL_TECHS, ALL_PROJECTS are injected by the template */

/* ── Constants ─────────────────────────────── */
const GRADE_POINTS = { '특급': 4.0, '고급': 3.0, '중급': 2.0, '초급': 1.0 };

/* ── Score refresh ──────────────────────────── */

async function refreshScores() {
  try {
    const s = await apiFetch(`/api/bids/${BID_ID}/scores`);

    // Grand total badge
    const gtBadge = document.getElementById('grand-total-badge');
    if (gtBadge) gtBadge.textContent = `총점: ${s.grand_total.toFixed(2)}점`;

    // Management
    const mgmtEl = document.getElementById('mgmt-score');
    if (mgmtEl) mgmtEl.textContent = `${s.management.score.toFixed(2)}점`;

    // Experience
    const expTotalEl = document.getElementById('exp-total-amount');
    if (expTotalEl) expTotalEl.textContent = `${s.experience.total_amount.toLocaleString()}원`;
    const expPctEl = document.getElementById('exp-pct');
    if (expPctEl) expPctEl.textContent = `${s.experience.percentage.toFixed(1)}%`;
    const expScoreEl = document.getElementById('exp-score');
    if (expScoreEl) expScoreEl.textContent = `${s.experience.score.toFixed(2)}점`;

    // Technician
    const retRawEl = document.getElementById('ret-raw');
    if (retRawEl) retRawEl.textContent = s.technician.retention_raw;
    const retScoreEl = document.getElementById('ret-score');
    if (retScoreEl) retScoreEl.textContent = s.technician.retention_score.toFixed(2);
    const depRawEl = document.getElementById('dep-raw');
    if (depRawEl) depRawEl.textContent = s.technician.deployment_raw;
    const depScoreEl = document.getElementById('dep-score');
    if (depScoreEl) depScoreEl.textContent = s.technician.deployment_score.toFixed(2);
    const techTotalEl = document.getElementById('tech-total-score');
    if (techTotalEl) techTotalEl.textContent = s.technician.total_score.toFixed(2);

    // Reputation
    const repScoreEl = document.getElementById('rep-score');
    if (repScoreEl) repScoreEl.textContent = `${s.reputation.score.toFixed(2)}점`;

    // Extra total
    const extraTotalEl = document.getElementById('extra-total');
    if (extraTotalEl) extraTotalEl.textContent = s.extra.total.toFixed(2);

    // Generate tab
    const genMgmt = document.getElementById('gen-mgmt-score');
    if (genMgmt) genMgmt.textContent = `${s.management.score.toFixed(2)}점`;
    const genExp = document.getElementById('gen-exp-score');
    if (genExp) genExp.textContent = `${s.experience.score.toFixed(2)}점`;
    const genTech = document.getElementById('gen-tech-score');
    if (genTech) genTech.textContent = `${s.technician.total_score.toFixed(2)}점`;
    const genRep = document.getElementById('gen-rep-score');
    if (genRep) genRep.textContent = `${s.reputation.score.toFixed(2)}점`;
    const genGrand = document.getElementById('gen-grand-total');
    if (genGrand) genGrand.textContent = `${s.grand_total.toFixed(2)}점`;
    const genDisplay = document.getElementById('gen-total-display');
    if (genDisplay) genDisplay.textContent = `${s.grand_total.toFixed(2)}점`;

  } catch (e) {
    console.warn('점수 갱신 실패:', e);
  }
}

/* ── Auto-save bid fields ────────────────────── */

const saveBidDebounced = debounce(async () => {
  const fields = {};
  document.querySelectorAll('.bid-field').forEach(el => {
    const field = el.dataset.field;
    if (!field) return;
    let val = el.type === 'number' ? (parseFloat(el.value) || 0) : el.value;
    if (el.type === 'radio' && !el.checked) return;
    fields[field] = val;
  });

  // Collect thresholds
  fields.retention_thresholds = collectThresholds('retention');
  fields.deployment_thresholds = collectThresholds('deployment');

  try {
    await apiFetch(`/api/bids/${BID_ID}`, { method: 'PUT', body: fields });
    showSaveIndicator('저장됨');
    await refreshScores();
  } catch (e) {
    showSaveIndicator('저장 실패', false);
  }
}, 1000);

function collectThresholds(type) {
  const container = document.getElementById(`${type === 'retention' ? 'ret' : 'dep'}-thresholds`);
  if (!container) return [];
  const rows = container.querySelectorAll('.thresh-row');
  return Array.from(rows).map(row => [
    parseFloat(row.querySelector('.thresh-min').value) || 0,
    parseFloat(row.querySelector('.thresh-frac').value) || 0,
  ]);
}

function addThresholdRow(containerId, type) {
  const container = document.getElementById(containerId);
  const div = document.createElement('div');
  div.className = 'row g-1 mb-1 thresh-row';
  div.dataset.type = type;
  div.innerHTML = `
    <div class="col-5"><input type="number" class="form-control form-control-sm thresh-min" placeholder="최소점수"></div>
    <div class="col-5"><input type="number" step="0.01" class="form-control form-control-sm thresh-frac" placeholder="비율"></div>
    <div class="col-2"><button class="btn btn-sm btn-outline-danger thresh-del" type="button">×</button></div>
  `;
  div.querySelector('.thresh-del').addEventListener('click', () => { div.remove(); saveBidDebounced(); });
  div.querySelectorAll('input').forEach(el => el.addEventListener('change', saveBidDebounced));
  container.appendChild(div);
}

/* ── Project selection ───────────────────────── */

async function updateProjectSelection() {
  const checkedIds = Array.from(document.querySelectorAll('.proj-check:checked'))
    .map(el => parseInt(el.dataset.id));

  try {
    await apiFetch(`/api/bids/${BID_ID}/projects`, {
      method: 'POST',
      body: { ids: checkedIds },
    });
    // Update selected projects panel
    const tbody = document.getElementById('selected-projects-body');
    if (tbody) {
      const selected = ALL_PROJECTS.filter(p => checkedIds.includes(p.id));
      tbody.innerHTML = selected.map(p =>
        `<tr><td class="small">${p.name}</td><td class="small">${p.client || ''}</td>
         <td class="text-end small">${(p.amount||0).toLocaleString()}</td></tr>`
      ).join('');
    }
    await refreshScores();
  } catch (e) {
    showSaveIndicator('실적 저장 실패', false);
  }
}

/* ── Technician selection ────────────────────── */

async function updateTechnicianSelection(role) {
  const cls = role === 'retention' ? '.ret-check' : '.dep-check';
  const ids = Array.from(document.querySelectorAll(`${cls}:checked`))
    .map(el => parseInt(el.dataset.id));

  try {
    await apiFetch(`/api/bids/${BID_ID}/technicians`, {
      method: 'POST',
      body: { role, ids },
    });
    refreshTechLists();
    await refreshScores();
    await refreshTechDocs();
  } catch (e) {
    showSaveIndicator('기술인력 저장 실패', false);
  }
}

function refreshTechLists() {
  const retIds = new Set(
    Array.from(document.querySelectorAll('.ret-check:checked')).map(el => parseInt(el.dataset.id))
  );
  const depIds = new Set(
    Array.from(document.querySelectorAll('.dep-check:checked')).map(el => parseInt(el.dataset.id))
  );

  const retList = document.getElementById('retention-list');
  const depList = document.getElementById('deployment-list');

  if (retList) {
    retList.innerHTML = ALL_TECHS.filter(t => retIds.has(t.id)).map(t =>
      `<li class="list-group-item d-flex justify-content-between align-items-center py-1 px-0">
        <span class="small">${t.name}</span>
        <span class="badge grade-badge-${t.grade}">${t.grade}</span>
      </li>`
    ).join('');
  }

  if (depList) {
    depList.innerHTML = ALL_TECHS.filter(t => depIds.has(t.id)).map(t =>
      `<li class="list-group-item d-flex justify-content-between align-items-center py-1 px-0">
        <span class="small">${t.name}</span>
        <span class="badge grade-badge-${t.grade}">${t.grade}</span>
      </li>`
    ).join('');
  }
}

/* ── Technician DB modal (on bid_detail page) ─── */

let _techModal = null;

function openAddTechModal() {
  document.getElementById('techModalTitle').textContent = '기술자 추가';
  document.getElementById('tech-edit-id').value = '';
  document.getElementById('tech-name').value = '';
  document.getElementById('tech-qual').value = '정보통신기술자';
  document.getElementById('tech-grade').value = '특급';
  document.getElementById('tech-hire-date').value = '';
  if (!_techModal) _techModal = new bootstrap.Modal(document.getElementById('techModal'));
  _techModal.show();
}

function openEditTechModal(t) {
  document.getElementById('techModalTitle').textContent = '기술자 수정';
  document.getElementById('tech-edit-id').value = t.id;
  document.getElementById('tech-name').value = t.name;
  document.getElementById('tech-qual').value = t.qualification;
  document.getElementById('tech-grade').value = t.grade;
  document.getElementById('tech-hire-date').value = t.hire_date || '';
  if (!_techModal) _techModal = new bootstrap.Modal(document.getElementById('techModal'));
  _techModal.show();
}

async function saveTech() {
  const id = document.getElementById('tech-edit-id').value;
  const body = {
    name: document.getElementById('tech-name').value,
    qualification: document.getElementById('tech-qual').value,
    grade: document.getElementById('tech-grade').value,
    hire_date: document.getElementById('tech-hire-date').value,
  };
  const url = id ? `/api/technicians/${id}` : '/api/technicians';
  const method = id ? 'PUT' : 'POST';
  try {
    await apiFetch(url, { method, body });
    showSaveIndicator('기술자 저장됨');
    location.reload();
  } catch (e) {
    alert('저장 실패: ' + e.message);
  }
}

async function deleteTech(id, name) {
  if (!confirm(`"${name}" 기술자를 삭제하시겠습니까?`)) return;
  try {
    await apiFetch(`/api/technicians/${id}`, { method: 'DELETE' });
    location.reload();
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

/* ── Extra items modal ───────────────────────── */

let _extraModal = null;

function openAddExtraModal() {
  document.getElementById('extraModalTitle').textContent = '추가 항목 추가';
  document.getElementById('extra-edit-id').value = '';
  document.getElementById('extra-name').value = '';
  document.getElementById('extra-max-score').value = '0';
  document.getElementById('extra-actual-score').value = '0';
  document.getElementById('extra-desc').value = '';
  if (!_extraModal) _extraModal = new bootstrap.Modal(document.getElementById('extraModal'));
  _extraModal.show();
}

function openEditExtraModal(item) {
  document.getElementById('extraModalTitle').textContent = '추가 항목 수정';
  document.getElementById('extra-edit-id').value = item.id;
  document.getElementById('extra-name').value = item.name;
  document.getElementById('extra-max-score').value = item.max_score;
  document.getElementById('extra-actual-score').value = item.actual_score;
  document.getElementById('extra-desc').value = item.description || '';
  if (!_extraModal) _extraModal = new bootstrap.Modal(document.getElementById('extraModal'));
  _extraModal.show();
}

async function saveExtra() {
  const id = document.getElementById('extra-edit-id').value;
  const body = {
    name: document.getElementById('extra-name').value,
    max_score: parseFloat(document.getElementById('extra-max-score').value) || 0,
    actual_score: parseFloat(document.getElementById('extra-actual-score').value) || 0,
    description: document.getElementById('extra-desc').value,
  };
  const url = id ? `/api/bids/${BID_ID}/extra_items/${id}` : `/api/bids/${BID_ID}/extra_items`;
  const method = id ? 'PUT' : 'POST';
  try {
    await apiFetch(url, { method, body });
    showSaveIndicator('저장됨');
    if (_extraModal) _extraModal.hide();
    location.reload();
  } catch (e) {
    alert('저장 실패: ' + e.message);
  }
}

async function deleteExtraItem(id) {
  if (!confirm('항목을 삭제하시겠습니까?')) return;
  try {
    await apiFetch(`/api/bids/${BID_ID}/extra_items/${id}`, { method: 'DELETE' });
    location.reload();
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

/* ── Document folder browsing ────────────────── */

async function loadDocSection(folder, containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;
  try {
    const files = await apiFetch(`/api/documents/browse?path=${encodeURIComponent(folder)}`);
    if (files.length === 0) {
      container.innerHTML = '<p class="text-muted small">파일이 없습니다.</p>';
      return;
    }
    container.innerHTML = files.map(f => {
      const icon = fileIcon(f.extension);
      return `
        <div class="doc-file-row">
          <i class="bi ${icon} doc-file-icon"></i>
          <a href="/api/documents/file?path=${encodeURIComponent(f.path)}" target="_blank"
             rel="noopener">${f.name}</a>
          <span class="text-muted small ms-auto">${formatBytes(f.size)}</span>
          <span class="text-muted small">${f.modified}</span>
        </div>`;
    }).join('');
  } catch (e) {
    container.innerHTML = `<p class="text-danger small">로드 실패: ${e.message}</p>`;
  }
}

function fileIcon(ext) {
  const map = {
    pdf: 'bi-file-earmark-pdf',
    jpg: 'bi-file-earmark-image', jpeg: 'bi-file-earmark-image', png: 'bi-file-earmark-image',
    doc: 'bi-file-earmark-word', docx: 'bi-file-earmark-word',
    xls: 'bi-file-earmark-excel', xlsx: 'bi-file-earmark-excel',
    hwp: 'bi-file-earmark-text', hwpx: 'bi-file-earmark-text',
  };
  return map[ext] || 'bi-file-earmark';
}

function formatBytes(b) {
  if (b < 1024) return `${b}B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)}KB`;
  return `${(b / 1024 / 1024).toFixed(1)}MB`;
}

/* ── File upload modal ───────────────────────── */

let _currentUploadFolder = '';
let _uploadModal = null;

function openUpload(folder) {
  _currentUploadFolder = folder;
  document.getElementById('upload-folder-label').textContent = folder;
  document.getElementById('upload-progress').innerHTML = '';
  if (!_uploadModal) _uploadModal = new bootstrap.Modal(document.getElementById('uploadModal'));
  _uploadModal.show();
}

async function uploadFiles(files, folder) {
  const progress = document.getElementById('upload-progress');
  progress.innerHTML = '';
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch(`/api/documents/upload?folder=${encodeURIComponent(folder)}`, {
      method: 'POST',
      body: fd,
    });
    const ok = res.ok;
    const msgEl = document.createElement('div');
    msgEl.className = `alert alert-${ok ? 'success' : 'danger'} py-1 small mb-1`;
    msgEl.textContent = ok ? `✓ ${file.name} 업로드 완료` : `✕ ${file.name} 업로드 실패`;
    progress.appendChild(msgEl);

    if (ok) {
      // Refresh the relevant doc section
      const safeId = `docs-${folder}`;
      if (document.getElementById(safeId)) {
        await loadDocSection(folder, safeId);
      } else {
        // technician sub-folder
        await refreshTechDocs();
      }
    }
  }
}

/* ── Tech docs panel ─────────────────────────── */

async function refreshTechDocs() {
  const container = document.getElementById('tech-docs-container');
  if (!container) return;

  const retIds = new Set(
    Array.from(document.querySelectorAll('.ret-check:checked')).map(el => parseInt(el.dataset.id))
  );
  const depIds = new Set(
    Array.from(document.querySelectorAll('.dep-check:checked')).map(el => parseInt(el.dataset.id))
  );
  const allIds = new Set([...retIds, ...depIds]);

  if (allIds.size === 0) {
    container.innerHTML = '<p class="text-muted small">보유인력 또는 투입인력을 선택하면 서류 폴더가 표시됩니다.</p>';
    return;
  }

  const selectedTechs = ALL_TECHS.filter(t => allIds.has(t.id));
  container.innerHTML = '';

  for (const t of selectedTechs) {
    const folderPath = `기술자/${t.name}`;
    const panel = document.createElement('div');
    panel.className = 'tech-doc-panel';
    panel.innerHTML = `
      <div class="d-flex justify-content-between align-items-center">
        <div class="tech-doc-name">
          <span class="badge grade-badge-${t.grade} me-1">${t.grade}</span>${t.name}
        </div>
        <button class="btn btn-sm btn-outline-primary" onclick="openUpload('${folderPath}')">
          <i class="bi bi-upload me-1"></i>업로드
        </button>
      </div>
      <div id="tech-doc-${t.id}" class="mt-2"></div>
    `;
    container.appendChild(panel);
    await loadDocSection(folderPath, `tech-doc-${t.id}`);
  }
}

/* ── Init ────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  // Auto-save on field change
  document.querySelectorAll('.bid-field').forEach(el => {
    el.addEventListener('change', saveBidDebounced);
    if (el.tagName === 'INPUT' && el.type === 'text') {
      el.addEventListener('input', saveBidDebounced);
    }
    if (el.tagName === 'INPUT' && el.type === 'number') {
      el.addEventListener('input', saveBidDebounced);
    }
  });

  // Threshold add buttons
  document.getElementById('add-ret-thresh')?.addEventListener('click', () => {
    addThresholdRow('ret-thresholds', 'retention');
  });
  document.getElementById('add-dep-thresh')?.addEventListener('click', () => {
    addThresholdRow('dep-thresholds', 'deployment');
  });

  // Wire existing threshold delete buttons
  document.querySelectorAll('.thresh-del').forEach(btn => {
    btn.addEventListener('click', () => { btn.closest('.thresh-row').remove(); saveBidDebounced(); });
  });
  document.querySelectorAll('.thresh-row input').forEach(el => {
    el.addEventListener('change', saveBidDebounced);
  });

  // Project checkboxes
  document.querySelectorAll('.proj-check').forEach(cb => {
    cb.addEventListener('change', updateProjectSelection);
  });

  // Select all projects
  document.getElementById('proj-select-all')?.addEventListener('change', function () {
    document.querySelectorAll('.proj-check').forEach(cb => { cb.checked = this.checked; });
    updateProjectSelection();
  });

  // Technician checkboxes
  document.querySelectorAll('.ret-check').forEach(cb => {
    cb.addEventListener('change', () => updateTechnicianSelection('retention'));
  });
  document.querySelectorAll('.dep-check').forEach(cb => {
    cb.addEventListener('change', () => updateTechnicianSelection('deployment'));
  });

  // Load document sections when tabs become visible
  const tabMap = {
    'tab-management':  [['신용평가등급', 'docs-신용평가등급']],
    'tab-experience':  [['실적증명서',   'docs-실적증명서']],
    'tab-reputation':  [
      ['입찰참가자격등록증', 'docs-입찰참가자격등록증'],
      ['부정당업자확인서',   'docs-부정당업자확인서'],
    ],
  };

  document.querySelectorAll('#bidTabs button[data-bs-toggle="tab"]').forEach(btn => {
    btn.addEventListener('shown.bs.tab', async (e) => {
      const target = e.target.getAttribute('data-bs-target').replace('#', '');
      if (tabMap[target]) {
        for (const [folder, id] of tabMap[target]) {
          await loadDocSection(folder, id);
        }
      }
      if (target === 'tab-technician') {
        await refreshTechDocs();
      }
    });
  });

  // Drop zone for file upload modal
  const dropZone = document.getElementById('drop-zone');
  if (dropZone) {
    dropZone.addEventListener('click', () => document.getElementById('file-input').click());
    dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', e => {
      e.preventDefault();
      dropZone.classList.remove('dragover');
      uploadFiles(Array.from(e.dataTransfer.files), _currentUploadFolder);
    });
    document.getElementById('file-input').addEventListener('change', function () {
      uploadFiles(Array.from(this.files), _currentUploadFolder);
      this.value = '';
    });
  }

  // PPT generate button
  const pptBtn = document.getElementById('ppt-generate-btn');
  if (pptBtn) {
    pptBtn.addEventListener('click', (e) => {
      pptBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>생성 중...';
      pptBtn.classList.add('disabled');
      setTimeout(() => {
        pptBtn.innerHTML = '<i class="bi bi-file-earmark-slides me-2"></i>PPT 생성 및 다운로드';
        pptBtn.classList.remove('disabled');
      }, 5000);
    });
  }

  // Initial score refresh
  refreshScores();
});
