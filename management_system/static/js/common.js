/* 경영관리업무시스템 – 공통 스크립트 */
const fmt = {
  won: (v) => (v === null || v === undefined || v === '') ? '-' : Math.round(Number(v)).toLocaleString('ko-KR'),
  man: (v) => (!v ? '-' : (Number(v) / 10000).toLocaleString('ko-KR', { maximumFractionDigits: 0 }) + '만'),
  pct: (v, d = 1) => (v === null || v === undefined) ? '-' : (v >= 0 ? '+' : '') + Number(v).toFixed(d) + '%',
};

// 차트 공통 팔레트 (색상은 의미 고정: 남색=주요, 청록=보조, 주황=주의, 빨강=감소/징계)
const PALETTE = ['#2d5a8c', '#3fa7a3', '#f2a541', '#8e6cc4', '#e06c75', '#6c8ebf', '#9aa5b1', '#57b36b'];

function toast(msg) {
  if (!window.bootstrap) return;   // 로그인 화면 등 bootstrap 없는 화면
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
  const msg = e.submitter?.dataset.confirm || e.target.dataset.confirm;   // 버튼별 확인 문구가 있으면 그것을
  if (msg && !confirm(msg)) e.preventDefault();
});
// 모두 선택/해제: <input type="checkbox" data-check-all="이름">
document.addEventListener('change', (e) => {
  const name = e.target.dataset?.checkAll;
  if (!name) return;
  (e.target.form || document).querySelectorAll(`input[type=checkbox][name="${name}"]`).forEach(c => { c.checked = e.target.checked; });
});

// 모든 저장(POST) 폼: 한 번 보낸 뒤 응답이 올 때까지 다시 보내지 않음 (두 번 눌러 두 건 생기는 것 방지)
document.addEventListener('submit', (e) => {
  const f = e.target;
  if ((f.getAttribute('method') || '').toLowerCase() !== 'post' || e.defaultPrevented) return;
  if (f.dataset.sent) { e.preventDefault(); return; }
  setTimeout(() => {                                          // 다른 검사(필수 칸·확인 창)가 막지 않았을 때만 '보냄' 표시
    if (e.defaultPrevented) return;
    f.dataset.sent = '1';
    setTimeout(() => { delete f.dataset.sent; }, 8000);      // 내려받기처럼 화면이 안 바뀌는 폼은 잠시 뒤 다시 허용
  }, 0);
});
window.addEventListener('pageshow', () => document.querySelectorAll('form[data-sent]').forEach(f => { delete f.dataset.sent; }));

// 오래 걸리는 폼: <form data-busy="진행 문구"> → 응답이 올 때까지 버튼을 잠가 중복 제출을 막음
document.addEventListener('submit', (e) => {
  const text = e.target.dataset.busy;
  const btn = e.target.querySelector('button:not([type="button"])');
  if (text && btn && !e.defaultPrevented) { btn.disabled = true; btn.textContent = text; }
});

// 금액 입력: data-money 속성 → 실시간 한글/콤마 미리보기
document.addEventListener('input', (e) => {
  if (!e.target.matches('[data-money]')) return;
  const hint = document.getElementById(e.target.dataset.money);
  const v = moneyVal(e.target.value);
  if (hint && !hint.classList.contains('monthly-hint') && !e.target.classList.contains('money-input')) hint.textContent = v ? fmt.won(v) + '원' : '';   // 칸에 콤마가 보이면 같은 숫자를 또 쓰지 않음
  const monthly = document.getElementById(e.target.dataset.monthly);
  if (monthly) monthly.value = v ? fmt.won(Math.ceil(v / 12)) : '';  // 회사 연봉계약서 기준 원 단위 올림
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
  if ((f.getAttribute('method') || '').toLowerCase() === 'post' && CSRF && !f.querySelector('input[name="csrf_token"]')) {
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
  if (e.target.closest('[data-nolink]')) return;          // 고르기 칸: 눌러도 상세로 넘어가지 않음
  const row = e.target.closest('[data-href]');
  if (row) location.href = row.dataset.href;
});
// 머리글의 '모두 고르기' (data-check-group="이름" → data-group="이름" 인 보이는 칸 전부)
document.addEventListener('change', (e) => {
  const all = e.target.closest('input[data-check-group]');
  if (!all) return;
  document.querySelectorAll('input[data-group="' + all.dataset.checkGroup + '"]').forEach(cb => {
    if (cb.offsetParent !== null) cb.checked = all.checked;
  });
});
document.querySelectorAll('[data-autosubmit]').forEach(el => el.addEventListener('change', () => { keepScroll(); el.form.submit(); }));

// ── 저장·필터 뒤에도 보던 위치 그대로: 같은 화면으로 돌아오면 스크롤 위치를 되돌림 ──
function keepScroll() {
  try { sessionStorage.setItem('bms_scroll', JSON.stringify({ p: location.pathname, y: window.scrollY, t: Date.now() })); } catch (_) {}
}
document.addEventListener('submit', (e) => { if (!e.defaultPrevented) keepScroll(); });
document.addEventListener('click', (e) => {          // 같은 화면 안의 탭·연도·필터 링크
  const a = e.target.closest('a[href]');
  if (!a || a.target || a.origin !== location.origin || a.pathname !== location.pathname || a.getAttribute('href').startsWith('#')) return;
  keepScroll();
});
(() => {
  let s = null;
  try { s = JSON.parse(sessionStorage.getItem('bms_scroll') || 'null'); sessionStorage.removeItem('bms_scroll'); } catch (_) {}
  if (!s || s.p !== location.pathname || Date.now() - s.t > 20000 || !s.y) return;
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  const back = () => window.scrollTo({ top: s.y, left: 0, behavior: 'instant' });   // 부드러운 이동 없이 바로
  back();
  window.addEventListener('load', () => { back(); setTimeout(back, 60); });   // '#위치'로 튀는 것보다 나중에 되돌림
})();

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
    else if (el.name === 'company' && el.tagName === 'SELECT') el.value = String(v || '') === '2' ? '2' : '1';   // 비어 있는 옛 자료는 본사
    else el.value = v === null || v === undefined ? '' : v;
  });
  m.querySelectorAll('[data-mode-title]').forEach((t) => { t.textContent = data.id ? '수정' : '등록'; });
  m.querySelectorAll('[data-new-only]').forEach((t) => { t.hidden = !!data.id; });
  f.dispatchEvent(new Event('bms:filled'));
});

