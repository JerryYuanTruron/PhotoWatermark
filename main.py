"""程序入口。

双击 / 无参数          → 启动操作人员 GUI（窗口模式，无控制台）
--version [输出.json]  → 写出版本元信息（供冻结产物验收）
--selftest <工作目录>  → 在冻结产物内部跑一次端到端自检，结果写入 JSON
                          （窗口程序 stdout 可能为 None，因此结果一律落文件，不依赖控制台）

用法：python main.py  /  PhotoWatermark.exe
"""

from __future__ import annotations

import json
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
        pic(1200, 900).save(p1, quality=95, exif=exif_bytes("2026:05:22 16:29:01"))
        p2 = work / "NOEXIF_0002.jpg"
        pic(800, 600).save(p2, quality=95)
        p3 = work / "BROKEN_0003.jpg"
        p3.write_bytes(b"\xff\xd8\xff\xe0" + b"bad" * 40)

        cfg = config.RuleConfig()
        cfg.output_dir = str(out)
        ck("config_valid", cfg.validate() == [], cfg.validate())

        used: set[str] = set()
        started = datetime.now()
        rs = [engine.process_one(p, cfg, i, used) for i, p in enumerate((p1, p2, p3), 1)]
        statuses = [r.status for r in rs]
        ck("statuses", statuses == [engine.STATUS_SUCCESS, engine.STATUS_FAILED,
                                    engine.STATUS_FAILED], statuses)
        ck("watermark_text", rs[0].watermark_text == "2026/05/22 16:29:01",
           rs[0].watermark_text)

        summary = reporter.summarize(rs, cfg, started, datetime.now())
        paths = reporter.export_reports(rs, cfg, summary)
        ck("reports", "error" not in paths and all(Path(v).is_file() for v in paths.values()),
           paths)
        if "失败清单CSV" in paths:
            text = Path(paths["失败清单CSV"]).read_text(encoding="utf-8-sig")
            ck("failure_list", "NOEXIF_0002.jpg" in text and "BROKEN_0003.jpg" in text, "")

        produced = list(out.glob("*.jpg"))
        ck("only_good_outputs", len(produced) == 1, [x.name for x in produced])
        if produced:
            with Image.open(produced[0]) as im:
                ck("output_size", im.size == (2400, 1800), im.size)
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
