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

from PIL import (Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps,
                 ImageStat, UnidentifiedImageError)

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

# --------------------------------------------------------------------------
# 时间水印取色：按每张照片的背景自动选择墨色，不使用固定白色
# --------------------------------------------------------------------------
WM_INK_DARK = (16, 16, 16)        # 浅色/同色背景用深墨
WM_INK_LIGHT = (250, 250, 250)    # 深色背景用浅墨
WM_MIN_INK_CONTRAST = 60.0        # 文字与底层背景的实测平均亮度差下限（0–255）

# 缺少 EXIF 时面向操作人员的统一提示。
# 约束：只说明「缺 EXIF」与「提供保留完整 EXIF 的原始照片」，
# 不附加诊断、建议、数量、技术细节或任何额外字样，避免歧义。
EXIF_NOTICE_TITLE = "照片缺少 EXIF 信息"
EXIF_NOTICE_MESSAGE = "缺少 EXIF 信息。请提供保留完整 EXIF 的原始照片。"
EXIF_MISSING_REASON = "缺少 EXIF 信息，无法生成时间水印。请提供保留完整 EXIF 的原始照片。"

# --------------------------------------------------------------------------
# 导入阶段：比例与方向校验（目标要求为「符合 4:3 的横版照片」）
#
# 判定只依据文件内实际编码的像素宽高（Image.size），不依赖文件名、文件时间
# 或任何照片来源信息。旋转方向的唯一可靠依据是 EXIF Orientation：
#   6 → 需顺时针旋转 90° 才是正向（对应 Pillow exif_transpose 的 ROTATE_270）
#   8 → 需逆时针旋转 90° 才是正向（对应 ROTATE_90）
# 其余取值（含 0/1、镜像 2/4/5/7、180° 3）无法可靠判断 90° 旋转方向，
# 一律要求操作人员人工确认，程序不指定方向。
# --------------------------------------------------------------------------

TARGET_ASPECT = 4.0 / 3.0
ASPECT_TOLERANCE = 0.01                 # 宽高比相对容差 1%
EXIF_TAG_ORIENTATION = 0x0112
ORIENTATION_ROTATION = {6: "cw", 8: "ccw"}

GEOMETRY_NOTICE_TITLE = "照片比例 / 方向提示"
GEOMETRY_TARGET_LABEL = ("目标要求：符合 4:3 的横版照片；处理时自动居中裁边"
                         "（横版左右各约 3.3%，不缩放、不重采样）。")
GEOMETRY_ROTATION_TEXT = {
    # 带方向标记的照片引擎会自动按标记校正，须明确「无需手工旋转」以免重复旋转
    "cw": "仍会照常处理；依据方向标记需顺时针旋转 90°，程序会自动校正，无需手工旋转",
    "ccw": "仍会照常处理；依据方向标记需逆时针旋转 90°，程序会自动校正，无需手工旋转",
    "manual": "仍会照常处理；如需 4:3 横版，旋转方向请人工确认，程序不指定方向",
}
# 横版比例不符：默认居中裁边即可达标，无需任何人工介入
GEOMETRY_RATIO_ADVICE = "处理时自动居中裁边到 4:3，无需手工调整或单独处理"
GEOMETRY_RATIO_ADVICE_NO_CROP = ("当前缩放方式不会裁成 4:3；仍会照常处理，"
                                 "如需 4:3 请改回「居中裁边到 4:3」")
GEOMETRY_RATIO_KEEP_ADVICE = "仍会照常处理（保持原样）；如需 4:3 横版，请裁剪或更换源图"
GEOMETRY_UNREADABLE_ADVICE = "该文件无法处理，请确认可正常打开或更换文件"
GEOMETRY_NONBLOCKING_TEXT = "上述照片均已导入，可直接开始批量处理；本提示不阻断导入。"


