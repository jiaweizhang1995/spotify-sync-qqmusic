"""QQ Music QR login — reusable by bootstrap + setup wizard."""

from __future__ import annotations

import asyncio
import io
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


def _decode_qr_url(png_bytes: bytes) -> str | None:
    try:
        img = Image.open(io.BytesIO(png_bytes))
        results = zxingcpp.read_barcodes(img)
        if results:
            return results[0].text
    except Exception as exc:
        print(f"(QR decode failed: {exc})", file=sys.stderr)
    return None


def _print_ascii_qr(url: str) -> None:
    qr = qrcode.QRCode(
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
    except Exception:
        pass


async def _run(login_type: QRLoginType) -> dict:
    client = Client()
    try:
        qr = await client.login.get_qrcode(login_type)

        print(
            "\n用 **手机 QQ**（不是 QQ 音乐）扫下面的二维码 → 在手机上确认登录：\n",
            file=sys.stderr,
        )

        url = _decode_qr_url(qr.data)
        if url:
            _print_ascii_qr(url)
            print(f"\n(备用链接: {url})", file=sys.stderr)
        png_path = _save_png(qr.data)
        print(f"(二维码图也存了一份: {png_path})\n", file=sys.stderr)
        if not url:
            _open_image(png_path)

        print("等待扫码...", file=sys.stderr)
        waited = 0
        last_event = None
        while waited < POLL_TIMEOUT_SEC:
            result = await client.login.check_qrcode(qr)
            if result.event != last_event:
                print(f"  状态: {result.event.name}", file=sys.stderr)
                last_event = result.event
            if result.event == QRCodeLoginEvents.DONE and result.credential:
                return result.credential.model_dump(by_alias=True)
            if result.event in {
                QRCodeLoginEvents.TIMEOUT,
                QRCodeLoginEvents.REFUSE,
                QRCodeLoginEvents.OTHER,
            }:
                raise RuntimeError(f"QR 登录失败: {result.event.name}")
            await asyncio.sleep(POLL_INTERVAL_SEC)
            waited += POLL_INTERVAL_SEC

        raise TimeoutError("QR 登录超时 (180s)")
    finally:
        await client.close()


def fetch_credential(login_type: QRLoginType = QRLoginType.QQ) -> dict:
    """Blocking call — run QR login flow, return the credential dict."""
    return asyncio.run(_run(login_type))