// ════════════════════════════════════════════════════════════════════
// 입력 화면 도우미 (모든 등록·수정 화면·등록 창에 자동 적용)
// ════════════════════════════════════════════════════════════════════
(() => {
  const FIELDS = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file]):not([type=submit]), select, textarea';
  const isEmpty = (el) => !String(el.value || '').trim();
  const mark = (el) => {
    if (!el.matches || !el.matches(FIELDS)) return;
    el.classList.toggle('is-empty', isEmpty(el));
    const box = el.closest('.field-box');
    if (box) box.classList.toggle('has-value', [...box.querySelectorAll(FIELDS)].some(x => !isEmpty(x)));
  };
  const markAll = (root = document) => root.querySelectorAll(FIELDS).forEach(mark);

  // 1) 필수 칸 이름에 * 자동 표시
  const labelOf = (el) => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l; }
    const p = el.closest('.col, [class*="col-"], .mb-2, .mb-3, .field');
    return p ? p.querySelector('label.form-label, .form-label') : null;
  };
  const addStars = (root = document) => root.querySelectorAll('[required]').forEach((el) => {
    const l = labelOf(el);
    if (l && !l.querySelector('.text-danger, .req-mark')) { const s = document.createElement('span'); s.className = 'req-mark'; s.textContent = ' *'; l.appendChild(s); }
  });
  const nameOf = (el) => ((labelOf(el)?.textContent || el.placeholder || '').replace(/\*/g, '').trim());

  // 2) 입력 중인 칸 이름 강조
  document.addEventListener('focusin', (e) => { const p = e.target.matches?.(FIELDS) && e.target.parentElement; if (p) p.classList.add('field-focus'); });
  document.addEventListener('focusout', (e) => { const p = e.target.parentElement; if (p) p.classList.remove('field-focus'); });

  // 3) 자동 서식: 전화번호·주민번호·계좌번호
  const phone = (v) => {
    const d = v.replace(/\D/g, '').slice(0, 11);
    if (d.startsWith('02')) return d.length > 9 ? `${d.slice(0, 2)}-${d.slice(2, 6)}-${d.slice(6)}` : d.length > 5 ? `${d.slice(0, 2)}-${d.slice(2, 5)}-${d.slice(5)}` : d;
    if (d.length > 10) return `${d.slice(0, 3)}-${d.slice(3, 7)}-${d.slice(7)}`;
    if (d.length > 6) return `${d.slice(0, 3)}-${d.slice(3, 6)}-${d.slice(6)}`;
    if (d.length > 3) return `${d.slice(0, 3)}-${d.slice(3)}`;
    return d;
  };
  const clearBad = (el) => {
    if (!el.classList) return;
    el.classList.remove('is-invalid');
    const host = el.closest('.input-group') || el;
    const m = host.parentElement?.querySelector(':scope > .invalid-msg'); if (m) m.remove();
  };
  document.addEventListener('input', (e) => {
    const el = e.target;
    if (!el.matches) return;
    if (el.matches('[name=phone], [name=emergency_contact], [name$="_phone"], [data-fmt=phone]')) {
      const atEnd = el.selectionStart === el.value.length; el.value = phone(el.value); if (atEnd) el.selectionStart = el.selectionEnd = el.value.length;
    } else if (el.matches('[name=rrn]')) {
      const d = el.value.replace(/\D/g, '').slice(0, 13); el.value = d.length > 6 ? `${d.slice(0, 6)}-${d.slice(6)}` : d;
    } else if (el.matches('[name=bank_account], [data-fmt=digits]')) {
      el.value = el.value.replace(/[^\d]/g, '');
    }
    mark(el); clearBad(el);
  });
  document.addEventListener('change', (e) => { mark(e.target); clearBad(e.target); });

  // 4) 틀린 칸: 빨간 테두리 + 칸 아래 이유 + 첫 칸으로 이동 (브라우저 말풍선 대신)
  const reason = (el) => {
    const v = el.validity;
    if (v.valueMissing) return el.tagName === 'SELECT' ? '골라 주세요' : '꼭 입력해야 합니다';
    if (v.typeMismatch) return el.type === 'email' ? '이메일 형식이 아닙니다 (예: name@company.com)' : '형식이 맞지 않습니다';
    if (v.patternMismatch) return el.title || (el.name === 'rrn' ? '주민등록번호 13자리를 넣어 주세요' : '형식이 맞지 않습니다');
    if (v.rangeUnderflow) return `${el.min} 이상으로 넣어 주세요`;
    if (v.rangeOverflow) return `${el.max} 이하로 넣어 주세요`;
    if (v.tooLong || v.tooShort) return '글자 수를 확인해 주세요';
    if (v.badInput || v.stepMismatch) return '숫자를 확인해 주세요';
    return el.validationMessage || '확인해 주세요';
  };
  let bad = [];
  document.addEventListener('invalid', (e) => {
    const el = e.target;
    e.preventDefault();
    el.classList.add('is-invalid');
    const host = el.closest('.input-group') || el;
    let m = host.parentElement.querySelector(':scope > .invalid-msg');
    if (!m) { m = document.createElement('div'); m.className = 'invalid-msg'; host.after(m); }
    m.textContent = reason(el);
    bad.push(el);
    if (bad.length === 1) setTimeout(() => {
      const first = bad[0], n = bad.length; bad = [];
      first.scrollIntoView({ block: 'center', behavior: 'smooth' });
      setTimeout(() => first.focus({ preventScroll: true }), 250);
      const nm = nameOf(first);
      toast(n > 1 ? `확인할 칸이 ${n}개 있습니다 — '${nm}'부터 보세요` : `'${nm}' 칸을 확인해 주세요`);
    }, 0);
  }, true);

  // 5) 저장하지 않고 나가면 확인 (<form data-dirty>)
  let submitting = false;
  document.addEventListener('submit', (e) => { if (!e.defaultPrevented) submitting = true; });
  document.querySelectorAll('form[data-dirty]').forEach((f) => {
    const on = (e) => { if (e.isTrusted) f.classList.add("is-dirty"); };
    f.addEventListener('input', on); f.addEventListener('change', on);
  });
  window.addEventListener('beforeunload', (e) => {
    if (!submitting && document.querySelector('form[data-dirty].is-dirty')) { e.preventDefault(); e.returnValue = ''; }
  });

  // 6) 구역 바로가기 + 구역별 입력 개수 + 남은 필수 칸 (<nav data-section-nav="폼 id">)
  document.querySelectorAll('[data-section-nav]').forEach((nav) => {
    const form = document.getElementById(nav.dataset.sectionNav);
    if (!form) return;
    const secs = [...form.querySelectorAll('.form-section[id]')];
    nav.innerHTML = secs.map(s => `<a href="#${s.id}" data-sec="${s.id}">${s.dataset.title || s.id}<span class="cnt"></span></a>`).join('') + '<span class="req-left"></span>';
    const refresh = () => {
      let left = 0;
      secs.forEach((s) => {
        const f = [...s.querySelectorAll(FIELDS)].filter(x => !x.readOnly && !x.disabled && x.offsetParent !== null);
        const filled = f.filter(x => !isEmpty(x)).length;
        const reqLeft = f.filter(x => x.required && isEmpty(x)).length;
        left += reqLeft;
        const done = reqLeft === 0 && filled > 0;
        s.classList.toggle('is-done', done);
        const a = nav.querySelector(`[data-sec="${s.id}"]`);
        a.classList.toggle('done', done);
        a.querySelector('.cnt').textContent = f.length ? `${filled}/${f.length}` : '';
        const c = s.querySelector('.form-section-count'); if (c) c.textContent = f.length ? `${filled} / ${f.length}칸 입력` : '';
      });
      const r = nav.querySelector('.req-left');
      r.textContent = left ? `필수 ${left}칸 남음` : '필수 입력 완료';
      r.classList.toggle('ok', !left);
    };
    form.addEventListener('input', refresh); form.addEventListener('change', refresh); refresh();
    if ('IntersectionObserver' in window) {
      const io = new IntersectionObserver((ents) => ents.forEach((en) => {
        if (en.isIntersecting) nav.querySelectorAll('a').forEach(a => a.classList.toggle('active', a.dataset.sec === en.target.id));
      }), { rootMargin: '-35% 0px -60% 0px' });
      secs.forEach(s => io.observe(s));
    }
  });

  // 7) 등록 창이 열리면 첫 빈 칸에 바로 입력
  document.addEventListener('shown.bs.modal', (e) => {
    markAll(e.target); addStars(e.target);
    const el = [...e.target.querySelectorAll(FIELDS)].find(x => !x.readOnly && !x.disabled && x.offsetParent !== null && isEmpty(x) && x.type !== 'date' && x.tagName !== 'SELECT');
    if (el) el.focus();
  });
  document.addEventListener('bms:filled', (e) => {
    markAll(e.target);
    e.target.querySelectorAll('.is-invalid').forEach(x => x.classList.remove('is-invalid'));
    e.target.querySelectorAll('.invalid-msg').forEach(x => x.remove());
  });
  document.addEventListener('reset', (e) => setTimeout(() => markAll(e.target), 0));

  markAll(); addStars();
})();

