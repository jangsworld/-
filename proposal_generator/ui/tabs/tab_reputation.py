"""
Tab: 신인도
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os

from core import database as db
from core.calculator import calc_reputation_score


class TabReputation(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._build_ui()

    def _build_ui(self):
        # ── 입찰참가자격 제한 현황 ──────────────────────────────
        restrict_frame = ttk.LabelFrame(self, text='입찰참가자격 제한 현황', padding=12)
        restrict_frame.pack(fill=tk.X, padx=16, pady=12)

        self.restriction_var = tk.StringVar(value='none')

        ttk.Radiobutton(restrict_frame, text='제한사실 없음 (만점)',
                        variable=self.restriction_var, value='none',
                        command=self._update_score).pack(anchor='w', pady=4)
        ttk.Radiobutton(restrict_frame, text='제한사실 있음 (1.4점)',
                        variable=self.restriction_var, value='restricted',
                        command=self._update_score).pack(anchor='w', pady=4)
        ttk.Radiobutton(restrict_frame, text='증빙자료 미제출 (1.4점)',
                        variable=self.restriction_var, value='no_docs',
                        command=self._update_score).pack(anchor='w', pady=4)

        score_row = ttk.Frame(restrict_frame)
        score_row.pack(fill=tk.X, pady=8)
        ttk.Label(score_row, text='취득 점수:').pack(side=tk.LEFT, padx=4)
        self.score_label = ttk.Label(score_row, text='-', foreground='blue',
                                     font=('', 12, 'bold'))
        self.score_label.pack(side=tk.LEFT, padx=4)

        # ── 증빙서류 ───────────────────────────────────────────
        docs_frame = ttk.LabelFrame(self, text='증빙서류', padding=12)
        docs_frame.pack(fill=tk.X, padx=16, pady=8)

        self._doc_paths = {}
        doc_items = [
            ('경쟁입찰참가자격등록증', 'reg_cert'),
            ('부정당업자 제재처분 확인서', 'sanction_cert'),
        ]
        for row_idx, (label, key) in enumerate(doc_items):
            ttk.Label(docs_frame, text=label + ':').grid(
                row=row_idx, column=0, sticky='e', padx=8, pady=6)
            path_var = tk.StringVar(value='(선택된 파일 없음)')
            self._doc_paths[key] = path_var
            ttk.Label(docs_frame, textvariable=path_var, width=48,
                      anchor='w', foreground='gray').grid(
                row=row_idx, column=1, padx=8, pady=6, sticky='w')
            ttk.Button(docs_frame, text='파일 선택',
                       command=lambda k=key: self._select_doc(k)).grid(
                row=row_idx, column=2, padx=4)

        # ── Save button ────────────────────────────────────────
        ttk.Button(self, text='저장', command=self.save).pack(pady=16)

    def _update_score(self, event=None):
        if not self.app.current_bid_id:
            return
        bid = db.get_bid(self.app.current_bid_id)
        if not bid:
            return
        status = self.restriction_var.get()
        max_score = bid.get('score_reputation', 2.0)
        score = calc_reputation_score(status, max_score)
        self.score_label.config(text=f'{score:.2f}점 / {max_score:.1f}점')

    def _select_doc(self, key):
        path = filedialog.askopenfilename(
            title='증빙서류 선택',
            filetypes=[('All files', '*.*'), ('PDF files', '*.pdf')]
        )
        if path:
            self._doc_paths[key].set(os.path.abspath(path))

    def refresh(self):
        if not self.app.current_bid_id:
            self.restriction_var.set('none')
            self.score_label.config(text='-')
            return

        bid = db.get_bid(self.app.current_bid_id)
        if not bid:
            return

        status = bid.get('restriction_status', 'none') or 'none'
        self.restriction_var.set(status)
        self._update_score()

    def save(self):
        if not self.app.current_bid_id:
            messagebox.showinfo('알림', '공고를 먼저 선택하세요.')
            return
        status = self.restriction_var.get()
        db.update_bid(self.app.current_bid_id, restriction_status=status)
        self.app.update_status()
        messagebox.showinfo('저장', '신인도 정보가 저장되었습니다.')
