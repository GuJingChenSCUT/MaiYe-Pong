"""Provision one local buyer demo identity; never print its access code.

Run after start-local.ps1. --show opens a local, read-only credential window.
This is an alternative local login, not an emulation of Google or WeChat OAuth.
"""
from pathlib import Path
import argparse
import hashlib
import json
import secrets
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
ACTOR = "local-demo-buyer"
TENANT = "local-hk"


def provision(state_dir):
    state_dir = Path(state_dir).resolve()
    db = state_dir / "maiyebang.sqlite"
    record_path = state_dir / "demo_account_private.json"
    if not db.is_file():
        raise RuntimeError("Start the local application first; no database will be created here.")
    conn = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("PRAGMA quick_check").fetchall()[0][0] != "ok":
            raise RuntimeError("Database integrity check failed; do not provision accounts.")
        conn.execute("BEGIN IMMEDIATE")
        if record_path.exists():
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise RuntimeError("Saved demo credential must be an object; recover it explicitly.")
            code = record.get("access_code")
            if not isinstance(code, str) or not 24 <= len(code) <= 200:
                raise RuntimeError("Saved demo credential is invalid; recover it explicitly.")
            row = conn.execute("SELECT * FROM app_access_codes WHERE code_hash=?",
                               (hashlib.sha256(code.encode()).hexdigest(),)).fetchone()
            if (row is None or row["actor_id"] != ACTOR or row["tenant_id"] != TENANT
                    or row["role"] != "buyer" or row["merchant_id"] is not None or row["active"] != 1):
                raise RuntimeError("Saved demo credential does not match an active buyer in this database; no automatic repair.")
        else:
            if conn.execute("SELECT 1 FROM app_access_codes WHERE actor_id=? AND tenant_id=?", (ACTOR,TENANT)).fetchone():
                raise RuntimeError("Demo identity already exists without its local credential file; recover it explicitly.")
            code = secrets.token_urlsafe(24)
            record = {"account": "demo-buyer", "access_code": code, "role": "buyer", "origin": "http://127.0.0.1:8765/"}
            conn.execute("INSERT INTO app_access_codes(code_hash,tenant_id,actor_id,role) VALUES(?,?,?,'buyer')",
                         (hashlib.sha256(code.encode()).hexdigest(), TENANT, ACTOR))
            # Exclusive creation avoids replacing a user's existing credential.
            with record_path.open("x", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
        conn.commit()
        instructions = ("買嘢幫 — 本地模擬買家\n\n帳戶：demo-buyer\n"
                        "在 http://127.0.0.1:8765/ 的「存取碼」欄貼上以下代碼：\n\n" + code +
                        "\n\n這是本機 buyer 身份，並非 Google／微信帳戶。\n"
                        "請從 docs/demo/cases.txt 複製一條完整需求到輸入框；沒有真實商戶訂單或錢包扣款。\n"
                        "請勿把本檔案上傳 GitHub 或分享給他人。\n")
        (state_dir / "demo_account_private.txt").write_text(instructions, encoding="utf-8")
        return record
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def show(record):
    import tkinter as tk
    from tkinter import ttk
    window = tk.Tk()
    window.title("MaiYePong — 本地模擬買家")
    window.geometry("560x225")
    window.attributes("-topmost", True)
    ttk.Label(window, text="帳戶：demo-buyer（僅本地買家）").pack(pady=(20,8))
    ttk.Label(window, text="將以下存取碼貼入買嘢幫登入頁：").pack()
    code = tk.StringVar(value=record["access_code"])
    ttk.Entry(window, textvariable=code, state="readonly", width=52).pack(pady=12)
    def copy():
        window.clipboard_clear()
        window.clipboard_append(code.get())
    ttk.Button(window, text="複製存取碼", command=copy).pack()
    ttk.Label(window, text="這不是 Google／微信帳戶，沒有商戶或運營權限。").pack(pady=12)
    window.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "local_state")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    try:
        record = provision(args.state_dir)
        print(json.dumps({"account": "demo-buyer", "role": "buyer", "status": "ready", "code_printed": False}))
        if args.show:
            show(record)
    except (RuntimeError, sqlite3.Error, ValueError, OSError):
        raise SystemExit("Demo account unavailable. Check the existing database and matching private credential; no new database or role was substituted.") from None


if __name__ == "__main__":
    main()
