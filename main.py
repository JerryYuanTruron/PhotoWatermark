"""程序入口。

双击 / 无参数          → 启动操作人员 GUI（窗口模式，无控制台）
--version [输出.json]  → 写出版本元信息（供冻结产物验收）
--selftest <工作目录>  → 在冻结产物内部跑一次端到端自检，结果写入 JSON
                          （窗口程序 stdout 可能为 None，因此结果一律落文件，不依赖控制台）

用法：python main.py  /  PhotoWatermark.exe
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_mode() -> dict:
    p = __import__("os").path.abspath(sys.executable)
    return {
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": p,
        "meipass": getattr(sys, "_MEIPASS", None),
        "cwd": str(Path.cwd()),
    }


def _version(args: list[str]) -> int:
    from src import __app_name__, __spec_source__, __version__
    payload = {"version": __version__, "app": __app_name__,
               "spec_source": __spec_source__, **_run_mode()}
    if args:
        _write_json(Path(args[0]), payload)
    return 0


def _selftest(workdir: str) -> int:
    """在 exe 内部真实跑一遍：生成样本 → 处理 → 导出 → 校验 → 报告。

    与源码测试的区别：这里验证的是「冻结后的 Pillow / 字体 / 模块收集 / 路径解析」是否可用。
    """
    from datetime import datetime

    payload: dict = {"version": None, "checks": {}, "errors": [], **_run_mode()}
    work = Path(workdir)
    out = work / "output"

    def ck(name: str, cond: bool, detail: object = "") -> None:
        payload["checks"][name] = {"ok": bool(cond), "detail": str(detail)}
        if not cond:
            payload["errors"].append(f"{name}: {detail}")

    try:
        from PIL import Image, ImageDraw

        from src import __version__, config, engine, reporter

        payload["version"] = __version__
        out.mkdir(parents=True, exist_ok=True)
        ck("font_msyh", bool(config.resolve_font("微软雅黑", "")),
           config.resolve_font("微软雅黑", ""))

        def pic(w: int, h: int) -> Image.Image:
            im = Image.new("RGB", (w, h), (60, 90, 140))
            d = ImageDraw.Draw(im)
            d.rectangle([w // 5, h // 5, w * 4 // 5, h * 4 // 5],
                        outline=(240, 200, 40), width=3)
            d.ellipse([w // 3, h // 3, w * 2 // 3, h * 2 // 3], fill=(190, 70, 60))
            return im

        def exif_bytes(original: str) -> bytes:
            ex = Image.Exif()
            ex.get_ifd(0x8769)[36867] = original
            return ex.tobytes()

        p1 = work / "SAMPLE_0001.jpg"
        pic(2400, 1800).save(p1, quality=95, exif=exif_bytes("2026:05:22 16:29:01"))
        p2 = work / "NOEXIF_0002.jpg"
        pic(800, 600).save(p2, quality=95)
        p3 = work / "BROKEN_0003.jpg"
        p3.write_bytes(b"\xff\xd8\xff\xe0" + b"bad" * 40)

        cfg = config.RuleConfig()
        cfg.output_dir = str(out)
        cfg.ecu_name = "EEPH"
        cfg.serial_number = "SELFTEST"
        cfg.min_output_megapixels = 0   # 自检用小合成图，主链路不施加 21 MP 门槛
        ck("config_valid", cfg.validate() == [], cfg.validate())
        ck("compliant_naming_template", cfg.filename_template == "{ecu}_{serial}_{seq}",
           cfg.filename_template)
        # 行为标记：确认 exe 内确实是本版代码（而非旧包配了新 BUILD_INFO）
        # v1.2.0：时间水印为系统固定规格，任何自定义都必须被拦截
        bad = config.RuleConfig(output_dir=str(out), font_opacity=0)
        ck("guard_watermark_not_configurable",
           any("不可自定义" in e for e in bad.validate()), bad.validate())
        ck("watermark_fixed_spec",
           config.RuleConfig().time_format == config.WATERMARK_TIME_FORMAT
           and config.RuleConfig().content_template == "{time}"
           and config.RuleConfig.from_dict({"time_format": "%Y-%m-%d"}).time_format
           == config.WATERMARK_TIME_FORMAT, config.WATERMARK_TIME_FORMAT)
        ck("watermark_content_locked",
           config.RuleConfig(output_dir=str(out), content_template="{filename} | {time}")
           .apply_fixed_watermark().content_template == "{time}", "")
        # v1.5.0：合规口径（像素下限 / 缩放方式 / 命名模板）收归系统固定，界面只读
        rogue = config.RuleConfig(output_dir=str(out), min_output_megapixels=0,
                                  resize_mode="contain", filename_template="{name}_结果")
        locked_cfg = rogue.apply_compliance_lock()
        ck("serial_verbatim_naming",
           engine.plan_output_names([str(p1)], config.RuleConfig(
               output_dir=str(out), ecu_name="EEPH",
               serial_number=config.SERIAL_FULL_EXAMPLE))[0].name
           == f"EEPH_{config.SERIAL_FULL_EXAMPLE}_0001.jpg",
           config.SERIAL_FULL_EXAMPLE)
        ck("compliance_locked",
           locked_cfg.min_output_megapixels == 21
           and locked_cfg.resize_mode == "crop43"
           and locked_cfg.filename_template == "{ecu}_{serial}_{seq}",
           f"{locked_cfg.min_output_megapixels}/{locked_cfg.resize_mode}/"
           f"{locked_cfg.filename_template}")
        ck("compliance_fields_declared",
           set(config.COMPLIANCE_LOCKED_FIELDS) ==
           {"resize_mode", "min_output_megapixels", "filename_template"},
           config.COMPLIANCE_LOCKED_FIELDS)
        ck("exif_diagnostics", hasattr(engine, "read_exif_datetime_ex"), "")
        # v1.1.0 客户合规门槛
        ck("guard_pixel_spec",
           any("低于客户要求" in e for e in config.RuleConfig(
               output_dir=str(out), ecu_name="EEPH", serial_number="SELFTEST",
               resize_mode="contain", target_width=2400, target_height=1800).validate()), "")
        naming_errs = config.RuleConfig(output_dir=str(out)).validate()
        ck("guard_naming_required",
           any("ECU 名称" in e for e in naming_errs)
           and any("序列号" in e for e in naming_errs), naming_errs)
        strict = config.RuleConfig(output_dir=str(out), ecu_name="EEPH",
                                   serial_number="SELFTEST")
        ck("strict_exif_policy",
           strict.time_source == "exif" and strict.missing_exif_policy == "fail"
           and strict.resize_mode == "crop43", "")
        ck("exif_only_trigger", hasattr(engine, "has_exif_capture_time")
           and engine.has_exif_capture_time(p1)
           and not engine.has_exif_capture_time(p2), "")
        # 提示文案：简洁、只要求提供保留完整 EXIF 的原始照片、无任何来源类信息
        banned = ("微信", "截图", "传输", "APP1", "剥离", "来源", "推断", "诊断")
        notice = engine.EXIF_NOTICE_MESSAGE
        ck("notice_clean",
           bool(notice) and "EXIF" in notice and "原始照片" in notice
           and not any(w in notice for w in banned)
           and not any(w in engine.EXIF_NOTICE_TITLE for w in banned)
           and not any(w in engine.EXIF_MISSING_REASON for w in banned),
           notice)
        ck("no_source_diagnostics",
           not hasattr(engine, "diagnose_app1_capture_time"), "")
        ck("precheck_uses_uniform_text",
           engine.precheck_exif(p1) == ""
           and engine.precheck_exif(p2) == engine.EXIF_MISSING_REASON,
           engine.precheck_exif(p2))
        # v1.1.2 导入阶段比例/方向校验的行为标记
        ck("geometry_check_available",
           hasattr(engine, "check_geometry") and hasattr(engine, "GeometryIssue")
           and hasattr(engine, "geometry_notice_text"), "")
        ck("geometry_compliant", engine.check_geometry(p1) is None, "")
        p4 = work / "GEO_PORTRAIT_0004.jpg"
        pic(900, 1200).save(p4, quality=95)
        g4 = engine.check_geometry(p4)
        ck("geometry_portrait_manual",
           g4 is not None and g4.kind == "portrait_rotatable"
           and g4.rotation == "manual" and "人工确认" in g4.advice
           and "不指定方向" in g4.advice and "顺时针" not in g4.advice
           and "逆时针" not in g4.advice, g4.brief if g4 else "")
        p5 = work / "GEO_WIDE_0005.jpg"
        pic(1600, 900).save(p5, quality=95)
        g5 = engine.check_geometry(p5)
        ck("geometry_ratio_advice",
           g5 is not None and g5.kind == "landscape_ratio"
           and all(w in g5.advice for w in ("自动居中裁边", "无需手工调整", "无需")),
           g5.brief if g5 else "")
        ex6 = Image.Exif()
        ex6[0x0112] = 6
        p6 = work / "GEO_ORIENT6_0006.jpg"
        pic(900, 1200).save(p6, quality=95, exif=ex6.tobytes())
        g6 = engine.check_geometry(p6)
        ck("geometry_direction_reliable",
           g6 is not None and g6.rotation == "cw"
           and "顺时针" in g6.advice and "无需手工旋转" in g6.advice,
           g6.brief if g6 else "")
        gnotice = engine.geometry_notice_text([x for x in (g4, g5, g6) if x])
        ck("geometry_notice_wording",
           "目标要求：符合 4:3 的横版照片" in gnotice
           and "仍会照常批量处理，无需手工调整或单独处理" in gnotice
           and all(n in gnotice for n in (p4.name, p5.name))
           and "不阻断导入" in gnotice, "")

        # v1.4.0：水印字号放大到 3 倍 + 按每张照片背景自动取色（不固定白色）
        ck("watermark_font_tripled",
           config.WATERMARK_FIXED["font_size"] == 210,
           config.WATERMARK_FIXED["font_size"])
        ck("watermark_ink_auto",
           config.WATERMARK_FIXED["font_color"] == "auto"
           and config.WATERMARK_FIXED["shadow_color"] == "auto", "")
        ink_out = work / "ink_output"
        ink_out.mkdir(parents=True, exist_ok=True)
        cfg_ink = config.RuleConfig()
        cfg_ink.output_dir = str(ink_out)
        cfg_ink.ecu_name = "EEPH"
        cfg_ink.serial_number = "SELFTEST"
        cfg_ink.min_output_megapixels = 0
        p_light = work / "INK_LIGHT_0007.jpg"
        Image.new("RGB", (2400, 1800), (245, 245, 245)).save(
            p_light, quality=95, exif=exif_bytes("2026:05:22 16:29:01"))
        p_dark = work / "INK_DARK_0008.jpg"
        Image.new("RGB", (2400, 1800), (12, 12, 12)).save(
            p_dark, quality=95, exif=exif_bytes("2026:05:22 16:29:01"))
        r_light = engine.process_one(p_light, cfg_ink, 1, set())
        r_dark = engine.process_one(p_dark, cfg_ink, 2, set())
        ck("watermark_light_bg_dark_ink",
           r_light.ok and r_light.watermark_color == "#101010",
           f"{r_light.status} {r_light.watermark_color}")
        ck("watermark_dark_bg_light_ink",
           r_dark.ok and r_dark.watermark_color == "#FAFAFA",
           f"{r_dark.status} {r_dark.watermark_color}")
        ck("watermark_contrast_ok",
           min(r_light.watermark_contrast, r_dark.watermark_contrast) >= 150,
           f"{r_light.watermark_contrast:.0f}/{r_dark.watermark_contrast:.0f}")

        used: set[str] = set()
        started = datetime.now()
        rs = [engine.process_one(p, cfg, i, used) for i, p in enumerate((p1, p2, p3), 1)]
        statuses = [r.status for r in rs]
        ck("statuses", statuses == [engine.STATUS_SUCCESS, engine.STATUS_FAILED,
                                    engine.STATUS_FAILED], statuses)
        ck("watermark_text", rs[0].watermark_text == "2026/05/22 16:29:01",
           rs[0].watermark_text)
        # 水印内容只允许「yyyy/mm/dd hh:mm:ss」这 19 个字符，不附加任何文字或标识
        ck("watermark_only_time_text",
           bool(re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}",
                             str(rs[0].watermark_text))), rs[0].watermark_text)

        summary = reporter.summarize(rs, cfg, started, datetime.now())
        paths = reporter.export_reports(rs, cfg, summary)
        ck("reports", "error" not in paths and all(Path(v).is_file() for v in paths.values()),
           paths)
        if "失败清单CSV" in paths:
            text = Path(paths["失败清单CSV"]).read_text(encoding="utf-8-sig")
            ck("failure_list", "NOEXIF_0002.jpg" in text and "BROKEN_0003.jpg" in text, "")

        produced = list(out.glob("*.jpg"))
        ck("only_good_outputs", len(produced) == 1, [x.name for x in produced])
        # 只核对「本次导出产生的临时文件」；配置写探针属于另一类，且可能来自
        # 复用同一工作目录的上一次运行，不应污染本条断言。
        residue = [x.name for x in out.glob("._pw_*") if "_probe_" not in x.name]
        ck("no_temp_residue", not residue, residue)
        ck("attempted_output_recorded",
           all(x.attempted_output for x in rs if x.ok),
           [x.attempted_output for x in rs if x.ok])
        ck("compliant_output_name",
           bool(produced) and produced[0].name == "EEPH_SELFTEST_0001.jpg",
           [x.name for x in produced])
        if produced:
            with Image.open(produced[0]) as im:
                ck("output_size", im.size == (2400, 1800), im.size)  # 保持原尺寸
                dpi = im.info.get("dpi")
                ck("output_dpi", dpi is not None and abs(dpi[0] - 300) < 2, dpi)
                ck("exif_preserved",
                   im.getexif().get_ifd(0x8769).get(36867) == "2026:05:22 16:29:01", "")
    except Exception:
        payload["errors"].append(traceback.format_exc())

    payload["ok"] = not payload["errors"]
    _write_json(work / "frozen_selftest.json", payload)
    return 0 if payload["ok"] else 1


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--version":
        return _version(args[1:2])
    if args and args[0] == "--selftest" and len(args) >= 2:
        return _selftest(args[1])
    from src.ui import main as gui_main  # 延迟导入：CLI 模式不加载 tkinter
    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
