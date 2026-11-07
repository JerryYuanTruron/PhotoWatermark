"""统一规则配置。

所有可调整规则集中在本模块的 RuleConfig，界面、引擎、报告、测试共用同一份定义，
避免同一参数在多处出现不同默认值（高内聚、单一事实来源）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 常量与可选值
# --------------------------------------------------------------------------

SUPPORTED_INPUT_EXTS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp",
}

RESIZE_MODES = {
    "contain": "适应留白（不裁切、不变形）",
    "cover": "填充裁切（铺满、裁掉边缘）",
    "stretch": "拉伸至目标尺寸（可能变形）",
    "none": "不缩放（保持原尺寸）",
}

OUTPUT_FORMATS = {
    "同原格式": "同原格式（JPG→JPEG / PNG→PNG；TIF/BMP/WEBP→JPEG）",
    "JPEG": "JPEG",
    "PNG": "PNG",
}

TIME_SOURCES = {
    "exif": "EXIF 拍摄日期（文档 3.6）",
    "file_mtime": "文件修改时间",
    "fixed": "固定文本（不读取照片时间）",
}

POSITIONS = {
    "top_left": "左上", "top_center": "上中", "top_right": "右上",
    "middle_left": "左中", "center": "正中", "middle_right": "右中",
    "bottom_left": "左下", "bottom_center": "下中", "bottom_right": "右下",
}

EXIF_POLICIES = {
    "fail": "判定失败并列入失败清单（推荐）",
    "use_file_mtime": "回退到文件修改时间（标记警告）",
    "no_watermark": "跳过水印并判失败（成品无时间戳，不推荐）",
}

ON_EXISTS = {
    "overwrite": "覆盖同名文件",
    "skip": "跳过并标记警告",
    "rename": "自动加序号另存",
}

# 文档 3.7「年/月/日 时:分:秒」对应文档界面示例 [EXIF:Date Taken [Y/m/d H:M:S]]
TIME_FORMAT_PRESETS: list[tuple[str, str]] = [
    ("%Y/%m/%d %H:%M:%S", "年/月/日 时:分:秒（文档 3.7，默认）"),
    ("%Y-%m-%d %H:%M:%S", "年-月-日 时:分:秒"),
    ("%Y/%m/%d %H:%M", "年/月/日 时:分"),
    ("%Y年%m月%d日 %H:%M:%S", "中文年月日 时分秒"),
    ("%d.%m.%Y %H:%M:%S", "日.月.年 时:分:秒"),
]

# V1 p.3 / V2 §11 命名规范：<ECU name>_<serial number>_<sequential number>
# {seq} 为 4 位序号（V1 示例 0001）；{ecu}/{serial} 由操作人员在界面填写，程序不预填，
# 避免把 OCR 误差或猜测值写进客户交付文件名。
FILENAME_TEMPLATE_PRESETS: list[tuple[str, str]] = [
    ("{ecu}_{serial}_{seq}", "{ecu}_{serial}_{seq}（客户合规命名，默认）"),
    ("{name}_结果", "{name}_结果（V2 步骤 3.9 界面示例，仅归档用）"),
    ("{name}", "{name}（保持原名，不追加后缀）"),
    ("{seq}_{name}", "{seq}_{name}（序号 + 原名）"),
]

# 文档 3.8：字体「微软雅黑」，尺寸 70；阴影「启用」
FONT_FAMILY_CANDIDATES: list[tuple[str, str]] = [
    ("微软雅黑", "msyh.ttc"),
    ("微软雅黑 粗体", "msyhbd.ttc"),
    ("微软雅黑 Light", "msyhl.ttc"),
    ("黑体", "simhei.ttf"),
    ("宋体", "simsun.ttc"),
    ("Arial", "arial.ttf"),
    ("Arial Bold", "arialbd.ttf"),
    ("Calibri", "calibri.ttf"),
    ("Consolas", "consola.ttf"),
]

# --------------------------------------------------------------------------
# 配置数据类
# --------------------------------------------------------------------------


@dataclass
class RuleConfig:
    """一次批量处理的全部规则。字段顺序即界面分组顺序。"""

    # --- ① 尺寸与像素规格 ---
    # 客户规范 V1 p.3「est. 21 MP」/ V2 §8·§10「≥2100 万像素」是评价检查项，
    # 交付成品必须满足。故默认「不缩放」以保持原分辨率（真实照片实测 50.3 MP）。
    target_width: int = 5292                 # 仅在自定义缩放 / 报告版副本时使用
    target_height: int = 3969                # 4:3，约 2100 万像素
    resize_mode: str = "none"
    background: str = "#FFFFFF"
    dpi: int = 300
    output_format: str = "同原格式"
    jpeg_quality: int = 92
    min_output_megapixels: int = 21          # 主输出像素数下限(MP)，低于即判失败
    report_copy_enabled: bool = False        # 可选：另出一套报告插图轻量副本
    report_copy_suffix: str = "_报告版"

    # --- ② 时间水印 ---
    watermark_enabled: bool = True
    time_source: str = "exif"                 # 3.6 读取 EXIF 拍摄日期
    fixed_time_text: str = ""
    time_format: str = "%Y/%m/%d %H:%M:%S"   # 3.7 年/月/日 时:分:秒
    content_template: str = "{time}"          # 支持 {time} {filename} {seq} 及任意文字
    position: str = "bottom_right"            # 3.7 位置「右下」
    margin_x: int = 24                        # 对应 X 留白（文档为 0，留出安全边距更利于贴报告）
    margin_y: int = 20
    font_family: str = "微软雅黑"              # 3.8 字体
    font_path: str = ""                       # 空 = 按 font_family 自动解析
    font_size: int = 70                       # 3.8 尺寸 70
    font_size_unit: str = "px"                # px=输出图像素；pt=按 DPI 换算
    font_color: str = "#FFFFFF"
    font_opacity: int = 100
    bold: bool = False
    shadow_enabled: bool = True               # 3.8 阴影「启用」
    shadow_offset_x: int = 2
    shadow_offset_y: int = 2
    shadow_blur: int = 2
    shadow_color: str = "#000000"
    shadow_opacity: int = 70

    # --- ③ 输出与合规命名 ---
    output_dir: str = ""
    filename_template: str = "{ecu}_{serial}_{seq}"   # V1 p.3 / V2 §11 合规命名
    ecu_name: str = ""                       # 由操作人员填写，程序不预填
    serial_number: str = ""                  # 由操作人员填写，程序不预填
    on_exists: str = "overwrite"
    start_index: int = 1
    missing_exif_policy: str = "fail"        # 严格口径：缺 EXIF 即失败，要求补原图
    keep_metadata: bool = True                # 3.9 输出页签「保持元数据」

    # --- ④ 校验 ---
    verify_output: bool = True
    min_output_bytes: int = 256
    max_source_pixels: int = 300_000_000      # 解压炸弹保护
    # 缺少 EXIF 的提示窗口为无条件行为：触发判断只读取照片 EXIF，
    # 不受任何配置项、文件名、文件时间或其它数据来源影响。

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["_schema_version"] = 1
        d["_app_version"] = _app_version()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RuleConfig":
        """兼容旧版/残缺规则文件：未知键忽略，缺失键用默认值，类型做安全转换。"""
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        valid = {f.name: f for f in fields(cls)}
        for key, value in data.items():
            if key.startswith("_") or key not in valid or value is None:
                continue
            expected = cls.__annotations__.get(key)
            try:
                if expected == "int" or expected is int:
                    value = int(value)
                elif expected == "float" or expected is float:
                    value = float(value)
                elif expected == "bool" or expected is bool:
                    value = bool(value) if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
                elif expected == "str" or expected is str:
                    value = str(value)
            except (TypeError, ValueError):
                continue
            setattr(cfg, key, value)
        return cfg

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "RuleConfig":
        p = Path(path)
        if not p.is_file():
            return cls()
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"规则文件无法读取或格式非法：{p}（{exc}）") from exc

    # ------------------------------------------------------------------
    # 合法性校验（返回错误列表，空列表表示通过）
    # ------------------------------------------------------------------
    def validate(self) -> list[str]:
        e: list[str] = []
        if self.resize_mode not in RESIZE_MODES:
            e.append(f"缩放方式非法：{self.resize_mode}")
        if self.resize_mode != "none":
            if not 16 <= self.target_width <= 20000:
                e.append("目标宽度须在 16–20000 px 之间")
            if not 16 <= self.target_height <= 20000:
                e.append("目标高度须在 16–20000 px 之间")
            # 客户硬规定：主输出必须 ≥ min_output_megapixels 百万像素。
            # 自定义缩放时提前拦截，避免整批产出不合规成品后才发现。
            mp = self.target_width * self.target_height / 1_000_000
            if mp < self.min_output_megapixels:
                e.append(f"目标尺寸 {self.target_width}×{self.target_height} = {mp:.2f} MP，"
                         f"低于客户要求 {self.min_output_megapixels:g} MP（≥2100 万像素）；"
                         "请改用「不缩放」或增大目标尺寸")
        if not 0 <= self.min_output_megapixels <= 500:
            e.append("输出像素下限须在 0–500 MP 之间")
        if re.search(r'[\\/:*?"<>|]', self.report_copy_suffix):
            e.append("报告版后缀含非法字符")
        if not 1 <= self.dpi <= 2400:
            e.append("像素规格 DPI 须在 1–2400 之间")
        if self.output_format not in OUTPUT_FORMATS:
            e.append(f"输出格式非法：{self.output_format}")
        if not 1 <= self.jpeg_quality <= 100:
            e.append("JPEG 质量须在 1–100 之间")
        if not _valid_hex_color(self.background):
            e.append("背景色须为 #RRGGBB 格式")

        if self.watermark_enabled:
            if self.time_source not in TIME_SOURCES:
                e.append(f"时间来源非法：{self.time_source}")
            if self.time_source == "fixed" and not self.fixed_time_text.strip():
                e.append("时间来源为「固定文本」时，水印内容不能为空")
            if not self.time_format.strip():
                e.append("时间格式不能为空")
            else:
                try:
                    datetime.now().strftime(self.time_format)
                except (ValueError, TypeError) as exc:
                    e.append(f"时间格式非法：{exc}")
                # Windows 的 strftime 会把未知 % 指令原样输出，导致水印显示成 "%Q" 之类乱码。
                # 显式拦截不支持的 ASCII 字母指令，避免错误配置静默产出不可用水印。
                allowed = set("aAbBcdHIjmMpSUwWxXyYZfGuVeD%")
                unknown = [c for c in re.findall(r"%(.)", self.time_format)
                           if c.isascii() and c.isalpha() and c not in allowed]
                if unknown:
                    e.append(f"时间格式含不支持的指令：%{unknown[0]}（请检查拼写）")
            if not self.content_template.strip():
                e.append("水印内容模板不能为空")
            elif self.time_source != "fixed" and "{time}" not in self.content_template:
                e.append("时间来源非「固定文本」时，水印内容模板必须包含 {time}，否则成品不含时间戳")
            if self.position not in POSITIONS:
                e.append(f"水印位置非法：{self.position}")
            if not _valid_hex_color(self.font_color) or not _valid_hex_color(self.shadow_color):
                e.append("字体色/阴影色须为 #RRGGBB 格式")
            if not 4 <= self.font_size <= 2000:
                e.append("字号须在 4–2000 之间")
            if self.font_size_unit not in ("px", "pt"):
                e.append("字号单位须为 px 或 pt")
            if not 0 <= self.font_opacity <= 100 or not 0 <= self.shadow_opacity <= 100:
                e.append("不透明度须在 0–100 之间")
            if self.font_opacity < 1:
                e.append("字体不透明度不能为 0：时间水印必须肉眼可见（文档「每张照片须包含可见时间戳」）")
            if self.font_opacity < 1 and self.shadow_opacity < 1:
                e.append("字体与阴影都不透明时水印不可见")
            if self.margin_x < 0 or self.margin_y < 0:
                e.append("边距不能为负")
            if self.shadow_blur < 0:
                e.append("阴影模糊不能为负")
            if self.font_path.strip() and not Path(self.font_path).is_file():
                e.append(f"自定义字体文件不存在：{self.font_path}")
            if not resolve_font(self.font_family, self.font_path):
                e.append(f"本机无可用字体：{self.font_family or self.font_path}（请安装字体或指定字体文件）")

        if not self.output_dir.strip():
            e.append("请指定输出目录")
        elif not _path_writable(self.output_dir):
            e.append(f"输出目录不存在且无法创建：{self.output_dir}")
        if not self.filename_template.strip():
            e.append("文件名模板不能为空")
        elif re.search(r'[\\/:*?"<>|]', self.filename_template):
            e.append("文件名模板不能包含 \\ / : * ? \" < > | 等非法字符")
        # 合规命名：模板引用 {ecu}/{serial} 时，必须由操作人员填写，程序不得预填或猜测
        needs_naming = ("{ecu}" in self.filename_template
                        or "{serial}" in self.filename_template)
        if needs_naming:
            for key, label in (("ecu_name", "ECU 名称"), ("serial_number", "序列号")):
                value = str(getattr(self, key, "")).strip()
                if not value:
                    e.append(f"请填写{label}（合规命名 {self.filename_template} 需要，程序不预填）")
                elif re.search(r'[\\/:*?"<>|\s]', value):
                    e.append(f"{label}含空格或非法字符：{value}")
        if self.on_exists not in ON_EXISTS:
            e.append(f"同名策略非法：{self.on_exists}")
        if self.missing_exif_policy not in EXIF_POLICIES:
            e.append(f"EXIF 缺失策略非法：{self.missing_exif_policy}")
        if self.start_index < 1:
            e.append("起始序号须 ≥ 1")
        if self.min_output_bytes < 0:
            e.append("校验阈值不能为负")
        if self.max_source_pixels < 1_000_000:
            e.append("源图像素安全上限过小")
        return e

    def effective_font_px(self) -> int:
        """把配置字号换算成输出图像素。pt 按 DPI / 72 换算（XnConvert「单位：点」语义）。"""
        if self.font_size_unit == "pt":
            return max(1, round(self.font_size * self.dpi / 72))
        return max(1, self.font_size)


def _valid_hex_color(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", value))


def _path_writable(value: str) -> bool:
    """真实写探针：只 mkdir 无法发现「目录存在但只读」的情况，会导致整批在导出阶段才失败。"""
    try:
        p = Path(value)
        p.mkdir(parents=True, exist_ok=True)
        if not p.is_dir():
            return False
        probe = p / f"._pw_probe_{os.getpid()}_{os.urandom(4).hex()}.tmp"
        try:
            probe.write_bytes(b"ok")
        finally:
            # Windows 安全软件可能短暂占用刚写入的文件；重试删除，避免输出目录留垃圾
            for _ in range(6):
                try:
                    probe.unlink()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.03)
        return True
    except OSError:
        return False


def _app_version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        pass
    try:
        return (app_base_dir() / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "1.0.0"


# --------------------------------------------------------------------------
# 字体解析
# --------------------------------------------------------------------------


def fonts_dir() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def list_system_fonts() -> list[tuple[str, str]]:
    """返回 [(显示名, 绝对路径)]，仅列出本机存在的字体。"""
    result: list[tuple[str, str]] = []
    d = fonts_dir()
    for label, filename in FONT_FAMILY_CANDIDATES:
        p = d / filename
        if p.is_file():
            result.append((label, str(p)))
    if not result:  # 非 Windows / 字体目录异常时的兜底
        for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",):
            if Path(candidate).is_file():
                result.append(("DejaVu Sans", candidate))
    return result


def resolve_font(font_family: str, explicit_path: str = "") -> str:
    """返回可用字体文件绝对路径；找不到返回空串。"""
    if explicit_path and Path(explicit_path).is_file():
        return str(Path(explicit_path))
    for label, path in list_system_fonts():
        if label == font_family:
            return path
    # 名称未命中时，用默认第一项兜底，避免整批因字体名笔误而全部失败
    available = list_system_fonts()
    return available[0][1] if available else ""


# --------------------------------------------------------------------------
# 预置方案
# --------------------------------------------------------------------------

PRESETS: dict[str, dict[str, Any]] = {
    "客户合规预置 · 保持原分辨率 + 合规命名（默认）": {
        # 与 RuleConfig() 默认值逐项一致，避免「启动默认」与「恢复默认」行为不同
        "resize_mode": "none", "dpi": 300, "output_format": "同原格式",
        "jpeg_quality": 92, "font_size": 70, "font_size_unit": "px",
        "position": "bottom_right", "time_format": "%Y/%m/%d %H:%M:%S",
        "filename_template": "{ecu}_{serial}_{seq}",
        "min_output_megapixels": 21, "report_copy_enabled": False,
        "missing_exif_policy": "fail",
    },
    "合规 + 报告插图副本（原件 + 2400×1800）": {
        "resize_mode": "none", "target_width": 2400, "target_height": 1800,
        "dpi": 300, "output_format": "同原格式", "jpeg_quality": 95,
        "font_size": 160, "font_size_unit": "px", "report_copy_enabled": True,
        "report_copy_suffix": "_报告版", "filename_template": "{ecu}_{serial}_{seq}",
        "min_output_megapixels": 21,
    },
    "统一重采样至 5292×3969（约 2100 万像素，4:3）": {
        "target_width": 5292, "target_height": 3969, "resize_mode": "contain",
        "dpi": 300, "output_format": "JPEG", "jpeg_quality": 92,
        "font_size": 150, "font_size_unit": "px",
        "filename_template": "{ecu}_{serial}_{seq}",
        "min_output_megapixels": 21,
    },
    "V2 步骤 3.9 归档口径（{name}_结果，不做像素合规）": {
        "resize_mode": "none", "dpi": 300, "output_format": "同原格式",
        "jpeg_quality": 95, "font_size": 160,
        "filename_template": "{name}_结果", "min_output_megapixels": 0,
    },
}

DEFAULT_PRESET = next(iter(PRESETS))


def preset_config(name: str) -> RuleConfig:
    cfg = RuleConfig()
    for k, v in PRESETS.get(name, {}).items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg


# --------------------------------------------------------------------------
# 路径与规则持久化
# --------------------------------------------------------------------------


def app_base_dir() -> Path:
    """冻结(exe)时取 exe 所在目录；源码运行时取项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def rules_dir() -> Path:
    p = app_base_dir() / "rules"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        p = Path.home() / ".photo_watermark"
        p.mkdir(parents=True, exist_ok=True)
    return p


def last_used_path() -> Path:
    return rules_dir() / "last_used.json"


def load_last_used() -> RuleConfig:
    try:
        return RuleConfig.load(last_used_path())
    except ValueError:
        return RuleConfig()


def save_last_used(cfg: RuleConfig) -> None:
    try:
        cfg.save(last_used_path())
    except OSError:
        pass