@dataclass(frozen=True)
class GeometryIssue:
    """一张不符合「4:3 横版」要求的照片。"""

    filename: str
    width: int = 0
    height: int = 0
    orientation: int = 0
    # portrait_rotatable | portrait_ratio | landscape_ratio | square | unreadable
    kind: str = "unreadable"
    # cw | ccw | manual | ""（仅 portrait_rotatable 有值）
    rotation: str = ""

    @property
    def anomaly(self) -> str:
        size = f"{self.width}×{self.height}"
        return {
            "portrait_rotatable": f"竖版 {size}（旋转 90° 后为 4:3 横版）",
            "portrait_ratio": f"竖版 {size}，比例不是 4:3",
            "landscape_ratio": f"横版 {size}，比例不是 4:3",
            "square": f"正方形 {size}，比例不是 4:3",
            "unreadable": "无法读取照片尺寸",
        }.get(self.kind, "比例或方向不符合要求")

    def advice_for(self, auto_crop: bool = True) -> str:
        """给出该张的处理说明。auto_crop=是否已启用「居中裁边到 4:3」（默认启用）。

        横版比例不符时明确「自动裁边、无需人工」：既给出裁后尺寸与每侧像素数，
        也点明无需手工调整或单独处理，避免被理解成需要操作人员先自行改图。
        """
        if self.kind == "unreadable":
            return GEOMETRY_UNREADABLE_ADVICE
        if self.kind == "portrait_rotatable":
            return GEOMETRY_ROTATION_TEXT.get(self.rotation, GEOMETRY_ROTATION_TEXT["manual"])
        if self.kind == "landscape_ratio":
            if not auto_crop:
                return GEOMETRY_RATIO_ADVICE_NO_CROP
            tw = round(self.height * 4 / 3)
            if self.width > self.height and tw < self.width:
                side = (self.width - tw) // 2
                return (f"处理时自动居中裁边为 {tw}×{self.height}"
                        f"（左右各裁 {side} px），无需手工调整或单独处理")
            return GEOMETRY_RATIO_ADVICE
        return GEOMETRY_RATIO_KEEP_ADVICE

    @property
    def advice(self) -> str:
        return self.advice_for(True)

    def brief_for(self, auto_crop: bool = True) -> str:
        return f"{self.anomaly}；{self.advice_for(auto_crop)}"

    @property
    def brief(self) -> str:
        return self.brief_for(True)

    @property
    def rotation_reliable(self) -> bool:
        return self.rotation in ("cw", "ccw")

    def as_lines(self, index: int, auto_crop: bool = True) -> str:
        return f"{index:>2}. {self.filename}\n     {self.anomaly} → {self.advice_for(auto_crop)}"


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
    watermark_color: str = ""          # 自动取色的实际墨色（#RRGGBB）
    watermark_contrast: float = 0.0    # 字形区域与底层的实测平均亮度差（0–255）
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0
    verified: bool = False
    watermark_rect: tuple[int, int, int, int] | None = None
    # 无论成功失败都记录「本次尝试写出的路径」，供批次审计反向核对残留成品
    attempted_output: str = ""
    output_megapixels: float = 0.0
    report_copy_path: str = ""

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
            "输出像素(MP)": self.output_megapixels if self.ok else "",
            "报告版副本": Path(self.report_copy_path).name if self.report_copy_path else "",
            "拍摄时间": self.capture_time,
            "水印内容": self.watermark_text,
            "水印颜色(自动)": self.watermark_color,
            "水印对比度": round(self.watermark_contrast, 1) if self.watermark_color else "",
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


def has_exif_capture_time(path: str | Path) -> bool:
    """触发判断的唯一依据：照片的 EXIF 中是否存在可用的拍摄日期信息。

    只读 EXIF 的 DateTimeOriginal / DateTimeDigitized / DateTime 三个字段，
    不读取文件名、文件时间或 JPEG 其它数据段。
    """
    try:
        with Image.open(path) as im:
            dt, _tag, _note = read_exif_datetime_ex(im.getexif())
        return dt is not None
    except (OSError, ValueError, UnidentifiedImageError):
        return False


