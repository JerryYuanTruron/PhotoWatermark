"""GUI 逻辑冒烟测试：无需人工点击，验证配置往返、批量导入、后台线程、逐张状态与报告。

用法：python tests/test_ui_smoke.py
"""

from __future__ import annotations

import shutil
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.ui as ui  # noqa: E402
from src import config, engine, reporter  # noqa: E402
from src.ui import App  # noqa: E402
from tests import _testfs as testfs  # noqa: E402
from tests.make_samples import build  # noqa: E402


POPUPS: list[tuple[str, str, str]] = []
GEOMETRY_CALLS: list[list[engine.GeometryIssue]] = []
REAL_SHOW_GEOMETRY = ui.show_geometry_notice


def main() -> int:
    # 记录弹窗而非屏蔽，以便断言「缺 EXIF 必弹窗、含 EXIF 不弹窗、文案统一」
    def record(kind: str):
        def f(title, message, *a, **k):
            POPUPS.append((kind, str(title), str(message)))
        return f

    for name in ("showinfo", "showwarning", "showerror"):
        setattr(messagebox, name, record(name))
    messagebox.askyesno = lambda *a, **k: True  # type: ignore[assignment]
    # 记录比例/方向提示窗口的调用（不真正弹窗，便于无人值守断言）
    ui.show_geometry_notice = lambda parent, issues: (
        GEOMETRY_CALLS.append(list(issues)) or None)

    # 每次运行用唯一空目录，不做批量删除（避免触发删除授权，详见 tests/_testfs.py）
    out = testfs.fresh_dir(ROOT / "_test_out_ui", "ui")
    samples = build(ROOT / "_test_samples")

    app = App()
    app.update()
    ui_keys = set(ui.UI_FIELDS)
    cfg_keys = set(config.RuleConfig.__dataclass_fields__)
    fixed = set(config.FIXED_WATERMARK_FIELDS)
    assert ui_keys == cfg_keys - fixed, f"界面与配置字段不一致: {ui_keys ^ (cfg_keys - fixed)}"
    assert not (ui_keys & fixed), f"水印字段不得出现在界面: {ui_keys & fixed}"
    print("[1] 窗口构建成功；界面字段 =", len(ui_keys),
          "= 配置字段 −", len(fixed), "项系统固定水印字段")

    # 归一化默认值，避免受上次运行遗留配置影响
    app._apply_config(config.RuleConfig())
    app.vars["output_dir"].set(str(out))

    # 配置往返（只涉及非水印项；水印恒为系统固定值）
    c0 = app._collect_config()
    assert c0.target_width == 5292 and c0.resize_mode == "none"
    assert (c0.time_format, c0.content_template, c0.font_size, c0.position) == (
        config.WATERMARK_TIME_FORMAT, "{time}", 70, "bottom_right")
    c1 = config.RuleConfig(target_width=1234, dpi=200, jpeg_quality=88,
                           on_exists="rename", output_dir=str(out))
    app._apply_config(c1)
    c2 = app._collect_config()
    assert (c2.target_width, c2.dpi, c2.jpeg_quality, c2.on_exists) == (1234, 200, 88, "rename")
    print("[2] 高级设置界面↔RuleConfig 往返一致；水印始终为系统固定值")

    # 必填项前置与门禁：未填满时禁用「开始批量处理」
    app.vars["ecu_name"].set("")
    app.vars["serial_number"].set("")
    app.update()
    assert app.required_filled == 1 and str(app.btn_start["state"]) == "disabled"
    assert "1/3" in app.required_label["text"]
    app.vars["ecu_name"].set("EEPH")
    app.update()
    assert app.required_filled == 2 and str(app.btn_start["state"]) == "disabled"
    app.vars["serial_number"].set("TESTSERIAL")
    app.update()
    assert app.required_filled == 3 and str(app.btn_start["state"]) == "normal"
    print("[2b] 必填项集中前置：未填满 3 项时禁用「开始批量处理」")

    # 恢复默认只作用于高级设置，必填信息与输出目录保持不变
    app.vars["jpeg_quality"].set("70")
    app._restore_advanced_defaults()
    d = app._collect_config()
    assert (d.target_width == 5292 and d.resize_mode == "none"
            and d.min_output_megapixels == 21 and d.jpeg_quality == 92
            and d.filename_template == "{ecu}_{serial}_{seq}")
    assert d.output_dir == str(out) and d.ecu_name == "EEPH"
    print("[3] 恢复默认只作用于高级设置，必填信息与输出目录保持不变")

    # 切回合成小图的测试基线（v1.1.0 默认是不缩放 + ≥21MP + 合规命名）
    app.vars["ecu_name"].set("EEPH")
    app.vars["serial_number"].set("TESTSERIAL")
    app.vars["min_output_megapixels"].set("0")
    app.vars["target_width"].set("2400")
    app.vars["target_height"].set("1800")
    app.vars["filename_template"].set("{name}_结果")
    app.widgets["resize_mode"].set_value("contain")

    # 仅导入含 EXIF 的照片：不得触发提示窗口
    good_only = [p for p in samples if engine.has_exif_capture_time(p)]
    assert good_only
    POPUPS.clear()
    app._merge_files([str(p) for p in good_only])
    app.update()
    exif_popups = [p for p in POPUPS if p[1] == engine.EXIF_NOTICE_TITLE]
    assert not exif_popups, f"含 EXIF 的照片不应弹窗：{exif_popups}"
    assert not app.precheck or all(v == "" for v in app.precheck.values())
    print("[4a] 含 EXIF 的照片不触发提示窗口，预检全部通过")

    # 批量导入（含缺少 EXIF 的样本）
    POPUPS.clear()
    app._merge_files([str(p) for p in samples])
    app.update()
    assert len(app.files) == len(samples)
    assert len(app.tree.get_children()) == len(samples)
    exif_popups = [p for p in POPUPS if p[1] == engine.EXIF_NOTICE_TITLE]
    assert len(exif_popups) == 1, f"缺 EXIF 应弹窗一次：{exif_popups}"
    assert exif_popups[0] == ("showwarning", engine.EXIF_NOTICE_TITLE,
                              engine.EXIF_NOTICE_MESSAGE), exif_popups[0]
    banned = ("微信", "截图", "传输", "APP1", "剥离", "来源", "推断", "诊断")
    assert not any(w in exif_popups[0][2] for w in banned)
    # 缺 EXIF 不得静默跳过：状态表必须标记
    marked = [app.tree.item(str(i), "values")[3]
              for i in range(1, len(samples) + 1)]
    assert "缺少 EXIF" in marked, marked
    print(f"[4] 一次导入 {len(app.files)} 张；缺 EXIF 弹窗一次且状态表标记，未静默跳过")

    # 一键批量处理：复核 EXIF 但不重复弹出同一提示
    errs = app._collect_config().validate()
    assert not errs, errs
    POPUPS.clear()
    app._start_batch()
    assert not [p for p in POPUPS if p[1] == engine.EXIF_NOTICE_TITLE], \
        f"已提示过的照片不应重复弹窗：{POPUPS}"
    t0 = time.time()
    while app.busy and time.time() - t0 < 120:
        app.update()
        time.sleep(0.02)
    assert not app.busy, "批量处理超时"
    app.update()
    print("[5] 批量处理线程结束；结果数 =", len(app.results))

    ok = sum(r.ok for r in app.results)
    bad = sum(r.status == engine.STATUS_FAILED for r in app.results)
    assert len(app.results) == len(samples)
    assert ok == 7 and bad == 3, (ok, bad)
    assert app.summary is not None and app.summary.verified == 7
    print("[6] 逐张状态：", app.summary.headline() if app.summary else "n/a")

    # 状态表与详情
    app.tree.selection_set("1")
    app.update()
    assert "拍摄时间" in app.detail.get("1.0", "end")
    tags = {app.tree.item(str(i), "tags")[0] for i in range(1, len(samples) + 1)}
    assert "ok" in tags and "fail" in tags
    print("[7] 状态表颜色标记与详情面板正常；tags =", sorted(tags))

    # 报告
    assert Path(app.report_paths["失败清单CSV"]).is_file()
    assert Path(app.report_paths["失败清单TXT"]).is_file()
    assert Path(app.report_paths["处理报告JSON"]).is_file()
    print("[8] 4 份报告已由批次自动写出")

    # 失败清单内容核对
    csv_text = Path(app.report_paths["失败清单CSV"]).read_text(encoding="utf-8-sig")
    assert "NOEXIF_0004.jpg" in csv_text and "BROKEN_0005.jpg" in csv_text
    assert "缺少 EXIF 信息" in csv_text
    print("[9] 失败清单含失败文件名与具体原因")

    # --- [10] 导入阶段比例/方向校验：非阻断、列出文件与异常类型、方向不擅自指定 ---
    geo_dir = out / "geo_samples"
    geo_dir.mkdir(parents=True, exist_ok=True)

    def geo_sample(name: str, w: int, h: int) -> Path:
        p = geo_dir / name
        ex = Image.Exif()
        ex.get_ifd(0x8769)[36867] = "2026:11:05 16:08:32"
        Image.new("RGB", (w, h), (90, 110, 130)).save(p, quality=90, exif=ex.tobytes())
        return p

    ok43 = geo_sample("OK_43.jpg", 1200, 900)
    vert = geo_sample("VERT_34.jpg", 900, 1200)
    wide = geo_sample("WIDE_169.jpg", 1600, 900)
    app._clear_files()
    GEOMETRY_CALLS.clear()
    POPUPS.clear()
    app._merge_files([str(ok43)])
    app.update()
    assert not GEOMETRY_CALLS, f"4:3 横版不应弹窗：{GEOMETRY_CALLS}"
    print("[10a] 仅 4:3 横版照片导入不触发比例/方向提示")

    app._merge_files([str(vert), str(wide)])
    app.update()
    assert len(GEOMETRY_CALLS) == 1, f"应弹出一次提示：{GEOMETRY_CALLS}"
    issues = GEOMETRY_CALLS[-1]
    text = engine.geometry_notice_text(issues)
    assert len(issues) == 2 and all(x.filename in text for x in issues)
    assert "目标要求：符合 4:3 的横版照片" in text
    assert "竖版" in text and "横版" in text
    assert "程序不指定方向" in text and "人工确认" in text
    assert "仅旋转无法修正比例" in text and "裁剪或更换" in text
    assert "不阻断导入" in text
    assert len(app.files) == 3 and ok43.name in [Path(f).name for f in app.files]
    statuses = [app.tree.item(str(i), "values")[3] for i in range(1, 4)]
    assert statuses == ["待处理", "比例异常", "比例异常"], statuses
    print("[10b] 提示列出受影响文件 / 异常类型 / 目标要求；合格照片仍照常导入")

    # 重复导入不重复弹窗；清空后重新导入会再次提示
    app._merge_files([str(vert)])
    app.update()
    assert len(GEOMETRY_CALLS) == 1
    app._clear_files()
    assert not app.geometry and not app.geometry_notified
    app._merge_files([str(wide)])
    app.update()
    assert len(GEOMETRY_CALLS) == 2
    print("[10c] 同一照片不重复弹窗；清空列表后状态复位")

    # 真实窗口：非模态（无 grab_set）、正文与纯文本文案一致
    win = REAL_SHOW_GEOMETRY(app, issues)
    app.update()
    assert isinstance(win, tk.Toplevel)
    assert win.grab_current() is None, "提示窗口不得为模态，否则会阻断其余照片导入"
    texts = []

    def collect(widget: tk.Misc) -> None:
        for child in widget.winfo_children():
            if isinstance(child, tk.Text):
                texts.append(child.get("1.0", "end"))
            collect(child)

    collect(win)
    assert texts and "目标要求：符合 4:3 的横版照片" in texts[0]
    btn_texts: list[str] = []

    def collect_buttons(widget: tk.Misc) -> None:
        for child in widget.winfo_children():
            if child.winfo_class() == "TButton":
                btn_texts.append(str(child.cget("text")))
            collect_buttons(child)

    collect_buttons(win)
    assert "知道了" in btn_texts, btn_texts
    win.destroy()
    print("[10d] 真实提示窗口为非模态、正文与统一文案一致")

    app._on_close()
    print("\nGUI 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
