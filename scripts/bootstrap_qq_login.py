"""Local-only QQ Music QR login bootstrap.

Run once locally:
    python scripts/bootstrap_qq_login.py                # QQ App (default)
    python scripts/bootstrap_qq_login.py --type wx      # WeChat App

The QR login core lives in `src/qq_qr_login.py` so `spotify-sync setup`
can reuse the same flow.

IMPORTANT: Scan the QR with the **QQ App** (not QQ Music App).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.qq_qr_login import fetch_credential  # noqa: E402
from qqmusic_api.models.login import QRLoginType  # noqa: E402

_TYPE_MAP = {
    "qq": QRLoginType.QQ,
    "wx": QRLoginType.WX,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ Music QR login bootstrap")
    parser.add_argument(
        "--type",
        choices=list(_TYPE_MAP.keys()),
        default="qq",
        help="Login scheme (default: qq — scan with QQ App)",
    )
    args = parser.parse_args()
    try:
        cred_dict = fetch_credential(_TYPE_MAP[args.type])
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    pretty = json.dumps(cred_dict, ensure_ascii=False, indent=2)
    compact = json.dumps(cred_dict, ensure_ascii=False)
    print("\n--- credential (pretty) ---", file=sys.stderr)
    print(pretty)
    print(
        "\n--- credential (single-line, paste into .env or GH secret) ---",
        file=sys.stderr,
    )
    print(f"QQ_CREDENTIAL_JSON={compact}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
