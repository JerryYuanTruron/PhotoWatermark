"""批量级校验、失败清单与处理报告输出。

职责：
  1. audit_batch：批次级交叉复核（失败照片不得残留输出、成功照片必须有合格成品）；
  2. 失败清单.csv / .txt：仅列出需要操作人员处理的照片（失败 / 跳过 / 警告）；
  3. 处理报告_*.txt / .json：配置快照 + 统计 + 失败明细 + 校验记录，供归档回溯。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from . import __app_name__, __spec_source__, __version__
from .config import RuleConfig
from .engine import (
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    STATUS_WARNING,
    ProcessResult,
)

STAGE_ADVICE = {
    "导入": "确认文件未损坏、格式受支持(JPG/PNG/BMP/TIF/WEBP)；必要时从相机重新导出或补拍",
    "时间源": "确认原图保留了 EXIF 拍摄日期；勿用微信/截图等会剥离 EXIF 的方式传输。"
              "如确无 EXIF，可在规则中改为「回退到文件修改时间」或「固定文本」",
    "尺寸调整": "检查目标宽高是否过大、内存是否充足；必要时改用「不缩放」或降低目标尺寸",
    "水印渲染": "检查字体文件是否存在、字号是否过大、目标画布是否过小",
    "导出": "检查输出目录权限、磁盘剩余空间、文件名模板是否含非法字符",
    "输出校验": "检查磁盘写入稳定性与编码设置；处理后重试该张",
    "未知阶段": "记录异常信息并反馈开发维护人员",
}

FAILURE_CSV_FIELDS = [
    "序号", "状态", "原文件名", "原文件路径", "失败阶段", "失败原因",
    "建议处理", "警告/说明", "原尺寸", "拍摄时间", "输出文件", "记录时间",
]


@dataclass
class BatchSummary:
    total: int = 0
    success: int = 0
    warning: int = 0
    failed: int = 0
    skipped: int = 0
    verified: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    output_dir: str = ""
    audit: list[str] = field(default_factory=list)
    report_files: dict[str, str] = field(default_factory=dict)

    @property
    def ok_count(self) -> int:
        return self.success + self.warning

    @property
    def attention_count(self) -> int:
        return self.failed + self.skipped + self.warning

    def headline(self) -> str:
        return (f"共 {self.total} 张｜成功 {self.success}｜警告 {self.warning}｜"
                f"失败 {self.failed}｜跳过 {self.skipped}｜已校验 {self.verified}")


def summarize(results: list[ProcessResult], cfg: RuleConfig,
              started: datetime, finished: datetime) -> BatchSummary:
    s = BatchSummary(
        total=len(results),
        success=sum(r.status == STATUS_SUCCESS for r in results),
        warning=sum(r.status == STATUS_WARNING for r in results),
        failed=sum(r.status == STATUS_FAILED for r in results),
        skipped=sum(r.status == STATUS_SKIPPED for r in results),
        verified=sum(r.verified for r in results),
        started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
        finished_at=finished.strftime("%Y-%m-%d %H:%M:%S"),
        duration_ms=int((finished - started).total_seconds() * 1000),
        output_dir=cfg.output_dir,
    )
    return s


def audit_batch(results: list[ProcessResult]) -> list[str]:
    """批次级交叉复核：确认失败照片没有污染输出目录、成功照片确有合格成品。"""
    findings: list[str] = []
    for r in results:
        if not r.output_path:
            continue
        p = Path(r.output_path)
        if r.status in (STATUS_SUCCESS, STATUS_WARNING):
            if not p.is_file():
                findings.append(f"#{r.index} {r.filename}：判定成功但输出文件缺失（{p.name}）")
                continue
            try:
                with Image.open(p) as im:
                    im.verify()
            except (OSError, ValueError, UnidentifiedImageError) as exc:
                findings.append(f"#{r.index} {r.filename}：成品回读失败（{exc}）")
        elif r.status == STATUS_FAILED:
            # 失败照片的输出路径已由 engine 清空；此处再确认未残留同名文件
            if p.is_file():
                findings.append(f"#{r.index} {r.filename}：判定失败但仍存在残留输出（{p.name}）")
    if not findings:
        findings.append("通过：失败照片均未产生成品，成功照片输出文件均可正常解码")
    return findings


def attention_rows(results: list[ProcessResult]) -> list[ProcessResult]:
    """需要操作人员关注的照片：失败 / 跳过 / 警告。"""
    return [r for r in results if r.status != STATUS_SUCCESS]


def _write_failure_csv(path: Path, rows: list[ProcessResult]) -> None:
    # utf-8-sig：Excel 双击直接正确显示中文，不出现乱码
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FAILURE_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        if not rows:
            w.writerow({
                "序号": "-", "状态": "无", "原文件名": "本次无失败/异常照片",
                "原文件路径": "", "失败阶段": "", "失败原因": "", "建议处理": "",
                "警告/说明": "", "记录时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            return
        for r in rows:
            d = r.as_row()
            d["建议处理"] = STAGE_ADVICE.get(r.stage, "—") if r.status == STATUS_FAILED else "—"
            d["记录时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            w.writerow({k: d.get(k, "") for k in FAILURE_CSV_FIELDS})


def _write_failure_txt(path: Path, rows: list[ProcessResult], s: BatchSummary) -> None:
    lines = [
        "失败 / 异常照片清单",
        "=" * 72,
        f"程序：{__app_name__} v{__version__}",
        f"批次时间：{s.started_at} ~ {s.finished_at}",
        f"统计：{s.headline()}",
        f"输出目录：{s.output_dir}",
        f"需关注照片数：{len(rows)}",
        "=" * 72,
        "",
    ]
    if not rows:
        lines.append("本次无失败 / 异常照片，全部照片均成功输出并通过校验。")
    for i, r in enumerate(rows, 1):
        lines += [
            f"[{i}] #{r.index}  {r.filename}   状态：{r.status}",
            f"    原路径：{r.source}",
        ]
        if r.status == STATUS_FAILED:
            lines += [
                f"    失败阶段：{r.stage}",
                f"    失败原因：{r.reason}",
                f"    建议处理：{STAGE_ADVICE.get(r.stage, '—')}",
            ]
        elif r.reason:
            lines.append(f"    说明：{r.reason}")
        for w in r.warnings:
            lines.append(f"    警告：{w}")
        if r.output_path:
            lines.append(f"    输出文件：{r.output_path}")
        lines.append("")
    lines += ["-" * 72, "校验记录："] + [f"  - {x}" for x in s.audit]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_summary_txt(path: Path, results: list[ProcessResult], cfg: RuleConfig,
                       s: BatchSummary) -> None:
    lines = [
        f"{__app_name__} — 批量处理报告",
        "=" * 72,
        f"程序版本      : v{__version__}",
        f"流程依据      : {__spec_source__}",
        f"批次开始      : {s.started_at}",
        f"批次结束      : {s.finished_at}",
        f"总耗时        : {s.duration_ms} ms",
        f"输出目录      : {s.output_dir}",
        "",
        "统计",
        "-" * 72,
        f"  导入总数    : {s.total}",
        f"  成功        : {s.success}",
        f"  警告        : {s.warning}",
        f"  失败        : {s.failed}",
        f"  跳过        : {s.skipped}",
        f"  输出回读校验通过 : {s.verified}",
        "",
        "校验记录",
        "-" * 72,
    ]
    lines += [f"  - {x}" for x in s.audit]
    lines += ["", "本次规则配置快照", "-" * 72]
    for k, v in cfg.to_dict().items():
        lines.append(f"  {k:24s}: {v}")
    lines += ["", "逐张处理结果", "-" * 72]
    for r in results:
        lines.append(
            f"  #{r.index:03d} {r.status:2s} {r.filename:36s} "
            f"{r.original_size:12s} -> {r.output_size:12s} "
            f"水印[{r.watermark_text}] {r.stage}{('：' + r.reason) if r.reason else ''}"
        )
        if r.output_path:
            lines.append(f"        输出：{r.output_path}")
    lines += ["", "=" * 72, f"报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def export_reports(results: list[ProcessResult], cfg: RuleConfig,
                   summary: BatchSummary, results_snapshot: list[dict[str, Any]] | None = None
                   ) -> dict[str, str]:
    """写出全部报告文件，返回 {类型: 绝对路径}。写入失败时返回带 error 键的字典。"""
    out: dict[str, str] = {}
    try:
        out_dir = Path(cfg.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        rows = attention_rows(results)
        summary.audit = audit_batch(results)

        p_csv = out_dir / "失败清单.csv"
        p_txt = out_dir / "失败清单.txt"
        p_sum = out_dir / f"处理报告_{stamp}.txt"
        p_json = out_dir / f"处理报告_{stamp}.json"

        _write_failure_csv(p_csv, rows)
        _write_failure_txt(p_txt, rows, summary)
        _write_summary_txt(p_sum, results, cfg, summary)

        payload = {
            "app": __app_name__, "version": __version__, "spec_source": __spec_source__,
            "summary": {
                "total": summary.total, "success": summary.success,
                "warning": summary.warning, "failed": summary.failed,
                "skipped": summary.skipped, "verified": summary.verified,
                "started_at": summary.started_at, "finished_at": summary.finished_at,
                "duration_ms": summary.duration_ms, "output_dir": summary.output_dir,
                "audit": summary.audit,
            },
            "config": cfg.to_dict(),
            "attention": [r.as_row() | {"建议处理": STAGE_ADVICE.get(r.stage, "—")
                                        if r.status == STATUS_FAILED else "—"} for r in rows],
            "results": results_snapshot or [r.as_row() for r in results],
        }
        p_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        out = {
            "失败清单CSV": str(p_csv), "失败清单TXT": str(p_txt),
            "处理报告TXT": str(p_sum), "处理报告JSON": str(p_json),
        }
        summary.report_files = out
    except OSError as exc:
        out = {"error": f"报告写出失败：{exc}"}
        summary.audit.append(f"报告写出失败：{exc}")
    return out