def precheck_exif(path: str | Path) -> str:
    """导入阶段轻量预检：仅依据 EXIF 判断，不解码整幅大图。

    含 EXIF 拍摄日期即返回空串（即使命中字段不是 DateTimeOriginal，也按原流程处理）；
    仅当 EXIF 中读不到任何拍摄日期时返回统一提示文案。
    """
    return "" if has_exif_capture_time(path) else EXIF_MISSING_REASON


def _is_4_3(ratio: float) -> bool:
    return abs(ratio - TARGET_ASPECT) <= TARGET_ASPECT * ASPECT_TOLERANCE


def check_geometry(path: str | Path) -> GeometryIssue | None:
    """导入阶段校验比例与方向；返回 None 表示符合「4:3 横版」要求。

    - 横版且宽高比 4:3（±1%）→ 合格
    - 竖版且高宽比 4:3 → 旋转 90° 即可满足；方向仅在 EXIF Orientation 为 6/8
      时可靠给出，否则标记为需人工确认（不指定方向）
    - 其余（比例不符的竖版 / 横版、正方形、无法读取）→ 横版由默认裁剪自动处理，
      竖版/正方形保持原样并提示；均不影响批量处理
    """
    p = Path(path)
    try:
        with Image.open(p) as im:
            w, h = int(im.size[0]), int(im.size[1])
            try:
                orientation = int(im.getexif().get(EXIF_TAG_ORIENTATION, 0) or 0)
            except (TypeError, ValueError, AttributeError):
                orientation = 0
    except (OSError, ValueError, UnidentifiedImageError):
        return GeometryIssue(p.name, kind="unreadable")
    if w <= 0 or h <= 0:
        return GeometryIssue(p.name, kind="unreadable")
    if w > h and _is_4_3(w / h):
        return None
    if h > w and _is_4_3(h / w):
        return GeometryIssue(p.name, w, h, orientation, "portrait_rotatable",
                             ORIENTATION_ROTATION.get(orientation, "manual"))
    kind = "portrait_ratio" if h > w else ("square" if w == h else "landscape_ratio")
    return GeometryIssue(p.name, w, h, orientation, kind, "")


def geometry_notice_text(issues: list[GeometryIssue], crop43_enabled: bool = True) -> str:
    """提示窗口正文的唯一文案来源（GUI、测试、命令行自检共用）。

    首句即点明「仍会照常批量处理、无需手工调整或单独处理」，避免被读成需要人工先改图。
    """
    head = (f"以下 {len(issues)} 张照片不是 4:3 横版，仍会照常批量处理，"
            "无需手工调整或单独处理。")
    lines = [head, GEOMETRY_TARGET_LABEL, ""]
    for i, issue in enumerate(issues, 1):
        lines.append(issue.as_lines(i, crop43_enabled))
    lines.extend(["", GEOMETRY_NONBLOCKING_TEXT])
    return "\n".join(lines)


def format_content(template: str, time_text: str, stem: str, seq: int,
                   extra: dict[str, str] | None = None) -> str:
    # {name}/{filename} 原文件名；{time} 时间文本；{seq} 四位序号(V1 示例 0001)
    # {ecu}/{serial} 由操作人员填写，程序不预填
    tokens = {"time": time_text, "filename": stem, "name": stem, "seq": f"{seq:04d}"}
    if extra:
        tokens.update(extra)
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
    naming_extra = {"ecu": cfg.ecu_name.strip(), "serial": cfg.serial_number.strip()}
    name = format_content(cfg.filename_template, safe_filename_part(time_text), stem,
                          cfg.start_index + seq - 1, naming_extra)
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


