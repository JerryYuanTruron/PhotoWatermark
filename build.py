"""构建并给产物写入可回溯档案 BUILD_INFO.txt。

用法：py -3 build.py
产物：dist/PhotoWatermark.exe（onefile，windowed）+ dist/BUILD_INFO.txt
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src import __version__  # noqa: E402

EXE = ROOT / "dist" / "PhotoWatermark.exe"


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=str(ROOT), text=True,
                                       encoding="utf-8").strip()
    except subprocess.CalledProcessError:
        return "unknown"


def main() -> int:
    # git 元信息必须在构建前采集：构建本身会生成 exe / spec / BUILD_INFO，
    # 之后再查会把「构建产物」误记成「源码有未提交改动」。
    # --untracked-files=no：只看已跟踪源码，未跟踪的构建产物不计入 dirty。
    head = _git("rev-parse", "HEAD")
    short = _git("rev-parse", "--short", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    subject = _git("log", "-1", "--pretty=%s")
    if dirty and "--allow-dirty" not in sys.argv:
        print("[FATAL] 工作区存在未提交的源码改动，拒绝产出可交付 exe。")
        print("        请先 git commit；确需临时构建请加 --allow-dirty（产物不得交付）。")
        return 1

    source_files = [ROOT / "main.py"] + sorted((ROOT / "src").glob("*.py"))
    source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in source_files}

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    # 旧产物改名归档（不删除，规避批量删除配额，同时保留可回滚版本）
    if EXE.is_file():
        stamp = time.strftime("%Y%m%d_%H%M%S")
        EXE.rename(dist / f"PhotoWatermark_prev_{stamp}.exe")

    # 每次构建使用独立的中间目录：PyInstaller --noconfirm 会清空 workpath，
    # 复用 build/ 等于每轮批量删除数百个中间文件（运行环境对此需要人工授权）。
    # 独立目录既避免批量删除，也天然保留上一次构建的中间物便于排查。
    workpath = ROOT / "build" / f"w_{time.strftime('%Y%m%d_%H%M%S')}"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed",
        "--name", "PhotoWatermark",
        "--distpath", str(dist),
        "--workpath", str(workpath),
        "--specpath", str(ROOT),
        "--hidden-import", "PIL",
        "--collect-submodules", "src",
        str(ROOT / "main.py"),
    ]
    print("构建命令：", " ".join(cmd[:8]), "...")
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0 or not EXE.is_file():
        print(f"[FAIL] 构建失败 rc={rc}")
        return 1

    data = EXE.read_bytes()
    try:
        py_inst = subprocess.check_output(
            [sys.executable, "-m", "PyInstaller", "--version"], text=True).strip()
    except subprocess.CalledProcessError:
        py_inst = "unknown"
    info = "\n".join([
        f"app              : 批量照片时间水印处理程序",
        f"version          : {__version__}",
        f"build_time       : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"python           : {sys.version.split()[0]} ({sys.executable})",
        f"pyinstaller      : {py_inst}",
        f"bundle_mode      : onefile / windowed",
        f"git_head         : {head}",
        f"git_short        : {short}",
        f"git_dirty        : {dirty}",
        f"git_subject      : {subject}",
        f"exe_size_bytes   : {len(data)}",
        f"exe_sha256       : {hashlib.sha256(data).hexdigest()}",
        f"entry            : main.py",
        "src_sha256       :",
    ] + [f"  {name} = {digest}" for name, digest in sorted(source_hashes.items())]) + "\n"
    (dist / "BUILD_INFO.txt").write_text(info, encoding="utf-8")
    print(info)

    # 交付目录纯净：rules/ 是程序运行时写在 exe 同级的启动配置缓存，
    # 一旦被上一次运行写回（ECU 名称、序列号、输出目录），整目录分发就会预填他人数据。
    stale_rules = dist / "rules"
    if stale_rules.is_dir():
        for f in stale_rules.iterdir():
            if f.is_file():
                f.unlink()
        stale_rules.rmdir()
        print("[CLEAN] 已移除 dist/rules（启动配置缓存不随包分发）")
    leftover = dist / "输出照片"
    if leftover.is_dir() and any(leftover.iterdir()):
        print(f"[WARN] dist/输出照片 仍有 {len(list(leftover.iterdir()))} 项，分发前请清理")

    print(f"[OK] 产物：{EXE}  ({len(data) / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