// ── 숫자 칸: 화살표 단위(step) 때문에 정상 금액이 '틀림'으로 막히지 않게 + 마우스 휠로 값이 바뀌지 않게 ──
(() => {
  const fixStep = (root = document) => root.querySelectorAll('input[type=number]').forEach((el) => {
    if (el.dataset.stepStrict !== undefined) return;
    const st = el.getAttribute('step');
    if (st && st !== 'any' && (Number(st) % 1 !== 0)) el.setAttribute('step', 'any');   // 소수: 6.35% 같은 값 허용
    else if (!st || Number(st) > 1) el.setAttribute('step', '1');                       // 금액·개수: 1원 단위까지 허용
  });
  fixStep();
  document.addEventListener('shown.bs.modal', (e) => fixStep(e.target));
  new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach((n) => { if (n.nodeType === 1) fixStep(n); })))
    .observe(document.body, { childList: true, subtree: true });
  document.addEventListener('wheel', (e) => {
    if (e.target.matches && e.target.matches('input[type=number]') && document.activeElement === e.target) e.target.blur();
  }, { passive: true });
})();

// ════════════════════════════════════════════════════════════════════
// 금액 칸: 입력하는 동안 1,000 단위 콤마 표시 → 저장할 때는 숫자만 보냄
//          연봉 칸 아래에는 월 급여(연봉 ÷ 12, 원 단위 올림)를 바로 표시
// 대상: data-money / data-won 속성, 이름이 금액 계열이거나 칸 이름에 '(원)'·'금액'·'연봉'이 들어간 칸
// ════════════════════════════════════════════════════════════════════
const moneyVal = (v) => { const s = String(v ?? '').replace(/[^\d-]/g, ''); return s && s !== '-' ? Number(s) : null; };
(() => {
  const NAME = /(salary|amount|cost|premium|price|budget|^total$|deposit|^pay_|insured_monthly|pension_monthly|employ_monthly|self_pay|monthly_cost|fuel_cost|toll_cost|repair_cost|sal_amount|ins_premium|^fine|^v\d+$)/i;
  const LABEL = /\(원\)|금액|연봉|공제료|보증금|경조금|포상금|급여|수리비|유류비|통행료/;
  const NOT = /(note|memo|pay_day|_date|rate|year|km|odometer|days|hours|count|copies|dependents|children|notice|alert|interval|useful|^month$|^day$)/i;
  const ANNUAL = /(annual_salary|initial_salary|sal_amount)/i;
  const labelOf = (el) => { const p = el.closest('[class*="col"], .mb-2, .mb-3, td, .field'); return p ? (p.querySelector('label')?.textContent || '') : ''; };
  const isMoney = (el) => {
    if (!el.matches('input') || el.dataset.noMoney !== undefined || ['hidden', 'date', 'checkbox', 'radio', 'file', 'email', 'password'].includes(el.type)) return false;
    if (el.dataset.money !== undefined || el.dataset.won !== undefined) return true;
    if (NOT.test(el.name || '')) return false;
    return NAME.test(el.name || '') || (LABEL.test(labelOf(el)) && !/%|율|연도|일\)|시간/.test(labelOf(el)));
  };
  const fmtDigits = (raw) => { const neg = /^\s*-/.test(raw); const d = String(raw).replace(/[^\d]/g, '').replace(/^0+(?=\d)/, ''); return d ? (neg ? '-' : '') + Number(d).toLocaleString('ko-KR') : (neg ? '-' : ''); };
  const monthlyHint = (el) => {
    if ((!ANNUAL.test(el.name || '') && el.dataset.annual === undefined) || el.dataset.monthly) return;
    let h = el.parentElement.querySelector(':scope > .monthly-hint');
    if (!h) { h = document.createElement('div'); h.className = 'help monthly-hint'; (el.closest('.input-group') || el).after(h); }
    const v = moneyVal(el.value);
    h.innerHTML = v ? `월 급여 <b class="text-dark">${Math.ceil(v / 12).toLocaleString('ko-KR')}원</b> <span class="text-muted">(연봉 ÷ 12)</span>` : '';
  };
  const setup = (el) => {
    if (el.dataset.moneyOn) return;
    el.dataset.moneyOn = '1';
    if (el.type === 'number') { el.type = 'text'; el.removeAttribute('step'); el.removeAttribute('min'); el.removeAttribute('max'); }
    el.setAttribute('inputmode', 'numeric'); el.setAttribute('autocomplete', 'off');
    el.classList.add('money-input');
    if (el.value) el.value = fmtDigits(el.value);
    monthlyHint(el);
  };
  const scan = (root = document) => root.querySelectorAll('input').forEach((el) => { if (isMoney(el)) setup(el); });
  scan();
  document.addEventListener('shown.bs.modal', (e) => scan(e.target));
  new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach((n) => { if (n.nodeType === 1) scan(n); }))).observe(document.body, { childList: true, subtree: true });
  // 값이 코드로 바뀐 경우(수정 창 채우기 등)도 콤마로
  document.addEventListener('bms:filled', (e) => e.target.querySelectorAll('.money-input').forEach((el) => { el.value = fmtDigits(el.value); monthlyHint(el); }));
  document.addEventListener('shown.bs.modal', (e) => e.target.querySelectorAll('.money-input').forEach((el) => { el.value = fmtDigits(el.value); monthlyHint(el); }));
  // 입력 중: 커서 위치를 지키며 콤마
  document.addEventListener('input', (e) => {
    const el = e.target;
    if (!el.classList || !el.classList.contains('money-input')) return;
    const before = el.value.slice(0, el.selectionStart ?? el.value.length).replace(/[^\d]/g, '').length;
    el.value = fmtDigits(el.value);
    let pos = 0, seen = 0;
    while (pos < el.value.length && seen < before) { if (/\d/.test(el.value[pos])) seen++; pos++; }
    try { el.setSelectionRange(pos, pos); } catch (_) {}
    monthlyHint(el);
  }, true);
  // 저장: 숫자만 보내고, 저장이 취소되면 다시 콤마
  document.addEventListener('submit', (e) => {
    const f = e.target, els = [...f.querySelectorAll('.money-input')];
    els.forEach((el) => { el.value = String(el.value).replace(/[^\d-]/g, ''); });
    setTimeout(() => { if (e.defaultPrevented) els.forEach((el) => { el.value = fmtDigits(el.value); }); }, 0);
  }, true);
})();