def plan_output_names(files: list[str] | list[Path], cfg: RuleConfig) -> list[Path]:
    """处理前预测每张成品的「原始目标路径」（只读，不写盘、不做同名去重）。

    仅用于「开始前统计同名文件并提示」：返回的是按命名模板算出的目标名，
    若该文件已存在，说明会命中同名策略。模板含 `{time}` 时才读取 EXIF 拍摄时间，
    读不到就留空——只影响预测精度，不影响实际处理。
    """
    out_dir = Path(cfg.output_dir)
    naming_extra = {"ecu": cfg.ecu_name.strip(), "serial": cfg.serial_number.strip()}
    planned: list[Path] = []
    for i, f in enumerate(files, 1):
        src = Path(f)
        if cfg.output_format == "PNG":
            ext = ".png"
        elif cfg.output_format == "同原格式" and src.suffix.lower() == ".png":
            ext = ".png"
        else:
            ext = ".jpg"
        time_text = ""
        if "{time}" in cfg.filename_template:
            try:
                with Image.open(src) as im:
                    dt, _tag, _note = read_exif_datetime_ex(im.getexif())
                if dt is not None:
                    time_text = dt.strftime(cfg.time_format)
            except (OSError, ValueError, UnidentifiedImageError):
                time_text = ""
        name = format_content(cfg.filename_template, safe_filename_part(time_text), src.stem,
                              cfg.start_index + i - 1, naming_extra)
        name = (safe_filename_part(name) or src.stem)[:MAX_FILENAME_CHARS]
        planned.append(out_dir / f"{name}{ext}")
    return planned


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


def _crop_to_4_3(img: Image.Image, notes: list[str] | None = None) -> Image.Image:
    """居中裁边到 4:3 横版：不缩放、不重采样、不补边。

    横版按长边裁到 4:3（通常裁左右两侧）；竖版/正方形无法仅靠裁边得到 4:3 横版，
    为避免切掉主体，保持原样并给出警告，由操作人员旋转或更换源图。
    """
    w, h = img.size
    if w <= h:
        if notes is not None:
            notes.append("该张为竖版/正方形，无法仅靠裁边得到 4:3 横版；"
                         "已保持原样，请旋转或更换源图后重跑")
        return img
    tw = round(h * 4 / 3)
    if tw == w:
        return img
    if tw < w:
        left = (w - tw) // 2
        right = w - tw - left
        if notes is not None:
            side = f"左右各裁 {left} px" if left == right else f"左裁 {left} px、右裁 {right} px"
            notes.append(f"已居中裁边至 4:3：{w}×{h} → {tw}×{h}"
                         f"（{side}，合计占宽度 {(w - tw) / w * 100:.2f}%）")
        return img.crop((left, 0, left + tw, h))
    th = round(w * 3 / 4)
    top = (h - th) // 2
    bottom = h - th - top
    if notes is not None:
        side = f"上下各裁 {top} px" if top == bottom else f"上裁 {top} px、下裁 {bottom} px"
        notes.append(f"已居中裁边至 4:3：{w}×{h} → {w}×{th}"
                     f"（{side}，合计占高度 {(h - th) / h * 100:.2f}%）")
    return img.crop((0, top, w, top + th))


def _stage_resize(img: Image.Image, cfg: RuleConfig,
                  notes: list[str] | None = None) -> Image.Image:
    """尺寸调整：crop43（默认）居中裁边到 4:3；其余模式按目标尺寸缩放。"""
    if img.mode != "RGB":
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, hex_to_rgb(cfg.background) + (255,))
        img = Image.alpha_composite(bg, rgba).convert("RGB")
    if cfg.resize_mode == "crop43":
        return _crop_to_4_3(img, notes)
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


def _median_luma(img: Image.Image) -> int:
    """区域背景亮度的中位数（0–255）。用灰度直方图统计，不做逐像素 Python 循环。"""
    hist = img.convert("L").histogram()
    total = sum(hist)
    if not total:
        return 128
    half = total / 2.0
    acc = 0
    for value, count in enumerate(hist):
        acc += count
        if acc >= half:
            return value
    return 255


