"""生成自动化测试样本照片（含 EXIF 拍摄日期、无 EXIF、损坏、非图片、竖图、旋转标记等）。

用法：python tests/make_samples.py [输出目录]
默认输出到 <项目>/_test_samples，该目录已被 .gitignore 忽略。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _picture(w: int, h: int, seed: int) -> Image.Image:
    """生成有纹理的测试图，便于肉眼确认缩放与水印效果。"""
    im = Image.new("RGB", (w, h), (58 + seed * 7 % 120, 92, 138))
    d = ImageDraw.Draw(im)
    step = max(12, w // 24)
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(210, 210, 215), width=1)
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(210, 210, 215), width=1)
    d.rectangle([w // 5, h // 5, w * 4 // 5, h * 4 // 5], outline=(240, 200, 40), width=max(2, w // 200))
    d.ellipse([w // 3, h // 3, w * 2 // 3, h * 2 // 3], fill=(190, 70, 60))
    d.text((w // 10, h // 10), f"SAMPLE {seed}  {w}x{h}", fill=(255, 255, 255))
    return im


def _exif_bytes(original: str | None, digitized: str | None = None,
                main: str | None = None, orientation: int | None = None) -> bytes:
    ex = Image.Exif()
    if main:
        ex[306] = main
    if orientation is not None:
        ex[0x0112] = orientation
    if original or digitized:
        sub = ex.get_ifd(0x8769)
        if original:
            sub[36867] = original
        if digitized:
            sub[36868] = digitized
    return ex.tobytes()


def build(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []

    def save(name: str, im: Image.Image, **kw) -> Path:
        p = out / name
        im.save(p, **kw)
        made.append(p)
        return p

    # 1 正常：EXIF DateTimeOriginal（文档 3.6 优先字段）
    save("PeCu_2718281828_0001.jpg", _picture(1200, 900, 1),
         quality=95, exif=_exif_bytes("2026:05:22 16:29:01", main="2020:01:01 00:00:00"))
    # 2 仅主 IFD DateTime（回退字段）
    save("PeCu_2718281828_0002.jpg", _picture(1600, 1200, 2),
         quality=95, exif=_exif_bytes(None, main="2026:05:23 09:05:07"))
    # 3 PNG 输入（同原格式→PNG 输出）
    save("PeCu_2718281828_0003.png", _picture(1000, 750, 3),
         exif=_exif_bytes("2026:05:24 11:12:13", main="2026:05:24 11:12:13"))
    # 4 完全无 EXIF（默认策略应判失败）
    save("NOEXIF_0004.jpg", _picture(800, 600, 4), quality=95)
    # 5 损坏文件
    p = out / "BROKEN_0005.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"garbage" * 40)
    made.append(p)
    # 6 非图片
    p = out / "NOTIMAGE_0006.txt"
    p.write_text("this is not a photo", encoding="utf-8")
    made.append(p)
    # 7 竖图（测试 contain 留白）
    save("PORTRAIT_0007.jpg", _picture(900, 1600, 7),
         quality=95, exif=_exif_bytes("2026:05:25 08:00:00"))
    # 8 带 EXIF 方向标记 orientation=6（测试 exif_transpose）
    save("ORIENT_0008.jpg", _picture(1200, 900, 8),
         quality=95, exif=_exif_bytes("2026:05:26 14:30:59", main="2026:05:26 14:30:59", orientation=6))
    # 9 极小图（测试字号自动缩小与水印区域边界）
    save("TINY_0009.jpg", _picture(120, 90, 9),
         quality=95, exif=_exif_bytes("2026:05:27 07:07:07"))
    # 10 同名不同目录（测试输出文件名冲突去重）
    sub = out / "sub"
    sub.mkdir(exist_ok=True)
    p = sub / "PeCu_2718281828_0001.jpg"
    _picture(640, 480, 10).save(p, quality=95, exif=_exif_bytes("2026:05:28 18:00:00"))
    made.append(p)
    return made


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_test_samples"
    files = build(target)
    print(f"已生成 {len(files)} 个测试样本 -> {target}")
    for f in files:
        print("  ", f.relative_to(target))