// ════════════════════════════════════════════════════════════════════
// 급여·연말정산: 급여항목(월)을 넣으면 연봉 칸 = 합계 × 12 (자동으로 넣되 고칠 수 있음)
//                건강보험 보수월액을 넣으면 국민연금·고용보험 칸도 같은 값으로 (직접 고친 칸은 그대로 둠)
// ════════════════════════════════════════════════════════════════════
(() => {
  const setVal = (el, v) => { el.value = v ? String(v) : ''; el.dispatchEvent(new Event('input', { bubbles: true })); };
  document.addEventListener('input', (e) => {
    const el = e.target;
    if (!el.matches || !e.isTrusted) return;                   // 코드가 넣은 값에는 다시 반응하지 않음
    const form = el.form;
    if (!form) return;
    if (el.matches('input[name^="pay_"]:not([name="pay_note"])')) {
      const annual = form.querySelector('[data-pay-annual]');
      if (!annual) return;
      let sum = 0;
      form.querySelectorAll('input[name^="pay_"]:not([name="pay_note"])').forEach((i) => { sum += moneyVal(i.value) || 0; });
      if (sum > 0) { setVal(annual, sum * 12); annual.dataset.auto = '1'; }
    } else if (el.name === 'insured_monthly') {
      ['pension_monthly', 'employ_monthly'].forEach((n) => {
        const o = form.querySelector(`input[name="${n}"]`);
        if (o && (o.dataset.auto === '1' || !o.value)) { setVal(o, moneyVal(el.value)); o.dataset.auto = '1'; }
      });
    } else if (el.name === 'pension_monthly' || el.name === 'employ_monthly' || el.dataset.payAnnual !== undefined) {
      el.dataset.auto = '';                                     // 직접 고친 칸은 자동으로 덮어쓰지 않음(연봉은 급여항목을 다시 고치면 새로 계산)
    }
  });
})();

