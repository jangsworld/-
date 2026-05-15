"""
정량적 제안서 자동 생성 프로그램
Entry point
"""
import tkinter as tk
from core.database import init_db
from ui.app import ProposalApp


def main():
    init_db()
    root = tk.Tk()
    root.title("정량적 제안서 자동 생성 프로그램")
    root.geometry("1200x800")
    root.minsize(1000, 700)
    app = ProposalApp(root)
    app.pack(fill=tk.BOTH, expand=True)
    root.mainloop()


if __name__ == '__main__':
    main()
