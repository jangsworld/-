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
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
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
