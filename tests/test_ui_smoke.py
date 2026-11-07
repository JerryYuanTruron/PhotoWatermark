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
from src.ui import App  # noqa: E402
from tests.make_samples import build  # noqa: E402


def main() -> int:
    # 屏蔽交互弹窗，使测试可无人值守运行
    for name in ("showinfo", "showwarning", "showerror"):
        setattr(messagebox, name, lambda *a, **k: None)
    messagebox.askyesno = lambda *a, **k: True  # type: ignore[assignment]

    out = ROOT / "_test_out_ui"
    shutil.rmtree(out, ignore_errors=True)
    samples = build(ROOT / "_test_samples")

    app = App()
    app.update()
    print("[1] 窗口构建成功；字段数 =", len(app.vars), "下拉/控件数 =", len(app.widgets))

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

    # 批量导入（一次导入，无需逐张设置）
    app._merge_files([str(p) for p in samples])
    app.update()
    assert len(app.files) == len(samples)
    assert len(app.tree.get_children()) == len(samples)
    print(f"[4] 一次导入 {len(app.files)} 张，状态表自动生成同等行数")

    # 一键批量处理
    errs = app._collect_config().validate()
    assert not errs, errs
    app._start_batch()
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
    assert "缺少 EXIF 拍摄日期" in csv_text
    print("[9] 失败清单含失败文件名与具体原因")

    app._on_close()
    print("\nGUI 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
