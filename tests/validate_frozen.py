"""冻结 exe 验收：版本元断言 + 端到端自检 + 双击启动窗口实测。

用法：py -3 tests/validate_frozen.py
前提：已执行 build.py 生成 dist/PhotoWatermark.exe 与 dist/BUILD_INFO.txt。

验收对象必须是「用户将要拿到的那份产物」，而不是源码；因此第一条断言是版本元断言，
不符立即中止，避免用旧版行为给新版盖章。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "PhotoWatermark.exe"
BUILD_INFO = ROOT / "dist" / "BUILD_INFO.txt"

PASS: list[str] = []
FAIL: list[str] = []


def ck(name: str, cond: bool, detail: object = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} — {detail}")


def source_version() -> str:
    # 期望值从被测源码读，不写死版本号（跨版本恒真）
    text = (ROOT / "src" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    assert m, "源码中找不到 __version__"
    return m.group(1)


def git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                                       text=True).strip()
    except subprocess.CalledProcessError:
        return "unknown"


def list_windows_by_title(needle: str) -> list[tuple[int, int, str, str]]:
    """全局枚举顶层可见窗口，按标题匹配（不能按 Popen.pid：onefile 会派生子进程）。"""
    user32 = ctypes.windll.user32
    results: list[tuple[int, int, str, str]] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title = buf.value
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if needle in title:
            results.append((int(hwnd), int(pid.value), title, cls.value))
        return True

    user32.EnumWindows(proc(cb), 0)
    return results


def main() -> int:
    want = source_version()
    print(f"\n[0] 版本元断言：源码声明 v{want}")
    ck("0.1 exe 存在", EXE.is_file(), str(EXE))
    ck("0.2 BUILD_INFO 存在", BUILD_INFO.is_file(), str(BUILD_INFO))
    if not EXE.is_file() or not BUILD_INFO.is_file():
        return 1
    info = BUILD_INFO.read_text(encoding="utf-8")
    m_ver = re.search(r"^version\s*:\s*(\S+)", info, re.M)
    m_head = re.search(r"^git_head\s*:\s*(\S+)", info, re.M)
    ck("0.3 BUILD_INFO 版本 == 源码版本", bool(m_ver) and m_ver.group(1) == want,
       m_ver.group(1) if m_ver else "未找到")
    ck("0.4 BUILD_INFO git HEAD == 当前 HEAD",
       bool(m_head) and m_head.group(1) == git_head(),
       f"构建={m_head.group(1) if m_head else '?'}, 当前={git_head()}")
    m_dirty = re.search(r"^git_dirty\s*:\s*(\S+)", info, re.M)
    ck("0.5 构建时源码工作区干净", bool(m_dirty) and m_dirty.group(1) == "False",
       f"git_dirty={m_dirty.group(1) if m_dirty else '未找到'}")
    m_exe_sha = re.search(r"^exe_sha256\s*:\s*(\S+)", info, re.M)
    import hashlib as _hashlib
    actual_sha = _hashlib.sha256(EXE.read_bytes()).hexdigest()
    ck("0.6 交付 exe 的 sha256 == BUILD_INFO 记录",
       bool(m_exe_sha) and m_exe_sha.group(1) == actual_sha,
       f"实测 {actual_sha[:16]}… / 记录 {(m_exe_sha.group(1)[:16] if m_exe_sha else '未找到')}…")
    # 源码哈希：检测「构建之后源码被改动」或「exe 是旧代码」
    recorded = dict(re.findall(r"^  (\S+\.py) = ([0-9a-f]{64})$", info, re.M))
    current = {"main.py": _hashlib.sha256((ROOT / "main.py").read_bytes()).hexdigest()}
    current.update({p.name: _hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((ROOT / "src").glob("*.py"))})
    ck("0.7 BUILD_INFO 源码哈希集合完整", set(recorded) == set(current),
       f"记录={sorted(recorded)}, 当前={sorted(current)}")
    drift = [k for k in current if recorded.get(k) != current[k]]
    ck("0.8 构建后源码零漂移", not drift, f"漂移文件={drift}")
    if drift:
        print("[FATAL] exe 不是由当前源码构建，中止后续断言")
        return 1

    with tempfile.TemporaryDirectory(prefix="pw_frozen_") as td:
        work = Path(td)

        # --- [1] 产物自述版本 ---
        vjson = work / "version.json"
        r = subprocess.run([str(EXE), "--version", str(vjson)], cwd=str(work),
                           timeout=120, capture_output=True)
        ck("1.1 --version 退出码 0", r.returncode == 0, r.returncode)
        ck("1.2 版本 JSON 已写出", vjson.is_file(), "")
        vdata = json.loads(vjson.read_text(encoding="utf-8")) if vjson.is_file() else {}
        ck("1.3 运行形态为 frozen", vdata.get("frozen") is True, vdata.get("frozen"))
        ck("1.4 产物自述版本 == 源码版本", vdata.get("version") == want,
           vdata.get("version"))
        if vdata.get("version") != want:
            print("[FATAL] 产物不是目标版本，中止后续断言")
            return 1

        # --- [2] 冻结产物端到端自检 ---
        st = work / "selftest"
        r = subprocess.run([str(EXE), "--selftest", str(st)], cwd=str(work),
                           timeout=180, capture_output=True)
        result_file = st / "frozen_selftest.json"
        ck("2.1 --selftest 退出码 0", r.returncode == 0, r.returncode)
        ck("2.2 自检结果文件已写出", result_file.is_file(), "")
        data = json.loads(result_file.read_text(encoding="utf-8")) if result_file.is_file() else {}
        ck("2.3 自检在 frozen 环境执行", data.get("frozen") is True, data.get("frozen"))
        ck("2.4 自检版本一致", data.get("version") == want, data.get("version"))
        for name, item in (data.get("checks") or {}).items():
            ck(f"2.5 冻结链路 / {name}", item.get("ok") is True, item.get("detail"))
        ck("2.6 无自检错误", data.get("ok") is True and not data.get("errors"),
           data.get("errors"))
        for marker in ("guard_watermark_not_configurable", "watermark_fixed_spec",
                       "watermark_content_locked", "watermark_only_time_text",
                       "exif_diagnostics", "no_temp_residue", "attempted_output_recorded",
                       "compliant_naming_template", "guard_pixel_spec",
                       "guard_naming_required", "strict_exif_policy",
                       "compliant_output_name", "exif_only_trigger", "notice_clean",
                       "no_source_diagnostics", "precheck_uses_uniform_text",
                       "compliance_locked", "compliance_fields_declared", "serial_verbatim_naming",
                       "watermark_font_tripled", "watermark_ink_auto",
                       "watermark_light_bg_dark_ink", "watermark_dark_bg_light_ink",
                       "watermark_contrast_ok",
                       "geometry_check_available", "geometry_compliant",
                       "geometry_portrait_manual", "geometry_ratio_advice",
                       "geometry_direction_reliable", "geometry_notice_wording"):
            item = (data.get("checks") or {}).get(marker, {})
            ck(f"2.7 产物含本版行为标记 / {marker}", item.get("ok") is True,
               item.get("detail", "缺失"))

        # --- [3] 双击无参数启动：实测顶层窗口 ---
        # 启动配置写到独立目录，避免验收把 ECU/序列号/输出目录写进交付目录
        gui_env = {**os.environ, "PW_RULES_DIR": str(work / "_gui_rules")}
        proc = subprocess.Popen([str(EXE)], cwd=str(work), env=gui_env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        found: list[tuple[int, int, str, str]] = []
        deadline = time.time() + 60
        while time.time() < deadline:
            found = list_windows_by_title("批量照片时间水印处理程序")
            if found:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        ck("3.1 双击启动出现可见顶层窗口", bool(found),
           [(w[1], w[2][:50], w[3]) for w in found])
        if found:
            title = found[0][2]
            ck("3.2 窗口标题含程序名与版本号", want in title, title)
            ck("3.3 窗口类为 TkTopLevel", found[0][3] == "TkTopLevel", found[0][3])
            ctypes.windll.user32.PostMessageW(found[0][0], 0x0010, 0, 0)  # WM_CLOSE
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
            rc = proc.wait(timeout=10)
        ck("3.4 关闭后进程正常退出", rc == 0, rc)
        time.sleep(1)
        ck("3.5 无残留窗口", not list_windows_by_title("批量照片时间水印处理程序"),
           [(w[1], w[2][:40]) for w in list_windows_by_title("批量照片时间水印处理程序")])

        # --- [4] 从任意 cwd 启动：输出/规则应落到 exe 同级，而非 cwd ---
        ck("4.1 临时 cwd 下未误建 rules/ 或 输出照片/",
           not (work / "rules").exists() and not (work / "输出照片").exists(),
           str(work))
        ck("4.2 交付目录不含 rules/（启动配置不随包分发）",
           not (EXE.parent / "rules").exists(), str(EXE.parent / "rules"))

    print("\n" + "=" * 68)
    print(f"冻结产物验收：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗", f)
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
