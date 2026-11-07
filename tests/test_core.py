"""核心链路全量自动化测试（无需 GUI）。

覆盖：配置、EXIF 优先级、四种缩放、DPI、九宫格水印、字号单位、模板、
失败识别与清理、输出回读校验、报告与批次审计、边界与对抗场景。

用法：python tests/test_core.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, engine, reporter  # noqa: E402
from tests.make_samples import build  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} — {detail}")


def run(files: list[Path], cfg: config.RuleConfig) -> list[engine.ProcessResult]:
    used: set[str] = set()
    return [engine.process_one(f, cfg, i, used) for i, f in enumerate(files, 1)]


def fresh(name: str) -> Path:
    p = ROOT / "_test_runs" / name
    shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True)
    return p


def main() -> int:
    samples = build(ROOT / "_test_samples")
    samples_root = ROOT / "_test_samples"
    # sub/ 下有一个同名文件用于重名测试；此处只取根目录样本，避免字典覆盖取错文件
    by_name = {p.name: p for p in samples if p.parent == samples_root}
    good = by_name["PeCu_2718281828_0001.jpg"]
    noexif = by_name["NOEXIF_0004.jpg"]
    png = by_name["PeCu_2718281828_0003.png"]
    portrait = by_name["PORTRAIT_0007.jpg"]
    orient = by_name["ORIENT_0008.jpg"]
    tiny = by_name["TINY_0009.jpg"]
    broken = by_name["BROKEN_0005.jpg"]
    notimage = by_name["NOTIMAGE_0006.txt"]

    # ------------------------------------------------------------------
    print("\n[A] 规则配置")
    c = config.RuleConfig()
    check("A1 文档默认值", (c.target_width, c.target_height, c.dpi, c.font_size,
                            c.position, c.time_format) ==
          (2400, 1800, 300, 70, "bottom_right", "%Y/%m/%d %H:%M:%S"))
    c.output_dir = str(ROOT / "_test_runs")
    check("A2 默认配置通过校验", c.validate() == [], str(c.validate()))
    c2 = config.RuleConfig.from_dict({"target_width": "1234", "dpi": None,
                                      "unknown_key": 1, "jpeg_quality": 999})
    check("A3 残缺/未知字段安全载入", c2.target_width == 1234 and c2.dpi == 300)
    check("A4 非法参数被校验拦截", config.RuleConfig(target_width=0, dpi=0,
                                                    output_dir=str(ROOT / "_test_runs")).validate() != [])
    tmp_json = ROOT / "_test_runs" / "rule.json"
    c.save(tmp_json)
    check("A5 规则文件 JSON 往返", config.RuleConfig.load(tmp_json).time_format == c.time_format)
    check("A6 pt 单位按 DPI 换算", config.RuleConfig(font_size=72, font_size_unit="pt",
                                                    dpi=300).effective_font_px() == 300)
    check("A7 三套预置均可实例化且字段不同",
          len({config.preset_config(n).target_width for n in config.PRESETS}) >= 2)

    # ------------------------------------------------------------------
    print("\n[B] EXIF 拍摄日期（文档 3.6）")
    im = Image.open(good)
    dt, tag = engine.read_exif_datetime(im.getexif())
    check("B1 DateTimeOriginal 优先于主 IFD DateTime",
          tag == "DateTimeOriginal" and dt.strftime("%Y/%m/%d %H:%M:%S") == "2026/05/22 16:29:01",
          f"tag={tag}, dt={dt}")
    im.close()
    im = Image.open(by_name["PeCu_2718281828_0002.jpg"])
    dt2, tag2 = engine.read_exif_datetime(im.getexif())
    check("B2 回退读取主 IFD DateTime", tag2 == "DateTime" and dt2.year == 2026)
    im.close()
    check("B3 畸形 EXIF 时间不抛异常", engine._parse_exif_dt("not-a-date") is None)
    check("B4 带时区/空字符的 EXIF 时间可解析",
          engine._parse_exif_dt("2026:05:22 16:29:01\x00+08:00") is not None)

    # ------------------------------------------------------------------
    print("\n[C] 尺寸与像素规格")
    for mode, exp in (("contain", (2400, 1800)), ("cover", (2400, 1800)),
                      ("stretch", (2400, 1800)), ("none", (1200, 900))):
        cfg = config.RuleConfig(resize_mode=mode, output_dir=str(fresh("size_" + mode)))
        rs = run([good], cfg)
        out = Path(rs[0].output_path)
        with Image.open(out) as chk:
            check(f"C1 {mode} 输出尺寸 {exp}", chk.size == exp, str(chk.size))
            check(f"C2 {mode} DPI=300",
                  chk.info.get("dpi") is not None and abs(chk.info["dpi"][0] - 300) < 2,
                  str(chk.info.get("dpi")))
    cfg = config.RuleConfig(output_dir=str(fresh("portrait")))
    rs = run([portrait], cfg)
    with Image.open(rs[0].output_path) as chk:
        check("C3 竖图 contain 输出仍为统一 2400×1800", chk.size == (2400, 1800))
    cfg = config.RuleConfig(output_dir=str(fresh("orient")), resize_mode="none")
    rs = run([orient], cfg)
    with Image.open(rs[0].output_path) as chk:
        ex = chk.getexif()
        check("C4 EXIF 方向已校正且方向标签清除",
              chk.size == (900, 1200) and ex.get(0x0112) is None,
              f"size={chk.size}, orientation={ex.get(0x0112)}")
    cfg = config.RuleConfig(output_dir=str(fresh("formats")))
    rs = run([png], cfg)
    with Image.open(rs[0].output_path) as chk:
        check("C5 PNG 输入同原格式→PNG 输出且保留 EXIF",
              chk.format == "PNG" and chk.getexif().get_ifd(0x8769).get(36867) is not None)

    # ------------------------------------------------------------------
    print("\n[D] 时间水印（文档 3.5 / 3.7 / 3.8）")
    cfg = config.RuleConfig(output_dir=str(fresh("watermark")))
    rs = run([good], cfg)
    r = rs[0]
    check("D1 水印文本为 年/月/日 时:分:秒", r.watermark_text == "2026/05/22 16:29:01")
    check("D2 默认水印位于右下（含 24/20 px 安全边距）",
          r.watermark_rect is not None and r.watermark_rect[2] >= 2300
          and r.watermark_rect[3] >= 1700, str(r.watermark_rect))
    for pos, where in (("top_left", "左上"), ("center", "正中"), ("bottom_left", "左下")):
        cfg = config.RuleConfig(output_dir=str(fresh("pos_" + pos)), position=pos)
        rr = run([good], cfg)[0]
        rect = rr.watermark_rect or (0, 0, 0, 0)
        ok = ({"top_left": rect[0] < 50 and rect[1] < 50,
               "center": abs(rect[0] + rect[2] - 2400) < 200,
               "bottom_left": rect[0] < 50 and rect[3] > 1700}[pos])
        check(f"D3 位置 {where} 生效", ok, str(rect))
    cfg = config.RuleConfig(output_dir=str(fresh("template")),
                            content_template="{filename} | {seq} | {time}")
    rr = run([good], cfg)[0]
    check("D4 内容模板 token 替换",
          rr.watermark_text == "PeCu_2718281828_0001 | 0001 | 2026/05/22 16:29:01",
          rr.watermark_text)
    cfg = config.RuleConfig(output_dir=str(fresh("dashfmt")), time_format="%Y-%m-%d %H:%M:%S")
    check("D5 时间格式可改", run([good], cfg)[0].watermark_text == "2026-05-22 16:29:01")
    cfg = config.RuleConfig(output_dir=str(fresh("nosha")), shadow_enabled=False,
                            font_family="黑体", font_size=120)
    check("D6 字体/阴影/字号可改且仍成功", run([good], cfg)[0].ok)
    cfg = config.RuleConfig(output_dir=str(fresh("longfmt")), font_size=500,
                            time_format="拍摄时间 %Y年%m月%d日 %H时%M分%S秒 样品编号ABCDEF")
    rr = run([tiny], cfg)[0]
    check("D7 超宽时间戳自动缩字号不截断",
          rr.ok and any("自动缩小" in w for w in rr.warnings), str(rr.warnings))
    cfg = config.RuleConfig(output_dir=str(fresh("no_wm")), watermark_enabled=False)
    rr = run([noexif], cfg)[0]
    check("D8 关闭水印后无 EXIF 照片也可正常输出", rr.ok and rr.watermark_text == "")

    # ------------------------------------------------------------------
    print("\n[E] 失败识别、策略与清理")
    base = fresh("fail")
    cfg = config.RuleConfig(output_dir=str(base))
    rs = run([good, noexif, broken, notimage, good.with_name("MISSING.jpg")], cfg)
    reasons = [(r.status, r.stage, r.reason) for r in rs]
    check("E1 正常照片成功", rs[0].status == engine.STATUS_SUCCESS)
    check("E2 缺 EXIF → 时间源阶段失败",
          rs[1].status == engine.STATUS_FAILED and rs[1].stage == engine.STAGE_TIME
          and "EXIF 拍摄日期" in rs[1].reason)
    check("E3 损坏图 → 导入阶段失败",
          rs[2].status == engine.STATUS_FAILED and rs[2].stage == engine.STAGE_IMPORT
          and "解码失败" in rs[2].reason)
    check("E4 非图片 → 导入阶段失败",
          rs[3].status == engine.STATUS_FAILED and "不支持的文件格式" in rs[3].reason)
    check("E5 文件不存在 → 导入阶段失败",
          rs[4].status == engine.STATUS_FAILED and "文件不存在" in rs[4].reason)
    check("E6 失败照片无残留输出",
          all(not r.output_path for r in rs[1:]), str([r.output_path for r in rs[1:]]))
    check("E7 输出目录只含合格成品",
          len(list(base.iterdir())) == 1, str([p.name for p in base.iterdir()]))

    for policy, expect_status, kw in (
        ("use_file_mtime", engine.STATUS_WARNING, "文件修改时间"),
        ("no_watermark", engine.STATUS_WARNING, "跳过该张水印"),
    ):
        cfg = config.RuleConfig(output_dir=str(fresh("policy_" + policy)),
                                missing_exif_policy=policy)
        rr = run([noexif], cfg)[0]
        check(f"E8 EXIF缺失策略 {policy}",
              rr.status == expect_status and any(kw in w for w in rr.warnings),
              f"{rr.status} {rr.warnings}")

    cfg = config.RuleConfig(output_dir=str(fresh("mtime")), time_source="file_mtime")
    rr = run([noexif], cfg)[0]
    check("E9 时间来源改为文件修改时间可用且有告警", rr.ok and rr.warnings)
    cfg = config.RuleConfig(output_dir=str(fresh("fixed")), time_source="fixed",
                            fixed_time_text="2026/11/07 12:00:00")
    check("E10 固定文本内容生效",
          run([noexif], cfg)[0].watermark_text == "2026/11/07 12:00:00")
    cfg = config.RuleConfig(output_dir=str(fresh("badfont")), font_path=r"Z:\nonexistent\font.ttf",
                            font_family="不存在字体")
    rr = run([good], cfg)[0]
    check("E11 字体不存在时回退到系统字体而非整批失败", rr.ok, str(rr.reason))

    # 模拟输出校验失败，验证残留成品被清理
    cfg = config.RuleConfig(output_dir=str(fresh("verify_fail")))
    with mock.patch.object(engine, "_verify_output",
                           return_value=(False, "模拟校验失败", (0, 0), None)):
        rr = run([good], cfg)[0]
    check("E12 校验失败 → 失败状态 + 清理输出文件",
          rr.status == engine.STATUS_FAILED and rr.stage == engine.STAGE_VERIFY
          and not list((ROOT / "_test_runs" / "verify_fail").glob("*.jpg")),
          f"{rr.status} {rr.reason}")

    # ------------------------------------------------------------------
    print("\n[F] 输出命名与同名策略")
    cfg = config.RuleConfig(output_dir=str(fresh("names")), filename_template="{name}")
    rr = run([good], cfg)[0]
    check("F1 {name} 保持原名（满足第10节命名规范）",
          Path(rr.output_path).stem == "PeCu_2718281828_0001", Path(rr.output_path).name)
    cfg = config.RuleConfig(output_dir=str(fresh("seqname")), filename_template="{seq}_{name}")
    check("F2 序号模板生效", Path(run([good], cfg)[0].output_path).name.startswith("0001_"))
    cfg = config.RuleConfig(output_dir=str(fresh("illegal")),
                            filename_template=r'{name}:*?_结果')
    rr = run([good], cfg)[0]
    check("F3 模板含非法字符被安全替换", rr.ok and Path(rr.output_path).is_file(),
          Path(rr.output_path).name if rr.output_path else "")
    dup = fresh("dup")
    cfg = config.RuleConfig(output_dir=str(dup))
    first = run([good, good], cfg)
    check("F4 同名输入自动去重另存，不互相覆盖",
          len({r.output_path for r in first}) == 2 and len(list(dup.glob("*.jpg"))) == 2)
    cfg.on_exists = "skip"
    second = run([good], config.RuleConfig(output_dir=str(dup), on_exists="skip",
                                           filename_template="{name}_结果"))
    check("F5 同名策略 skip 生效", second[0].status == engine.STATUS_SKIPPED)
    cfg2 = config.RuleConfig(output_dir=str(fresh("rename")), on_exists="rename",
                             filename_template="{name}")
    run([good], cfg2)
    run([good], cfg2)
    check("F6 同名策略 rename 生效",
          len(list((ROOT / "_test_runs" / "rename").glob("*.jpg"))) == 2)

    # ------------------------------------------------------------------
    print("\n[G] 报告、批次审计与一致性")
    out = fresh("report")
    cfg = config.RuleConfig(output_dir=str(out))
    all_files = [good, noexif, broken, notimage, png, portrait, orient, tiny]
    rs = run(all_files, cfg)
    st, fi = datetime.now(), datetime.now()
    s = reporter.summarize(rs, cfg, st, fi)
    paths = reporter.export_reports(rs, cfg, s)
    check("G1 统计正确", s.total == 8 and s.failed == 3 and s.success == 5, s.headline())
    check("G2 四份报告均生成", all(Path(p).is_file() for p in paths.values()), str(paths))
    csv_bytes = Path(paths["失败清单CSV"]).read_bytes()
    csv_text = csv_bytes.decode("utf-8-sig")
    check("G3 失败清单 CSV 含 UTF-8 BOM 与失败项",
          csv_bytes.startswith(b"\xef\xbb\xbf") and "NOEXIF_0004.jpg" in csv_text
          and "缺少 EXIF 拍摄日期" in csv_text)
    payload = json.loads(Path(paths["处理报告JSON"]).read_text(encoding="utf-8"))
    check("G4 报告含版本/配置快照/逐张结果",
          payload["version"] == "1.0.0" and payload["config"]["dpi"] == 300
          and len(payload["results"]) == 8)
    check("G5 批次审计通过", any("通过" in a for a in s.audit), str(s.audit))
    sizes = set()
    dpis = set()
    for p in out.glob("*.jpg"):
        with Image.open(p) as im:
            sizes.add(im.size)
            dpis.add(round(im.info["dpi"][0]))
    check("G6 所有 JPEG 成品尺寸/DPI 完全一致",
          sizes == {(2400, 1800)} and dpis == {300}, f"{sizes} {dpis}")
    check("G7 中文/特殊路径可用",
          config.RuleConfig(output_dir=str(ROOT / "_test_runs" / "中文 目录 (1)")).validate() == [])

    # ------------------------------------------------------------------
    print("\n[H] 对抗场景")
    cfg = config.RuleConfig(output_dir=str(fresh("zero_opacity")), font_opacity=0,
                            shadow_opacity=0)
    rr = run([good], cfg)[0]
    check("H1 全透明水印被渲染自检拦截并判失败",
          rr.status == engine.STATUS_FAILED and rr.stage == engine.STAGE_WATERMARK
          and "水印图层为空" in rr.reason,
          f"{rr.status} {rr.reason}")
    cfg = config.RuleConfig(output_dir=str(fresh("tiny_canvas")), target_width=32,
                            target_height=24, font_size=500)
    rr = run([good], cfg)[0]
    check("H2 极小画布不崩溃（失败或自动缩字号）",
          rr.status in (engine.STATUS_SUCCESS, engine.STATUS_FAILED, engine.STATUS_WARNING),
          f"{rr.status} {rr.reason}")
    bad_cfg = config.RuleConfig(output_dir=str(fresh("badformat")), time_format="%Q-%Z")
    check("H3 非法时间格式指令被配置校验拦截",
          any("时间格式" in e for e in bad_cfg.validate()), str(bad_cfg.validate()))
    rr = run([good], bad_cfg)[0]
    check("H3b 非法时间格式也不导致程序崩溃", isinstance(rr.status, str))
    src_copy_dir = fresh("out_equals_src")
    src_copy = src_copy_dir / "sample.jpg"
    shutil.copy2(good, src_copy)
    cfg = config.RuleConfig(output_dir=str(src_copy_dir), filename_template="{name}")
    rr = run([src_copy], cfg)[0]
    check("H4 输出目录=源目录时不覆盖原图",
          rr.ok and Path(rr.output_path).resolve() != src_copy.resolve(),
          str(rr.output_path))
    check("H5 原始样本文件未被改写",
          Image.open(good).size == (1200, 900) and Image.open(src_copy).size == (1200, 900))

    # ------------------------------------------------------------------
    print("\n" + "=" * 68)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗", f)
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
