/* 경영관리업무시스템 – 공통 스크립트 */
const fmt = {
  won: (v) => (v === null || v === undefined || v === '') ? '-' : Math.round(Number(v)).toLocaleString('ko-KR'),
  man: (v) => (!v ? '-' : (Number(v) / 10000).toLocaleString('ko-KR', { maximumFractionDigits: 0 }) + '만'),
  pct: (v, d = 1) => (v === null || v === undefined) ? '-' : (v >= 0 ? '+' : '') + Number(v).toFixed(d) + '%',
};

// 차트 공통 팔레트 (색상은 의미 고정: 남색=주요, 청록=보조, 주황=주의, 빨강=감소/징계)
const PALETTE = ['#2d5a8c', '#3fa7a3', '#f2a541', '#8e6cc4', '#e06c75', '#6c8ebf', '#9aa5b1', '#57b36b'];

function toast(msg) {
  let c = document.querySelector('.toast-container');
  if (!c) {
    c = document.createElement('div');
    c.className = 'toast-container position-fixed bottom-0 end-0 p-3 no-print';
    document.body.appendChild(c);
  }
  const el = document.createElement('div');
  el.className = 'toast align-items-center text-bg-dark border-0';
  el.innerHTML = `<div class="d-flex"><div class="toast-body"></div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>`;
  el.querySelector('.toast-body').textContent = msg;
  c.appendChild(el);
  new bootstrap.Toast(el, { delay: 2500 }).show();
}

async function postJSON(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || '' }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// 삭제 등 확인이 필요한 폼: <form data-confirm="메시지">
document.addEventListener('submit', (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// 금액 입력: data-money 속성 → 실시간 한글/콤마 미리보기
document.addEventListener('input', (e) => {
  if (!e.target.matches('[data-money]')) return;
  const hint = document.getElementById(e.target.dataset.money);
  if (hint) hint.textContent = e.target.value ? fmt.won(e.target.value) + '원 (월 ' + fmt.won(e.target.value / 12) + '원)' : '';
});

// 플래시 토스트 자동 숨김
setTimeout(() => { const t = document.getElementById('flashToast'); if (t) t.classList.remove('show'); }, 3000);

// URL의 msg 파라미터 제거 (새로고침 시 재표시 방지)
if (location.search.includes('msg=')) {
  const u = new URL(location.href); u.searchParams.delete('msg'); history.replaceState(null, '', u);
}

// ── 보안: CSRF 토큰 자동 첨부 ───────────────────────────────────────────
const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || '';
document.addEventListener('submit', (e) => {
  const f = e.target;
  if (f.method?.toLowerCase() === 'post' && CSRF && !f.querySelector('input[name="csrf_token"]')) {
    const i = document.createElement('input'); i.type = 'hidden'; i.name = 'csrf_token'; i.value = CSRF; f.prepend(i);
  }
}, true);
document.querySelectorAll('form[method="post"], form[method="POST"]').forEach(f => {
  if (CSRF && !f.querySelector('input[name="csrf_token"]')) {
    const i = document.createElement('input'); i.type = 'hidden'; i.name = 'csrf_token'; i.value = CSRF; f.prepend(i);
  }
});

// ── 인라인 이벤트 대체 (CSP: 인라인 스크립트 금지) ─────────────────────
document.addEventListener('click', (e) => {
  if (e.target.closest('a, button, input, select, label, textarea')) {
    if (e.target.closest('[data-print]')) window.print();
    if (e.target.closest('[data-toggle-sidebar]')) document.getElementById('sidebar').classList.toggle('open');
    return;
  }
  const row = e.target.closest('[data-href]');
  if (row) location.href = row.dataset.href;
});
document.querySelectorAll('[data-autosubmit]').forEach(el => el.addEventListener('change', () => el.form.submit()));

// ── 유휴 시간 표시 및 자동 로그아웃 안내 ─────────────────────────────────
(() => {
  const el = document.getElementById('idleTimer');
  if (!el) return;
  const limit = Number(el.dataset.idle) * 60; let left = limit;
  ['click', 'keydown'].forEach(ev => document.addEventListener(ev, () => { left = limit; }));
  setInterval(() => {
    left -= 1;
    if (left <= 0) { location.href = '/login?next=' + encodeURIComponent(location.pathname); return; }
    el.textContent = left <= 300 ? `⏱ ${Math.ceil(left / 60)}분 후 자동 로그아웃` : '';
  }, 1000);
})();

// ── 등록/수정 겸용 모달: <button data-bs-toggle="modal" data-bs-target="#m" data-fill='{"id":1,...}'> ──
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-fill]');
  if (!b) return;
  const m = document.querySelector(b.dataset.bsTarget);
  const f = m && m.querySelector('form');
  if (!f) return;
  f.reset();
  let data = {};
  try { data = JSON.parse(b.dataset.fill || '{}'); } catch (_) {}
  f.querySelectorAll('[name]').forEach((el) => {
    if (el.name === 'csrf_token' || el.name === 'next') return;
    if (!(el.name in data)) { if (el.name === 'id') el.value = ''; return; }
    const v = data[el.name];
    if (el.type === 'checkbox') el.checked = !!v && v !== '0';
    else el.value = v === null || v === undefined ? '' : v;
  });
  m.querySelectorAll('[data-mode-title]').forEach((t) => { t.textContent = data.id ? '수정' : '등록'; });
  m.querySelectorAll('[data-new-only]').forEach((t) => { t.hidden = !!data.id; });
  f.dispatchEvent(new Event('bms:filled'));
});
