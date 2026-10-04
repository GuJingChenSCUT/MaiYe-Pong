"""Show two explicitly enabled local test buyers; no private credential reads."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "checkpoint" / "engineering"))
from app.local_demo_auth import LOCAL_DEMO_BINDINGS


def show():
    import tkinter as tk
    from tkinter import ttk
    window = tk.Tk()
    window.title("MaiYePong — 本機模擬買家")
    window.geometry("550x245")
    window.attributes("-topmost", True)
    ttk.Label(window, text="兩個獨立的本機模擬買家").pack(pady=(18, 8))
    ttk.Label(window, text="只在明確啟用的本機演示服務有效，沒有商戶或營運權限。").pack()
    for number, code in enumerate(LOCAL_DEMO_BINDINGS, 1):
        row = ttk.Frame(window); row.pack(pady=8)
        ttk.Label(row, text=f"模擬買家 {number}", width=15).pack(side="left")
        value = tk.StringVar(value=code)
        entry = ttk.Entry(row, textvariable=value, width=15, state="readonly")
        entry.pack(side="left", padx=10)
        # Keep each Tcl variable alive; bind each callback to its own code.
        entry._demo_value = value
        def copy(value=code):
            window.clipboard_clear(); window.clipboard_append(value)
        ttk.Button(row, text="複製", command=copy).pack(side="left")
    ttk.Label(window, text="先登出其他身份，再將選定代碼貼到網頁「存取碼」欄。").pack(pady=8)
    ttk.Label(window, text="Google／微信帳戶及真實收款尚未接通。").pack()
    window.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", required=True)
    parser.parse_args()
    show()
