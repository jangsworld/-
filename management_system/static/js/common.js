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
  const NAME = /(salary|amount|cost|premium|price|budget|^total$|deposit|^pay_|insured_monthly|self_pay|monthly_cost|fuel_cost|toll_cost|repair_cost|sal_amount|ins_premium|^fine|^v\d+$)/i;
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
