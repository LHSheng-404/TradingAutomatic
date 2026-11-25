"""
SYNAPSE TW — 跨平台一鍵打包腳本 (package.py)
執行方式：
  python3 package.py --version v1.0.0

產出於 dist/：
1. 獨立免安裝可執行檔（透過 PyInstaller，支援直接執行免裝 Python）：
   - Linux:   SYNAPSE-TW-v1.0.0-linux-x86_64
   - Windows: SYNAPSE-TW-v1.0.0-windows-x86_64.exe
   - macOS:   SYNAPSE-TW-v1.0.0-macos-<arch>
2. 跨平台可攜式一鍵執行包：
   - SYNAPSE-TW-v1.0.0-portable.zip
   - SYNAPSE-TW-v1.0.0-portable.tar.gz
3. SHA256SUMS.txt 雜湊校驗檔
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"

PORTABLE_FILES = [
    "index.html",
    "manifest.webmanifest",
    "sw.js",
    "icons/icon-192.png",
    "icons/icon-512.png",
    "icons/apple-touch-icon.png",
    "icons/apple-touch-icon-167.png",
    "icons/apple-touch-icon-152.png",
    "icons/apple-touch-icon-120.png",
    "mobile/termux-start.sh",
    "server.py",
    "strategy30_engine.py",
    "launcher.py",
    "start.sh",
    "start.bat",
    "requirements.txt",
    ".env.example",
    "Dockerfile",
    "render.yaml",
    "vercel.json",
    "README.md",
    "LICENSE",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_portable_archives(version: str) -> list[Path]:
    DIST.mkdir(parents=True, exist_ok=True)
    folder_name = f"SYNAPSE-TW-{version}-portable"
    zip_path = DIST / f"{folder_name}.zip"
    tar_path = DIST / f"{folder_name}.tar.gz"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in PORTABLE_FILES:
            src = ROOT / rel
            if src.exists():
                zf.write(src, arcname=f"{folder_name}/{rel}")

    with tarfile.open(tar_path, "w:gz") as tf:
        for rel in PORTABLE_FILES:
            src = ROOT / rel
            if src.exists():
                tf.add(src, arcname=f"{folder_name}/{rel}")

    print(f"[OK] Portable ZIP:    {zip_path} ({zip_path.stat().st_size:,} bytes)")
    print(f"[OK] Portable TAR.GZ: {tar_path} ({tar_path.stat().st_size:,} bytes)")
    return [zip_path, tar_path]


def build_standalone_binary(version: str) -> Path | None:
    sys_name = platform.system().lower()
    if sys_name == "darwin":
        sys_name = "macos"
    arch = platform.machine().lower()
    if arch in ("amd64", "x86_64"):
        arch = "x64"
    elif arch in ("aarch64", "arm64"):
        arch = "arm64"

    ext = ".exe" if sys_name == "windows" else ""
    bin_name = f"SYNAPSE-TW-{version}-{sys_name}-{arch}{ext}"
    add_data_sep = ";" if sys_name == "windows" else ":"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        bin_name[:-4] if ext == ".exe" else bin_name,
        "--add-data",
        f"{ROOT / 'index.html'}{add_data_sep}.",
        "--add-data",
        f"{ROOT / 'manifest.webmanifest'}{add_data_sep}.",
        "--add-data",
        f"{ROOT / 'sw.js'}{add_data_sep}.",
        "--add-data",
        f"{ROOT / 'icons'}{add_data_sep}icons",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols",
        "--hidden-import",
        "uvicorn.protocols.http",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan",
        "--hidden-import",
        "uvicorn.lifespan.on",
        str(ROOT / "launcher.py"),
    ]

    print(f"[BUILD] Compiling standalone binary: {bin_name}")
    res = subprocess.run(cmd, cwd=str(ROOT))
    if res.returncode != 0:
        print("[WARN] PyInstaller build failed; skipping standalone binary.")
        return None

    out_path = DIST / bin_name
    if out_path.exists():
        if sys_name != "windows":
            os.chmod(out_path, 0o755)
        print(f"[OK] Standalone binary: {out_path} ({out_path.stat().st_size:,} bytes)")
        return out_path
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="SYNAPSE TW 一鍵打包工具")
    parser.add_argument("--version", default="v1.0.0", help="發行版本號（預設 v1.0.0）")
    parser.add_argument("--skip-binary", action="store_true", help="僅打包可攜式壓縮檔，略過 PyInstaller")
    args = parser.parse_args()

    if BUILD.exists():
        shutil.rmtree(BUILD, ignore_errors=True)

    artifacts: list[Path] = []
    artifacts.extend(build_portable_archives(args.version))

    if not args.skip_binary:
        bin_path = build_standalone_binary(args.version)
        if bin_path:
            artifacts.append(bin_path)

    sums_path = DIST / "SHA256SUMS.txt"
    lines = [f"{sha256_file(p)}  {p.name}" for p in artifacts if p.exists()]
    sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] Checksums:       {sums_path}")


if __name__ == "__main__":
    main()
