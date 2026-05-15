"""
Main application window for 정량적 제안서 자동 생성 프로그램.
"""
import tkinter as tk
from tkinter import ttk, messagebox
import json

from core import database as db
from core.calculator import (
    calc_management_score, calc_experience_score,
    calc_technician_score, calc_reputation_score, calc_total_score
)


class ProposalApp(tk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.master = master
        self.current_bid_id = None

        self._build_ui()
        self._load_bids_menu()

    def _build_ui(self):
        # ── Top toolbar ──────────────────────────────────────────────
        toolbar = ttk.Frame(self, relief='raised', padding=4)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text='새 공고', command=self.new_bid).pack(side=tk.LEFT, padx=4)

        ttk.Label(toolbar, text='공고 불러오기:').pack(side=tk.LEFT, padx=(12, 2))
        self.bid_var = tk.StringVar()
        self.bid_combo = ttk.Combobox(toolbar, textvariable=self.bid_var,
                                      state='readonly', width=40)
        self.bid_combo.pack(side=tk.LEFT, padx=2)
        self.bid_combo.bind('<<ComboboxSelected>>', self._on_bid_selected)

        ttk.Button(toolbar, text='저장', command=self.save_current).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text='공고 삭제', command=self.delete_bid).pack(side=tk.LEFT, padx=4)

        # ── Notebook (tabs) ──────────────────────────────────────────
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Import tab modules here to avoid circular imports at module level
        from ui.tabs.tab_config import TabConfig
        from ui.tabs.tab_management import TabManagement
        from ui.tabs.tab_experience import TabExperience
        from ui.tabs.tab_technician import TabTechnician
        from ui.tabs.tab_reputation import TabReputation
        from ui.tabs.tab_extra import TabExtra
        from ui.tabs.tab_generate import TabGenerate

        self.tab_config = TabConfig(self.notebook, self)
        self.tab_management = TabManagement(self.notebook, self)
        self.tab_experience = TabExperience(self.notebook, self)
        self.tab_technician = TabTechnician(self.notebook, self)
        self.tab_reputation = TabReputation(self.notebook, self)
        self.tab_extra = TabExtra(self.notebook, self)
        self.tab_generate = TabGenerate(self.notebook, self)

        self.notebook.add(self.tab_config, text='📋 공고 설정')
        self.notebook.add(self.tab_management, text='💼 경영실태')
        self.notebook.add(self.tab_experience, text='📊 수행경험')
        self.notebook.add(self.tab_technician, text='👥 기술인력')
        self.notebook.add(self.tab_reputation, text='⭐ 신인도')
        self.notebook.add(self.tab_extra, text='➕ 추가 항목')
        self.notebook.add(self.tab_generate, text='📄 최종 생성')

        self.notebook.bind('<<NotebookTabChanged>>', self._on_tab_changed)

        # ── Status bar ────────────────────────────────────────────────
        status_bar = ttk.Frame(self, relief='sunken', padding=2)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_bid_label = ttk.Label(status_bar, text='공고 없음')
        self.status_bid_label.pack(side=tk.LEFT, padx=6)
        self.status_score_label = ttk.Label(status_bar, text='총점: -')
        self.status_score_label.pack(side=tk.RIGHT, padx=6)

    def _load_bids_menu(self):
        bids = db.get_all_bids()
        self._bids_list = bids
        values = [f"[{b['id']}] {b['bid_name']}" for b in bids]
        self.bid_combo['values'] = values
        if self.current_bid_id:
            for i, b in enumerate(bids):
                if b['id'] == self.current_bid_id:
                    self.bid_combo.current(i)
                    break

    def _on_bid_selected(self, event=None):
        sel = self.bid_combo.current()
        if sel < 0:
            return
        bid = self._bids_list[sel]
        self.current_bid_id = bid['id']
        self.refresh_all_tabs()

    def _on_tab_changed(self, event=None):
        tab = self.notebook.select()
        tab_name = self.notebook.tab(tab, 'text')
        if '최종 생성' in tab_name:
            self.tab_generate.refresh()

    def new_bid(self):
        dialog = NewBidDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            bid_name = dialog.result
            bid_id = db.create_bid(bid_name)
            self.current_bid_id = bid_id
            self._load_bids_menu()
            self.refresh_all_tabs()

    def save_current(self):
        if not self.current_bid_id:
            messagebox.showinfo('알림', '저장할 공고가 없습니다.')
            return
        self.tab_config.save()
        self.tab_management.save()
        self.tab_experience.save()
        self.tab_technician.save()
        self.tab_reputation.save()
        self.tab_extra.save()
        self._load_bids_menu()
        self.update_status()
        messagebox.showinfo('저장', '저장되었습니다.')

    def delete_bid(self):
        if not self.current_bid_id:
            messagebox.showinfo('알림', '삭제할 공고가 없습니다.')
            return
        bid = db.get_bid(self.current_bid_id)
        if messagebox.askyesno('삭제 확인', f"공고 '{bid['bid_name']}'을(를) 삭제하시겠습니까?"):
            db.delete_bid(self.current_bid_id)
            self.current_bid_id = None
            self._load_bids_menu()
            self.refresh_all_tabs()

    def refresh_all_tabs(self):
        for tab in [self.tab_config, self.tab_management, self.tab_experience,
                    self.tab_technician, self.tab_reputation, self.tab_extra,
                    self.tab_generate]:
            tab.refresh()
        self.update_status()

    def update_status(self):
        if not self.current_bid_id:
            self.status_bid_label.config(text='공고 없음')
            self.status_score_label.config(text='총점: -')
            return
        bid = db.get_bid(self.current_bid_id)
        if not bid:
            return
        self.status_bid_label.config(text=f"공고: {bid['bid_name']}")

        # Calculate scores
        try:
            mgmt_score = calc_management_score(
                bid.get('credit_rating', 'BBB+'),
                bid.get('score_management', 6.0)
            )
            retention = db.get_bid_technicians(self.current_bid_id, 'retention')
            deployment = db.get_bid_technicians(self.current_bid_id, 'deployment')
            ret_thresh = json.loads(bid.get('retention_thresholds') or '[[40,1.0],[30,0.8],[20,0.6],[10,0.4]]')
            dep_thresh = json.loads(bid.get('deployment_thresholds') or '[[10,1.0],[7,0.7],[4,0.4]]')
            tech_result = calc_technician_score(
                retention, deployment,
                bid.get('score_technician', 6.0),
                ret_thresh, dep_thresh
            )
            projects = db.get_bid_projects(self.current_bid_id)
            total_amount = sum(p.get('amount', 0) for p in projects)
            exp_score, _ = calc_experience_score(
                total_amount,
                bid.get('bid_amount', 0),
                bid.get('score_experience', 6.0)
            )
            rep_score = calc_reputation_score(
                bid.get('restriction_status', 'none'),
                bid.get('score_reputation', 2.0)
            )
            extras = db.get_extra_items(self.current_bid_id)
            total = calc_total_score(
                mgmt_score, exp_score, tech_result['total_score'], rep_score, extras
            )
            self.status_score_label.config(text=f'총점: {total:.2f}점')
        except Exception:
            self.status_score_label.config(text='총점: 계산 중 오류')


class NewBidDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title('새 공고 생성')
        self.geometry('400x140')
        self.resizable(False, False)
        self.result = None
        self.grab_set()

        ttk.Label(self, text='공고명:').grid(row=0, column=0, padx=10, pady=20, sticky='e')
        self.name_var = tk.StringVar()
        entry = ttk.Entry(self, textvariable=self.name_var, width=35)
        entry.grid(row=0, column=1, padx=10, pady=20)
        entry.focus_set()
        entry.bind('<Return>', lambda e: self._ok())

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=1, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text='확인', command=self._ok).pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_frame, text='취소', command=self.destroy).pack(side=tk.LEFT, padx=8)

    def _ok(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning('입력 오류', '공고명을 입력하세요.', parent=self)
            return
        self.result = name
        self.destroy()
