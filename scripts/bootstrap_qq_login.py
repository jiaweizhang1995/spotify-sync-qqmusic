"""Local-only QQ Music QR login bootstrap.

Run once locally:
    python scripts/bootstrap_qq_login.py                # QQ App (default)
    python scripts/bootstrap_qq_login.py --type wx      # WeChat App
    python scripts/bootstrap_qq_login.py --type mobile  # SMS (no QR)

IMPORTANT: Scan the QR code with the **QQ App** (not QQ Music App).
The QQ App login grants QQ Music access because both use the same
QQ account — QQ Music App itself cannot scan this QR code.

The QR code is decoded from the server-provided PNG and re-rendered
as ASCII in the terminal so you can scan it in place. The original
PNG is also saved to a temp file as a fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile

import qrcode
import zxingcpp
from PIL import Image
from qqmusic_api import Client
from qqmusic_api.models.login import QRCodeLoginEvents, QRLoginType

POLL_INTERVAL_SEC = 2
POLL_TIMEOUT_SEC = 180

_TYPE_MAP = {
    "qq": (QRLoginType.QQ, "QQ App (手机QQ)"),
    "wx": (QRLoginType.WX, "WeChat App (微信)"),
    "mobile": (QRLoginType.MOBILE, "Mobile SMS (no QR)"),
}


def _decode_qr_url(png_bytes: bytes) -> str | None:
    """Decode the QR payload (a URL) from the PNG bytes."""
    try:
        img = Image.open(io.BytesIO(png_bytes))
        results = zxingcpp.read_barcodes(img)
        if results:
            return results[0].text
    except Exception as exc:
        print(f"(QR decode failed: {exc})", file=sys.stderr)
    return None


def _print_ascii_qr(url: str) -> None:
    """Render the URL as an ASCII QR code in the terminal."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stderr, invert=True)


def _save_png(png_bytes: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".png", prefix="qq_login_")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(png_bytes)
    return path


def _open_image(path: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", path])
        elif sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
    except Exception as exc:
        print(f"(could not auto-open QR image: {exc})", file=sys.stderr)


async def _run_qr_flow(login_type: QRLoginType, label: str) -> dict:
    client = Client()
    try:
        qr = await client.login.get_qrcode(login_type)

        print(f"\n== QQ Music login via {label} ==", file=sys.stderr)
        print("Scan the QR below with the **QQ App** (手机QQ).", file=sys.stderr)
        print("Do NOT use QQ Music App — that will not work.\n", file=sys.stderr)

        url = _decode_qr_url(qr.data)
        if url:
            _print_ascii_qr(url)
            print(f"\nRaw QR URL (fallback): {url}", file=sys.stderr)
        else:
            print("(could not decode QR payload — falling back to image)", file=sys.stderr)

        png_path = _save_png(qr.data)
        print(f"PNG saved: {png_path}", file=sys.stderr)
        if not url:
            _open_image(png_path)

        print("\nWaiting for scan...", file=sys.stderr)
        waited = 0
        last_event = None
        while waited < POLL_TIMEOUT_SEC:
            result = await client.login.check_qrcode(qr)
            if result.event != last_event:
                print(f"  status: {result.event.name}", file=sys.stderr)
                last_event = result.event
            if result.event == QRCodeLoginEvents.DONE and result.credential:
                return result.credential.model_dump(by_alias=True)
            if result.event in {
                QRCodeLoginEvents.TIMEOUT,
                QRCodeLoginEvents.REFUSE,
                QRCodeLoginEvents.OTHER,
            }:
                raise RuntimeError(f"QR login failed: {result.event.name}")
            await asyncio.sleep(POLL_INTERVAL_SEC)
            waited += POLL_INTERVAL_SEC

        raise TimeoutError("QR login timed out — no scan within 180s")
    finally:
        await client.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QQ Music QR login bootstrap")
    parser.add_argument(
        "--type",
        choices=list(_TYPE_MAP.keys()),
        default="qq",
        help="Login scheme (default: qq — scan with QQ App)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    login_type, label = _TYPE_MAP[args.type]

    if args.type == "mobile":
        print(
            "Mobile SMS login not implemented in this script — "
            "use --type qq or --type wx for the QR flow.",
            file=sys.stderr,
        )
        return 1

    try:
        cred_dict = asyncio.run(_run_qr_flow(login_type, label))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    pretty = json.dumps(cred_dict, ensure_ascii=False, indent=2)
    compact = json.dumps(cred_dict, ensure_ascii=False)
    print("\n--- credential (pretty) ---", file=sys.stderr)
    print(pretty)
    print("\n--- credential (single-line, paste into .env or GH secret) ---", file=sys.stderr)
    print(f"QQ_CREDENTIAL_JSON={compact}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