// ════════════════════════════════════════════════════════════════════
// 입력 화면 키보드: Enter = 다음 칸으로 (저장하지 않음), F8 = 저장
// 대상: 칸이 2개 이상인 저장(post) 양식. 로그인·인증 화면, 검색·필터, 한 칸짜리 양식은 예전 그대로.
// ════════════════════════════════════════════════════════════════════
(() => {
  if (/^\/(login|logout|account|leave-plan|sign|q\/|vq\/)/.test(location.pathname) || !document.getElementById('sidebar')) return;
  const FIELD = 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([disabled]):not([readonly]), select:not([disabled]), textarea:not([disabled]):not([readonly])';
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const fields = (form) => [...form.querySelectorAll(FIELD)].filter(visible);
  const isEntry = (form) => !!form && (form.getAttribute('method') || '').toLowerCase() === 'post'
    && form.dataset.enterSubmit === undefined && fields(form).length >= 2;
  const saveBtn = (form) => [...form.querySelectorAll('button:not([type=button]), input[type=submit]')]
    .concat(form.id ? [...document.querySelectorAll(`button[form="${form.id}"]:not([type=button])`)] : [])
    .filter((b) => visible(b) && !b.disabled && !b.name)[0] || null;      // name 이 있는 버튼(삭제·반려 등)은 저장으로 보지 않음

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229 || e.ctrlKey || e.altKey || e.metaKey) return;
    const el = e.target;
    if (!el.matches || !el.matches('input, select') || ['submit', 'button', 'file'].includes(el.type)) return;   // 글 상자(textarea)는 줄바꿈 그대로
    const form = el.form;
    if (!isEntry(form)) return;
    e.preventDefault();
    const list = fields(form), i = list.indexOf(el);
    const next = list[i + (e.shiftKey ? -1 : 1)];
    if (next) { next.focus(); if (next.select && next.type !== 'date') { try { next.select(); } catch (_) {} } }
    else { const b = saveBtn(form); if (b) b.focus(); }                   // 마지막 칸에서는 저장 버튼으로 (눌러야 저장)
  });

  const target = () => {
    const modal = document.querySelector('.modal.show');
    const scope = modal || document;
    const cur = document.activeElement && document.activeElement.form;
    if (cur && isEntry(cur) && scope.contains(cur)) return cur;
    const cands = [...scope.querySelectorAll('form')].filter((f) => visible(f) && isEntry(f) && saveBtn(f));
    return cands.length === 1 ? cands[0] : null;                          // 어느 양식인지 분명할 때만
  };
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'F8' || e.ctrlKey || e.altKey || e.metaKey) return;
    const form = target();
    if (!form) return;
    e.preventDefault();
    const b = saveBtn(form);
    if (!b) return;
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();   // 고치던 칸의 값 확정
    form.requestSubmit ? form.requestSubmit(b.form === form && b.matches('button, input') ? b : undefined) : b.click();
  });

  // 날짜 칸: 연도 4자리를 치면 바로 월로, 월을 치면 일로 넘어가게 (연도를 4자리로 제한하면 브라우저가 알아서 넘김)
  const dates = (root = document) => root.querySelectorAll('input[type=date]:not([max])').forEach((d) => d.setAttribute('max', '9999-12-31'));
  dates();
  document.addEventListener('shown.bs.modal', (e) => dates(e.target));
  document.addEventListener('focusin', (e) => { if (e.target.matches && e.target.matches('input[type=date]:not([max])')) e.target.setAttribute('max', '9999-12-31'); });

  // 저장 버튼에 (F8) 표시
  const mark = (root = document) => root.querySelectorAll('form').forEach((f) => {
    if (!isEntry(f)) return;
    const b = saveBtn(f);
    if (b && b.tagName === 'BUTTON' && !b.dataset.f8 && /저장|등록|추가/.test(b.textContent)) {
      b.dataset.f8 = '1'; b.append(' (F8)'); b.title = (b.title ? b.title + ' · ' : '') + '단축키 F8';
    }
  });
  mark();
  document.addEventListener('shown.bs.modal', (e) => mark(e.target));
})();


// 직원 고르는 칸에 이름 찾기: <select data-search> 위에 입력 칸이 생기고, 쓰는 대로 목록이 줄어듭니다
document.querySelectorAll('select[data-search]').forEach((sel) => {
  const all = [...sel.options];
  const box = document.createElement('input');
  box.type = 'search'; box.className = 'form-control form-control-sm sel-search'; box.placeholder = '이름을 써서 찾기';
  box.autocomplete = 'off'; box.setAttribute('aria-label', '이름을 써서 찾기');
  const none = new Option('찾는 이름이 없습니다', ''); none.disabled = true;
  sel.parentNode.insertBefore(box, sel);
  const apply = () => {
    const q = box.value.trim().toLowerCase(), keep = sel.value;
    const hit = all.filter((o) => !q || (o.value && o.text.toLowerCase().includes(q)));
    sel.replaceChildren(...(hit.length ? hit : [none]));
    const real = hit.filter((o) => o.value);
    sel.value = real.some((o) => o.value === keep) ? keep : (q && real.length ? real[0].value : (hit.length ? hit[0].value : ''));
    if (!q && all.some((o) => o.value === keep)) sel.value = keep;
    sel.dispatchEvent(new Event('change', { bubbles: true }));
  };
  box.addEventListener('input', apply);
  box.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); sel.focus(); } });   // Enter 로 저장되지 않게
  const modal = sel.closest('.modal');
  if (modal) modal.addEventListener('hidden.bs.modal', () => { box.value = ''; sel.replaceChildren(...all); });
  const form = sel.closest('form');                       // 등록·수정 창을 다시 열 때: 찾던 글자를 지우고 전체 목록에서 값이 잡히게
  if (form) form.addEventListener('reset', () => { box.value = ''; sel.replaceChildren(...all); });
});

