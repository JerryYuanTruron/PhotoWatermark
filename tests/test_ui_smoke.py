"""GUI 逻辑冒烟测试：无需人工点击，验证配置往返、批量导入、后台线程、逐张状态与报告。

用法：python tests/test_ui_smoke.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from tkinter import messagebox

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, engine, reporter  # noqa: E402
from src.ui import FIELD_GROUPS, App  # noqa: E402
from tests.make_samples import build  # noqa: E402


POPUPS: list[tuple[str, str, str]] = []


def main() -> int:
    # 记录弹窗而非屏蔽，以便断言「缺 EXIF 必弹窗、含 EXIF 不弹窗、文案统一」
    def record(kind: str):
        def f(title, message, *a, **k):
            POPUPS.append((kind, str(title), str(message)))
        return f

    for name in ("showinfo", "showwarning", "showerror"):
        setattr(messagebox, name, record(name))
    messagebox.askyesno = lambda *a, **k: True  # type: ignore[assignment]

    out = ROOT / "_test_out_ui"
    shutil.rmtree(out, ignore_errors=True)
    samples = build(ROOT / "_test_samples")

    app = App()
    app.update()
    ui_keys = {k for _, specs, _ in FIELD_GROUPS for k, *_ in specs}
    cfg_keys = set(config.RuleConfig.__dataclass_fields__)
    assert ui_keys == cfg_keys, f"界面与配置字段不一致: {ui_keys ^ cfg_keys}"
    print("[1] 窗口构建成功；界面字段 =", len(ui_keys), "与 RuleConfig 一一对应")

    # 配置往返
    c0 = app._collect_config()
    assert c0.target_width == 2400 and c0.font_family == "微软雅黑" and c0.position == "bottom_right"
    c1 = config.RuleConfig(target_width=1234, font_size=99, position="top_left", dpi=200)
    app._apply_config(c1)
    c2 = app._collect_config()
    assert c2.target_width == 1234 and c2.font_size == 99 and c2.position == "top_left" and c2.dpi == 200
    print("[2] 规则配置界面↔RuleConfig 往返一致")

    # 恢复默认 + 应用预置（不应清空输出目录）
    app._restore_default()
    d = app._collect_config()
    assert (d.target_width == 5292 and d.font_size == 70
            and d.resize_mode == "none" and d.min_output_megapixels == 21
            and d.filename_template == "{ecu}_{serial}_{seq}")
    app.vars["output_dir"].set(str(out))
    app._apply_preset()
    d = app._collect_config()
    assert d.output_dir == str(out), "应用预置不应清空输出目录"
    print("[3] 恢复默认 / 应用预置正常，输出目录保持不变")

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

    app._on_close()
    print("\nGUI 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
