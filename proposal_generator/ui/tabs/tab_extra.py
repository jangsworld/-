"""
Tab: 추가 항목
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os

from core import database as db


class TabExtra(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._build_ui()

    def _build_ui(self):
        # ── Toolbar ───────────────────────────────────────────────
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, padx=8, pady=4)
        ttk.Button(toolbar, text='항목 추가', command=self._add_item).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text='수정', command=self._edit_item).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text='삭제', command=self._delete_item).pack(side=tk.LEFT, padx=4)

        # ── Treeview ──────────────────────────────────────────────
        cols = ('항목명', '배점', '취득점수', '설명')
        self.tree = ttk.Treeview(self, columns=cols, show='headings', height=16)
        for col in cols:
            self.tree.heading(col, text=col)
            w = 200 if col == '항목명' else 80 if col in ('배점', '취득점수') else 300
            self.tree.column(col, width=w, anchor='center' if col != '설명' else 'w')
        tree_scroll = ttk.Scrollbar(self, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, in_=tree_frame)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y, in_=tree_frame)

        # ── Summary ───────────────────────────────────────────────
        summary_frame = ttk.Frame(self)
        summary_frame.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(summary_frame, text='추가항목 합계:').pack(side=tk.LEFT, padx=4)
        self.total_label = ttk.Label(summary_frame, text='0.00점',
                                     foreground='blue', font=('', 11, 'bold'))
        self.total_label.pack(side=tk.LEFT, padx=4)

        ttk.Button(self, text='저장', command=self.save).pack(pady=6)

        self._items = []

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        total = 0.0
        for item in self._items:
            self.tree.insert('', tk.END, tags=(item['id'],),
                             values=(item.get('name', ''),
                                     f"{item.get('max_score', 0):.1f}점",
                                     f"{item.get('actual_score', 0):.2f}점",
                                     item.get('description', '')))
            total += item.get('actual_score', 0) or 0
        self.total_label.config(text=f'{total:.2f}점')

    def _get_selected_id(self):
        sel = self.tree.selection()
        if not sel:
            return None
        tags = self.tree.item(sel[0])['tags']
        return int(tags[0]) if tags else None

    def _add_item(self):
        if not self.app.current_bid_id:
            messagebox.showinfo('알림', '공고를 먼저 선택하세요.')
            return
        dialog = ExtraItemDialog(self, title='항목 추가')
        self.wait_window(dialog)
        if dialog.result:
            data = dialog.result
            db.add_extra_item(
                self.app.current_bid_id,
                name=data['name'],
                max_score=data['max_score'],
                actual_score=data['actual_score'],
                description=data['description'],
            )
            self.refresh()

    def _edit_item(self):
        item_id = self._get_selected_id()
        if item_id is None:
            messagebox.showinfo('알림', '수정할 항목을 선택하세요.')
            return
        item = next((i for i in self._items if i['id'] == item_id), None)
        if not item:
            return
        dialog = ExtraItemDialog(self, title='항목 수정', item=item)
        self.wait_window(dialog)
        if dialog.result:
            data = dialog.result
            db.update_extra_item(
                item_id,
                name=data['name'],
                max_score=data['max_score'],
                actual_score=data['actual_score'],
                description=data['description'],
            )
            self.refresh()

    def _delete_item(self):
        item_id = self._get_selected_id()
        if item_id is None:
            messagebox.showinfo('알림', '삭제할 항목을 선택하세요.')
            return
        item = next((i for i in self._items if i['id'] == item_id), None)
        if not item:
            return
        if messagebox.askyesno('삭제 확인',
                               f"항목 '{item['name']}'을(를) 삭제하시겠습니까?"):
            db.delete_extra_item(item_id)
            self.refresh()

    def refresh(self):
        if not self.app.current_bid_id:
            self._items = []
            self._refresh_tree()
            return
        self._items = db.get_extra_items(self.app.current_bid_id)
        self._refresh_tree()

    def save(self):
        # Extra items are saved immediately via DB calls in add/edit/delete
        self.app.update_status()
        messagebox.showinfo('저장', '추가 항목 정보가 저장되었습니다.')


class ExtraItemDialog(tk.Toplevel):
    def __init__(self, parent, title='항목', item=None):
        super().__init__(parent)
        self.title(title)
        self.geometry('480x320')
        self.resizable(False, False)
        self.grab_set()
        self.result = None

        ttk.Label(self, text='항목명:').grid(row=0, column=0, sticky='e', padx=10, pady=8)
        self.name_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.name_var, width=35).grid(
            row=0, column=1, padx=10, pady=8, sticky='w')

        ttk.Label(self, text='배점:').grid(row=1, column=0, sticky='e', padx=10, pady=8)
        self.max_score_var = tk.StringVar(value='0.0')
        ttk.Spinbox(self, from_=0.0, to=50.0, increment=0.5,
                    textvariable=self.max_score_var, width=10).grid(
            row=1, column=1, padx=10, pady=8, sticky='w')

        ttk.Label(self, text='취득점수:').grid(row=2, column=0, sticky='e', padx=10, pady=8)
        self.actual_score_var = tk.StringVar(value='0.0')
        ttk.Spinbox(self, from_=0.0, to=50.0, increment=0.1,
                    textvariable=self.actual_score_var, width=10).grid(
            row=2, column=1, padx=10, pady=8, sticky='w')

        ttk.Label(self, text='설명:').grid(row=3, column=0, sticky='ne', padx=10, pady=8)
        self.desc_text = tk.Text(self, width=35, height=5)
        self.desc_text.grid(row=3, column=1, padx=10, pady=8, sticky='w')

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=5, column=0, columnspan=2, pady=12)
        ttk.Button(btn_frame, text='확인', command=self._ok).pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_frame, text='취소', command=self.destroy).pack(side=tk.LEFT, padx=8)

        if item:
            self.name_var.set(item.get('name', ''))
            self.max_score_var.set(str(item.get('max_score', 0.0)))
            self.actual_score_var.set(str(item.get('actual_score', 0.0)))
            self.desc_text.insert('1.0', item.get('description', '') or '')

    def _ok(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning('입력 오류', '항목명을 입력하세요.', parent=self)
            return
        try:
            max_score = float(self.max_score_var.get())
        except ValueError:
            max_score = 0.0
        try:
            actual_score = float(self.actual_score_var.get())
        except ValueError:
            actual_score = 0.0
        desc = self.desc_text.get('1.0', tk.END).strip()
        self.result = {
            'name': name,
            'max_score': max_score,
            'actual_score': actual_score,
            'description': desc,
        }
        self.destroy()