// ── 서류 탐색기: 폴더 이동, 올리기(끌어다 놓기), 새 폴더, 옮기기, 이름 바꾸기, 지우기, 새로 고침, 미리보기 ──
(() => {
  const wraps = document.querySelectorAll('[data-fxwrap]');
  if (!wraps.length) return;
  const token = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
  const el = (tag, cls, text) => { const x = document.createElement(tag); if (cls) x.className = cls; if (text !== undefined) x.textContent = text; return x; };
  const icon = (name) => el('i', 'bi ' + name);
  const EXT = ['pdf', 'jpg', 'jpeg', 'png', 'webp', 'hwp', 'hwpx', 'doc', 'docx', 'xls', 'xlsx', 'pptx'];

  // 작은 물음 창 (이름 넣기 / 폴더 고르기)
  const ask = (title, field, okText) => new Promise((done) => {
    const d = el('dialog', 'fx-dlg'), f = el('form'); f.method = 'dialog';
    f.append(el('h6', 'mb-2', title), field);
    const row = el('div', 'd-flex gap-2 justify-content-end mt-3'), no = el('button', 'btn btn-sm btn-outline-secondary', '취소'), ok = el('button', 'btn btn-sm btn-primary', okText);
    no.type = 'button'; no.addEventListener('click', () => d.close('no'));
    ok.value = 'ok'; row.append(no, ok); f.append(row); d.append(f); document.body.append(d);
    d.addEventListener('close', () => { const v = d.returnValue === 'ok' ? field.value : null; d.remove(); done(v); });
    d.showModal(); field.focus(); if (field.select) field.select();
  });

  // 미리보기 창
  const preview = (list, at, closed) => {
    const d = el('dialog', 'fx-view'), bar = el('div', 'fx-view-bar'), name = el('b', 'text-truncate'), body = el('div', 'fx-view-body');
    const btn = (ic, title, fn) => { const b = el('button', 'btn btn-sm btn-outline-light'); b.type = 'button'; b.title = title; b.append(icon(ic)); b.addEventListener('click', fn); return b; };
    const open = el('a', 'btn btn-sm btn-outline-light text-nowrap', '새 창'), down = el('a', 'btn btn-sm btn-outline-light'); open.target = '_blank'; open.rel = 'noopener'; down.title = '내려받기'; down.append(icon('bi-download'));
    const show = (n) => {
      at = (n + list.length) % list.length;
      const it = list[at], url = '/files/get/' + it.id;
      name.textContent = it.name + (list.length > 1 ? '  (' + (at + 1) + '/' + list.length + ')' : '');
      open.href = url; down.href = url + '?dl=1';
      let v;
      if (it.view === 'img') { v = el('img'); v.alt = it.name; v.src = url; } else { v = el('iframe'); v.title = it.name; v.src = url + '#view=FitH'; }
      body.replaceChildren(v);
    };
    const prev = btn('bi-chevron-left', '이전', () => show(at - 1)), next = btn('bi-chevron-right', '다음', () => show(at + 1));
    prev.hidden = next.hidden = list.length < 2;
    bar.append(name, el('span', 'ms-auto'), prev, next, open, down, btn('bi-x-lg', '닫기 (Esc)', () => d.close()));
    d.append(bar, body); document.body.append(d);
    d.addEventListener('keydown', (e) => { if (e.key === 'ArrowLeft' && list.length > 1) show(at - 1); if (e.key === 'ArrowRight' && list.length > 1) show(at + 1); });
    d.addEventListener('click', (e) => { if (e.target === d) d.close(); });
    d.addEventListener('close', () => { body.replaceChildren(); d.remove(); if (closed) closed(); });
    d.showModal(); show(at);
  };

  wraps.forEach((wrap) => {
    const url = wrap.dataset.url, next = wrap.dataset.next;
    let cur = '', busy = false, menu = null, drag = null, timer = 0, tries = 0;
    const $ = (q) => wrap.querySelector(q), $$ = (q) => [...wrap.querySelectorAll(q)];
    const fail = (m) => { toast(m); };

    const selected = () => $$('.fx-item:not([hidden]) .fx-sel:checked').map((c) => c.closest('.fx-item'));
    const tools = () => {
      const sel = selected(), files = sel.filter((x) => x.dataset.id), dirs = sel.filter((x) => x.dataset.path);
      $$('.fx-item').forEach((x) => x.classList.toggle('fx-on', !!x.querySelector('.fx-sel:checked')));
      $('[data-fx-act=rename]').disabled = sel.length !== 1;
      $('[data-fx-act=move]').disabled = !files.length || dirs.length > 0;
      $('[data-fx-act=trash]').disabled = !sel.length || dirs.length > 1 || (dirs.length === 1 && files.length > 0);
      $('[data-fx-sel]').textContent = sel.length ? sel.length + '개 고름' : '';
    };
    const show = (dir) => {
      if (dir && !$$('.fx-folder').some((x) => x.dataset.path === dir)) dir = '';
      cur = dir;
      let folders = 0, files = 0;
      $$('.fx-sel').forEach((c) => { c.checked = false; });
      $$('.fx-folder').forEach((x) => { x.hidden = x.dataset.parent !== cur; if (!x.hidden) folders++; });
      $$('.fx-file').forEach((x) => { x.hidden = x.dataset.dir !== cur; if (!x.hidden) files++; });
      $('[data-fx-empty]').hidden = folders + files > 0;
      $('[data-fx-up]').disabled = !cur;
      const path = $('[data-fx-path]'), head = path.querySelector('span'), parts = cur ? cur.split('/') : [];
      path.replaceChildren(icon('bi-folder2-open text-warning'), ' ', head, ' ');
      [['', $('[data-fx]').dataset.root], ...parts.map((p, i) => [parts.slice(0, i + 1).join('/'), p])].forEach(([to, label], i, all) => {
        if (i) path.append(' › ');
        const a = el('a'); a.href = '#'; a.dataset.fxGo = to; a.append(i === all.length - 1 ? el('b', '', label) : label); path.append(a);
      });
      $('[data-fx-status]').textContent = '폴더 ' + folders + '개 · 파일 ' + files + '개';
      tools();
    };
    const view = (mode, keep) => {
      const body = $('[data-fx-body]');
      body.classList.toggle('fx-icons', mode !== 'list'); body.classList.toggle('fx-list', mode === 'list');
      $('.fx-head').classList.toggle('fx-on', mode === 'list');
      $$('[data-fx-view]').forEach((b) => b.classList.toggle('active', b.dataset.fxView === mode));
      if (keep) { try { localStorage.setItem('bms_fx_view', mode); } catch (_) {} }
    };
    const init = () => {
      let m = 'icons'; try { m = localStorage.getItem('bms_fx_view') || 'icons'; } catch (_) {}
      view(m === 'list' ? 'list' : 'icons');
      clearTimeout(timer);                                  // NAS 에서 들어오거나 고쳐진 파일이 반영될 때까지 스스로 새로 고침 (최대 3분)
      if ($('[data-fx]').dataset.wait !== '0') { if (tries < 30) timer = setTimeout(poll, 6000); } else tries = 0;
      $$('[data-fx-thumb]').forEach((im) => im.addEventListener('error', () => { im.hidden = true; im.nextElementSibling.hidden = false; }));
      show(cur);
    };
    const poll = () => {                                    // 쓰는 중(고르기·끌기·창 열림)에는 방해하지 않고 다음에
      if (document.hidden || busy || drag || menu || selected().length || document.querySelector('dialog[open]')) { timer = setTimeout(poll, 6000); return; }
      tries++;
      refresh('', true);
    };
    const refresh = async (msg, quiet) => {
      const r = await fetch(url + '/panel' + (next ? '?next=' + encodeURIComponent(next) : ''), { headers: { 'X-FX': '1' } });
      if (!r.ok || r.redirected) { if (!quiet) fail('화면을 새로 고치지 못했습니다. 다시 로그인해 주세요'); return; }
      const note = $('[data-fx-note]').value;
      wrap.innerHTML = await r.text();
      $('[data-fx-note]').value = note;
      init();
      document.querySelectorAll('a[href$="#files"] > .badge').forEach((b) => {      // 화면 위 '서류 N' 단추의 숫자도 맞춤
        const isNew = b.classList.contains('text-bg-warning'), n = $('[data-fx]').dataset[isNew ? 'new' : 'total'];
        b.textContent = isNew ? '새 파일 ' + n : n; if (isNew) b.hidden = n === '0';
      });
      if (msg) toast(msg);
    };
    const run = async (body) => {
      if (busy) return;
      busy = true;
      try {
        const r = await postJSON(url + '/op', body);
        await refresh(r.msg);
      } catch (_) { fail('처리하지 못했습니다. 새로 고침 뒤 다시 해 주세요'); } finally { busy = false; }
    };
    const upload = async (list) => {            // [{file, dir}]
      if (busy || !list.length) return;
      busy = true;
      const note = $('[data-fx-note]').value, st = $('[data-fx-status]'), errs = [];
      let ok = 0;
      for (let i = 0; i < list.length; i++) {
        const { file, dir } = list[i];
        st.textContent = '올리는 중… ' + (i + 1) + '/' + list.length + '  ' + file.name;
        const ext = file.name.includes('.') ? file.name.split('.').pop().toLowerCase() : '';
        if (!EXT.includes(ext)) { errs.push(file.name + ': 올릴 수 없는 종류'); continue; }
        if (file.size > Number(wrap.dataset.max) * 1048576) { errs.push(file.name + ': ' + wrap.dataset.max + 'MB 넘음'); continue; }
        const fd = new FormData(); fd.append('dir', dir); fd.append('note', note); fd.append('files', file);
        try {
          const r = await fetch(url + '/upload', { method: 'POST', headers: { 'X-FX': '1', 'X-CSRF-Token': token() }, body: fd });
          if (!r.ok || r.redirected) throw new Error();
          const j = await r.json();
          if (j.ok) ok++; else errs.push(j.msg.replace(/^ ?·? ?저장하지 못한 파일 — /, ''));
        } catch (_) { errs.push(file.name + ': 올리지 못함'); }
      }
      busy = false;
      $('[data-fx-note]').value = '';
      await refresh(ok ? ok + '개를 올렸습니다' : '');
      if (errs.length) alert('올리지 못한 파일이 있습니다.\n\n' + errs.slice(0, 15).join('\n') + (errs.length > 15 ? '\n… 외 ' + (errs.length - 15) + '개' : ''));
    };
    // 탐색기에서 끌어온 것: 폴더면 안의 파일까지
    const walk = async (entry, dir, out) => {
      if (out.length >= 300) return;
      if (entry.isFile) { out.push({ file: await new Promise((a, b) => entry.file(a, b)), dir }); return; }
      const rd = entry.createReader();
      for (;;) { const some = await new Promise((a, b) => rd.readEntries(a, b)); if (!some.length) break; for (const x of some) await walk(x, (dir ? dir + '/' : '') + entry.name, out); }
    };
    const dropped = async (dt, dir) => {
      const entries = [...dt.items].map((x) => x.webkitGetAsEntry && x.webkitGetAsEntry()).filter(Boolean), plain = [...dt.files], out = [];
      if (!entries.length) return upload(plain.map((file) => ({ file, dir })));
      try { for (const x of entries) await walk(x, dir, out); } catch (_) { return fail('끌어온 파일을 읽지 못했습니다'); }
      return upload(out);
    };

    const act = {
      mkdir: async () => { const i = el('input', 'form-control form-control-sm'); i.maxLength = 60; i.value = '새 폴더'; const v = await ask('새 폴더 이름', i, '만들기'); if (v && v.trim()) run({ op: 'mkdir', dir: cur, name: v }); },
      rename: async (sel) => {
        const x = sel[0], i = el('input', 'form-control form-control-sm'); i.maxLength = 100;
        const ext = x.dataset.id ? (x.dataset.name.match(/\.[^.]+$/) || [''])[0] : '';
        i.value = ext ? x.dataset.name.slice(0, -ext.length) : x.dataset.name;
        const v = await ask(x.dataset.id ? '파일 이름 바꾸기 (' + ext + ' 는 그대로)' : '폴더 이름 바꾸기', i, '바꾸기');
        if (v && v.trim()) run(x.dataset.id ? { op: 'rename', ids: [x.dataset.id], name: v } : { op: 'rename_dir', dir: x.dataset.path, name: v });
      },
      move: async (sel) => {
        const s = el('select', 'form-select form-select-sm');
        [['', $('[data-fx]').dataset.root + ' (맨 위)'], ...$$('.fx-folder').map((x) => [x.dataset.path, x.dataset.path.split('/').join(' › ')])].filter(([p]) => p !== cur).forEach(([p, t]) => { const o = el('option', '', t); o.value = p; s.append(o); });
        if (!s.options.length) { fail('옮길 폴더가 없습니다. 먼저 [새 폴더]를 만드세요'); return; }
        const v = await ask(sel.length + '개를 어느 폴더로 옮길까요?', s, '옮기기');
        if (v !== null) run({ op: 'move', ids: sel.map((x) => x.dataset.id), dir: v });
      },
      trash: (sel) => {
        const x = sel[0], isDir = !!x.dataset.path;
        const q = isDir ? "'" + x.dataset.name + "' 폴더를 안에 든 것까지 모두 지울까요? (파일 " + x.dataset.count + '개)' : (sel.length === 1 ? "'" + x.dataset.name + "' 을(를) 지울까요?" : sel.length + '개를 지울까요?');
        if (confirm(q + "\n\n없애지 않고 보관함의 '_삭제함' 폴더로 옮깁니다.")) run(isDir ? { op: 'trash_dir', dir: x.dataset.path } : { op: 'trash', ids: sel.map((y) => y.dataset.id) });
      },
    };
    const openFile = (x) => {
      if (!x.dataset.view) { location.href = '/files/get/' + x.dataset.id + '?dl=1'; return; }
      const all = $$('.fx-file:not([hidden])[data-view]');
      preview(all.map((y) => ({ id: y.dataset.id, name: y.dataset.name, view: y.dataset.view })), all.indexOf(x), all.some((y) => y.dataset.changed) ? () => refresh('', true) : null);
    };
    const closeMenu = () => { if (menu) { menu.remove(); menu = null; } };

    wrap.addEventListener('click', (e) => {
      closeMenu();
      const t = e.target;
      if (t.closest('.fx-sel')) return;
      const go = t.closest('[data-fx-go]');
      if (go) { e.preventDefault(); show(go.dataset.fxGo); return; }
      if (t.closest('[data-fx-up]')) { show(cur.includes('/') ? cur.slice(0, cur.lastIndexOf('/')) : ''); return; }
      if (t.closest('[data-fx-refresh]')) { refresh('새로 고쳤습니다'); return; }
      const v = t.closest('[data-fx-view]'); if (v) { view(v.dataset.fxView, true); return; }
      const a = t.closest('[data-fx-act]'); if (a) { act[a.dataset.fxAct](selected()); return; }
      const f = t.closest('.fx-folder'); if (f) { show(f.dataset.path); return; }
      const link = t.closest('a.fx-name, a.fx-ico');
      if (link) { e.preventDefault(); openFile(link.closest('.fx-item')); }
    });
    wrap.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.classList.contains('fx-folder')) show(e.target.dataset.path); });
    wrap.addEventListener('change', (e) => {
      if (e.target.closest('.fx-sel')) tools();
      if (e.target.closest('[data-fx-pick]')) { const l = [...e.target.files].map((file) => ({ file, dir: cur })); e.target.value = ''; upload(l); }
    });
    // 오른쪽 단추 메뉴
    wrap.addEventListener('contextmenu', (e) => {
      const body = e.target.closest('[data-fx-body]'); if (!body) return;
      const x = e.target.closest('.fx-item'); if (x && x.classList.contains('fx-isnew')) return;
      e.preventDefault(); closeMenu();
      if (x && !x.querySelector('.fx-sel').checked) { $$('.fx-sel').forEach((c) => { c.checked = false; }); x.querySelector('.fx-sel').checked = true; tools(); }
      const sel = x ? selected() : [], items = [];
      if (x && sel.length === 1) items.push([x.dataset.path ? '열기' : (x.dataset.view ? '미리보기' : '열기 (내려받기)'), 'bi-eye', () => (x.dataset.path ? show(x.dataset.path) : openFile(x))]);
      if (x && sel.length === 1 && x.dataset.id) items.push(['내려받기', 'bi-download', () => { location.href = '/files/get/' + x.dataset.id + '?dl=1'; }]);
      if (x && sel.length === 1 && x.dataset.changed) items.push(['NAS에서 고친 내용 반영', 'bi-arrow-repeat', () => run({ op: 'accept', ids: [x.dataset.id] })]);
      if (x) [['rename', '이름 바꾸기', 'bi-input-cursor-text'], ['move', '옮기기', 'bi-folder-symlink'], ['trash', '지우기', 'bi-trash']].forEach(([k, t, ic]) => { if (!$('[data-fx-act=' + k + ']').disabled) items.push([t, ic, () => act[k](sel)]); });
      if (!x) items.push(['새 폴더', 'bi-folder-plus', act.mkdir], ['서류 올리기', 'bi-cloud-arrow-up', () => $('[data-fx-pick]').click()], ['새로 고침', 'bi-arrow-clockwise', () => refresh('새로 고쳤습니다')]);
      menu = el('div', 'fx-menu');
      items.forEach(([t, ic, fn]) => { const b = el('button'); b.type = 'button'; b.append(icon(ic + ' me-2'), t); b.addEventListener('click', () => { closeMenu(); fn(); }); menu.append(b); });
      document.body.append(menu);
      menu.style.left = Math.min(e.clientX, innerWidth - menu.offsetWidth - 8) + 'px'; menu.style.top = Math.min(e.clientY, innerHeight - menu.offsetHeight - 8) + 'px';
    });
    document.addEventListener('click', closeMenu); addEventListener('scroll', closeMenu, true);

    // 끌어다 놓기
    wrap.addEventListener('dragstart', (e) => {
      const x = e.target.closest('.fx-file[data-id]'); if (!x) return;
      const sel = selected().filter((y) => y.dataset.id);
      drag = sel.includes(x) ? sel.map((y) => y.dataset.id) : [x.dataset.id];
      e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', '');
    });
    wrap.addEventListener('dragend', () => { drag = null; clear(); });
    const clear = () => $$('.fx-drop').forEach((x) => x.classList.remove('fx-drop'));
    const spot = (e) => {
      const f = e.target.closest('.fx-folder'); if (f) return [f, f.dataset.path];
      const g = e.target.closest('[data-fx-go]'); if (g) return [g, g.dataset.fxGo];
      const u = e.target.closest('[data-fx-up]'); if (u && cur) return [u, cur.includes('/') ? cur.slice(0, cur.lastIndexOf('/')) : ''];
      return [$('[data-fx]'), cur];
    };
    wrap.addEventListener('dragover', (e) => {
      const files = [...e.dataTransfer.types].includes('Files');
      if (!drag && !files) return;
      const [x, dir] = spot(e); clear();
      if (drag && dir === cur) return;
      e.preventDefault(); e.dataTransfer.dropEffect = drag ? 'move' : 'copy'; x.classList.add('fx-drop');
    });
    wrap.addEventListener('dragleave', (e) => { if (!wrap.contains(e.relatedTarget)) clear(); });
    wrap.addEventListener('drop', (e) => {
      const [, dir] = spot(e); clear();
      if (drag) { e.preventDefault(); const ids = drag; drag = null; if (dir !== cur) run({ op: 'move', ids, dir }); return; }
      if (e.dataTransfer.files.length || e.dataTransfer.items.length) { e.preventDefault(); dropped(e.dataTransfer, dir); }
    });
    init();
  });
  // 창 밖에 잘못 놓았을 때 브라우저가 그 파일을 열어 버리지 않게
  ['dragover', 'drop'].forEach((n) => addEventListener(n, (e) => { if ([...(e.dataTransfer?.types || [])].includes('Files') && !e.target.closest?.('[data-fxwrap], input[type=file]')) e.preventDefault(); }));
})();

// ── 연봉 가리기: [연봉 보기] 를 누르면 보이고, 10분 뒤 또는 다시 누르면 가려짐 ──
(() => {
  const btns = [...document.querySelectorAll('[data-sal-toggle]')];
  if (!btns.length) return;
  let timer = 0;
  const set = (on) => {
    document.querySelectorAll('table').forEach((t) => { if (t.querySelector('th.sal')) t.classList.toggle('sal-hide', !on); });
    btns.forEach((b) => {
      b.querySelector('i').className = 'bi ' + (on ? 'bi-eye-slash' : 'bi-eye');
      const s = b.querySelector('span'); if (s) s.textContent = on ? '연봉 숨기기' : '연봉 보기';
    });
    if (!on) document.querySelectorAll('.xf-pop').forEach((p) => p.remove());      // 금액 목록이 보이는 필터 창도 닫음
    clearTimeout(timer);
    if (on) timer = setTimeout(() => set(false), 10 * 60 * 1000);
  };
  btns.forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const on = !!document.querySelector('table.sal-hide');
    set(on);
    if (on) postJSON('/hr/api/salary-view', { page: b.dataset.salToggle }).catch(() => {});
  }));
})();
