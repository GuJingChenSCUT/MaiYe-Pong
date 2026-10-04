"""Run the local MVP; credentials and runtime state never enter the deliverable."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "checkpoint" / "engineering"))
from app.auth import Auth
from app.task_store import TaskStore
from app.task_api import ApplicationServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "local_state")
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    store = TaskStore(args.state_dir / "maiyebang.sqlite")
    credentials = args.state_dir / "access_private.json"
    if not credentials.exists():
        auth = Auth(store)
        codes = {}
        for role, merchant in (("buyer", None), ("merchant", "fixture-merchant-A"), ("operator", None)):
            codes[role] = auth.provision(actor_id="local-" + role, role=role, merchant_id=merchant)
        credentials.write_text(json.dumps(codes), encoding="utf-8")
    server = ApplicationServer(("127.0.0.1", args.port), store)
    print(f"MaiYeBang ready: http://127.0.0.1:{args.port}. Synthetic catalog / LocalPSP; no real payment.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
