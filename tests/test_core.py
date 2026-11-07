"""核心链路全量自动化测试（无需 GUI）。

覆盖：配置、EXIF 优先级、四种缩放、DPI、九宫格水印、字号单位、模板、
失败识别与清理、输出回读校验、报告与批次审计、边界与对抗场景。

用法：python tests/test_core.py
"""

from __future__ import annotations

import json
import shutil
import struct
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import __version__, config, engine, reporter  # noqa: E402
from tests.make_samples import build  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def C(**kw) -> config.RuleConfig:
    """测试基线配置：沿用 v1.0.0 的合成小图口径（2400×1800、{name}_结果）。

    v1.1.0 起默认改为「不缩放 + ≥21MP + 合规命名」，这几项对 1200×900 合成样本
    不适用，故在通用测试里显式关闭；默认行为由 J 组单独验证。
    """
    base = dict(resize_mode="contain", target_width=2400, target_height=1800,
                min_output_megapixels=0, filename_template="{name}_结果",
                ecu_name="EEPH", serial_number="TESTSERIAL")
    base.update(kw)
    return config.RuleConfig(**base)


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
    c = C()
    check("A1 文档默认值", (c.target_width, c.target_height, c.dpi, c.font_size,
                            c.position, c.time_format) ==
          (2400, 1800, 300, 70, "bottom_right", "%Y/%m/%d %H:%M:%S"))
    c.output_dir = str(ROOT / "_test_runs")
    check("A2 默认配置通过校验", c.validate() == [], str(c.validate()))
    c2 = config.RuleConfig.from_dict({"target_width": "1234", "dpi": None,
                                      "unknown_key": 1, "jpeg_quality": 999})
    check("A3 残缺/未知字段安全载入", c2.target_width == 1234 and c2.dpi == 300)
    check("A4 非法参数被校验拦截", C(target_width=0, dpi=0,
                                                    output_dir=str(ROOT / "_test_runs")).validate() != [])
    tmp_json = ROOT / "_test_runs" / "rule.json"
    c.save(tmp_json)
    check("A5 规则文件 JSON 往返", config.RuleConfig.load(tmp_json).time_format == c.time_format)
    check("A6 pt 单位按 DPI 换算", C(font_size=72, font_size_unit="pt",
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
        cfg = C(resize_mode=mode, output_dir=str(fresh("size_" + mode)))
        rs = run([good], cfg)
        out = Path(rs[0].output_path)
        with Image.open(out) as chk:
            check(f"C1 {mode} 输出尺寸 {exp}", chk.size == exp, str(chk.size))
            check(f"C2 {mode} DPI=300",
                  chk.info.get("dpi") is not None and abs(chk.info["dpi"][0] - 300) < 2,
                  str(chk.info.get("dpi")))
    cfg = C(output_dir=str(fresh("portrait")))
    rs = run([portrait], cfg)
    with Image.open(rs[0].output_path) as chk:
        check("C3 竖图 contain 输出仍为统一 2400×1800", chk.size == (2400, 1800))
    cfg = C(output_dir=str(fresh("orient")), resize_mode="none")
    rs = run([orient], cfg)
    with Image.open(rs[0].output_path) as chk:
        ex = chk.getexif()
        check("C4 EXIF 方向已校正且方向标签清除",
              chk.size == (900, 1200) and ex.get(0x0112) is None,
              f"size={chk.size}, orientation={ex.get(0x0112)}")
    cfg = C(output_dir=str(fresh("formats")))
    rs = run([png], cfg)
    with Image.open(rs[0].output_path) as chk:
        check("C5 PNG 输入同原格式→PNG 输出且保留 EXIF",
              chk.format == "PNG" and chk.getexif().get_ifd(0x8769).get(36867) is not None)

    # ------------------------------------------------------------------
    print("\n[D] 时间水印（文档 3.5 / 3.7 / 3.8）")
    cfg = C(output_dir=str(fresh("watermark")))
    rs = run([good], cfg)
    r = rs[0]
    check("D1 水印文本为 年/月/日 时:分:秒", r.watermark_text == "2026/05/22 16:29:01")
    check("D2 默认水印位于右下（含 24/20 px 安全边距）",
          r.watermark_rect is not None and r.watermark_rect[2] >= 2300
          and r.watermark_rect[3] >= 1700, str(r.watermark_rect))
    for pos, where in (("top_left", "左上"), ("center", "正中"), ("bottom_left", "左下")):
        cfg = C(output_dir=str(fresh("pos_" + pos)), position=pos)
        rr = run([good], cfg)[0]
        rect = rr.watermark_rect or (0, 0, 0, 0)
        ok = ({"top_left": rect[0] < 50 and rect[1] < 50,
               "center": abs(rect[0] + rect[2] - 2400) < 200,
               "bottom_left": rect[0] < 50 and rect[3] > 1700}[pos])
        check(f"D3 位置 {where} 生效", ok, str(rect))
    cfg = C(output_dir=str(fresh("template")),
                            content_template="{filename} | {seq} | {time}")
    rr = run([good], cfg)[0]
    check("D4 内容模板 token 替换",
          rr.watermark_text == "PeCu_2718281828_0001 | 0001 | 2026/05/22 16:29:01",
          rr.watermark_text)
    cfg = C(output_dir=str(fresh("dashfmt")), time_format="%Y-%m-%d %H:%M:%S")
    check("D5 时间格式可改", run([good], cfg)[0].watermark_text == "2026-05-22 16:29:01")
    cfg = C(output_dir=str(fresh("nosha")), shadow_enabled=False,
                            font_family="黑体", font_size=120)
    check("D6 字体/阴影/字号可改且仍成功", run([good], cfg)[0].ok)
    cfg = C(output_dir=str(fresh("longfmt")), font_size=500,
                            time_format="拍摄时间 %Y年%m月%d日 %H时%M分%S秒 样品编号ABCDEF")
    rr = run([tiny], cfg)[0]
    check("D7 超宽时间戳自动缩字号不截断",
          rr.ok and any("自动缩小" in w for w in rr.warnings), str(rr.warnings))
    cfg = C(output_dir=str(fresh("no_wm")), watermark_enabled=False)
    rr = run([noexif], cfg)[0]
    check("D8 关闭水印后无 EXIF 照片也可正常输出", rr.ok and rr.watermark_text == "")

    # ------------------------------------------------------------------
    print("\n[E] 失败识别、策略与清理")
    base = fresh("fail")
    cfg = C(output_dir=str(base))
    rs = run([good, noexif, broken, notimage, good.with_name("MISSING.jpg")], cfg)
    reasons = [(r.status, r.stage, r.reason) for r in rs]
    check("E1 正常照片成功", rs[0].status == engine.STATUS_SUCCESS)
    check("E2 缺 EXIF → 时间源阶段失败",
          rs[1].status == engine.STATUS_FAILED and rs[1].stage == engine.STAGE_TIME
          and "缺少 EXIF 信息" in rs[1].reason)
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

    cfg = C(output_dir=str(fresh("policy_use_file_mtime")),
                            missing_exif_policy="use_file_mtime")
    rr = run([noexif], cfg)[0]
    check("E8 EXIF缺失策略 use_file_mtime → 警告且说明非拍摄日期",
          rr.status == engine.STATUS_WARNING and any("非 EXIF 拍摄日期" in w for w in rr.warnings),
          f"{rr.status} {rr.warnings}")
    cfg = C(output_dir=str(fresh("policy_no_watermark")),
                            missing_exif_policy="no_watermark")
    rr = run([noexif], cfg)[0]
    check("E8b EXIF缺失策略 no_watermark → 判失败（成品无时间戳不可交付）",
          rr.status == engine.STATUS_FAILED and "不含可见时间戳" in rr.reason,
          f"{rr.status} {rr.reason}")

    cfg = C(output_dir=str(fresh("mtime")), time_source="file_mtime")
    rr = run([noexif], cfg)[0]
    check("E9 时间来源改为文件修改时间可用且有告警", rr.ok and rr.warnings)
    cfg = C(output_dir=str(fresh("fixed")), time_source="fixed",
                            fixed_time_text="2026/11/07 12:00:00")
    check("E10 固定文本内容生效",
          run([noexif], cfg)[0].watermark_text == "2026/11/07 12:00:00")
    cfg = C(output_dir=str(fresh("badfont")), font_path=r"Z:\nonexistent\font.ttf",
                            font_family="不存在字体")
    rr = run([good], cfg)[0]
    check("E11 字体不存在时回退到系统字体而非整批失败", rr.ok, str(rr.reason))

    # 模拟输出校验失败，验证残留成品被清理
    cfg = C(output_dir=str(fresh("verify_fail")))
    with mock.patch.object(engine, "_verify_output",
                           return_value=(False, "模拟校验失败", (0, 0), None)):
        rr = run([good], cfg)[0]
    check("E12 校验失败 → 失败状态 + 清理输出文件",
          rr.status == engine.STATUS_FAILED and rr.stage == engine.STAGE_VERIFY
          and not list((ROOT / "_test_runs" / "verify_fail").glob("*.jpg")),
          f"{rr.status} {rr.reason}")

    # ------------------------------------------------------------------
    print("\n[F] 输出命名与同名策略")
    cfg = C(output_dir=str(fresh("names")), filename_template="{name}")
    rr = run([good], cfg)[0]
    check("F1 {name} 保持原名（不追加 _结果 后缀）",
          Path(rr.output_path).stem == "PeCu_2718281828_0001", Path(rr.output_path).name)
    cfg = C(output_dir=str(fresh("seqname")), filename_template="{seq}_{name}")
    check("F2 序号模板生效", Path(run([good], cfg)[0].output_path).name.startswith("0001_"))
    cfg = C(output_dir=str(fresh("illegal")),
                            filename_template=r'{name}:*?_结果')
    rr = run([good], cfg)[0]
    check("F3 模板含非法字符被安全替换", rr.ok and Path(rr.output_path).is_file(),
          Path(rr.output_path).name if rr.output_path else "")
    dup = fresh("dup")
    cfg = C(output_dir=str(dup))
    first = run([good, good], cfg)
    check("F4 同名输入自动去重另存，不互相覆盖",
          len({r.output_path for r in first}) == 2 and len(list(dup.glob("*.jpg"))) == 2)
    cfg.on_exists = "skip"
    second = run([good], C(output_dir=str(dup), on_exists="skip",
                                           filename_template="{name}_结果"))
    check("F5 同名策略 skip 生效", second[0].status == engine.STATUS_SKIPPED)
    cfg2 = C(output_dir=str(fresh("rename")), on_exists="rename",
                             filename_template="{name}")
    run([good], cfg2)
    run([good], cfg2)
    check("F6 同名策略 rename 生效",
          len(list((ROOT / "_test_runs" / "rename").glob("*.jpg"))) == 2)

    # ------------------------------------------------------------------
    print("\n[G] 报告、批次审计与一致性")
    out = fresh("report")
    cfg = C(output_dir=str(out))
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
          and "缺少 EXIF 信息" in csv_text)
    payload = json.loads(Path(paths["处理报告JSON"]).read_text(encoding="utf-8"))
    # 版本期望值从源码读取，断言「报告版本 == 源码声明版本」这一跨版本恒真关系，
    # 不写死 1.0.0/1.1.0（升版即假红）。
    check("G4 报告含版本/配置快照/逐张结果",
          payload["version"] == __version__ and payload["config"]["dpi"] == 300
          and len(payload["results"]) == 8, payload.get("version"))
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
          C(output_dir=str(ROOT / "_test_runs" / "中文 目录 (1)")).validate() == [])

    # ------------------------------------------------------------------
    print("\n[H] 对抗场景")
    cfg = C(output_dir=str(fresh("zero_opacity")), font_opacity=0,
                            shadow_opacity=0)
    rr = run([good], cfg)[0]
    check("H1 全透明水印被渲染自检拦截并判失败",
          rr.status == engine.STATUS_FAILED and rr.stage == engine.STAGE_WATERMARK
          and ("水印图层为空" in rr.reason or "字体不透明度" in rr.reason),
          f"{rr.status} {rr.reason}")
    cfg = C(output_dir=str(fresh("tiny_canvas")), target_width=32,
                            target_height=24, font_size=500)
    rr = run([good], cfg)[0]
    check("H2 极小画布不崩溃（失败或自动缩字号）",
          rr.status in (engine.STATUS_SUCCESS, engine.STATUS_FAILED, engine.STATUS_WARNING),
          f"{rr.status} {rr.reason}")
    bad_cfg = C(output_dir=str(fresh("badformat")), time_format="%Q-%Z")
    check("H3 非法时间格式指令被配置校验拦截",
          any("时间格式" in e for e in bad_cfg.validate()), str(bad_cfg.validate()))
    rr = run([good], bad_cfg)[0]
    check("H3b 非法时间格式也不导致程序崩溃", isinstance(rr.status, str))
    src_copy_dir = fresh("out_equals_src")
    src_copy = src_copy_dir / "sample.jpg"
    shutil.copy2(good, src_copy)
    cfg = C(output_dir=str(src_copy_dir), filename_template="{name}")
    rr = run([src_copy], cfg)[0]
    check("H4 输出目录=源目录时不覆盖原图",
          rr.ok and Path(rr.output_path).resolve() != src_copy.resolve(),
          str(rr.output_path))
    check("H5 原始样本文件未被改写",
          Image.open(good).size == (1200, 900) and Image.open(src_copy).size == (1200, 900))

    # ------------------------------------------------------------------
    print("\n[I] 对抗式审查回归（独立审查复现的高危场景）")
    cfg = C(output_dir=str(fresh("invisible")), font_opacity=0,
                            shadow_opacity=70)
    rr = run([good], cfg)[0]
    check("I1 文字全透明+有阴影 不再判成功",
          rr.status == engine.STATUS_FAILED, f"{rr.status} {rr.reason}")
    check("I1b 配置校验拦截 font_opacity=0",
          any("字体不透明度" in e for e in cfg.validate()), str(cfg.validate()))
    cfg = C(output_dir=str(fresh("no_token")), content_template="PHOTO OK")
    rr = run([good], cfg)[0]
    check("I2 水印模板不含 {time} 被拦截",
          rr.status == engine.STATUS_FAILED and "不含时间戳" in rr.reason,
          f"{rr.status} {rr.reason}")
    check("I2b 配置校验拦截缺失 {time}",
          any("{time}" in e for e in cfg.validate()), str(cfg.validate()))
    dt3, tag3, note3 = engine.read_exif_datetime_ex(
        Image.open(by_name["PeCu_2718281828_0002.jpg"]).getexif())
    check("I3 回退到 DateTime 时显式报告偏差",
          tag3 == "DateTime" and "非拍摄日期" in note3, f"{tag3} / {note3}")
    cfg = C(output_dir=str(fresh("fallback_note")))
    rr = run([by_name["PeCu_2718281828_0002.jpg"]], cfg)[0]
    check("I3b 回退时间戳成品降级为警告",
          rr.status == engine.STATUS_WARNING and any("非 EXIF 拍摄日期" in w for w in rr.warnings),
          f"{rr.status} {rr.warnings}")
    cfg = C(output_dir=str(fresh("fixed_note")), time_source="fixed",
                            fixed_time_text="2026/11/07 12:00:00")
    rr = run([good], cfg)[0]
    check("I3c 固定文本也降级为警告",
          rr.status == engine.STATUS_WARNING and any("非 EXIF 拍摄日期" in w for w in rr.warnings),
          f"{rr.status} {rr.warnings}")

    # H2：输出目录 = 源目录，且输出名与「另一张待处理源」同名
    coll = fresh("collision")
    a = coll / "A.jpg"
    b = coll / "A_结果.jpg"
    shutil.copy2(good, a)
    shutil.copy2(by_name["PeCu_2718281828_0002.jpg"], b)
    b_before = b.read_bytes()
    cfg = C(output_dir=str(coll))
    protected = {str(x.resolve()).lower() for x in (a, b)}
    used: set[str] = set()
    r1 = engine.process_one(a, cfg, 1, used, protected)
    r2 = engine.process_one(b, cfg, 2, used, protected)
    check("I4 批次内源照片不被前一张成品覆盖",
          b.read_bytes() == b_before and r1.ok and r2.ok
          and Path(r1.output_path).resolve() != b.resolve(),
          f"r1={r1.status}:{r1.output_path} r2={r2.status} b_changed={b.read_bytes() != b_before}")

    # H5：导出阶段中途异常不得残留半成品，且批次审计必须发现
    leak = fresh("export_leak")
    cfg = C(output_dir=str(leak))
    original_save = engine._save_image

    def broken_save(img, path, fmt, c, raw_exif, icc=None):
        Path(path).write_bytes(b"partial")
        raise OSError("模拟写入中断")

    with mock.patch.object(engine, "_save_image", broken_save):
        bad = run([good], cfg)[0]
    engine._save_image = original_save
    check("I5 导出异常 → 失败且输出目录无残留",
          bad.status == engine.STATUS_FAILED and bad.stage == engine.STAGE_EXPORT
          and not list(leak.iterdir()),
          f"{bad.status} {[p.name for p in leak.iterdir()]}")
    check("I5b 失败结果记录 attempted_output 供审计",
          bool(bad.attempted_output), bad.attempted_output)

    # M5：校验失败不得销毁运行前已存在的同名旧成品
    keep = fresh("keep_old")
    old = keep / "PeCu_2718281828_0001_结果.jpg"
    old.write_bytes(b"PREEXISTING")
    cfg = C(output_dir=str(keep), min_output_bytes=10_000_000)
    rr = run([good], cfg)[0]
    check("I6 校验失败不破坏运行前旧成品",
          rr.status == engine.STATUS_FAILED and old.read_bytes() == b"PREEXISTING",
          f"{rr.status} old={old.read_bytes()[:20]}")

    # 批次尺寸一致性提示
    cfg = C(output_dir=str(fresh("size_note")), resize_mode="none")
    rs = run([good, portrait], cfg)
    s = reporter.summarize(rs, cfg, datetime.now(), datetime.now())
    findings = reporter.audit_batch(rs, cfg)
    check("I7 不缩放导致尺寸不一致时审计给出提示",
          any("尺寸不一致" in f for f in findings), str(findings))
    cfg_gate = C(output_dir=str(fresh("pixel_note")), resize_mode="none",
                 min_output_megapixels=21)
    rs2 = run([good, portrait], cfg_gate)
    findings2 = reporter.audit_batch(rs2, cfg_gate)
    check("I8 报告显式记录 2100 万像素合规门槛",
          any("2100 万像素" in f and "21 MP" in f for f in findings2), str(findings2))

    # ------------------------------------------------------------------
    print("\n[J] v1.1.0 客户合规口径（V1 p.3 / V2 §8·§10·§11）")
    d = config.RuleConfig()
    check("J1 程序默认即为合规口径",
          d.resize_mode == "none" and d.min_output_megapixels == 21
          and d.filename_template == "{ecu}_{serial}_{seq}"
          and d.time_source == "exif" and d.missing_exif_policy == "fail"
          and d.ecu_name == "" and d.serial_number == "",
          f"{d.resize_mode}/{d.min_output_megapixels}/{d.filename_template}")
    check("J2 默认配置强制要求填写命名前缀",
          any("ECU 名称" in e for e in C(ecu_name="", serial_number="",
                                         filename_template="{ecu}_{serial}_{seq}").validate()), "")
    check("J3 目标尺寸低于 21MP 被配置校验拦截",
          any("低于客户要求" in e for e in C(
              resize_mode="contain", target_width=2400, target_height=1800,
              min_output_megapixels=21).validate()), "")
    check("J4 21MP 目标尺寸(5292×3969)通过校验",
          not any("低于客户要求" in e for e in C(
              resize_mode="contain", target_width=5292, target_height=3969,
              min_output_megapixels=21).validate()), "")
    low = fresh("pixel_gate")
    cfg_low = C(output_dir=str(low), resize_mode="none", min_output_megapixels=21)
    rl = run([good], cfg_low)[0]
    check("J5 主输出像素不足时判失败且无残留",
          rl.status == engine.STATUS_FAILED and rl.stage == engine.STAGE_VERIFY
          and "低于客户要求" in rl.reason and not list(low.glob("*.jpg")),
          f"{rl.status} {rl.reason[:80]}")

    named = fresh("naming")
    cfg_name = C(output_dir=str(named), filename_template="{ecu}_{serial}_{seq}",
                 ecu_name="EEPH", serial_number="T55B92BD20126B119X", start_index=7)
    rn = run([good], cfg_name)[0]
    check("J6 合规命名生成 <ECU>_<序列号>_<四位序号>",
          rn.ok and Path(rn.output_path).name == "EEPH_T55B92BD20126B119X_0007.jpg",
          Path(rn.output_path).name if rn.output_path else "")
    check("J7 命名 token 未留字面量占位符",
          "{" not in Path(rn.output_path).name, rn.output_path)

    # 构造「EXIF 块存在但无拍摄日期字段、且数据区残留时间字符串」的样本：
    # 用于验证触发判断只看 EXIF 字段，不因残留数据而误判为「含 EXIF」。
    ex_dir = fresh("exif_missing")
    ex = ex_dir / "no_exif_time.jpg"
    Image.new("RGB", (64, 48), (100, 120, 140)).save(ex, format="JPEG", quality=90)
    raw = ex.read_bytes()
    tiff = (b"MM\x00*\x00\x00\x00\x08" + b"\x00\x00" + b"\x00\x00\x00\x00"
            + b"2026:11:02 15:52:21\x00")
    app1_payload = b"Exif\x00\x00" + tiff
    seg = b"\xff\xe1" + struct.pack(">H", len(app1_payload) + 2) + app1_payload
    ex.write_bytes(raw[:2] + seg + raw[2:])
    check("J8 触发判断只依据 EXIF：缺拍摄日期即触发",
          not engine.has_exif_capture_time(ex)
          and engine.precheck_exif(ex) == engine.EXIF_MISSING_REASON,
          engine.precheck_exif(ex))
    check("J9 含 EXIF 的照片不触发",
          engine.has_exif_capture_time(good) and engine.precheck_exif(good) == "",
          engine.precheck_exif(good))
    # 含 EXIF 但命中字段不是 DateTimeOriginal：仍属「含 EXIF」，不得触发提示
    only_dt = by_name["PeCu_2718281828_0002.jpg"]
    check("J9b 含 EXIF(仅 DateTime)也不触发",
          engine.has_exif_capture_time(only_dt) and engine.precheck_exif(only_dt) == "",
          engine.precheck_exif(only_dt))
    banned = ("微信", "截图", "传输", "APP1", "剥离", "来源", "推断", "诊断")
    check("J10 提示文案简洁且不含来源类信息",
          "EXIF" in engine.EXIF_NOTICE_MESSAGE
          and "原始照片" in engine.EXIF_NOTICE_MESSAGE
          and all(w not in engine.EXIF_NOTICE_MESSAGE for w in banned)
          and all(w not in engine.EXIF_NOTICE_TITLE for w in banned)
          and all(w not in engine.EXIF_MISSING_REASON for w in banned),
          engine.EXIF_NOTICE_MESSAGE)
    check("J10b 已移除来源类诊断能力",
          not hasattr(engine, "diagnose_app1_capture_time"), "")
    cfg_wx = C(output_dir=str(ex_dir / "out"), missing_exif_policy="fail")
    rw = run([ex], cfg_wx)[0]
    check("J11 缺 EXIF 不静默跳过：判失败、无成品、原因统一",
          rw.status == engine.STATUS_FAILED and rw.stage == engine.STAGE_TIME
          and rw.reason == engine.EXIF_MISSING_REASON
          and rw.watermark_text == "" and rw.output_path == "",
          f"{rw.status} {rw.reason[:110]}")

    rc_dir = fresh("report_copy")
    cfg_rc = C(output_dir=str(rc_dir), resize_mode="none",
               target_width=1600, target_height=1200, report_copy_enabled=True)
    rrc = run([good], cfg_rc)[0]
    copies = sorted(p.name for p in rc_dir.glob("*.jpg"))
    check("J12 可选报告插图副本生成且不替代主交付原件",
          rrc.ok and bool(rrc.report_copy_path) and len(copies) == 2
          and any("_报告版" in n for n in copies)
          and any("报告插图副本" in w for w in rrc.warnings), str(copies))
    check("J13 报告副本水印与主输出同源",
          bool(rrc.watermark_text) and rrc.watermark_text == "2026/05/22 16:29:01",
          rrc.watermark_text)

    # ------------------------------------------------------------------
    print("\n[K] 导入阶段：4:3 横版比例与方向校验")
    gd = fresh("geometry")

    def geo_img(name: str, w: int, h: int, ori: int | None = None) -> Path:
        p = gd / name
        ex = Image.Exif()
        ex.get_ifd(0x8769)[36867] = "2026:11:05 16:08:32"
        if ori is not None:
            ex[0x0112] = ori
        Image.new("RGB", (w, h), (90, 110, 130)).save(p, quality=90, exif=ex.tobytes())
        return p

    ok43 = [geo_img("OK_1200.jpg", 1200, 900), geo_img("OK_BIG.jpg", 8192, 6144),
            geo_img("OK_5292.jpg", 5292, 3969)]
    check("K1 4:3 横版（含大图）判定合格",
          all(engine.check_geometry(p) is None for p in ok43), "")

    pm = geo_img("P_MANUAL.jpg", 900, 1200)
    gm = engine.check_geometry(pm)
    check("K2 竖版 3:4 判定为旋转可修正",
          gm is not None and gm.kind == "portrait_rotatable", str(gm))
    check("K3 无可靠方向依据时要求人工确认且不指定方向",
          gm is not None and gm.rotation == "manual"
          and "人工确认" in gm.advice and "不指定方向" in gm.advice
          and "顺时针" not in gm.advice and "逆时针" not in gm.advice,
          gm.advice if gm else "")

    gcw = engine.check_geometry(geo_img("P_CW.jpg", 900, 1200, 6))
    gccw = engine.check_geometry(geo_img("P_CCW.jpg", 900, 1200, 8))
    check("K4 EXIF 方向 6 → 顺时针建议且注明程序自动校正",
          gcw is not None and gcw.rotation_reliable and gcw.rotation == "cw"
          and "顺时针" in gcw.advice and "无需手工旋转" in gcw.advice,
          gcw.advice if gcw else "")
    check("K5 EXIF 方向 8 → 逆时针建议且注明程序自动校正",
          gccw is not None and gccw.rotation_reliable and gccw.rotation == "ccw"
          and "逆时针" in gccw.advice and "无需手工旋转" in gccw.advice,
          gccw.advice if gccw else "")
    other_ori = [engine.check_geometry(geo_img(f"P_O{x}.jpg", 900, 1200, x))
                 for x in (0, 1, 2, 3, 4, 5, 7)]
    check("K6 其余方向标记一律不擅自指定方向",
          all(g is not None and g.rotation == "manual" for g in other_ori), "")

    ratio_cases = [
        ("K7 竖版且比例不符", geo_img("P_BAD.jpg", 900, 1600), "portrait_ratio"),
        ("K8 横版但比例不符", geo_img("L_BAD.jpg", 1600, 900), "landscape_ratio"),
        ("K9 正方形比例不符", geo_img("S_BAD.jpg", 1000, 1000), "square"),
        ("K9b 真实规格 8768×6144 判为横版比例不符",
         geo_img("REAL.jpg", 8768, 6144), "landscape_ratio"),
    ]
    ratio_issues = []
    for label, path, kind in ratio_cases:
        g = engine.check_geometry(path)
        ratio_issues.append(g)
        check(label + "：仅旋转无法修正并提示裁剪/更换",
              g is not None and g.kind == kind
              and all(w in g.advice for w in ("仅旋转无法修正比例", "裁剪", "更换", "4:3")),
              g.brief if g else "")

    broken_geo = gd / "BROKEN.jpg"
    broken_geo.write_bytes(b"\xff\xd8\xff\xe0" + b"garbage" * 20)
    gbr = engine.check_geometry(broken_geo)
    check("K10 无法读取尺寸的文件单独标记，不误报为竖版",
          gbr is not None and gbr.kind == "unreadable"
          and "竖版" not in gbr.anomaly, gbr.brief if gbr else "")

    notice = engine.geometry_notice_text([x for x in [gm, gcw, gccw] + ratio_issues + [gbr] if x])
    check("K11 提示正文列出受影响文件、异常类型、目标要求与非阻断说明",
          all(n in notice for n in (pm.name, "P_CW.jpg", "L_BAD.jpg", "BROKEN.jpg"))
          and "竖版" in notice and "横版" in notice
          and "目标要求：符合 4:3 的横版照片" in notice
          and "不阻断导入" in notice, "")
    banned_geo = ("微信", "截图", "传输", "APP1", "剥离", "来源", "推断", "诊断")
    check("K12 提示文案纯净、简洁、无来源类信息",
          all(w not in notice for w in banned_geo)
          and "程序不指定方向" in notice and "仅旋转无法修正比例" in notice, "")
    # 文件名不得影响判定（防止用文件名或来源类信息替代像素尺寸判断）
    check("K13 判定只依据像素尺寸，不依据文件名",
          engine.check_geometry(geo_img("名为竖版实则横版.jpg", 1200, 900)) is None
          and engine.check_geometry(geo_img("名为横版实则竖版.jpg", 900, 1200)) is not None, "")
    cfg_ori = C(output_dir=str(fresh("geo_orient_out")), resize_mode="none",
                min_output_megapixels=0)
    ro = run([geo_img("P_CW_PROCESS.jpg", 900, 1200, 6)], cfg_ori)[0]
    if ro.ok and ro.output_path and Path(ro.output_path).is_file():
        with Image.open(ro.output_path) as oim:
            actual = oim.size
    else:
        actual = ()
    check("K14 措辞属实：方向标记在引擎中自动校正为 4:3 横版",
          ro.ok and actual == (1200, 900), f"{ro.status} {actual} {ro.reason[:60]}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 68)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗", f)
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
