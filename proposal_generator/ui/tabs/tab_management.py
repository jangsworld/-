"""
Tab: 경영실태 (신용평가등급)
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os

from core import database as db
from core.calculator import calc_management_score

RATING_LIST = [
    'AAA', 'AA+', 'AA', 'AA-', 'A+', 'A', 'A-',
    'BBB+', 'BBB', 'BBB-', 'BB+', 'BB', 'BB-',
    'B+', 'B', 'B-', 'CCC', 'CC', 'C', 'D'
]


class TabManagement(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._cert_path = ''
        self._build_ui()

    def _build_ui(self):
        # ── 신용평가등급 정보 ─────────────────────────────────────
        info_frame = ttk.LabelFrame(self, text='신용평가등급 정보', padding=12)
        info_frame.pack(fill=tk.X, padx=16, pady=12)

        ttk.Label(info_frame, text='등급 선택:').grid(
            row=0, column=0, sticky='e', padx=8, pady=6)
        self.rating_var = tk.StringVar(value='BBB+')
        self.rating_combo = ttk.Combobox(info_frame, textvariable=self.rating_var,
                                         values=RATING_LIST, state='readonly', width=12)
        self.rating_combo.grid(row=0, column=1, sticky='w', padx=8, pady=6)
        self.rating_combo.bind('<<ComboboxSelected>>', self._update_score)

        ttk.Label(info_frame, text='취득 점수:').grid(
            row=1, column=0, sticky='e', padx=8, pady=6)
        self.score_label = ttk.Label(info_frame, text='-', foreground='blue',
                                     font=('', 12, 'bold'))
        self.score_label.grid(row=1, column=1, sticky='w', padx=8, pady=6)

        ttk.Label(info_frame, text='증빙서류:').grid(
            row=2, column=0, sticky='e', padx=8, pady=6)
        self.cert_path_label = ttk.Label(info_frame, text='(선택된 파일 없음)',
                                         width=50, anchor='w', foreground='gray')
        self.cert_path_label.grid(row=2, column=1, sticky='w', padx=8, pady=6)

        btn_frame = ttk.Frame(info_frame)
        btn_frame.grid(row=3, column=1, sticky='w', padx=8, pady=4)
        ttk.Button(btn_frame, text='파일 선택', command=self._select_file).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text='폴더에서 자동 검색', command=self._auto_search).pack(
            side=tk.LEFT, padx=4)

        # ── Save button ────────────────────────────────────────────
        ttk.Button(self, text='저장', command=self.save).pack(pady=16)

    def _update_score(self, event=None):
        if not self.app.current_bid_id:
            return
        bid = db.get_bid(self.app.current_bid_id)
        if not bid:
            return
        rating = self.rating_var.get()
        max_score = bid.get('score_management', 6.0)
        score = calc_management_score(rating, max_score)
        self.score_label.config(text=f'{score:.2f}점 / {max_score:.1f}점')

    def _select_file(self):
        path = filedialog.askopenfilename(
            title='증빙서류 선택',
            filetypes=[('All files', '*.*'), ('PDF files', '*.pdf'),
                       ('Image files', '*.png *.jpg *.jpeg')]
        )
        if path:
            self._cert_path = os.path.abspath(path)
            self.cert_path_label.config(text=self._cert_path, foreground='black')

    def _auto_search(self):
        folder = filedialog.askdirectory(title='검색할 폴더 선택')
        if not folder:
            return
        keywords = ['신용평가등급', '기업신용', '신용평가', 'credit']
        found = []
        for fname in os.listdir(folder):
            lower_name = fname.lower()
            if any(kw.lower() in lower_name for kw in keywords):
                found.append(os.path.join(folder, fname))
        if found:
            self._cert_path = os.path.abspath(found[0])
            self.cert_path_label.config(text=self._cert_path, foreground='black')
            if len(found) > 1:
                messagebox.showinfo('자동 검색',
                                    f'{len(found)}개 파일을 찾았습니다.\n첫 번째 파일을 선택했습니다:\n{found[0]}')
        else:
            messagebox.showinfo('자동 검색', '해당 키워드로 파일을 찾지 못했습니다.')

    def refresh(self):
        if not self.app.current_bid_id:
            self.rating_var.set('BBB+')
            self.score_label.config(text='-')
            self.cert_path_label.config(text='(선택된 파일 없음)', foreground='gray')
            self._cert_path = ''
            return

        bid = db.get_bid(self.app.current_bid_id)
        if not bid:
            return

        rating = bid.get('credit_rating', 'BBB+') or 'BBB+'
        self.rating_var.set(rating)
        self._update_score()

    def save(self):
        if not self.app.current_bid_id:
            messagebox.showinfo('알림', '공고를 먼저 선택하세요.')
            return
        rating = self.rating_var.get()
        db.update_bid(self.app.current_bid_id, credit_rating=rating)
        self.app.update_status()
        messagebox.showinfo('저장', '경영실태 정보가 저장되었습니다.')