def _wcag_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(v: int) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG 对比度（1–21）。用于在深墨/浅墨之间做选择。"""
    la, lb = _wcag_luminance(a), _wcag_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def pick_watermark_ink(base: Image.Image, rect: tuple[int, int, int, int]) -> tuple[int, int, int]:
    """按水印区域背景的中位亮度，在深墨/浅墨之间选对比度更高的一种。

    不使用固定白色：浅色背景取深墨、深色背景取浅墨；背景色与水印色接近
    （同色背景）时会自动切到差异更大的一侧。
    """
    med = _median_luma(base.crop(rect))
    bg = (med, med, med)
    return (WM_INK_DARK
            if contrast_ratio(WM_INK_DARK, bg) >= contrast_ratio(WM_INK_LIGHT, bg)
            else WM_INK_LIGHT)


def _compose_watermark(
    base: Image.Image, cfg: RuleConfig, text: str, font: ImageFont.FreeTypeFont,
    x: int, y: int, tw: int, th: int,
    ink: tuple[int, int, int], shadow_rgb: tuple[int, int, int],
) -> tuple[Image.Image, tuple[int, int, int, int], float]:
    """按指定墨色/阴影色渲染并合成，返回 (输出图, 文字矩形, 字形区域实测亮度差)。"""
    W, H = base.size
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
        ImageDraw.Draw(shadow).text(
            (lx + cfg.shadow_offset_x, ly + cfg.shadow_offset_y), text, font=font,
            fill=shadow_rgb + (round(255 * cfg.shadow_opacity / 100),), anchor="lt")
        if cfg.shadow_blur > 0:
            shadow = shadow.filter(ImageFilter.GaussianBlur(cfg.shadow_blur))
        layer = Image.alpha_composite(layer, shadow)

    draw = ImageDraw.Draw(layer)
    kwargs: dict[str, Any] = {}
    if cfg.bold:
        kwargs = {"stroke_width": max(1, font.size // 40),
                  "stroke_fill": ink + (round(255 * cfg.font_opacity / 100),)}
    draw.text((lx, ly), text, font=font,
              fill=ink + (round(255 * cfg.font_opacity / 100),), anchor="lt", **kwargs)
    if layer.getbbox() is None:
        raise RuntimeError("水印图层为空，渲染失败")

    out = base.copy()
    region = Image.alpha_composite(out.crop((px0, py0, px1, py1)).convert("RGBA"), layer)
    out.paste(region.convert("RGB"), (px0, py0))

    # 文字蒙版（仅字形、不含阴影）：只统计「字压住的那部分背景」的亮度差，
    # 避免把大片空白区域计入而高估可读性。
    mask = Image.new("L", (lw, lh), 0)
    ImageDraw.Draw(mask).text((lx, ly), text, font=font, fill=255, anchor="lt")
    before = base.crop((px0, py0, px1, py1)).convert("L")
    after = out.crop((px0, py0, px1, py1)).convert("L")
    delta = ImageChops.difference(before, after)
    contrast = float(ImageStat.Stat(delta, mask=mask).mean[0])

    rect = (x, y, min(W, x + tw), min(H, y + th))
    return out, rect, contrast


def _stage_watermark(
    base: Image.Image, cfg: RuleConfig, text: str
) -> tuple[Image.Image, tuple[int, int, int, int] | None, list[str], str, float]:
    """时间水印渲染：位置 / 字号 / 自动取色 / 不透明度 / 阴影。

    位置与排版固定（右下、固定边距、单行）；字号为系统固定的 210 px（原 70 px 的 3 倍），
    颜色按每张照片水印区域的背景自动在深墨/浅墨之间选择，不使用固定白色，
    浅色或与文字同色的背景也能保持清晰可读。
    """
    warnings: list[str] = []
    if not cfg.watermark_enabled or not text:
        return base, None, warnings, "", 0.0
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

    auto_ink = cfg.font_color.strip().lower() == "auto"
    ink = pick_watermark_ink(base, rect) if auto_ink else hex_to_rgb(cfg.font_color)
    if cfg.shadow_color.strip().lower() == "auto":
        shadow_rgb = WM_INK_LIGHT if ink == WM_INK_DARK else WM_INK_DARK
    else:
        shadow_rgb = hex_to_rgb(cfg.shadow_color)

    out, rect, contrast = _compose_watermark(base, cfg, text, font, x, y, tw, th, ink, shadow_rgb)
    # 自动取色的兜底：若按中位亮度选出的墨色在实际字形区域上对比度不足
    # （例如文字横跨明暗交界），换另一侧墨色重算，取实测对比度更高的结果。
    if auto_ink and contrast < WM_MIN_INK_CONTRAST:
        alt = WM_INK_LIGHT if ink == WM_INK_DARK else WM_INK_DARK
        alt_shadow = WM_INK_DARK if alt == WM_INK_LIGHT else WM_INK_LIGHT
        alt_out, rect, alt_contrast = _compose_watermark(
            base, cfg, text, font, x, y, tw, th, alt, alt_shadow)
        if alt_contrast > contrast:
            out, contrast, ink = alt_out, alt_contrast, alt
    if contrast < WM_MIN_INK_CONTRAST:
        warnings.append(f"水印与背景对比度偏低（平均亮度差 {contrast:.0f}），请人工确认该张可读性")

    # 渲染有效性自检：水印区域必须发生「足量、肉眼可辨」的变化。
    # 只判 getbbox() 会被「文字全透明但有阴影」「1 像素差异」等配置绕过。
    diff = ImageChops.difference(base.crop(rect), out.crop(rect)).convert("L")
    changed = sum(1 for px in diff.getdata() if px > 8)
    need = max(MIN_VISIBLE_WM_PIXELS, int(diff.width * diff.height * 0.01))
    if changed < need:
        raise RuntimeError(
            f"水印不可见或过淡（仅 {changed} 个像素发生可见变化，需要 ≥{need}）；"
            "请检查字体不透明度、字体色与水印内容")
    ink_hex = "#{:02X}{:02X}{:02X}".format(*ink)
    return out, rect, warnings, ink_hex, contrast


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
    # 时间水印由系统统一生成：入口处强制回写固定规格，
    # 保证任何调用方（界面、规则文件、脚本）都无法改变水印内容、格式或外观。
    cfg.apply_fixed_watermark()
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
                        return abort(STAGE_TIME, EXIF_MISSING_REASON)
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
            r.watermark_text = format_content(
                cfg.content_template, time_text, src.stem, cfg.start_index + seq - 1,
                {"ecu": cfg.ecu_name.strip(), "serial": cfg.serial_number.strip()})
            if not r.watermark_text:
                return abort(STAGE_TIME, "水印内容为空（请检查内容模板与时间格式）")
            if cfg.time_source != "fixed" and time_text and time_text not in r.watermark_text:
                return abort(STAGE_TIME,
                             "水印内容模板未包含 {time}，成品不含时间戳；请在模板中加入 {time}")

        # --- 阶段 3：尺寸与像素规格调整 ---
        try:
            image = _stage_resize(oriented, cfg, r.warnings)
        except (ValueError, MemoryError, OSError) as exc:
            return abort(STAGE_RESIZE, f"尺寸调整失败：{exc}")
        del oriented

        # --- 阶段 4：时间水印渲染（文档 3.5 / 3.7 / 3.8） ---
        if cfg.watermark_enabled:
            try:
                image, rect, wm_warn, ink_hex, wm_contrast = _stage_watermark(
                    image, cfg, r.watermark_text)
                r.watermark_rect = rect
                r.watermark_color = ink_hex
                r.watermark_contrast = wm_contrast
                r.warnings.extend(wm_warn)
            except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
                return abort(STAGE_WATERMARK, f"水印渲染失败：{exc}")

        # --- 阶段 5/6：先写临时文件 → 校验临时文件 → 原子替换到目标路径 ---
        # 这样：① 校验失败不会破坏运行前已存在的同名旧成品；
        #      ② 任何失败都不会在输出目录留下「判失败但已产出」的残留文件。
        fmt, ext = resolve_output_format(cfg, src)
        expected_size = image.size
        # 客户硬规定：主交付成品必须 ≥2100 万像素（V1 p.3 / V2 §8·§10）。
        # 在写盘前拦截，避免整批产出不合规文件。
        out_mp = expected_size[0] * expected_size[1] / 1_000_000
        if cfg.min_output_megapixels > 0 and out_mp < cfg.min_output_megapixels:
            return abort(
                STAGE_VERIFY,
                f"主输出像素数 {out_mp:.2f} MP（{expected_size[0]}×{expected_size[1]}）低于客户要求 "
                f"{cfg.min_output_megapixels:g} MP；请改用「不缩放」保持原分辨率，或增大目标尺寸")
        r.output_megapixels = round(out_mp, 2)
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

        if cfg.report_copy_enabled:
            copy_path, copy_err = _export_report_copy(
                image, cfg, out_path, fmt, raw_exif, icc_profile, seq, used_names, prot)
            if copy_path:
                r.report_copy_path = copy_path
                r.warnings.append("已生成报告插图副本；该副本不满足 ≥2100 万像素，不可替代主交付原件")
            else:
                r.warnings.append(copy_err)

        downgrade = ("缺失 EXIF", "非 EXIF", "未执行输出回读校验", "未写入 EXIF",
                     "自动缩小", "回退到", "不足", "报告版导出失败", "报告版校验失败",
                     "无法仅靠裁边", "对比度偏低")
        r.status = STATUS_WARNING if any(
            any(k in w for k in downgrade) for w in r.warnings) else STATUS_SUCCESS
        return r

    except Exception as exc:  # 兜底：任何未预料异常都必须落到逐张状态里
        return abort(r.stage or "未知阶段", f"未预料异常：{type(exc).__name__}: {exc}")
    finally:
        _cleanup(tmp_path)
        r.duration_ms = int((time.perf_counter() - started) * 1000)


def _export_report_copy(image: Image.Image, cfg: RuleConfig, out_path: Path,
                        fmt: str, raw_exif: bytes, icc_profile: bytes | None,
                        seq: int, used: set[str], protected: set[str] | None) -> tuple[str, str]:
    """可选：另出一套用于报告插图的轻量副本。

    该副本**不满足 ≥2100 万像素**，不可替代主交付原件；失败只记警告、不影响主输出。
    水印沿用主图（随缩放等比缩小），时间、字体、位置与主输出完全一致。
    """
    tw, th = cfg.target_width, cfg.target_height
    small = ImageOps.contain(image, (tw, th), method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (tw, th), hex_to_rgb(cfg.background))
    canvas.paste(small, ((tw - small.width) // 2, (th - small.height) // 2))
    suffix = cfg.report_copy_suffix.strip() or "_报告版"
    rp = out_path.with_name(out_path.stem + suffix + out_path.suffix)
    prot = protected or set()
    try:
        if str(rp.resolve()).lower() in prot or str(rp).lower() in used:
            rp = _unique_path(rp, used, prot)
        rp.parent.mkdir(parents=True, exist_ok=True)
        tmp = rp.with_name(f"._pw_report_{os.getpid()}_{seq}_{int(time.time() * 1000)}.tmp")
        try:
            _save_image(canvas, tmp, fmt, cfg, raw_exif, icc_profile)
            ok, why, _, _ = _verify_output(tmp, (tw, th), fmt, cfg)
            if not ok:
                return "", f"报告版校验失败：{why}"
            os.replace(str(tmp), str(rp))
            tmp = None  # type: ignore[assignment]
            used.add(str(rp).lower())
            return str(rp), ""
        finally:
            _cleanup(tmp)  # type: ignore[arg-type]
    except (OSError, ValueError, TypeError) as exc:
        return "", f"报告版导出失败：{exc}"


def _cleanup(path: Path | None) -> None:
    """失败时清理残留输出，保证输出目录内只保留合格成品。"""
    if path is None:
        return
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
