"""图像处理引擎：单张照片的完整处理链，严格映射文档步骤 3.3 → 3.10。

处理链（任一阶段失败即终止该张、清理残留输出、记录阶段与原因）：
  导入解码 → 读取 EXIF 拍摄日期 → 尺寸与像素规格调整 → 时间水印渲染
  → 导出到指定目录 → 输出回读校验

本模块不依赖任何 GUI，可单独被测试与命令行调用。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError

from .config import (
    POSITIONS,
    SUPPORTED_INPUT_EXTS,
    RuleConfig,
    list_system_fonts,
    resolve_font,
)

# --------------------------------------------------------------------------
# 结果与常量
# --------------------------------------------------------------------------

STATUS_SUCCESS = "成功"
STATUS_FAILED = "失败"
STATUS_WARNING = "警告"
STATUS_SKIPPED = "跳过"

STAGE_IMPORT = "导入"
STAGE_TIME = "时间源"
STAGE_RESIZE = "尺寸调整"
STAGE_WATERMARK = "水印渲染"
STAGE_EXPORT = "导出"
STAGE_VERIFY = "输出校验"

# 文档 3.6「拍摄日期」：EXIF 优先级 DateTimeOriginal > DateTimeDigitized > DateTime
EXIF_IFD_EXIF = 0x8769
EXIF_TAG_DATETIME = 306
EXIF_TAG_DATETIME_ORIGINAL = 36867
EXIF_TAG_DATETIME_DIGITIZED = 36868

POS_ANCHORS = {
    "top_left": ("l", "t"), "top_center": ("m", "t"), "top_right": ("r", "t"),
    "middle_left": ("l", "m"), "center": ("m", "m"), "middle_right": ("r", "m"),
    "bottom_left": ("l", "b"), "bottom_center": ("m", "b"), "bottom_right": ("r", "b"),
}

ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')
MAX_FILENAME_CHARS = 120          # 单文件名上限，避免 Windows 260 字符路径限制
MIN_VISIBLE_WM_PIXELS = 20        # 水印区域至少要有这么多个像素被明显改变


@dataclass
class ProcessResult:
    """逐张处理结果。UI 状态表、失败清单、汇总报告共用此结构。"""

    index: int
    source: str
    filename: str
    status: str = STATUS_FAILED
    stage: str = ""
    reason: str = ""
    output_path: str = ""
    original_size: str = ""
    output_size: str = ""
    capture_time: str = ""
    watermark_text: str = ""
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0
    verified: bool = False
    watermark_rect: tuple[int, int, int, int] | None = None
    # 无论成功失败都记录「本次尝试写出的路径」，供批次审计反向核对残留成品
    attempted_output: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_SUCCESS, STATUS_WARNING)

    def as_row(self) -> dict[str, Any]:
        return {
            "序号": self.index,
            "原文件名": self.filename,
            "原文件路径": self.source,
            "状态": self.status,
            "失败阶段": self.stage if self.status in (STATUS_FAILED, STATUS_SKIPPED) else "",
            "失败原因": self.reason if self.status in (STATUS_FAILED, STATUS_SKIPPED, STATUS_WARNING) else "",
            "警告/说明": "；".join(self.warnings),
            "原尺寸": self.original_size,
            "输出尺寸": self.output_size,
            "拍摄时间": self.capture_time,
            "水印内容": self.watermark_text,
            "输出文件": self.output_path,
            "输出校验": "通过" if self.verified else ("未执行" if self.ok else "—"),
            "耗时(ms)": self.duration_ms,
        }


def _fail(r: ProcessResult, stage: str, reason: str) -> ProcessResult:
    r.status, r.stage, r.reason = STATUS_FAILED, stage, reason
    return r


# --------------------------------------------------------------------------
# 辅助：颜色 / EXIF 时间 / 文本 / 文件名
# --------------------------------------------------------------------------


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def read_exif_datetime_ex(exif: Image.Exif) -> tuple[datetime | None, str, str]:
    """按文档 3.6 读取「拍摄日期」。

    返回 (datetime, 命中字段, 偏差说明)。文档要求的是 DateTimeOriginal(拍摄日期)；
    命中其它字段或 DateTimeOriginal 本身损坏时，必须显式报告偏差，禁止静默换字段。
    """
    candidates: list[tuple[int, Any, str]] = []
    try:
        sub = exif.get_ifd(EXIF_IFD_EXIF) or {}
        for tag, label in ((EXIF_TAG_DATETIME_ORIGINAL, "DateTimeOriginal"),
                           (EXIF_TAG_DATETIME_DIGITIZED, "DateTimeDigitized")):
            v = sub.get(tag)
            if v:
                candidates.append((0 if tag == EXIF_TAG_DATETIME_ORIGINAL else 1, v, label))
    except Exception:
        pass
    v = exif.get(EXIF_TAG_DATETIME)
    if v:
        candidates.append((2, v, "DateTime"))
    ordered = sorted(candidates, key=lambda x: x[0])
    original_raw = next((raw for _, raw, label in ordered if label == "DateTimeOriginal"), None)
    original_bad = original_raw is not None and _parse_exif_dt(original_raw) is None
    for _, raw, label in ordered:
        dt = _parse_exif_dt(raw)
        if dt:
            note = ""
            if label != "DateTimeOriginal":
                note = (f"未找到有效的 EXIF 拍摄日期(DateTimeOriginal)，已回退使用 {label}"
                        f"（非拍摄日期，可能不代表实际拍摄时间）")
                if original_bad:
                    note = f"EXIF DateTimeOriginal 存在但格式非法；{note}"
            return dt, label, note
    return None, "", ""


def read_exif_datetime(exif: Image.Exif) -> tuple[datetime | None, str]:
    """兼容接口：返回 (datetime, 命中字段)，忽略偏差说明。"""
    dt, tag, _ = read_exif_datetime_ex(exif)
    return dt, tag


def _parse_exif_dt(raw: Any) -> datetime | None:
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1", errors="ignore")
    s = str(raw).strip().strip("\x00")
    m = re.search(r"(\d{4})[:/\-](\d{1,2})[:/\-](\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})", s)
    if not m:
        return None
    try:
        return datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return None


def format_content(template: str, time_text: str, stem: str, seq: int) -> str:
    # {name}/{filename} 均为原文件名（不含扩展名）；{time} 时间文本；{seq} 四位序号
    tokens = {"time": time_text, "filename": stem, "name": stem, "seq": f"{seq:04d}"}
    out = template
    for key, val in tokens.items():
        out = out.replace("{" + key + "}", val)
    return out.strip()


def safe_filename_part(value: str) -> str:
    return ILLEGAL_FILENAME_CHARS.sub("-", value).strip().strip(".")


def build_output_path(
    source: Path,
    cfg: RuleConfig,
    seq: int,
    extension: str,
    time_text: str,
    used: set[str],
    protected: set[str] | None = None,
) -> Path:
    """生成输出路径。

    protected = 本批次**全部**源照片的规范化路径集合。输出绝不能覆盖其中任何一个，
    否则「输出目录 = 源目录」时，后处理的源照片会被前一张的成品静默替换（原始证据丢失）。
    """
    stem = source.stem
    name = format_content(cfg.filename_template, safe_filename_part(time_text), stem,
                          cfg.start_index + seq - 1)
    name = safe_filename_part(name) or stem
    name = name[:MAX_FILENAME_CHARS]  # 防止模板过长触发 OS 路径长度错误
    out_dir = Path(cfg.output_dir)
    candidate = out_dir / f"{name}{extension}"
    prot = protected if protected is not None else {str(source.resolve()).lower()}

    def collides(p: Path) -> bool:
        try:
            return str(p.resolve()).lower() in prot
        except OSError:
            return False

    if collides(candidate):
        candidate = out_dir / f"{name}_结果{extension}"

    key = str(candidate).lower()
    need_unique = key in used or collides(candidate) or (
        candidate.exists() and cfg.on_exists == "rename")
    if need_unique:
        candidate = _unique_path(candidate, used, prot)
    used.add(str(candidate).lower())
    return candidate


def _unique_path(base: Path, used: set[str], protected: set[str] | None = None) -> Path:
    prot = protected or set()

    def free(p: Path) -> bool:
        try:
            return (str(p.resolve()).lower() not in prot
                    and str(p).lower() not in used and not p.exists())
        except OSError:
            return False

    for i in range(1, 10000):
        p = base.with_name(f"{base.stem}_{i}{base.suffix}")
        if free(p):
            return p
    return base.with_name(f"{base.stem}_{int(time.time() * 1000)}{base.suffix}")


def resolve_output_format(cfg: RuleConfig, source: Path) -> tuple[str, str]:
    """返回 (Pillow 格式名, 扩展名)。同原格式：PNG→PNG，其余→JPEG。"""
    fmt = cfg.output_format
    if fmt == "同原格式":
        fmt = "PNG" if source.suffix.lower() == ".png" else "JPEG"
    return (fmt, ".png" if fmt == "PNG" else ".jpg")


# --------------------------------------------------------------------------
# 处理链各阶段
# --------------------------------------------------------------------------


def _stage_resize(img: Image.Image, cfg: RuleConfig) -> Image.Image:
    """尺寸调整：4 种模式均不改变证据内容的完整性（contain 为默认）。"""
    if img.mode != "RGB":
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, hex_to_rgb(cfg.background) + (255,))
        img = Image.alpha_composite(bg, rgba).convert("RGB")
    if cfg.resize_mode == "none":
        return img
    target = (cfg.target_width, cfg.target_height)
    if cfg.resize_mode == "contain":
        fitted = ImageOps.contain(img, target, method=Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", target, hex_to_rgb(cfg.background))
        canvas.paste(fitted, ((target[0] - fitted.width) // 2,
                              (target[1] - fitted.height) // 2))
        return canvas
    if cfg.resize_mode == "cover":
        return ImageOps.fit(img, target, method=Image.Resampling.LANCZOS,
                            centering=(0.5, 0.5))
    return img.resize(target, Image.Resampling.LANCZOS)


def _load_font(cfg: RuleConfig, text: str, canvas_w: int) -> tuple[ImageFont.FreeTypeFont, int, int, bool]:
    """加载字体并在文本超宽时自动缩小字号，返回 (font, 文本宽, 文本高, 是否缩过)。"""
    path = resolve_font(cfg.font_family, cfg.font_path)
    if not path:
        raise FileNotFoundError("未找到可用字体文件")
    size = cfg.effective_font_px()
    avail = max(32, canvas_w - 2 * cfg.margin_x)
    shrunk = False
    for _ in range(40):
        try:
            font = ImageFont.truetype(path, size, index=0)
        except OSError:
            # 少数 .ttc 需要显式 index；再试一次索引 1
            font = ImageFont.truetype(path, size, index=1)
        probe = Image.new("RGB", (8, 8))
        bbox = ImageDraw.Draw(probe).textbbox((0, 0), text, font=font, anchor="lt")
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= avail or size <= 8:
            return font, tw, th, shrunk
        size = max(8, int(size * avail / tw) - 1)
        shrunk = True
    return font, tw, th, shrunk


def _stage_watermark(
    base: Image.Image, cfg: RuleConfig, text: str
) -> tuple[Image.Image, tuple[int, int, int, int] | None, list[str]]:
    """时间水印渲染：位置 / 字体 / 字号 / 颜色 / 不透明度 / 阴影（文档 3.7、3.8）。"""
    warnings: list[str] = []
    if not cfg.watermark_enabled or not text:
        return base, None, warnings
    # 运行期也拦截（引擎可被命令行/测试直接调用，不经过 config.validate）
    if cfg.font_opacity < 1:
        raise RuntimeError("字体不透明度为 0：文字不可见（仅阴影可见），不满足可见时间戳要求")

    available = {label for label, _ in list_system_fonts()}
    if cfg.font_path and not Path(cfg.font_path).is_file():
        warnings.append("自定义字体文件不存在，已回退到系统字体")
    elif not cfg.font_path and cfg.font_family not in available:
        fallback = resolve_font(cfg.font_family, "")
        warnings.append(f"配置的字体「{cfg.font_family}」在本机不存在，已回退到 {Path(fallback).name}")

    font, tw, th, shrunk = _load_font(cfg, text, base.width)
    if shrunk:
        warnings.append("时间戳宽度超过可用区域，字号已自动缩小以避免截断")
    h, v = POS_ANCHORS.get(cfg.position, ("r", "b"))
    W, H = base.size
    mx, my = cfg.margin_x, cfg.margin_y
    x = mx if h == "l" else ((W - tw) // 2 if h == "m" else W - mx - tw)
    y = my if v == "t" else ((H - th) // 2 if v == "m" else H - my - th)
    x = max(0, min(x, max(0, W - tw)))
    y = max(0, min(y, max(0, H - th)))
    rect = (x, y, min(W, x + tw), min(H, y + th))
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        raise RuntimeError("画布过小，水印文本无可绘制区域（请增大目标尺寸或缩小字号）")

    # 性能优化：只在水印局部区域（含阴影/模糊外扩）建 RGBA 图层。
    # 对整张画布做 GaussianBlur 与 alpha_composite 会让单张处理慢 1 个数量级。
    pad = 4 + cfg.shadow_blur * 3
    px0 = max(0, x - abs(cfg.shadow_offset_x) - pad)
    py0 = max(0, y - abs(cfg.shadow_offset_y) - pad)
    px1 = min(W, x + tw + abs(cfg.shadow_offset_x) + pad)
    py1 = min(H, y + th + abs(cfg.shadow_offset_y) + pad)
    lw, lh = px1 - px0, py1 - py0
    lx, ly = x - px0, y - py0

    layer = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    if cfg.shadow_enabled and cfg.shadow_opacity > 0:
        shadow = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sc = hex_to_rgb(cfg.shadow_color)
        sd.text((lx + cfg.shadow_offset_x, ly + cfg.shadow_offset_y), text, font=font,
                fill=sc + (round(255 * cfg.shadow_opacity / 100),), anchor="lt")
        if cfg.shadow_blur > 0:
            shadow = shadow.filter(ImageFilter.GaussianBlur(cfg.shadow_blur))
        layer = Image.alpha_composite(layer, shadow)

    d = ImageDraw.Draw(layer)
    fc = hex_to_rgb(cfg.font_color)
    kwargs: dict[str, Any] = {}
    if cfg.bold:
        kwargs = {"stroke_width": max(1, font.size // 40), "stroke_fill": fc + (round(255 * cfg.font_opacity / 100),)}
    d.text((lx, ly), text, font=font,
           fill=fc + (round(255 * cfg.font_opacity / 100),), anchor="lt", **kwargs)

    if layer.getbbox() is None:
        raise RuntimeError("水印图层为空，渲染失败")

    out = base.copy()
    region = Image.alpha_composite(out.crop((px0, py0, px1, py1)).convert("RGBA"), layer).convert("RGB")
    out.paste(region, (px0, py0))
    # 渲染有效性自检：水印区域必须发生「足量、肉眼可辨」的变化。
    # 只判 getbbox() 会被「文字全透明但有阴影」「1 像素差异」等配置绕过。
    diff = ImageChops.difference(base.crop(rect), out.crop(rect)).convert("L")
    changed = sum(1 for px in diff.getdata() if px > 8)
    need = max(MIN_VISIBLE_WM_PIXELS, int(diff.width * diff.height * 0.01))
    if changed < need:
        raise RuntimeError(
            f"水印不可见或过淡（仅 {changed} 个像素发生可见变化，需要 ≥{need}）；"
            "请检查字体不透明度、字体色与水印内容")
    return out, rect, warnings


def _save_image(img: Image.Image, path: Path, fmt: str, cfg: RuleConfig,
                raw_exif: bytes, icc_profile: bytes | None = None) -> list[str]:
    """导出。保留 EXIF 与 ICC（文档 3.9 输出页签「保持元数据」）。"""
    warnings: list[str] = []
    opts: dict[str, Any] = {"dpi": (cfg.dpi, cfg.dpi)}
    if fmt == "JPEG":
        opts.update(quality=cfg.jpeg_quality, optimize=True, subsampling=0)
    if cfg.keep_metadata:
        if raw_exif:
            opts["exif"] = raw_exif
        if icc_profile:
            opts["icc_profile"] = icc_profile
    try:
        img.save(path, format=fmt, **opts)
    except (TypeError, ValueError) as exc:
        # 某些格式不接受 exif 参数（旧版 Pillow 的 PNG）：降级但保留告警
        if "exif" in opts:
            opts.pop("exif")
            img.save(path, format=fmt, **opts)
            warnings.append(f"该格式未写入 EXIF 元数据（{exc}）")
        else:
            raise
    return warnings


def _verify_output(path: Path, expected_size: tuple[int, int], fmt: str,
                   cfg: RuleConfig) -> tuple[bool, str, tuple[int, int], tuple[float, float] | None]:
    """输出回读校验：可解码 / 尺寸 / DPI / 文件大小 / 格式。"""
    if not path.is_file():
        return False, "输出文件不存在", (0, 0), None
    size_bytes = path.stat().st_size
    if size_bytes < max(1, cfg.min_output_bytes):
        return False, f"输出文件过小（{size_bytes} B < 阈值 {cfg.min_output_bytes} B），疑似写入不完整", (0, 0), None
    try:
        with Image.open(path) as probe:
            probe.verify()
        with Image.open(path) as chk:
            chk.load()
            out_size = chk.size
            got_fmt = chk.format
            dpi = chk.info.get("dpi")
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        return False, f"输出文件无法解码：{exc}", (0, 0), None
    if got_fmt != fmt:
        return False, f"输出格式不符（期望 {fmt}，实际 {got_fmt}）", out_size, dpi
    if out_size != expected_size:
        return False, f"输出尺寸不符（期望 {expected_size[0]}×{expected_size[1]}，实际 {out_size[0]}×{out_size[1]}）", out_size, dpi
    if dpi:
        # PNG 的 pHYs 会有 ±0.001 级换算误差，给 2% 容差
        for got in dpi:
            if abs(got - cfg.dpi) > max(2.0, cfg.dpi * 0.02):
                return False, f"输出 DPI 不符（期望 {cfg.dpi}，实际 {dpi}）", out_size, dpi
    else:
        return False, "输出文件缺失 DPI(像素规格) 标记", out_size, dpi
    return True, "", out_size, dpi


# --------------------------------------------------------------------------
# 对外主函数
# --------------------------------------------------------------------------


def process_one(source: str | Path, cfg: RuleConfig, seq: int,
                used_names: set[str], protected_paths: set[str] | None = None) -> ProcessResult:
    """处理单张照片，永不抛出未捕获异常；所有失败以 ProcessResult 返回。

    protected_paths：本批次全部源照片的规范化路径集合，防止输出覆盖其它源照片。
    """
    started = time.perf_counter()
    src = Path(source)
    r = ProcessResult(index=seq, source=str(src), filename=src.name)
    out_path: Path | None = None
    tmp_path: Path | None = None

    def abort(stage: str, reason: str) -> ProcessResult:
        """统一失败出口：任何失败路径都清理本次临时/残留文件，杜绝「判失败但已产出」。"""
        _cleanup(tmp_path)
        return _fail(r, stage, reason)

    try:
        # --- 阶段 1：导入解码（文档 3.3） ---
        if not src.exists():
            return abort(STAGE_IMPORT, "文件不存在或已被移动")
        if not src.is_file():
            return abort(STAGE_IMPORT, "目标不是文件（可能是目录）")
        if src.suffix.lower() not in SUPPORTED_INPUT_EXTS:
            return abort(STAGE_IMPORT,
                         f"不支持的文件格式 {src.suffix or '(无扩展名)'}；仅支持 JPG/PNG/BMP/TIF/WEBP")
        try:
            raw = Image.open(src)
            raw.load()
        except UnidentifiedImageError:
            return abort(STAGE_IMPORT, "文件无法识别为图片（内容损坏或格式伪装）")
        except Image.DecompressionBombError:
            return abort(STAGE_IMPORT, "图片像素过大，触发解压保护，疑似异常文件")
        except OSError as exc:
            return abort(STAGE_IMPORT, f"图片解码失败（文件可能截断或损坏）：{exc}")
        r.original_size = f"{raw.width}×{raw.height}"
        if raw.width * raw.height > cfg.max_source_pixels:
            return abort(STAGE_IMPORT,
                         f"源图像素 {raw.width * raw.height:,} 超过安全上限 {cfg.max_source_pixels:,}")

        # EXIF 读取必须在 exif_transpose 之前（transpose 会清理方向标签）
        try:
            exif_obj = raw.getexif()
            exif_dt, exif_tag, exif_note = read_exif_datetime_ex(exif_obj)
        except Exception:
            exif_dt, exif_tag, exif_note = None, "", ""
        icc_profile = raw.info.get("icc_profile") if isinstance(raw.info.get("icc_profile"), bytes) else None
        try:
            oriented = ImageOps.exif_transpose(raw)
        except Exception as exc:
            return abort(STAGE_IMPORT, f"EXIF 方向校正失败：{exc}")
        raw_exif = oriented.info.get("exif", b"") if isinstance(oriented.info.get("exif"), bytes) else b""
        del raw  # 释放大图内存；oriented 为独立副本，不调用 close() 以免误关共享句柄

        # --- 阶段 2：时间源（文档 3.6 / 3.7） ---
        time_text = ""
        name_time_text = datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d_%H%M%S")
        if cfg.watermark_enabled:
            dt: datetime | None = None
            if cfg.time_source == "exif":
                dt, tag = exif_dt, exif_tag
                if dt is None:
                    if cfg.missing_exif_policy == "fail":
                        return abort(STAGE_TIME,
                                     "缺少 EXIF 拍摄日期（DateTimeOriginal / DateTimeDigitized / DateTime 均未找到），"
                                     "无法按文档 3.6 生成时间水印")
                    if cfg.missing_exif_policy == "use_file_mtime":
                        dt = datetime.fromtimestamp(src.stat().st_mtime)
                        r.warnings.append("缺失 EXIF 拍摄日期，按策略回退到文件修改时间，非 EXIF 拍摄日期")
                    else:  # no_watermark：成品无时间戳，按不合格处理，不计入成功
                        return abort(STAGE_TIME,
                                     "按策略「跳过该张水印」处理：成品将不含可见时间戳，不满足文档"
                                     " 3.6 与第 10 节评价要求，故判定失败；请改用 EXIF 或文件修改时间策略")
                else:
                    if exif_note:
                        r.warnings.append(exif_note)
                    r.capture_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                    if exif_tag != "DateTimeOriginal":
                        r.warnings.append("时间戳非 DateTimeOriginal 拍摄日期，非 EXIF 拍摄日期，请人工核对")
            elif cfg.time_source == "file_mtime":
                dt = datetime.fromtimestamp(src.stat().st_mtime)
                r.capture_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                r.warnings.append("按配置使用文件修改时间，非 EXIF 拍摄日期，不满足文档 3.6")
            else:
                time_text = cfg.fixed_time_text
                r.capture_time = "（固定文本）"
                r.warnings.append("时间来源为固定文本，非 EXIF 拍摄日期，不满足文档 3.6")

            if dt is not None:
                try:
                    time_text = dt.strftime(cfg.time_format)
                    name_time_text = time_text
                except (ValueError, TypeError) as exc:
                    return abort(STAGE_TIME, f"时间格式渲染失败：{exc}")
            r.watermark_text = format_content(cfg.content_template, time_text, src.stem,
                                              cfg.start_index + seq - 1)
            if not r.watermark_text:
                return abort(STAGE_TIME, "水印内容为空（请检查内容模板与时间格式）")
            if cfg.time_source != "fixed" and time_text and time_text not in r.watermark_text:
                return abort(STAGE_TIME,
                             "水印内容模板未包含 {time}，成品不含时间戳；请在模板中加入 {time}")

        # --- 阶段 3：尺寸与像素规格调整 ---
        try:
            image = _stage_resize(oriented, cfg)
        except (ValueError, MemoryError, OSError) as exc:
            return abort(STAGE_RESIZE, f"尺寸调整失败：{exc}")
        del oriented

        # --- 阶段 4：时间水印渲染（文档 3.5 / 3.7 / 3.8） ---
        if cfg.watermark_enabled:
            try:
                image, rect, wm_warn = _stage_watermark(image, cfg, r.watermark_text)
                r.watermark_rect = rect
                r.warnings.extend(wm_warn)
            except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
                return abort(STAGE_WATERMARK, f"水印渲染失败：{exc}")

        # --- 阶段 5/6：先写临时文件 → 校验临时文件 → 原子替换到目标路径 ---
        # 这样：① 校验失败不会破坏运行前已存在的同名旧成品；
        #      ② 任何失败都不会在输出目录留下「判失败但已产出」的残留文件。
        fmt, ext = resolve_output_format(cfg, src)
        expected_size = image.size
        prot = protected_paths
        try:
            out_path = build_output_path(src, cfg, seq, ext, name_time_text, used_names, prot)
            r.attempted_output = str(out_path)
            if out_path.exists() and cfg.on_exists == "skip":
                r.status = STATUS_SKIPPED
                r.stage = STAGE_EXPORT
                r.reason = f"同名输出文件已存在（{out_path.name}），按配置跳过；该路径不是本次产出"
                r.warnings.append(f"已存在的同名文件：{out_path}")
                return r
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = out_path.with_name(f"._pw_{os.getpid()}_{seq}_{int(time.time() * 1000)}.tmp")
            r.warnings.extend(_save_image(image, tmp_path, fmt, cfg, raw_exif, icc_profile))
        except (OSError, ValueError, TypeError) as exc:
            return abort(STAGE_EXPORT, f"导出失败：{exc}")

        if cfg.verify_output:
            ok, why, _, _ = _verify_output(tmp_path, expected_size, fmt, cfg)
            if not ok:
                return abort(STAGE_VERIFY, f"输出校验未通过：{why}")
            r.verified = True
        else:
            if not tmp_path.is_file() or tmp_path.stat().st_size < 1:
                return abort(STAGE_VERIFY, "输出文件未生成或为空")
            r.warnings.append("按配置未执行输出回读校验")

        try:
            os.replace(str(tmp_path), str(out_path))
        except OSError as exc:
            return abort(STAGE_EXPORT, f"成品落盘失败：{exc}")
        tmp_path = None

        r.output_path = str(out_path)
        r.output_size = f"{expected_size[0]}×{expected_size[1]}"
        downgrade = ("缺失 EXIF", "非 EXIF", "未执行输出回读校验", "未写入 EXIF",
                     "自动缩小", "回退到", "不足")
        r.status = STATUS_WARNING if any(
            any(k in w for k in downgrade) for w in r.warnings) else STATUS_SUCCESS
        return r

    except Exception as exc:  # 兜底：任何未预料异常都必须落到逐张状态里
        return abort(r.stage or "未知阶段", f"未预料异常：{type(exc).__name__}: {exc}")
    finally:
        _cleanup(tmp_path)
        r.duration_ms = int((time.perf_counter() - started) * 1000)


def _cleanup(path: Path | None) -> None:
    """失败时清理残留输出，保证输出目录内只保留合格成品。"""
    if path is None:
        return
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
