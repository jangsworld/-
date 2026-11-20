/* 글씨 편집기 (굵게·기울임·밑줄·색·형광펜·크기·목록). templates/_rte.html 의 .rte 묶음에 붙는다.
   서버가 저장·표시 때 허용한 모양만 남기므로 여기서는 편집만 맡는다. */
(() => {
  document.querySelectorAll('.rte').forEach((box) => {
    const ed = box.querySelector('.rte-body');
    const input = box.querySelector('input[type=hidden]');
    let saved = null;
    const keep = () => {
      const s = window.getSelection();
      if (s.rangeCount && ed.contains(s.anchorNode)) saved = s.getRangeAt(0).cloneRange();
    };
    const restore = () => {
      ed.focus();
      if (!saved) return;
      const s = window.getSelection();
      s.removeAllRanges();
      s.addRange(saved);
    };
    const run = (cmd, val) => {
      restore();
      document.execCommand('styleWithCSS', false, true);
      document.execCommand(cmd, false, val === undefined ? null : val);
      keep();
      input.value = ed.innerHTML;
    };
    ed.addEventListener('keyup', keep);
    ed.addEventListener('mouseup', keep);
    ed.addEventListener('blur', keep);
    ed.addEventListener('input', () => { input.value = ed.innerHTML; });
    // 다른 곳에서 붙여 넣을 때는 글자만 (엉뚱한 모양·그림이 딸려 오지 않게)
    ed.addEventListener('paste', (e) => {
      e.preventDefault();
      const t = (e.clipboardData || window.clipboardData).getData('text/plain');
      document.execCommand('insertText', false, t);
    });
    box.querySelectorAll('button[data-cmd]').forEach((b) => {
      b.addEventListener('mousedown', (e) => e.preventDefault());      // 누를 때 고른 글자가 풀리지 않게
      b.addEventListener('click', () => run(b.dataset.cmd, b.dataset.val));
    });
    box.querySelectorAll('select[data-cmd]').forEach((s) => s.addEventListener('change', () => {
      if (s.value) run(s.dataset.cmd, s.value);
      s.value = '';
    }));
    box.querySelectorAll('input[type=color][data-cmd]').forEach((c) => c.addEventListener('input', () => run(c.dataset.cmd, c.value)));
    const form = box.closest('form');
    if (form) {
      form.addEventListener('submit', (e) => {
        input.value = ed.innerHTML;
        if (!ed.textContent.trim() && !e.submitter?.name) { e.preventDefault(); alert('내용을 입력하세요.'); }
      });
    }
    input.value = ed.innerHTML;
  });
})();
