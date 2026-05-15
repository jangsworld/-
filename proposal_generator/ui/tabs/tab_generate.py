"""
Tab: 최종 생성 (PPT 생성)
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import threading
import json
from datetime import datetime

from core import database as db
from core.calculator import (
    calc_management_score, calc_experience_score,
    calc_technician_score, calc_reputation_score, calc_total_score
)
from core.ppt_generator import generate_ppt


class TabGenerate(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._build_ui()

    def _build_ui(self):
        # ── Score preview ────────────────────────────────────────
        preview_frame = ttk.LabelFrame(self, text='점수 종합 (미리보기)', padding=8)
        preview_frame.pack(fill=tk.X, padx=12, pady=8)

        cols = ('항목', '배점', '취득점수')
        self.preview_tree = ttk.Treeview(preview_frame, columns=cols,
                                         show='headings', height=8)
        for col in cols:
            self.preview_tree.heading(col, text=col)
            w = 250 if col == '항목' else 100
            self.preview_tree.column(col, width=w, anchor='center')
        self.preview_tree.pack(fill=tk.X)

        # ── Output settings ───────────────────────────────────────
        out_frame = ttk.LabelFrame(self, text='출력 설정', padding=10)
        out_frame.pack(fill=tk.X, padx=12, pady=8)

        ttk.Label(out_frame, text='출력 파일명:').grid(
            row=0, column=0, sticky='e', padx=8, pady=6)
        self.filename_var = tk.StringVar(value='정량적제안서')
        ttk.Entry(out_frame, textvariable=self.filename_var, width=45).grid(
            row=0, column=1, padx=8, pady=6, sticky='w')

        ttk.Label(out_frame, text='저장 위치:').grid(
            row=1, column=0, sticky='e', padx=8, pady=6)
        self.save_dir_var = tk.StringVar(value=os.path.expanduser('~'))
        ttk.Entry(out_frame, textvariable=self.save_dir_var, width=45).grid(
            row=1, column=1, padx=8, pady=6, sticky='w')
        ttk.Button(out_frame, text='찾아보기',
                   command=self._browse_dir).grid(row=1, column=2, padx=4)

        # ── Generate button ───────────────────────────────────────
        gen_frame = ttk.Frame(self)
        gen_frame.pack(pady=16)

        gen_btn = tk.Button(gen_frame, text='📄  PPT 생성',
                            bg='#1F5B8A', fg='white',
                            font=('', 14, 'bold'),
                            relief='flat', padx=24, pady=12,
                            cursor='hand2',
                            command=self._generate)
        gen_btn.pack()

        self.status_label = ttk.Label(self, text='', foreground='gray')
        self.status_label.pack(pady=6)

    def _browse_dir(self):
        folder = filedialog.askdirectory(title='저장 위치 선택')
        if folder:
            self.save_dir_var.set(folder)

    def refresh(self):
        self.preview_tree.delete(*self.preview_tree.get_children())
        if not self.app.current_bid_id:
            self.status_label.config(text='공고를 먼저 선택하세요.')
            return

        bid = db.get_bid(self.app.current_bid_id)
        if not bid:
            return

        # Calc scores
        mgmt_score = calc_management_score(
            bid.get('credit_rating', 'BBB+'),
            bid.get('score_management', 6.0)
        )
        retention = db.get_bid_technicians(self.app.current_bid_id, 'retention')
        deployment = db.get_bid_technicians(self.app.current_bid_id, 'deployment')
        try:
            ret_thresh = json.loads(bid.get('retention_thresholds') or '[[40,1.0],[30,0.8],[20,0.6],[10,0.4]]')
            dep_thresh = json.loads(bid.get('deployment_thresholds') or '[[10,1.0],[7,0.7],[4,0.4]]')
        except Exception:
            ret_thresh = [[40, 1.0], [30, 0.8], [20, 0.6], [10, 0.4]]
            dep_thresh = [[10, 1.0], [7, 0.7], [4, 0.4]]
        tech_result = calc_technician_score(
            retention, deployment,
            bid.get('score_technician', 6.0),
            ret_thresh, dep_thresh
        )
        projects = db.get_bid_projects(self.app.current_bid_id)
        total_amount = sum(p.get('amount', 0) or 0 for p in projects)
        exp_score, exp_pct = calc_experience_score(
            total_amount, bid.get('bid_amount', 0) or 0,
            bid.get('score_experience', 6.0)
        )
        rep_score = calc_reputation_score(
            bid.get('restriction_status', 'none'),
            bid.get('score_reputation', 2.0)
        )
        extras = db.get_extra_items(self.app.current_bid_id)
        total = calc_total_score(mgmt_score, exp_score,
                                 tech_result['total_score'], rep_score, extras)

        rows = [
            ('경영실태', f"{bid.get('score_management', 6.0):.1f}점",
             f"{mgmt_score:.2f}점"),
            ('수행경험', f"{bid.get('score_experience', 6.0):.1f}점",
             f"{exp_score:.2f}점"),
            ('기술인력', f"{bid.get('score_technician', 6.0):.1f}점",
             f"{tech_result['total_score']:.2f}점"),
            ('신인도', f"{bid.get('score_reputation', 2.0):.1f}점",
             f"{rep_score:.2f}점"),
        ]
        for item in extras:
            rows.append((item['name'],
                         f"{item.get('max_score', 0):.1f}점",
                         f"{item.get('actual_score', 0):.2f}점"))
        max_total = (bid.get('score_management', 6.0) +
                     bid.get('score_experience', 6.0) +
                     bid.get('score_technician', 6.0) +
                     bid.get('score_reputation', 2.0) +
                     sum(i.get('max_score', 0) for i in extras))
        rows.append(('합계', f'{max_total:.1f}점', f'{total:.2f}점'))

        for row in rows:
            self.preview_tree.insert('', tk.END, values=row)

        # Auto-fill filename
        company = bid.get('company_name', '') or ''
        date_str = datetime.now().strftime('%Y%m%d')
        fname = f"정량적제안서_{company}_{date_str}" if company else f"정량적제안서_{date_str}"
        self.filename_var.set(fname)

        self.status_label.config(text='')

    def _generate(self):
        if not self.app.current_bid_id:
            messagebox.showinfo('알림', '공고를 먼저 선택하세요.')
            return

        filename = self.filename_var.get().strip()
        if not filename:
            filename = '정량적제안서'
        if not filename.endswith('.pptx'):
            filename += '.pptx'

        save_dir = self.save_dir_var.get().strip()
        if not save_dir or not os.path.isdir(save_dir):
            messagebox.showerror('오류', '유효한 저장 위치를 선택하세요.')
            return

        output_path = os.path.join(save_dir, filename)

        self.status_label.config(text='생성 중...', foreground='orange')
        self.update()

        try:
            bid_data = self._collect_bid_data()
            generate_ppt(bid_data, output_path)
            self.status_label.config(
                text=f'✅ 생성 완료: {output_path}', foreground='green')
            messagebox.showinfo('완료', f'PPT가 생성되었습니다:\n{output_path}')
        except Exception as e:
            self.status_label.config(
                text=f'❌ 오류: {e}', foreground='red')
            messagebox.showerror('생성 오류', str(e))

    def _collect_bid_data(self):
        bid = db.get_bid(self.app.current_bid_id)

        mgmt_score = calc_management_score(
            bid.get('credit_rating', 'BBB+'),
            bid.get('score_management', 6.0)
        )
        retention = db.get_bid_technicians(self.app.current_bid_id, 'retention')
        deployment = db.get_bid_technicians(self.app.current_bid_id, 'deployment')
        try:
            ret_thresh = json.loads(bid.get('retention_thresholds') or '[[40,1.0],[30,0.8],[20,0.6],[10,0.4]]')
            dep_thresh = json.loads(bid.get('deployment_thresholds') or '[[10,1.0],[7,0.7],[4,0.4]]')
        except Exception:
            ret_thresh = [[40, 1.0], [30, 0.8], [20, 0.6], [10, 0.4]]
            dep_thresh = [[10, 1.0], [7, 0.7], [4, 0.4]]
        tech_result = calc_technician_score(
            retention, deployment,
            bid.get('score_technician', 6.0),
            ret_thresh, dep_thresh
        )
        projects = db.get_bid_projects(self.app.current_bid_id)
        total_amount = sum(p.get('amount', 0) or 0 for p in projects)
        exp_score, exp_pct = calc_experience_score(
            total_amount, bid.get('bid_amount', 0) or 0,
            bid.get('score_experience', 6.0)
        )
        rep_score = calc_reputation_score(
            bid.get('restriction_status', 'none'),
            bid.get('score_reputation', 2.0)
        )
        extras = db.get_extra_items(self.app.current_bid_id)
        total = calc_total_score(mgmt_score, exp_score,
                                 tech_result['total_score'], rep_score, extras)

        return {
            'bid_info': {
                'company_name': bid.get('company_name', ''),
                'bid_date': bid.get('bid_date', ''),
                'bid_name': bid.get('bid_name', ''),
            },
            'management': {
                'rating': bid.get('credit_rating', 'BBB+'),
                'score': mgmt_score,
                'max_score': bid.get('score_management', 6.0),
            },
            'experience': {
                'projects': projects,
                'total_amount': total_amount,
                'bid_amount': bid.get('bid_amount', 0),
                'percentage': exp_pct,
                'score': exp_score,
                'max_score': bid.get('score_experience', 6.0),
            },
            'technician': {
                'retention_list': retention,
                'deployment_list': deployment,
                'retention_raw': tech_result['retention_raw'],
                'deployment_raw': tech_result['deployment_raw'],
                'retention_score': tech_result['retention_score'],
                'deployment_score': tech_result['deployment_score'],
                'total_score': tech_result['total_score'],
                'max_score': bid.get('score_technician', 6.0),
            },
            'reputation': {
                'status': bid.get('restriction_status', 'none'),
                'score': rep_score,
                'max_score': bid.get('score_reputation', 2.0),
            },
            'extra_items': [
                {
                    'name': i.get('name', ''),
                    'max_score': i.get('max_score', 0),
                    'actual_score': i.get('actual_score', 0),
                    'description': i.get('description', ''),
                }
                for i in extras
            ],
            'total_score': total,
        }
