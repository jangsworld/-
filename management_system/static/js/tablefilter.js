/* 엑셀처럼 표 머리글에서 정렬·필터 (<table data-excel>).
   - 머리글의 ▾ 를 누르면: 오름차순/내림차순, 값 검색, 값 체크(모두 선택), 적용/지우기
   - 칸의 정렬·필터 값은 data-v 가 있으면 그것을, 없으면 보이는 글자를 씁니다.
   - 머리글에 data-nofilter 가 있으면 그 칸은 건너뜁니다.
   - 보이는 행 수는 [data-excel-count] 요소에 표시합니다. */
(function () {
  const cellVal = (td) => (td ? (td.dataset.v ?? td.innerText).trim() : '');
  const num = (s) => { const t = s.replace(/[,원\s]/g, ''); return /^-?\d+(\.\d+)?$/.test(t) ? parseFloat(t) : null; };
  const cmp = (a, b) => {
    const x = num(a), y = num(b);
    if (x !== null && y !== null) return x - y;
    if (!a) return 1; if (!b) return -1;                        // 빈 값은 항상 뒤로
    return a.localeCompare(b, 'ko', { numeric: true });
  };

  function setup(table) {
    const tbody = table.tBodies[0];
    const rows = [...tbody.rows].filter(r => r.cells.length > 1);
    if (!rows.length) return;
    const heads = [...table.tHead.rows[0].cells];
    const filters = {};                                          // 칸 번호 → 보일 값 Set
    let sort = null;                                             // {col, dir}
    const counter = document.querySelector(table.dataset.excelCount || '[data-excel-count]');
    let pop = null;

    const apply = () => {
      let shown = 0;
      rows.forEach(r => {
        const ok = Object.entries(filters).every(([c, set]) => set.has(cellVal(r.cells[c])));
        r.hidden = !ok; if (ok) shown++;
      });
      if (sort) {
        const sorted = [...rows].sort((a, b) => cmp(cellVal(a.cells[sort.col]), cellVal(b.cells[sort.col])) * sort.dir);
        sorted.forEach(r => tbody.appendChild(r));
      }
      heads.forEach((th, i) => {
        const b = th.querySelector('.xf-btn'); if (!b) return;
        b.classList.toggle('on', !!filters[i] || (sort && sort.col === i));
        b.textContent = sort && sort.col === i ? (sort.dir > 0 ? '▲' : '▼') : (filters[i] ? '⏷' : '▾');
      });
      const u = table.dataset.excelUnit || '건';
      if (counter) counter.textContent = Object.keys(filters).length ? `${shown}${u} / 전체 ${rows.length}${u}` : `${rows.length}${u}`;
    };

    const close = () => { if (pop) { pop.remove(); pop = null; } };

    const open = (i, btn) => {
      close();
      const values = [...new Set(rows.map(r => cellVal(r.cells[i])))].sort(cmp);
      const cur = filters[i];
      pop = document.createElement('div');
      pop.className = 'xf-pop card shadow';
      pop.innerHTML = `
        <div class="list-group list-group-flush small">
          <button type="button" class="list-group-item list-group-item-action" data-s="1">↑ 오름차순 정렬 (ㄱ→ㅎ, 작은 값 먼저)</button>
          <button type="button" class="list-group-item list-group-item-action" data-s="-1">↓ 내림차순 정렬 (ㅎ→ㄱ, 큰 값 먼저)</button>
        </div>
        <div class="p-2 border-top">
          <input class="form-control form-control-sm mb-1" placeholder="값 검색">
          <label class="d-block small fw-semibold"><input type="checkbox" class="form-check-input me-1" data-all> (모두 선택)</label>
          <div class="xf-vals"></div>
          <div class="d-flex gap-1 mt-2"><button type="button" class="btn btn-sm btn-primary flex-fill" data-ok>적용</button>
            <button type="button" class="btn btn-sm btn-outline-secondary" data-clear>지우기</button></div>
        </div>`;
      const box = pop.querySelector('.xf-vals');
      values.forEach(v => {
        const l = document.createElement('label'); l.className = 'd-block small text-truncate';
        const c = document.createElement('input'); c.type = 'checkbox'; c.className = 'form-check-input me-1'; c.value = v;
        c.checked = !cur || cur.has(v);
        l.append(c, document.createTextNode(v || '(빈 값)')); box.appendChild(l);
      });
      const boxes = () => [...box.querySelectorAll('input')];
      const all = pop.querySelector('[data-all]');
      const syncAll = () => { const vis = boxes().filter(c => !c.parentElement.hidden); all.checked = vis.every(c => c.checked); };
      syncAll();
      all.addEventListener('change', () => boxes().filter(c => !c.parentElement.hidden).forEach(c => c.checked = all.checked));
      box.addEventListener('change', syncAll);
      pop.querySelector('input:not([type])').addEventListener('input', (e) => {
        const q = e.target.value.trim().toLowerCase();
        boxes().forEach(c => { const hit = !q || c.value.toLowerCase().includes(q); c.parentElement.hidden = !hit; c.checked = hit; });
        syncAll();
      });
      pop.querySelectorAll('[data-s]').forEach(b => b.addEventListener('click', () => { sort = { col: i, dir: +b.dataset.s }; close(); apply(); }));
      pop.querySelector('[data-ok]').addEventListener('click', () => {
        const sel = new Set(boxes().filter(c => c.checked).map(c => c.value));
        if (sel.size === values.length) delete filters[i]; else filters[i] = sel;
        close(); apply();
      });
      pop.querySelector('[data-clear]').addEventListener('click', () => { delete filters[i]; if (sort && sort.col === i) sort = null; close(); apply(); });
      document.body.appendChild(pop);
      const r = btn.getBoundingClientRect();
      pop.style.top = `${window.scrollY + r.bottom + 4}px`;
      pop.style.left = `${Math.max(8, Math.min(window.scrollX + r.left, window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 8))}px`;
      pop.querySelector('input:not([type])').focus();
    };

    heads.forEach((th, i) => {
      if (th.hasAttribute('data-nofilter')) return;
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'xf-btn'; b.textContent = '▾'; b.title = '정렬·필터';
      b.addEventListener('click', (e) => { e.stopPropagation(); const same = pop && pop.dataset.col === String(i); close(); if (!same) { open(i, b); pop.dataset.col = i; } });
      th.classList.add('xf-th'); th.appendChild(b);
    });
    document.addEventListener('click', (e) => { if (pop && !pop.contains(e.target)) close(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    apply();
  }

  document.querySelectorAll('table[data-excel]').forEach(setup);
})();
