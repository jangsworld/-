"""
Tab: 기술인력
"""
import tkinter as tk
from tkinter import ttk, messagebox

from core import database as db
from core.calculator import calc_technician_score, GRADE_POINTS
import json


class TabTechnician(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._build_ui()

    def _build_ui(self):
        # ── Three-panel layout ────────────────────────────────────
        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        container.columnconfigure(2, weight=1)
        container.rowconfigure(0, weight=1)

        # ── LEFT: 기술자 DB ───────────────────────────────────────
        left_frame = ttk.LabelFrame(container, text='기술자 DB 관리', padding=4)
        left_frame.grid(row=0, column=0, sticky='nsew', padx=4, pady=4)

        left_toolbar = ttk.Frame(left_frame)
        left_toolbar.pack(fill=tk.X, pady=2)
        ttk.Button(left_toolbar, text='추가', command=self._add_technician).pack(side=tk.LEFT, padx=2)
        ttk.Button(left_toolbar, text='수정', command=self._edit_technician).pack(side=tk.LEFT, padx=2)
        ttk.Button(left_toolbar, text='삭제', command=self._delete_technician).pack(side=tk.LEFT, padx=2)

        db_cols = ('이름', '자격기준', '등급', '입사일')
        self.db_tree = ttk.Treeview(left_frame, columns=db_cols, show='headings', height=18)
        for col in db_cols:
            self.db_tree.heading(col, text=col)
            w = 70 if col == '이름' else 120 if col == '자격기준' else 60 if col == '등급' else 90
            self.db_tree.column(col, width=w, anchor='center')
        db_scroll = ttk.Scrollbar(left_frame, orient='vertical', command=self.db_tree.yview)
        self.db_tree.configure(yscrollcommand=db_scroll.set)
        self.db_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        db_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # ── MIDDLE: 보유인력 ──────────────────────────────────────
        mid_frame = ttk.LabelFrame(container, text='보유인력 (이번 공고)', padding=4)
        mid_frame.grid(row=0, column=1, sticky='nsew', padx=4, pady=4)

        mid_toolbar = ttk.Frame(mid_frame)
        mid_toolbar.pack(fill=tk.X, pady=2)
        ttk.Button(mid_toolbar, text='→ 추가', command=self._add_to_retention).pack(side=tk.LEFT, padx=2)
        ttk.Button(mid_toolbar, text='← 제거', command=self._remove_from_retention).pack(side=tk.LEFT, padx=2)

        ret_cols = ('이름', '등급', '점수')
        self.ret_tree = ttk.Treeview(mid_frame, columns=ret_cols, show='headings', height=14)
        for col in ret_cols:
            self.ret_tree.heading(col, text=col)
            w = 80 if col == '이름' else 60
            self.ret_tree.column(col, width=w, anchor='center')
        ret_scroll = ttk.Scrollbar(mid_frame, orient='vertical', command=self.ret_tree.yview)
        self.ret_tree.configure(yscrollcommand=ret_scroll.set)
        self.ret_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ret_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        ret_summary = ttk.Frame(mid_frame)
        ret_summary.pack(fill=tk.X, pady=4)
        ttk.Label(ret_summary, text='합계점수:').pack(side=tk.LEFT, padx=4)
        self.ret_raw_label = ttk.Label(ret_summary, text='0.0점', foreground='navy')
        self.ret_raw_label.pack(side=tk.LEFT)
        ttk.Label(ret_summary, text=' | 평가점수:').pack(side=tk.LEFT)
        self.ret_score_label = ttk.Label(ret_summary, text='-', foreground='blue',
                                         font=('', 10, 'bold'))
        self.ret_score_label.pack(side=tk.LEFT, padx=4)

        # ── RIGHT: 투입인력 ───────────────────────────────────────
        right_frame = ttk.LabelFrame(container, text='투입인력 (이번 공고)', padding=4)
        right_frame.grid(row=0, column=2, sticky='nsew', padx=4, pady=4)

        right_toolbar = ttk.Frame(right_frame)
        right_toolbar.pack(fill=tk.X, pady=2)
        ttk.Button(right_toolbar, text='→ 추가', command=self._add_to_deployment).pack(side=tk.LEFT, padx=2)
        ttk.Button(right_toolbar, text='← 제거', command=self._remove_from_deployment).pack(side=tk.LEFT, padx=2)

        dep_cols = ('이름', '등급', '점수')
        self.dep_tree = ttk.Treeview(right_frame, columns=dep_cols, show='headings', height=14)
        for col in dep_cols:
            self.dep_tree.heading(col, text=col)
            w = 80 if col == '이름' else 60
            self.dep_tree.column(col, width=w, anchor='center')
        dep_scroll = ttk.Scrollbar(right_frame, orient='vertical', command=self.dep_tree.yview)
        self.dep_tree.configure(yscrollcommand=dep_scroll.set)
        self.dep_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        dep_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        dep_summary = ttk.Frame(right_frame)
        dep_summary.pack(fill=tk.X, pady=4)
        ttk.Label(dep_summary, text='합계점수:').pack(side=tk.LEFT, padx=4)
        self.dep_raw_label = ttk.Label(dep_summary, text='0.0점', foreground='navy')
        self.dep_raw_label.pack(side=tk.LEFT)
        ttk.Label(dep_summary, text=' | 평가점수:').pack(side=tk.LEFT)
        self.dep_score_label = ttk.Label(dep_summary, text='-', foreground='blue',
                                         font=('', 10, 'bold'))
        self.dep_score_label.pack(side=tk.LEFT, padx=4)

        # ── Save button ────────────────────────────────────────────
        save_frame = ttk.Frame(self)
        save_frame.pack(fill=tk.X, pady=6)
        ttk.Button(save_frame, text='저장', command=self.save).pack(pady=4)

        # Internal state
        self._all_technicians = []
        self._retention_ids = []
        self._deployment_ids = []

    # ── DB tree helpers ───────────────────────────────────────────

    def _refresh_db_tree(self):
        self.db_tree.delete(*self.db_tree.get_children())
        self._all_technicians = db.get_all_technicians()
        for t in self._all_technicians:
            self.db_tree.insert('', tk.END, tags=(t['id'],),
                                values=(t.get('name', ''),
                                        t.get('qualification', ''),
                                        t.get('grade', ''),
                                        t.get('hire_date', '')))

    def _refresh_ret_tree(self):
        self.ret_tree.delete(*self.ret_tree.get_children())
        for tid in self._retention_ids:
            tech = next((t for t in self._all_technicians if t['id'] == tid), None)
            if tech:
                pts = GRADE_POINTS.get(tech.get('grade', '초급'), 1.0)
                self.ret_tree.insert('', tk.END, tags=(tid,),
                                     values=(tech.get('name', ''),
                                             tech.get('grade', ''),
                                             f'{pts:.1f}'))
        self._update_scores()

    def _refresh_dep_tree(self):
        self.dep_tree.delete(*self.dep_tree.get_children())
        for tid in self._deployment_ids:
            tech = next((t for t in self._all_technicians if t['id'] == tid), None)
            if tech:
                pts = GRADE_POINTS.get(tech.get('grade', '초급'), 1.0)
                self.dep_tree.insert('', tk.END, tags=(tid,),
                                     values=(tech.get('name', ''),
                                             tech.get('grade', ''),
                                             f'{pts:.1f}'))
        self._update_scores()

    def _update_scores(self):
        if not self.app.current_bid_id:
            return
        bid = db.get_bid(self.app.current_bid_id)
        if not bid:
            return

        ret_list = [t for t in self._all_technicians if t['id'] in self._retention_ids]
        dep_list = [t for t in self._all_technicians if t['id'] in self._deployment_ids]

        try:
            ret_thresh = json.loads(bid.get('retention_thresholds') or '[[40,1.0],[30,0.8],[20,0.6],[10,0.4]]')
            dep_thresh = json.loads(bid.get('deployment_thresholds') or '[[10,1.0],[7,0.7],[4,0.4]]')
        except Exception:
            ret_thresh = [[40, 1.0], [30, 0.8], [20, 0.6], [10, 0.4]]
            dep_thresh = [[10, 1.0], [7, 0.7], [4, 0.4]]

        result = calc_technician_score(
            ret_list, dep_list,
            bid.get('score_technician', 6.0),
            ret_thresh, dep_thresh
        )
        half = bid.get('score_technician', 6.0) / 2

        self.ret_raw_label.config(text=f"{result['retention_raw']:.1f}점")
        self.ret_score_label.config(
            text=f"{result['retention_score']:.2f}점 / {half:.1f}점")
        self.dep_raw_label.config(text=f"{result['deployment_raw']:.1f}점")
        self.dep_score_label.config(
            text=f"{result['deployment_score']:.2f}점 / {half:.1f}점")

    def _get_selected_db_id(self):
        sel = self.db_tree.selection()
        if not sel:
            return None
        tags = self.db_tree.item(sel[0])['tags']
        return int(tags[0]) if tags else None

    def _get_selected_ret_id(self):
        sel = self.ret_tree.selection()
        if not sel:
            return None
        tags = self.ret_tree.item(sel[0])['tags']
        return int(tags[0]) if tags else None

    def _get_selected_dep_id(self):
        sel = self.dep_tree.selection()
        if not sel:
            return None
        tags = self.dep_tree.item(sel[0])['tags']
        return int(tags[0]) if tags else None

    def _add_to_retention(self):
        tid = self._get_selected_db_id()
        if tid is None:
            return
        if tid not in self._retention_ids:
            self._retention_ids.append(tid)
            self._refresh_ret_tree()

    def _remove_from_retention(self):
        tid = self._get_selected_ret_id()
        if tid is not None and tid in self._retention_ids:
            self._retention_ids.remove(tid)
            self._refresh_ret_tree()

    def _add_to_deployment(self):
        tid = self._get_selected_db_id()
        if tid is None:
            return
        if tid not in self._deployment_ids:
            self._deployment_ids.append(tid)
            self._refresh_dep_tree()

    def _remove_from_deployment(self):
        tid = self._get_selected_dep_id()
        if tid is not None and tid in self._deployment_ids:
            self._deployment_ids.remove(tid)
            self._refresh_dep_tree()

    def _add_technician(self):
        dialog = TechnicianDialog(self, title='기술자 추가')
        self.wait_window(dialog)
        if dialog.result:
            data = dialog.result
            db.add_technician(data['name'], data['qualification'],
                              data['grade'], data['hire_date'])
            self._refresh_db_tree()

    def _edit_technician(self):
        tid = self._get_selected_db_id()
        if tid is None:
            messagebox.showinfo('알림', '수정할 기술자를 선택하세요.')
            return
        tech = next((t for t in self._all_technicians if t['id'] == tid), None)
        if not tech:
            return
        dialog = TechnicianDialog(self, title='기술자 수정', technician=tech)
        self.wait_window(dialog)
        if dialog.result:
            data = dialog.result
            db.update_technician(tid, name=data['name'],
                                 qualification=data['qualification'],
                                 grade=data['grade'],
                                 hire_date=data['hire_date'])
            self._refresh_db_tree()
            self._refresh_ret_tree()
            self._refresh_dep_tree()

    def _delete_technician(self):
        tid = self._get_selected_db_id()
        if tid is None:
            messagebox.showinfo('알림', '삭제할 기술자를 선택하세요.')
            return
        tech = next((t for t in self._all_technicians if t['id'] == tid), None)
        if not tech:
            return
        if messagebox.askyesno('삭제 확인',
                               f"기술자 '{tech['name']}'을(를) 삭제하시겠습니까?"):
            db.delete_technician(tid)
            self._retention_ids = [i for i in self._retention_ids if i != tid]
            self._deployment_ids = [i for i in self._deployment_ids if i != tid]
            self._refresh_db_tree()
            self._refresh_ret_tree()
            self._refresh_dep_tree()

    def refresh(self):
        self._refresh_db_tree()
        if self.app.current_bid_id:
            ret = db.get_bid_technicians(self.app.current_bid_id, 'retention')
            dep = db.get_bid_technicians(self.app.current_bid_id, 'deployment')
            self._retention_ids = [t['id'] for t in ret]
            self._deployment_ids = [t['id'] for t in dep]
        else:
            self._retention_ids = []
            self._deployment_ids = []
        self._refresh_ret_tree()
        self._refresh_dep_tree()

    def save(self):
        if not self.app.current_bid_id:
            messagebox.showinfo('알림', '공고를 먼저 선택하세요.')
            return
        db.set_bid_technicians(self.app.current_bid_id, 'retention', self._retention_ids)
        db.set_bid_technicians(self.app.current_bid_id, 'deployment', self._deployment_ids)
        self.app.update_status()
        messagebox.showinfo('저장', '기술인력 정보가 저장되었습니다.')


class TechnicianDialog(tk.Toplevel):
    def __init__(self, parent, title='기술자', technician=None):
        super().__init__(parent)
        self.title(title)
        self.geometry('400x240')
        self.resizable(False, False)
        self.grab_set()
        self.result = None

        fields = [
            ('이름', 'name'),
            ('자격기준', 'qualification'),
        ]
        self._vars = {}

        for row_idx, (label, key) in enumerate(fields):
            ttk.Label(self, text=label + ':').grid(
                row=row_idx, column=0, sticky='e', padx=10, pady=8)
            var = tk.StringVar()
            self._vars[key] = var
            ttk.Entry(self, textvariable=var, width=28).grid(
                row=row_idx, column=1, padx=10, pady=8, sticky='w')

        # Grade combo
        ttk.Label(self, text='등급:').grid(row=2, column=0, sticky='e', padx=10, pady=8)
        self.grade_var = tk.StringVar(value='중급')
        ttk.Combobox(self, textvariable=self.grade_var,
                     values=['특급', '고급', '중급', '초급'],
                     state='readonly', width=12).grid(
            row=2, column=1, padx=10, pady=8, sticky='w')

        # Hire date
        ttk.Label(self, text='입사일 (YYYY.MM.DD):').grid(
            row=3, column=0, sticky='e', padx=10, pady=8)
        self.hire_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.hire_var, width=18).grid(
            row=3, column=1, padx=10, pady=8, sticky='w')

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=12)
        ttk.Button(btn_frame, text='확인', command=self._ok).pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_frame, text='취소', command=self.destroy).pack(side=tk.LEFT, padx=8)

        if technician:
            self._vars['name'].set(technician.get('name', ''))
            self._vars['qualification'].set(
                technician.get('qualification', '정보통신기술자') or '정보통신기술자')
            self.grade_var.set(technician.get('grade', '중급'))
            self.hire_var.set(technician.get('hire_date', '') or '')

    def _ok(self):
        name = self._vars['name'].get().strip()
        if not name:
            messagebox.showwarning('입력 오류', '이름을 입력하세요.', parent=self)
            return
        qual = self._vars['qualification'].get().strip() or '정보통신기술자'
        self.result = {
            'name': name,
            'qualification': qual,
            'grade': self.grade_var.get(),
            'hire_date': self.hire_var.get().strip(),
        }
        self.destroy()
