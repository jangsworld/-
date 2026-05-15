"""
Tab: 공고 설정
"""
import tkinter as tk
from tkinter import ttk, messagebox
import json

from core import database as db


class TabConfig(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._build_ui()

    def _build_ui(self):
        canvas = tk.Canvas(self, borderwidth=0)
        scrollbar = ttk.Scrollbar(self, orient='vertical', command=canvas.yview)
        self.scrollable_frame = ttk.Frame(canvas)

        self.scrollable_frame.bind(
            '<Configure>',
            lambda e: canvas.configure(scrollregion=canvas.bbox('all'))
        )

        canvas.create_window((0, 0), window=self.scrollable_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        frame = self.scrollable_frame

        # ── 기본 정보 ──────────────────────────────────────────────
        info_frame = ttk.LabelFrame(frame, text='공고 기본 정보', padding=10)
        info_frame.pack(fill=tk.X, padx=16, pady=8)

        fields = [
            ('공고명', 'bid_name'),
            ('공고번호', 'bid_number'),
            ('발주기관', 'client'),
            ('입찰공고일 (YYYY.MM.DD)', 'bid_date'),
            ('회사명', 'company_name'),
            ('사업비 (VAT포함, 원)', 'bid_amount'),
        ]

        self._vars = {}
        for row_idx, (label_text, key) in enumerate(fields):
            ttk.Label(info_frame, text=label_text + ':').grid(
                row=row_idx, column=0, sticky='e', padx=8, pady=4
            )
            var = tk.StringVar()
            self._vars[key] = var
            entry = ttk.Entry(info_frame, textvariable=var, width=45)
            entry.grid(row=row_idx, column=1, sticky='w', padx=8, pady=4)
            if key == 'bid_amount':
                entry.bind('<FocusOut>', self._format_amount)

        # ── 배점 설정 ──────────────────────────────────────────────
        score_frame = ttk.LabelFrame(frame, text='배점 설정', padding=10)
        score_frame.pack(fill=tk.X, padx=16, pady=8)

        score_fields = [
            ('경영실태 배점', 'score_management', 6.0),
            ('수행경험 배점', 'score_experience', 6.0),
            ('기술인력 배점', 'score_technician', 6.0),
            ('신인도 배점', 'score_reputation', 2.0),
        ]

        self._score_vars = {}
        for row_idx, (label_text, key, default) in enumerate(score_fields):
            ttk.Label(score_frame, text=label_text + ':').grid(
                row=row_idx, column=0, sticky='e', padx=8, pady=4
            )
            var = tk.StringVar(value=str(default))
            self._score_vars[key] = var
            spinbox = ttk.Spinbox(score_frame, from_=0.0, to=20.0,
                                  increment=0.5, textvariable=var, width=8)
            spinbox.grid(row=row_idx, column=1, sticky='w', padx=8, pady=4)
            spinbox.bind('<KeyRelease>', self._update_total_score)
            spinbox.bind('<<Increment>>', self._update_total_score)
            spinbox.bind('<<Decrement>>', self._update_total_score)

        ttk.Label(score_frame, text='총 배점 합계:').grid(
            row=len(score_fields), column=0, sticky='e', padx=8, pady=4
        )
        self.total_score_label = ttk.Label(score_frame, text='20.0점',
                                           foreground='blue')
        self.total_score_label.grid(row=len(score_fields), column=1,
                                    sticky='w', padx=8, pady=4)

        # ── 기술인력 평가 기준 ─────────────────────────────────────
        tech_frame = ttk.LabelFrame(frame, text='기술인력 평가 기준', padding=10)
        tech_frame.pack(fill=tk.X, padx=16, pady=8)

        # Retention thresholds
        ttk.Label(tech_frame, text='보유인력 기준점수 (원점수 이상 → 배점비율)',
                  font=('', 10, 'bold')).grid(row=0, column=0, columnspan=6,
                                              sticky='w', padx=4, pady=4)

        headers = ['기준점수', '비율', '기준점수', '비율', '기준점수', '비율']
        for ci, h in enumerate(headers):
            ttk.Label(tech_frame, text=h, width=9).grid(row=1, column=ci, padx=2)

        self._ret_thresh_vars = []
        defaults_ret = [[40, 1.0], [30, 0.8], [20, 0.6], [10, 0.4]]
        for i, (min_v, frac) in enumerate(defaults_ret):
            col_base = (i % 3) * 2
            row_base = 2 + i // 3
            v_min = tk.StringVar(value=str(min_v))
            v_frac = tk.StringVar(value=str(frac))
            self._ret_thresh_vars.append((v_min, v_frac))
            ttk.Entry(tech_frame, textvariable=v_min, width=8).grid(
                row=row_base, column=col_base, padx=2, pady=2)
            ttk.Entry(tech_frame, textvariable=v_frac, width=8).grid(
                row=row_base, column=col_base + 1, padx=2, pady=2)

        ttk.Button(tech_frame, text='+ 행 추가', command=self._add_ret_row).grid(
            row=10, column=0, columnspan=2, pady=4, sticky='w'
        )

        ttk.Separator(tech_frame, orient='horizontal').grid(
            row=11, column=0, columnspan=6, sticky='ew', pady=8
        )

        # Deployment thresholds
        ttk.Label(tech_frame, text='투입인력 기준점수 (원점수 이상 → 배점비율)',
                  font=('', 10, 'bold')).grid(row=12, column=0, columnspan=6,
                                              sticky='w', padx=4, pady=4)

        for ci, h in enumerate(headers):
            ttk.Label(tech_frame, text=h, width=9).grid(row=13, column=ci, padx=2)

        self._dep_thresh_vars = []
        defaults_dep = [[10, 1.0], [7, 0.7], [4, 0.4]]
        for i, (min_v, frac) in enumerate(defaults_dep):
            col_base = (i % 3) * 2
            row_base = 14 + i // 3
            v_min = tk.StringVar(value=str(min_v))
            v_frac = tk.StringVar(value=str(frac))
            self._dep_thresh_vars.append((v_min, v_frac))
            ttk.Entry(tech_frame, textvariable=v_min, width=8).grid(
                row=row_base, column=col_base, padx=2, pady=2)
            ttk.Entry(tech_frame, textvariable=v_frac, width=8).grid(
                row=row_base, column=col_base + 1, padx=2, pady=2)

        ttk.Button(tech_frame, text='+ 행 추가', command=self._add_dep_row).grid(
            row=20, column=0, columnspan=2, pady=4, sticky='w'
        )

        # ── Save button ────────────────────────────────────────────
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=12)
        ttk.Button(btn_frame, text='저장', command=self.save).pack()

    def _format_amount(self, event=None):
        val = self._vars['bid_amount'].get().replace(',', '').strip()
        try:
            num = int(val)
            self._vars['bid_amount'].set(f'{num:,}')
        except ValueError:
            pass

    def _update_total_score(self, event=None):
        try:
            total = sum(float(v.get() or 0) for v in self._score_vars.values())
            self.total_score_label.config(text=f'{total:.1f}점')
        except ValueError:
            pass

    def _add_ret_row(self):
        self._ret_thresh_vars.append((tk.StringVar(value='0'), tk.StringVar(value='0.0')))

    def _add_dep_row(self):
        self._dep_thresh_vars.append((tk.StringVar(value='0'), tk.StringVar(value='0.0')))

    def refresh(self):
        if not self.app.current_bid_id:
            for var in self._vars.values():
                var.set('')
            for var in self._score_vars.values():
                var.set('0.0')
            self._update_total_score()
            return

        bid = db.get_bid(self.app.current_bid_id)
        if not bid:
            return

        self._vars['bid_name'].set(bid.get('bid_name', ''))
        self._vars['bid_number'].set(bid.get('bid_number', '') or '')
        self._vars['client'].set(bid.get('client', '') or '')
        self._vars['bid_date'].set(bid.get('bid_date', '') or '')
        self._vars['company_name'].set(bid.get('company_name', '') or '')
        amount = bid.get('bid_amount', 0) or 0
        self._vars['bid_amount'].set(f'{int(amount):,}' if amount else '')

        self._score_vars['score_management'].set(str(bid.get('score_management', 6.0)))
        self._score_vars['score_experience'].set(str(bid.get('score_experience', 6.0)))
        self._score_vars['score_technician'].set(str(bid.get('score_technician', 6.0)))
        self._score_vars['score_reputation'].set(str(bid.get('score_reputation', 2.0)))

        # Load thresholds
        try:
            ret = json.loads(bid.get('retention_thresholds') or '[[40,1.0],[30,0.8],[20,0.6],[10,0.4]]')
            self._ret_thresh_vars.clear()
            for min_v, frac in ret:
                self._ret_thresh_vars.append((
                    tk.StringVar(value=str(min_v)),
                    tk.StringVar(value=str(frac))
                ))
        except Exception:
            pass

        try:
            dep = json.loads(bid.get('deployment_thresholds') or '[[10,1.0],[7,0.7],[4,0.4]]')
            self._dep_thresh_vars.clear()
            for min_v, frac in dep:
                self._dep_thresh_vars.append((
                    tk.StringVar(value=str(min_v)),
                    tk.StringVar(value=str(frac))
                ))
        except Exception:
            pass

        self._update_total_score()

    def save(self):
        if not self.app.current_bid_id:
            messagebox.showinfo('알림', '공고를 먼저 생성하거나 선택하세요.')
            return

        bid_name = self._vars['bid_name'].get().strip()
        if not bid_name:
            messagebox.showwarning('입력 오류', '공고명을 입력하세요.')
            return

        amount_str = self._vars['bid_amount'].get().replace(',', '').strip()
        try:
            bid_amount = int(amount_str) if amount_str else 0
        except ValueError:
            bid_amount = 0

        try:
            score_management = float(self._score_vars['score_management'].get())
        except ValueError:
            score_management = 6.0
        try:
            score_experience = float(self._score_vars['score_experience'].get())
        except ValueError:
            score_experience = 6.0
        try:
            score_technician = float(self._score_vars['score_technician'].get())
        except ValueError:
            score_technician = 6.0
        try:
            score_reputation = float(self._score_vars['score_reputation'].get())
        except ValueError:
            score_reputation = 2.0

        ret_thresh = []
        for v_min, v_frac in self._ret_thresh_vars:
            try:
                ret_thresh.append([float(v_min.get()), float(v_frac.get())])
            except ValueError:
                pass

        dep_thresh = []
        for v_min, v_frac in self._dep_thresh_vars:
            try:
                dep_thresh.append([float(v_min.get()), float(v_frac.get())])
            except ValueError:
                pass

        db.update_bid(
            self.app.current_bid_id,
            bid_name=bid_name,
            bid_number=self._vars['bid_number'].get().strip(),
            client=self._vars['client'].get().strip(),
            bid_date=self._vars['bid_date'].get().strip(),
            company_name=self._vars['company_name'].get().strip(),
            bid_amount=bid_amount,
            score_management=score_management,
            score_experience=score_experience,
            score_technician=score_technician,
            score_reputation=score_reputation,
            retention_thresholds=json.dumps(ret_thresh),
            deployment_thresholds=json.dumps(dep_thresh),
        )
        self.app._load_bids_menu()
        self.app.update_status()
