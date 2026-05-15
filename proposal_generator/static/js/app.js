/* =============================================
   정량적 제안서 시스템 – Common JS utilities
   ============================================= */

/**
 * Debounce: returns a function that delays calling `fn` by `ms` ms.
 */
function debounce(fn, ms) {
  let timer;
  return function (...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), ms);
  };
}

/**
 * Show a toast-like save indicator.
 */
function showSaveIndicator(msg = '저장됨', ok = true) {
  let el = document.getElementById('save-indicator');
  if (!el) {
    el = document.createElement('div');
    el.id = 'save-indicator';
    el.className = 'alert py-1 px-3 mb-0';
    document.body.appendChild(el);
  }
  el.className = `alert py-1 px-3 mb-0 alert-${ok ? 'success' : 'danger'}`;
  el.textContent = ok ? `✓ ${msg}` : `✕ ${msg}`;
  el.classList.add('show');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('show'), 2000);
}

/**
 * Generic JSON fetch helper.
 */
async function apiFetch(url, options = {}) {
  const defaults = { headers: { 'Content-Type': 'application/json' } };
  const merged = { ...defaults, ...options };
  if (merged.body && typeof merged.body !== 'string') {
    merged.body = JSON.stringify(merged.body);
  }
  const res = await fetch(url, merged);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: '오류 발생' }));
    throw new Error(err.detail || '오류 발생');
  }
  return res.json();
}
