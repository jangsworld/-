"""
Tab: 수행경험
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os

from core import database as db
from core.calculator import calc_experience_score


class TabExperience(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._build_ui()

    def _build_ui(self):
        # ── Split pane ────────────────────────────────────────────
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # ── LEFT: 전체 프로젝트 DB ────────────────────────────────
        left_frame = ttk.LabelFrame(paned, text='프로젝트 DB (전체)', padding=4)
        paned.add(left_frame, weight=1)

        left_toolbar = ttk.Frame(left_frame)
        left_toolbar.pack(fill=tk.X, pady=2)
        ttk.Button(left_toolbar, text='추가', command=self._add_project).pack(side=tk.LEFT, padx=2)
        ttk.Button(left_toolbar, text='수정', command=self._edit_project).pack(side=tk.LEFT, padx=2)
        ttk.Button(left_toolbar, text='삭제', command=self._delete_project).pack(side=tk.LEFT, padx=2)

        cols = ('사업명', '발주처', '계약금액', '계약일', '완료일')
        self.all_tree = ttk.Treeview(left_frame, columns=cols, show='headings', height=16)
        for col in cols:
            self.all_tree.heading(col, text=col)
            width = 180 if col == '사업명' else 90 if col in ('계약일', '완료일') else 110
            self.all_tree.column(col, width=width, anchor='center')
        left_scroll = ttk.Scrollbar(left_frame, orient='vertical', command=self.all_tree.yview)
        self.all_tree.configure(yscrollcommand=left_scroll.set)
        self.all_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        left_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # ── RIGHT: 이번 공고 적용 실적 ────────────────────────────
        right_frame = ttk.LabelFrame(paned, text='이번 공고 적용 실적', padding=4)
        paned.add(right_frame, weight=1)

        arrow_frame = ttk.Frame(right_frame)
        arrow_frame.pack(fill=tk.X, pady=4)
        ttk.Button(arrow_frame, text='← 이 공고에 포함',
                   command=self._include_project).pack(side=tk.LEFT, padx=4)
        ttk.Button(arrow_frame, text='제외 →',
                   command=self._exclude_project).pack(side=tk.LEFT, padx=4)

        self.bid_tree = ttk.Treeview(right_frame, columns=cols, show='headings', height=12)
        for col in cols:
            self.bid_tree.heading(col, text=col)
            width = 180 if col == '사업명' else 90 if col in ('계약일', '완료일') else 110
            self.bid_tree.column(col, width=width, anchor='center')
        right_scroll = ttk.Scrollbar(right_frame, orient='vertical', command=self.bid_tree.yview)
        self.bid_tree.configure(yscrollcommand=right_scroll.set)
        self.bid_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Summary
        summary_frame = ttk.Frame(right_frame)
        summary_frame.pack(fill=tk.X, pady=4)
        ttk.Label(summary_frame, text='총 실적금액:').pack(side=tk.LEFT, padx=4)
        self.total_amount_label = ttk.Label(summary_frame, text='0 원', foreground='blue')
        self.total_amount_label.pack(side=tk.LEFT, padx=4)
        ttk.Label(summary_frame, text=' | 평가점수:').pack(side=tk.LEFT)
        self.exp_score_label = ttk.Label(summary_frame, text='-', foreground='blue',
                                         font=('', 10, 'bold'))
        self.exp_score_label.pack(side=tk.LEFT, padx=4)

        ttk.Button(right_frame, text='저장', command=self.save).pack(pady=4)

        # Store project IDs in treeview item tags
        self._all_projects = []
        self._bid_project_ids = set()

    def _get_selected_all_id(self):
        sel = self.all_tree.selection()
        if not sel:
            return None
        return self.all_tree.item(sel[0])['tags'][0] if self.all_tree.item(sel[0])['tags'] else None

    def _get_selected_bid_id(self):
        sel = self.bid_tree.selection()
        if not sel:
            return None
        return self.bid_tree.item(sel[0])['tags'][0] if self.bid_tree.item(sel[0])['tags'] else None

    def _refresh_all_tree(self):
        self.all_tree.delete(*self.all_tree.get_children())
        self._all_projects = db.get_all_projects()
        for p in self._all_projects:
            self.all_tree.insert('', tk.END, tags=(p['id'],),
                                 values=(p.get('name', ''),
                                         p.get('client', ''),
                                         f"{int(p.get('amount', 0)):,}",
                                         p.get('contract_date', ''),
                                         p.get('completion_date', '')))

    def _refresh_bid_tree(self):
        self.bid_tree.delete(*self.bid_tree.get_children())
        if not self.app.current_bid_id:
            self._bid_project_ids = set()
            self.total_amount_label.config(text='0 원')
            self.exp_score_label.config(text='-')
            return

        bid_projects = db.get_bid_projects(self.app.current_bid_id)
        self._bid_project_ids = {p['id'] for p in bid_projects}
        total = 0
        for p in bid_projects:
            self.bid_tree.insert('', tk.END, tags=(p['id'],),
                                 values=(p.get('name', ''),
                                         p.get('client', ''),
                                         f"{int(p.get('amount', 0)):,}",
                                         p.get('contract_date', ''),
                                         p.get('completion_date', '')))
            total += p.get('amount', 0) or 0

        self.total_amount_label.config(text=f'{total:,} 원')
        self._update_score(total)

    def _update_score(self, total_amount=None):
        if not self.app.current_bid_id:
            return
        bid = db.get_bid(self.app.current_bid_id)
        if not bid:
            return

        if total_amount is None:
            bid_projects = db.get_bid_projects(self.app.current_bid_id)
            total_amount = sum(p.get('amount', 0) or 0 for p in bid_projects)

        max_score = bid.get('score_experience', 6.0)
        bid_amount = bid.get('bid_amount', 0) or 0
        score, pct = calc_experience_score(total_amount, bid_amount, max_score)
        self.exp_score_label.config(text=f'{score:.2f}점 / {max_score:.1f}점  ({pct:.1f}%)')

    def _include_project(self):
        pid = self._get_selected_all_id()
        if pid is None:
            return
        pid = int(pid)
        if pid not in self._bid_project_ids:
            self._bid_project_ids.add(pid)
            self._refresh_bid_tree()

    def _exclude_project(self):
        pid = self._get_selected_bid_id()
        if pid is None:
            return
        pid = int(pid)
        if pid in self._bid_project_ids:
            self._bid_project_ids.discard(pid)
            self._refresh_bid_tree()

    def _add_project(self):
        dialog = ProjectDialog(self, title='프로젝트 추가')
        self.wait_window(dialog)
        if dialog.result:
            data = dialog.result
            db.add_project(
                name=data['name'],
                client=data['client'],
                amount=data['amount'],
                contract_date=data['contract_date'],
                completion_date=data['completion_date'],
                certificate_path=data['certificate_path'],
            )
            self._refresh_all_tree()

    def _edit_project(self):
        pid = self._get_selected_all_id()
        if pid is None:
            messagebox.showinfo('알림', '수정할 프로젝트를 선택하세요.')
            return
        pid = int(pid)
        project = next((p for p in self._all_projects if p['id'] == pid), None)
        if not project:
            return
        dialog = ProjectDialog(self, title='프로젝트 수정', project=project)
        self.wait_window(dialog)
        if dialog.result:
            data = dialog.result
            db.update_project(
                pid,
                name=data['name'],
                client=data['client'],
                amount=data['amount'],
                contract_date=data['contract_date'],
                completion_date=data['completion_date'],
                certificate_path=data['certificate_path'],
            )
            self._refresh_all_tree()
            self._refresh_bid_tree()

    def _delete_project(self):
        pid = self._get_selected_all_id()
        if pid is None:
            messagebox.showinfo('알림', '삭제할 프로젝트를 선택하세요.')
            return
        pid = int(pid)
        project = next((p for p in self._all_projects if p['id'] == pid), None)
        if not project:
            return
        if messagebox.askyesno('삭제 확인',
                               f"프로젝트 '{project['name']}'을(를) 삭제하시겠습니까?"):
            db.delete_project(pid)
            self._refresh_all_tree()
            self._refresh_bid_tree()

    def refresh(self):
        self._refresh_all_tree()
        self._refresh_bid_tree()

    def save(self):
        if not self.app.current_bid_id:
            messagebox.showinfo('알림', '공고를 먼저 선택하세요.')
            return
        db.set_bid_projects(self.app.current_bid_id, list(self._bid_project_ids))
        self.app.update_status()
        messagebox.showinfo('저장', '수행경험 정보가 저장되었습니다.')


class ProjectDialog(tk.Toplevel):
    def __init__(self, parent, title='프로젝트', project=None):
        super().__init__(parent)
        self.title(title)
        self.geometry('500x360')
        self.resizable(False, False)
        self.grab_set()
        self.result = None

        fields = [
            ('사업명', 'name', ''),
            ('발주처', 'client', ''),
            ('계약금액 (VAT포함, 원)', 'amount', ''),
            ('계약일 (YYYY.MM.DD)', 'contract_date', ''),
            ('완료일 (YYYY.MM.DD)', 'completion_date', ''),
        ]

        self._vars = {}
        for row_idx, (label, key, default) in enumerate(fields):
            ttk.Label(self, text=label + ':').grid(
                row=row_idx, column=0, sticky='e', padx=10, pady=6)
            var = tk.StringVar(value=default)
            self._vars[key] = var
            entry = ttk.Entry(self, textvariable=var, width=38)
            entry.grid(row=row_idx, column=1, padx=10, pady=6, sticky='w')

        # Certificate file
        cert_row = len(fields)
        ttk.Label(self, text='나라장터 실적증명서:').grid(
            row=cert_row, column=0, sticky='e', padx=10, pady=6)
        self._cert_var = tk.StringVar()
        cert_label = ttk.Label(self, textvariable=self._cert_var,
                               width=30, anchor='w', foreground='gray')
        cert_label.grid(row=cert_row, column=1, sticky='w', padx=10)
        ttk.Button(self, text='파일 선택',
                   command=self._select_cert).grid(row=cert_row + 1, column=1,
                                                   sticky='w', padx=10, pady=4)

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=cert_row + 2, column=0, columnspan=2, pady=12)
        ttk.Button(btn_frame, text='확인', command=self._ok).pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_frame, text='취소', command=self.destroy).pack(side=tk.LEFT, padx=8)

        if project:
            self._vars['name'].set(project.get('name', ''))
            self._vars['client'].set(project.get('client', '') or '')
            amount = project.get('amount', 0) or 0
            self._vars['amount'].set(f'{int(amount):,}' if amount else '')
            self._vars['contract_date'].set(project.get('contract_date', '') or '')
            self._vars['completion_date'].set(project.get('completion_date', '') or '')
            cert = project.get('certificate_path', '') or ''
            self._cert_var.set(cert)

    def _select_cert(self):
        path = filedialog.askopenfilename(
            title='실적증명서 선택',
            filetypes=[('All files', '*.*'), ('PDF files', '*.pdf')]
        )
        if path:
            self._cert_var.set(os.path.abspath(path))

    def _ok(self):
        name = self._vars['name'].get().strip()
        if not name:
            messagebox.showwarning('입력 오류', '사업명을 입력하세요.', parent=self)
            return
        amount_str = self._vars['amount'].get().replace(',', '').strip()
        try:
            amount = int(amount_str) if amount_str else 0
        except ValueError:
            amount = 0
        self.result = {
            'name': name,
            'client': self._vars['client'].get().strip(),
            'amount': amount,
            'contract_date': self._vars['contract_date'].get().strip(),
            'completion_date': self._vars['completion_date'].get().strip(),
            'certificate_path': self._cert_var.get().strip(),
        }
        self.destroy()
