"""操作人员图形界面（tkinter，标准库，无额外 UI 依赖）。

设计目标：一次批量导入 → 无需逐张设置 → 一键完成尺寸/像素规格调整、时间水印、
批量导出 → 逐张状态与失败原因一目了然。
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Any, Callable

from PIL import Image  # noqa: F401  (确保打包时 PIL 被收集)

from . import __app_name__, __spec_source__, __version__
from .config import (
    FILENAME_TEMPLATE_PRESETS,
    ON_EXISTS,
    OUTPUT_FORMATS,
    RESIZE_MODES,
    SUPPORTED_INPUT_EXTS,
    WATERMARK_PREVIEW_TEXT,
    RuleConfig,
    app_base_dir,
    load_last_used,
    rules_dir,
    save_last_used,
)
from .engine import (
    EXIF_MISSING_REASON,
    EXIF_NOTICE_MESSAGE,
    EXIF_NOTICE_TITLE,
    GEOMETRY_NOTICE_TITLE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    STATUS_WARNING,
    GeometryIssue,
    ProcessResult,
    check_geometry,
    geometry_notice_text,
    has_exif_capture_time,
    plan_output_names,
    process_one,
)
from .reporter import BatchSummary, export_reports, summarize

# --------------------------------------------------------------------------
# 字段声明：与 RuleConfig 一一对应，避免界面与引擎出现两套默认值
# kind: int | bool | text | mapped | editable | color | dir | fontfile
# --------------------------------------------------------------------------

Field = tuple[str, str, str, Any, str]

# 顶部「必填信息」面板中的字段：优先前置、集中展示、明确标识
REQUIRED_FIELDS: tuple[str, ...] = ("ecu_name", "serial_number", "output_dir")
PANEL_FIELDS: tuple[str, ...] = REQUIRED_FIELDS + ("start_index",)

# 高级设置页签（只含非水印项；时间水印由系统固定生成，不在此出现、也不可修改）
ADVANCED_GROUPS: list[tuple[str, list[Field], str]] = [
    ("输出规格", [
        ("resize_mode", "缩放方式", "mapped", RESIZE_MODES, ""),
        ("target_width", "目标宽度 (px)", "int", None, ""),
        ("target_height", "目标高度 (px)", "int", None, ""),
        ("dpi", "像素规格 DPI", "int", None, ""),
        ("output_format", "输出格式", "mapped", OUTPUT_FORMATS, ""),
        ("jpeg_quality", "JPEG 质量", "int", None, ""),
        ("min_output_megapixels", "主输出像素下限 (MP)", "int", None, ""),
        ("background", "留白背景色", "color", None, ""),
    ], "客户规范 V1 p.3 / V2 §8·§10：交付成品必须 ≥2100 万像素。默认「不缩放」保持原分辨率，通常无需修改。"),

    ("输出与命名", [
        ("filename_template", "批量文件名模板", "editable", [t for t, _ in FILENAME_TEMPLATE_PRESETS], ""),
        ("on_exists", "同名文件时", "mapped", ON_EXISTS, ""),
        ("keep_metadata", "保留 EXIF 元数据", "bool", None, ""),
        ("report_copy_enabled", "另出报告插图副本", "bool", None, ""),
        ("report_copy_suffix", "报告版文件名后缀", "text", None, ""),
    ], "命名默认 <ECU 名称>_<序列号>_<四位序号>（V1 p.3 / V2 §11）；报告插图副本不满足 2100 万像素，不可替代主交付原件。"),

    ("输出校验", [
        ("verify_output", "输出后回读校验", "bool", None, ""),
        ("min_output_bytes", "成品最小字节数", "int", None, ""),
        ("max_source_pixels", "源图像素安全上限", "int", None, ""),
    ], "校验可解码、目标尺寸、DPI、文件大小、格式、主输出像素数。"),
]

ADVANCED_FIELDS: tuple[str, ...] = tuple(k for _, specs, _ in ADVANCED_GROUPS for k, *_ in specs)
# 界面上出现的全部字段 = 配置字段 − 系统固定的水印字段
UI_FIELDS: tuple[str, ...] = PANEL_FIELDS + ADVANCED_FIELDS

TREE_COLUMNS = [
    ("idx", "#", 48), ("name", "文件名", 230), ("osize", "原尺寸", 100),
    ("status", "状态", 62), ("stage", "失败阶段", 78),
    ("reason", "失败原因 / 说明", 330), ("out", "输出文件", 230),
]


class MappedCombo(ttk.Combobox):
    """显示中文标签、内部取配置值的下拉框。mapping: 值 -> 显示标签。"""

    def __init__(self, parent: tk.Widget, mapping: dict[str, str], width: int = 22):
        self._fwd = dict(mapping)
        self._rev = {v: k for k, v in mapping.items()}
        super().__init__(parent, values=list(mapping.values()), state="readonly", width=width)

    def value(self) -> str:
        return self._rev.get(self.get(), self.get())

    def set_value(self, value: str) -> None:
        self.set(self._fwd.get(value, value))


def show_geometry_notice(parent: tk.Misc, issues: list[GeometryIssue]) -> tk.Toplevel:
    """导入时的比例/方向提示窗口：非模态、可滚动，不阻断其余照片继续导入。

    正文由 engine.geometry_notice_text() 统一提供（GUI / 测试 / 自检共用同一文案）。
    这里不使用 grab_set，窗口弹出后主窗口仍可操作、已导入的合格照片照常保留。
    """
    win = tk.Toplevel(parent)
    win.title(GEOMETRY_NOTICE_TITLE)
    win.configure(bg="#F5F7FA")
    width = 860
    height = min(620, max(360, 220 + 30 * len(issues)))
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        sw, sh = parent.winfo_screenwidth(), parent.winfo_screenheight()
        x = px + (pw - width) // 2
        y = py + (ph - height) // 2
        # 主窗口高于屏幕时，居中会把底部按钮挤出屏幕；钳制到可视区域内
        x = max(0, min(x, max(0, sw - width)))
        y = max(0, min(y, max(0, sh - height - 40)))
        win.geometry(f"{width}x{height}+{x}+{y}")
    except tk.TclError:
        win.geometry(f"{width}x{height}")
    win.minsize(700, 320)
    try:
        win.transient(parent)  # 仅保持在父窗口之上，不设置模态抓取
    except tk.TclError:
        pass

    head = tk.Frame(win, bg="#F5F7FA")
    head.pack(fill="x", padx=14, pady=(12, 2))
    tk.Label(head, text=f"发现 {len(issues)} 张照片不符合导入要求",
             bg="#F5F7FA", fg="#A8410E",
             font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")

    body = tk.Frame(win, bg="#F5F7FA")
    body.pack(fill="both", expand=True, padx=14, pady=(6, 4))
    txt = tk.Text(body, wrap="word", bg="#FFFFFF", fg="#1F2933",
                  font=("Microsoft YaHei UI", 9), relief="solid", borderwidth=1,
                  padx=8, pady=6)
    sb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    txt.pack(side="left", fill="both", expand=True)
    txt.insert("1.0", geometry_notice_text(issues))
    txt.configure(state="disabled")

    foot = tk.Frame(win, bg="#F5F7FA")
    foot.pack(fill="x", padx=14, pady=(4, 12))
    ttk.Button(foot, text="知道了", command=win.destroy).pack(side="right")
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.lift()
    return win


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{__app_name__}  v{__version__}")
        self.geometry("1220x900")
        self.minsize(1040, 760)
        self.configure(bg="#F5F7FA")

        self.vars: dict[str, tk.Variable] = {}
        self.widgets: dict[str, tk.Widget] = {}
        self.invalid_fields: list[str] = []
        self.precheck: dict[str, str] = {}
        self.exif_notified: set[str] = set()   # 已弹窗提示过的缺 EXIF 照片，避免重复弹窗
        # 比例/方向校验：按路径缓存结果（None = 符合 4:3 横版），避免重复解码大图
        self.geometry: dict[str, GeometryIssue | None] = {}
        self.geometry_notified: set[str] = set()
        self.geometry_window: tk.Toplevel | None = None
        self.files: list[str] = []
        self.results: list[ProcessResult] = []
        self.summary: BatchSummary | None = None
        self.report_paths: dict[str, str] = {}
        self.msg_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.include_subfolders = tk.BooleanVar(value=False)
        self.required_entries: dict[str, ttk.Entry] = {}
        self.required_filled = 0

        self._build_style()
        self._build_header()
        self._build_required_panel()
        self._build_notebook()
        self._build_footer()
        boot = load_last_used()
        if not boot.output_dir.strip():
            # 首次运行给一个程序目录内的默认输出位置，开箱即用、不触碰个人目录
            boot.output_dir = str(app_base_dir() / "输出照片")
        self._apply_config(boot)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_import_count()
        self._update_required_state()
        if self.required_filled < len(REQUIRED_FIELDS):
            self._focus_first_required()
        self._set_status("请先完成顶部 3 项必填信息，然后批量导入照片。" if
                         self.required_filled < len(REQUIRED_FIELDS) else
                         "必填信息已就绪，可批量导入照片。")

    # ------------------------------------------------------------------
    # 样式与骨架
    # ------------------------------------------------------------------
    def _build_style(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TLabel", background="#F5F7FA", foreground="#1F2933", font=("Microsoft YaHei UI", 9))
        st.configure("TFrame", background="#F5F7FA")
        st.configure("TLabelframe", background="#F5F7FA", foreground="#1F2933")
        st.configure("TLabelframe.Label", background="#F5F7FA", foreground="#134E7C",
                     font=("Microsoft YaHei UI", 9, "bold"))
        st.configure("TButton", font=("Microsoft YaHei UI", 9), padding=4)
        st.configure("TCheckbutton", background="#F5F7FA", foreground="#1F2933")
        st.configure("Treeview", font=("Microsoft YaHei UI", 9), rowheight=24,
                     background="#FFFFFF", fieldbackground="#FFFFFF", foreground="#1F2933")
        st.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        st.configure("Horizontal.TProgressbar", troughcolor="#E3E8EF", background="#2D7DD2")
        # 必填项未填时输入框底色变红，配合 ★ 标识让缺项一眼可见
        st.configure("Required.TEntry", fieldbackground="#FFFFFF", padding=2)
        st.map("Required.TEntry",
               fieldbackground=[("invalid", "#FFF1F0"), ("!invalid", "#FFFFFF")],
               foreground=[("invalid", "#C5221F"), ("!invalid", "#1F2933")])

    def _build_header(self) -> None:
        f = tk.Frame(self, bg="#134E7C", height=54)
        f.pack(side="top", fill="x")
        f.pack_propagate(False)
        tk.Label(f, text=f"  {__app_name__}", bg="#134E7C", fg="white",
                 font=("Microsoft YaHei UI", 14, "bold")).pack(side="left", padx=10)
        tk.Label(f, text=f"v{__version__}   |   流程依据：{__spec_source__}", bg="#134E7C",
                 fg="#CFE3F5", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=4)
        tk.Label(f, text=f"水印固定 {WATERMARK_PREVIEW_TEXT}",
                 bg="#134E7C", fg="#FFD98A", font=("Microsoft YaHei UI", 9)).pack(side="right", padx=14)

    # ------------------------------------------------------------------
    # 顶部「必填信息」面板：优先前置、集中展示、明确标识
    # ------------------------------------------------------------------
    def _build_required_panel(self) -> None:
        box = ttk.LabelFrame(self, text=" 第 1 步 · 必填信息（★ 为必填，填一次下次自动带入） ",
                             padding=(10, 8))
        box.pack(side="top", fill="x", padx=10, pady=(8, 0))
        self.required_box = box

        row1 = ttk.Frame(box)
        row1.pack(fill="x")
        self._required_field(row1, "ecu_name", "★ ECU 名称", 26)
        self._required_field(row1, "serial_number", "★ 样品序列号", 30)
        self._required_field(row1, "output_dir", "★ 输出目录", 46, browse=True)
        ttk.Label(row1, text="起始序号").pack(side="left", padx=(10, 3))
        var = tk.StringVar()
        self.vars["start_index"] = var
        self.widgets["start_index"] = ttk.Entry(row1, textvariable=var, width=6)
        self.widgets["start_index"].pack(side="left")

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(9, 0))
        ttk.Button(row2, text="① 导入照片", command=self._add_files).pack(side="left", padx=2)
        ttk.Button(row2, text="① 导入文件夹", command=self._add_folder).pack(side="left", padx=2)
        ttk.Checkbutton(row2, text="包含子文件夹",
                        variable=self.include_subfolders).pack(side="left", padx=(8, 2))
        ttk.Button(row2, text="清空列表", command=self._clear_files).pack(side="left", padx=2)
        self.import_label = ttk.Label(row2, text="已导入 0 张", foreground="#134E7C",
                                      font=("Microsoft YaHei UI", 9, "bold"))
        self.import_label.pack(side="left", padx=12)
        self.btn_start = ttk.Button(row2, text="② 开始批量处理", command=self._start_batch)
        self.btn_start.pack(side="right", padx=2)
        self.required_label = ttk.Label(row2, text="", font=("Microsoft YaHei UI", 9, "bold"))
        self.required_label.pack(side="right", padx=10)

        row3 = ttk.Frame(box)
        row3.pack(fill="x", pady=(7, 0))
        tk.Label(row3, text=f"时间水印：系统统一生成 · {WATERMARK_PREVIEW_TEXT} · 不可修改，无需任何设置",
                 bg="#F5F7FA", fg="#0B6B3A",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        ttk.Label(row3, text="（水印内容仅年月日时分秒，不附加文字、标识或可变信息）",
                  foreground="#5F6B7A").pack(side="left", padx=8)
        ttk.Label(row3, text="支持 JPG/PNG/BMP/TIF/WEBP，重复文件自动去重",
                  foreground="#5F6B7A").pack(side="right")

    def _required_field(self, parent: tk.Widget, key: str, label: str, width: int,
                        browse: bool = False) -> None:
        ttk.Label(parent, text=label, foreground="#C5221F",
                  font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(8, 3))
        var = tk.StringVar()
        self.vars[key] = var
        entry = ttk.Entry(parent, textvariable=var, width=width, style="Required.TEntry")
        entry.pack(side="left")
        self.required_entries[key] = entry
        if browse:
            ttk.Button(parent, text="浏览", width=5, command=self._pick_dir).pack(side="left", padx=3)
        var.trace_add("write", lambda *_: self._update_required_state())

    def _update_required_state(self) -> None:
        """刷新必填状态：空值高亮、显示已填数量、未填满时禁用「开始批量处理」。"""
        filled = 0
        for key in REQUIRED_FIELDS:
            var = self.vars.get(key)
            value = "" if var is None else str(var.get()).strip()
            entry = self.required_entries.get(key)
            if value:
                filled += 1
                if entry is not None:
                    entry.state(["!invalid"])
            elif entry is not None:
                entry.state(["invalid"])
        self.required_filled = filled
        total = len(REQUIRED_FIELDS)
        if hasattr(self, "required_label"):
            done = filled == total
            self.required_label.configure(
                text="必填项已完整 ✓" if done else f"必填 {filled}/{total} 已填写",
                foreground="#0B6B3A" if done else "#C5221F")
        if hasattr(self, "btn_start"):
            self.btn_start.configure(
                state="normal" if filled == total and not self.busy else "disabled")

    def _focus_first_required(self) -> None:
        for key in REQUIRED_FIELDS:
            var = self.vars.get(key)
            if var is not None and not str(var.get()).strip():
                entry = self.required_entries.get(key)
                if entry is not None:
                    entry.focus_set()
                return

    def _missing_required(self) -> list[str]:
        labels = {"ecu_name": "ECU 名称", "serial_number": "样品序列号", "output_dir": "输出目录"}
        return [labels[k] for k in REQUIRED_FIELDS
                if not str(self.vars.get(k, tk.StringVar()).get()).strip()]

    def _build_notebook(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(side="top", fill="both", expand=True, padx=10, pady=6)
        self.nb = nb
        self.tab_results = ttk.Frame(nb)
        self.tab_advanced = ttk.Frame(nb)
        nb.add(self.tab_results, text="  第 2 步 · 照片与逐张处理结果  ")
        nb.add(self.tab_advanced, text="  高级设置（一般无需修改）  ")
        self._build_results_tab()
        self._build_advanced_tab()

    def _build_advanced_tab(self) -> None:
        outer = ttk.Frame(self.tab_advanced)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg="#F5F7FA", highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def _wheel(e: Any) -> None:
            # 仅当「高级设置」页签处于激活状态才滚动配置区，避免干扰结果表滚动
            try:
                if self.nb.index(self.nb.select()) == 1:
                    canvas.yview_scroll(int(-e.delta / 120), "units")
            except (tk.TclError, AttributeError):
                pass

        canvas.bind_all("<MouseWheel>", _wheel)

        tools = ttk.Frame(inner)
        tools.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(tools,
                  text=f"时间水印由系统统一生成（{WATERMARK_PREVIEW_TEXT}），"
                       "本页不含任何水印项，也不可修改水印。",
                  foreground="#0B6B3A",
                  font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        ttk.Button(tools, text="载入设置…", command=self._load_rules).pack(side="right", padx=2)
        ttk.Button(tools, text="保存设置…", command=self._save_rules).pack(side="right", padx=2)
        ttk.Button(tools, text="恢复默认", command=self._restore_advanced_defaults).pack(
            side="right", padx=2)

        for title, specs, hint in ADVANCED_GROUPS:
            grp = ttk.LabelFrame(inner, text=title, padding=(10, 8))
            grp.pack(fill="x", padx=8, pady=6)
            for i, (key, label, kind, options, _) in enumerate(specs):
                self._make_field(grp, i // 2, i % 2, key, label, kind, options)
            ttk.Label(grp, text=hint, foreground="#5F6B7A", wraplength=1050,
                      justify="left").grid(row=(len(specs) + 1) // 2, column=0,
                                          columnspan=2, sticky="w", padx=6, pady=(8, 2))

    def _make_field(self, parent: tk.Widget, row: int, col: int, key: str,
                    label: str, kind: str, options: Any) -> None:
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=col, sticky="ew", padx=6, pady=4)
        parent.grid_columnconfigure(col, weight=1)
        ttk.Label(cell, text=label, width=17, anchor="w").pack(side="left")

        if kind == "int":
            var = tk.StringVar()
            w = ttk.Entry(cell, textvariable=var, width=22)
            w.pack(side="left", fill="x", expand=True)
            self.vars[key] = var
        elif kind == "text":
            var = tk.StringVar()
            w = ttk.Entry(cell, textvariable=var, width=22)
            w.pack(side="left", fill="x", expand=True)
            self.vars[key] = var
        elif kind == "bool":
            var = tk.BooleanVar()
            w = ttk.Checkbutton(cell, variable=var)
            w.pack(side="left")
            self.vars[key] = var
        elif kind == "mapped":
            w = MappedCombo(cell, options, width=21)
            w.pack(side="left", fill="x", expand=True)
            self.widgets[key] = w
            var = None
        elif kind == "editable":
            var = tk.StringVar()
            w = ttk.Combobox(cell, textvariable=var, values=options, width=20)
            w.pack(side="left", fill="x", expand=True)
            self.vars[key] = var
        elif kind == "color":
            var = tk.StringVar()
            w = ttk.Entry(cell, textvariable=var, width=12)
            w.pack(side="left")
            swatch = tk.Label(cell, width=2, bg="#FFFFFF", relief="groove")
            swatch.pack(side="left", padx=3)
            ttk.Button(cell, text="选色", width=5,
                       command=lambda k=key, s=swatch: self._pick_color(k, s)).pack(side="left")
            self.vars[key] = var
            self.widgets[key + "_swatch"] = swatch
        elif kind == "dir":
            var = tk.StringVar()
            w = ttk.Entry(cell, textvariable=var, width=22)
            w.pack(side="left", fill="x", expand=True)
            ttk.Button(cell, text="浏览", width=5, command=self._pick_dir).pack(side="left", padx=3)
            self.vars[key] = var
        self.widgets[key] = w if kind not in ("mapped",) else self.widgets.get(key, w)

    def _build_results_tab(self) -> None:
        top = ttk.Frame(self.tab_results, padding=(6, 6))
        top.pack(fill="both", expand=True)
        cols = [c[0] for c in TREE_COLUMNS]
        self.tree = ttk.Treeview(top, columns=cols, show="headings", selectmode="browse")
        for cid, head, width in TREE_COLUMNS:
            self.tree.heading(cid, text=head)
            self.tree.column(cid, width=width, anchor="w",
                             stretch=(cid in ("name", "reason", "out")))
        vsb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(top, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        top.rowconfigure(0, weight=1)
        top.columnconfigure(0, weight=1)
        self.tree.tag_configure("ok", foreground="#137333")
        self.tree.tag_configure("fail", foreground="#C5221F")
        self.tree.tag_configure("warn", foreground="#B06000")
        self.tree.tag_configure("skip", foreground="#5F6368")
        self.tree.tag_configure("pending", foreground="#7A8699")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        detail = ttk.LabelFrame(self.tab_results, text="选中照片的详细信息（可滚动）", padding=6)
        detail.pack(fill="x", padx=8, pady=(0, 8))
        dsb = ttk.Scrollbar(detail, orient="vertical")
        self.detail = tk.Text(detail, height=7, wrap="word", bg="#FFFFFF", fg="#1F2933",
                              font=("Microsoft YaHei UI", 9), relief="flat",
                              yscrollcommand=dsb.set)
        dsb.configure(command=self.detail.yview)
        self.detail.pack(side="left", fill="x", expand=True)
        dsb.pack(side="right", fill="y")
        self.detail.configure(state="disabled")

    def _build_footer(self) -> None:
        f = ttk.Frame(self, padding=(10, 4, 10, 10))
        f.pack(side="bottom", fill="x")
        btns = ttk.Frame(f)
        btns.pack(fill="x")
        # 「② 开始批量处理」在顶部必填面板内（唯一入口，未填满必填项时禁用）
        self.btn_stop = ttk.Button(btns, text="停止", command=self._stop_batch, state="disabled")
        self.btn_stop.pack(side="left", padx=2)
        self.btn_failure = ttk.Button(btns, text="③ 导出 / 查看失败清单", command=self._open_failure_list)
        self.btn_failure.pack(side="left", padx=8)
        ttk.Button(btns, text="打开输出目录", command=self._open_output_dir).pack(side="left", padx=2)
        self.count_label = ttk.Label(btns, text="成功 0 ｜ 警告 0 ｜ 失败 0 ｜ 跳过 0",
                                     foreground="#134E7C", font=("Microsoft YaHei UI", 9, "bold"))
        self.count_label.pack(side="right", padx=6)

        prog = ttk.Frame(f)
        prog.pack(fill="x", pady=(6, 0))
        self.progress = ttk.Progressbar(prog, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(prog, text="0 / 0", width=12, anchor="e")
        self.progress_label.pack(side="left", padx=6)

        self.status_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.status_var, foreground="#5F6B7A").pack(fill="x", pady=(4, 0))

    # ------------------------------------------------------------------
    # 配置 <-> 界面
    # ------------------------------------------------------------------
    def _collect_config(self) -> RuleConfig:
        cfg = RuleConfig()
        self.invalid_fields: list[str] = []
        for key, var in self.vars.items():
            value = var.get()
            if isinstance(self.widgets.get(key), MappedCombo):
                continue
            if isinstance(var, tk.BooleanVar):
                setattr(cfg, key, bool(value))
            elif isinstance(getattr(cfg, key), int) and not isinstance(getattr(cfg, key), bool):
                try:
                    setattr(cfg, key, int(str(value).strip()))
                except ValueError:
                    # 不静默置 0（0 可能是合法值，会绕过配置校验）：保留默认值并显式报错
                    self.invalid_fields.append(key)
                    setattr(cfg, key, getattr(RuleConfig(), key))
            else:
                setattr(cfg, key, str(value))
        for key, widget in self.widgets.items():
            if isinstance(widget, MappedCombo):
                setattr(cfg, key, widget.value())
        # 时间水印由系统统一生成：界面不提供任何水印项，此处再固化一次，
        # 确保即使界面字段被误改也无法改变水印内容/格式/外观。
        return cfg.apply_fixed_watermark()

    def _apply_config(self, cfg: RuleConfig) -> None:
        for key, var in self.vars.items():
            value = getattr(cfg, key, "")
            if isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            else:
                var.set("" if value is None else str(value))
        for key, widget in self.widgets.items():
            if isinstance(widget, MappedCombo):
                widget.set_value(str(getattr(cfg, key, "")))
        sw = self.widgets.get("background_swatch")
        if sw is not None:
            try:
                sw.configure(bg=str(cfg.background))
            except tk.TclError:
                pass
        self._update_required_state()

    def _pick_color(self, key: str, swatch: tk.Label) -> None:
        initial = self.vars[key].get() or "#FFFFFF"
        try:
            rgb, hexv = colorchooser.askcolor(color=initial, title="选择颜色")
        except tk.TclError:
            return
        if hexv:
            self.vars[key].set(hexv.upper())
            swatch.configure(bg=hexv)

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(title="选择照片输出目录", initialdir=self.vars["output_dir"].get() or None)
        if d:
            self.vars["output_dir"].set(str(Path(d)))

    def _restore_advanced_defaults(self) -> None:
        """只把「高级设置」里的非水印项恢复默认，必填信息与已导入照片保持不变。"""
        keep = {k: str(self.vars[k].get()) for k in PANEL_FIELDS if k in self.vars}
        self._apply_config(RuleConfig())
        for k, v in keep.items():
            self.vars[k].set(v)
        self._update_required_state()
        self._set_status("高级设置已恢复默认（必填信息与已导入照片保持不变）")

    def _save_rules(self) -> None:
        cfg = self._collect_config()
        p = filedialog.asksaveasfilename(title="保存规则文件", defaultextension=".json",
                                         initialdir=str(rules_dir()),
                                         initialfile=f"规则_{datetime.now():%Y%m%d}.json",
                                         filetypes=[("JSON 规则", "*.json")])
        if not p:
            return
        try:
            cfg.save(p)
            self._set_status(f"规则已保存：{p}")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def _load_rules(self) -> None:
        p = filedialog.askopenfilename(title="载入规则文件", initialdir=str(rules_dir()),
                                       filetypes=[("JSON 规则", "*.json"), ("所有文件", "*.*")])
        if not p:
            return
        try:
            cfg = RuleConfig.load(p)
            self._apply_config(cfg)
            self._set_status(f"已载入规则：{p}")
        except ValueError as exc:
            messagebox.showerror("载入失败", str(exc))

    # ------------------------------------------------------------------
    # 批量导入
    # ------------------------------------------------------------------
    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="批量导入照片（可多选）",
            filetypes=[("照片文件", "*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp"),
                       ("所有文件", "*.*")])
        if paths:
            self._merge_files([str(Path(p)) for p in paths])

    def _add_folder(self) -> None:
        d = filedialog.askdirectory(title="选择照片所在文件夹")
        if not d:
            return
        root = Path(d)
        it = root.rglob("*") if self.include_subfolders.get() else root.glob("*")
        found = [str(p) for p in it if p.is_file() and p.suffix.lower() in SUPPORTED_INPUT_EXTS]
        if not found:
            messagebox.showinfo("未找到照片", f"所选文件夹内没有受支持的照片文件：\n{d}")
            return
        self._merge_files(sorted(found))

    def _merge_files(self, paths: list[str]) -> None:
        existing = {p.lower() for p in self.files}
        added = 0
        for p in paths:
            if p.lower() not in existing:
                self.files.append(p)
                existing.add(p.lower())
                added += 1
        self._refresh_import_count()
        self._run_precheck()
        self._run_geometry_check()
        self._rebuild_pending_rows()
        # 先弹非模态的比例/方向窗口，再弹 EXIF 提示；两者都不移除任何已导入照片
        self._notify_geometry()
        self._notify_missing_exif()
        bad = sum(1 for v in self.precheck.values() if v)
        geo_bad = sum(1 for v in self.geometry.values() if v)
        head = f"导入 {added} 张（去重后共 {len(self.files)} 张）；无需逐张设置。"
        notes = []
        if bad:
            notes.append(f"{bad} 张缺少 EXIF 信息（已弹窗提示）")
        if geo_bad:
            notes.append(f"{geo_bad} 张比例/方向不符合 4:3 横版（已弹窗列出）")
        self._set_status(head + ((" 其中 " + "，".join(notes) + "。") if notes
                                 else " 全部通过 EXIF 与 4:3 横版检查。"))
        if added:
            self.nb.select(self.tab_results)

    def _run_precheck(self) -> None:
        """EXIF 检查：固定、无条件执行，唯一依据是照片 EXIF 中是否存在拍摄日期。

        不读取文件名、文件时间、JPEG 其它数据段，也不受任何配置项影响。
        """
        self.precheck = {
            p: ("" if has_exif_capture_time(p) else EXIF_MISSING_REASON)
            for p in self.files
        }

    def _notify_missing_exif(self) -> None:
        """发现新增的缺 EXIF 照片时弹出一次提示窗口；含 EXIF 的照片不触发。

        文案由 engine 常量统一提供，简洁且不附加任何额外字样。
        弹窗之外仍保留状态表标记与失败清单，保证缺 EXIF 不被静默跳过。
        """
        missing = [p for p, issue in self.precheck.items() if issue]
        fresh = [p for p in missing if p not in self.exif_notified]
        if not fresh:
            return
        self.exif_notified.update(fresh)
        messagebox.showwarning(EXIF_NOTICE_TITLE, EXIF_NOTICE_MESSAGE)

    def _run_geometry_check(self) -> None:
        """导入阶段校验比例与方向；只校验新增文件，结果按路径缓存。

        固定行为，不受任何配置项影响；不合格照片仍保留在导入列表中，
        仅做标记与提示，不移除、不阻断其余合格照片。
        """
        current = set(self.files)
        for p in self.files:
            if p not in self.geometry:
                self.geometry[p] = check_geometry(p)
        for p in list(self.geometry):
            if p not in current:
                self.geometry.pop(p, None)

    def _notify_geometry(self) -> None:
        """发现新增的不合格照片时弹出一次非模态提示窗口，列出当前全部受影响文件。"""
        fresh = [p for p, issue in self.geometry.items()
                 if issue is not None and p not in self.geometry_notified]
        if not fresh:
            return
        self.geometry_notified.update(fresh)
        issues = [self.geometry[p] for p in self.files if self.geometry.get(p)]
        # 复用同一个窗口，避免多次导入堆叠出多个提示窗口
        if self.geometry_window is not None:
            try:
                self.geometry_window.destroy()
            except tk.TclError:
                pass
            self.geometry_window = None
        self.geometry_window = show_geometry_notice(self, issues)

    def _clear_files(self) -> None:
        if self.busy:
            return
        self.files.clear()
        self.results.clear()
        self.precheck.clear()
        self.exif_notified.clear()
        self.geometry.clear()
        self.geometry_notified.clear()
        if self.geometry_window is not None:
            try:
                self.geometry_window.destroy()
            except tk.TclError:
                pass
            self.geometry_window = None
        self.summary = None
        self.report_paths.clear()
        self.tree.delete(*self.tree.get_children())
        self._refresh_import_count()
        self._update_counts()
        self._set_status("列表已清空")

    def _refresh_import_count(self) -> None:
        self.import_label.configure(text=f"已导入 {len(self.files)} 张")

    def _rebuild_pending_rows(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(self.files, 1):
            exif_issue = self.precheck.get(p, "")
            geo_issue = self.geometry.get(p)
            if exif_issue:
                status, stage, reason, tag = "缺少 EXIF", "EXIF 检查", exif_issue, "warn"
            elif geo_issue:
                status, stage, reason, tag = "比例异常", "导入检查", geo_issue.brief, "warn"
            else:
                status, stage, reason, tag = "待处理", "", "", "pending"
            self.tree.insert("", "end", iid=str(i),
                             values=(i, Path(p).name, "", status, stage, reason, ""),
                             tags=(tag,))
        self.progress.configure(maximum=max(1, len(self.files)), value=0)
        self.progress_label.configure(text=f"0 / {len(self.files)}")

    # ------------------------------------------------------------------
    # 批量处理
    # ------------------------------------------------------------------
    def _confirm_name_collisions(self, cfg: RuleConfig) -> bool:
        """开始前统计「目标文件名已存在」的数量，并按同名策略说明后果。

        默认策略是「自动加序号另存」，不覆盖已有文件；只有操作人员显式改成
        「覆盖」时才会提示不可恢复并要求二次确认。
        """
        try:
            planned = plan_output_names(self.files, cfg)
        except Exception:  # 预测失败不阻断：真正处理时仍会按同名策略处理
            return True
        existing = [p for p in planned if p.exists()]
        if not existing:
            return True
        n = len(existing)
        sample = "、".join(p.name for p in existing[:3])
        more = " 等" if n > 3 else ""
        head = f"输出目录中已有 {n} 个与本次目标同名的文件：\n{sample}{more}\n\n"
        if cfg.on_exists == "rename":
            title = "输出目录已有同名文件（不会覆盖）"
            msg = head + ("当前同名策略为「自动加序号另存（默认，不覆盖）」：本次成品会另存为 "
                          "_1、_2 …，已有文件保持不变。\n\n是否继续？")
        elif cfg.on_exists == "skip":
            title = "输出目录已有同名文件（将跳过）"
            msg = head + "当前同名策略为「跳过并标记警告」：这些照片不会产出成品。\n\n是否继续？"
        else:
            title = "⚠ 即将覆盖已有文件"
            msg = head + ("当前同名策略为「覆盖同名文件」：这些文件将被本次成品覆盖且不可恢复。\n"
                          "建议改用「自动加序号另存」。是否仍要继续？")
        return bool(messagebox.askyesno(title, msg))

    def _start_batch(self) -> None:
        if self.busy:
            return
        missing = self._missing_required()
        if missing:
            self._focus_first_required()
            messagebox.showwarning("必填信息未完成",
                                   "请先在顶部「必填信息」填写：" + "、".join(missing))
            return
        cfg = self._collect_config()
        errors = list(cfg.validate())
        if self.invalid_fields:
            errors.insert(0, "以下数值字段填写非法，请改为数字：" + "、".join(self.invalid_fields))
        if errors:
            self.nb.select(self.tab_advanced)
            messagebox.showerror("设置项有误，无法开始", "\n".join(f"• {e}" for e in errors))
            return
        if not self.files:
            messagebox.showinfo("请先导入照片", "请点击「① 导入照片」或「① 导入文件夹」批量导入后再开始。")
            return
        # 处理前再按同一纯 EXIF 条件复核一次（已弹窗提示过的不会重复弹出），
        # 保证任何入口下缺 EXIF 都不会被静默跳过。
        self._run_precheck()
        self._notify_missing_exif()
        if Path(cfg.output_dir).resolve() in {Path(p).parent.resolve() for p in self.files}:
            if not messagebox.askyesno(
                    "输出目录与源照片目录相同",
                    "输出目录与部分源照片位于同一文件夹。程序会避免覆盖本批次任何源照片，\n"
                    "但强烈建议另选独立输出目录以便区分原件与成品。是否仍继续？"):
                return
        # B4：开始前统计同名文件，按当前同名策略明确告知后果（默认不覆盖）
        if not self._confirm_name_collisions(cfg):
            return

        save_last_used(cfg)
        self.stop_event.clear()
        self.results.clear()
        self.summary = None
        self.report_paths.clear()
        self._rebuild_pending_rows()
        self.busy = True
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._set_status("正在批量处理…")
        self.worker = threading.Thread(target=self._run_batch, args=(cfg, list(self.files)), daemon=True)
        self.worker.start()
        self.after(80, self._poll_queue)

    def _run_batch(self, cfg: RuleConfig, files: list[str]) -> None:
        started = datetime.now()
        used: set[str] = set()
        # 整批源照片集合：任何输出都不得覆盖其中任何一个（含尚未处理的照片）
        protected: set[str] = set()
        for f in files:
            try:
                protected.add(str(Path(f).resolve()).lower())
            except OSError:
                pass
        results: list[ProcessResult] = []
        try:
            for i, path in enumerate(files, 1):
                if self.stop_event.is_set():
                    break
                r = process_one(path, cfg, i, used, protected)
                results.append(r)
                self.msg_queue.put(("item", i, r))
                self.msg_queue.put(("progress", i, len(files)))
            for i in range(len(results) + 1, len(files) + 1):
                cancelled = ProcessResult(index=i, source=files[i - 1],
                                          filename=Path(files[i - 1]).name,
                                          status=STATUS_SKIPPED, reason="用户中止，本张未处理")
                results.append(cancelled)
                self.msg_queue.put(("item", i, cancelled))
            finished = datetime.now()
            summary = summarize(results, cfg, started, finished)
            paths = export_reports(results, cfg, summary)
            self.msg_queue.put(("done", summary, paths, cfg))
        except Exception as exc:  # 线程内兜底，避免界面永远停在「处理中」
            self.msg_queue.put(("error", f"{type(exc).__name__}: {exc}"))

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind == "item":
                    _, i, r = msg
                    self._update_row(i, r)
                elif kind == "progress":
                    _, i, total = msg
                    self.progress.configure(value=i, maximum=max(1, total))
                    self.progress_label.configure(text=f"{i} / {total}")
                elif kind == "done":
                    _, summary, paths, cfg = msg
                    self.summary = summary
                    self.report_paths = paths
                    self._finish_batch()
                    self._show_done_message(summary, paths, cfg)
                    return
                elif kind == "error":
                    self._finish_batch()
                    messagebox.showerror("批量处理异常中止", str(msg[1]))
                    self._set_status(f"异常中止：{msg[1]}")
                    return
        except queue.Empty:
            pass
        if self.busy:
            self.after(80, self._poll_queue)

    def _update_row(self, i: int, r: ProcessResult) -> None:
        if len(self.results) < i:
            self.results.append(r)
        else:
            self.results[i - 1] = r
        tag = {
            STATUS_SUCCESS: "ok", STATUS_WARNING: "warn",
            STATUS_FAILED: "fail", STATUS_SKIPPED: "skip",
        }.get(r.status, "pending")
        text = r.reason or "；".join(r.warnings) or (
            f"水印[{r.watermark_text}]  {r.original_size} → {r.output_size}" if r.ok else "")
        values = (i, r.filename, r.original_size, r.status,
                  r.stage if r.status == STATUS_FAILED else "", text,
                  Path(r.output_path).name if r.output_path else "")
        iid = str(i)
        if self.tree.exists(iid):
            self.tree.item(iid, values=values, tags=(tag,))
        else:
            self.tree.insert("", "end", iid=iid, values=values, tags=(tag,))
        self.tree.see(iid)
        self._update_counts()

    def _update_counts(self) -> None:
        c = {s: 0 for s in (STATUS_SUCCESS, STATUS_WARNING, STATUS_FAILED, STATUS_SKIPPED)}
        for r in self.results:
            if r.status in c:
                c[r.status] += 1
        self.count_label.configure(
            text=f"成功 {c[STATUS_SUCCESS]} ｜ 警告 {c[STATUS_WARNING]} ｜ "
                 f"失败 {c[STATUS_FAILED]} ｜ 跳过 {c[STATUS_SKIPPED]}")

    def _stop_batch(self) -> None:
        if self.busy:
            self.stop_event.set()
            self._set_status("已请求停止，将在当前照片处理完成后结束…")

    def _finish_batch(self) -> None:
        self.busy = False
        self.btn_stop.configure(state="disabled")
        self._update_required_state()   # 必填完整时才恢复「开始批量处理」
        self.nb.select(self.tab_results)

    def _show_done_message(self, s: BatchSummary, paths: dict[str, str], cfg: RuleConfig) -> None:
        failed = s.failed + s.skipped
        self._set_status(f"处理完成：{s.headline()}；报告已输出到 {cfg.output_dir}")
        if cfg.verify_output:
            check_text = (f"已对 {s.verified} 张成品完成输出回读校验"
                          "（可解码 / 目标尺寸 / DPI / 格式 / 文件大小）。")
        else:
            check_text = "按配置【未执行】输出回读校验，请人工抽检成品。"
        scope_text = ("校验范围不含：水印可见性、时间戳正确性、≥2100 万像素、命名规范；"
                      "发客户或贴报告前请按处理报告中的提示复核。")
        if "error" in paths:
            messagebox.showwarning("处理完成，但报告写出失败", paths["error"])
        elif failed:
            messagebox.showwarning(
                "批量处理完成（存在失败照片）",
                f"{s.headline()}\n\n失败/跳过 {failed} 张，详见输出目录中的「失败清单.csv / .txt」。\n"
                f"失败照片不会产出成品。{check_text}\n{scope_text}\n\n输出目录：{cfg.output_dir}")
        else:
            messagebox.showinfo(
                "批量处理完成",
                f"{s.headline()}\n\n本批照片已按统一规则批量导出。{check_text}\n"
                f"{scope_text}\n\n输出目录：{cfg.output_dir}")

    # ------------------------------------------------------------------
    # 结果交互与报告
    # ------------------------------------------------------------------
    def _selected_index(self) -> int | None:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _on_tree_select(self, _event: Any) -> None:
        i = self._selected_index()
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if i is None or i > len(self.results):
            self.detail.insert("end", "选中一行可查看该照片的完整处理结果。")
        else:
            r = self.results[i - 1]
            for k, v in r.as_row().items():
                self.detail.insert("end", f"{k}：{v}\n")
            for w in r.warnings:
                self.detail.insert("end", f"警告：{w}\n")
        self.detail.configure(state="disabled")

    def _on_tree_double_click(self, _event: Any) -> None:
        i = self._selected_index()
        if i and i <= len(self.results):
            p = self.results[i - 1].output_path
            if p and Path(p).is_file():
                self._open_path(Path(p))

    def _open_failure_list(self) -> None:
        if not self.results:
            messagebox.showinfo("暂无结果", "请先执行一次批量处理。")
            return
        p = Path(self.report_paths.get("失败清单CSV", ""))
        if not p.is_file():
            cfg = self._collect_config()
            if self.summary is None:
                self.summary = summarize(self.results, cfg, datetime.now(), datetime.now())
            paths = export_reports(self.results, cfg, self.summary)
            if "error" in paths:
                messagebox.showerror("导出失败", paths["error"])
                return
            self.report_paths = paths
            p = Path(paths["失败清单CSV"])
        self._set_status(f"失败清单：{p}")
        if not messagebox.askyesno("失败清单", f"清单位置：\n{p}\n\n是否立即打开？"):
            return
        self._open_path(p)

    def _open_output_dir(self) -> None:
        d = self.vars["output_dir"].get().strip()
        if not d:
            messagebox.showinfo("未设置输出目录", "请先在顶部「必填信息」中填写 ★ 输出目录。")
            entry = self.required_entries.get("output_dir")
            if entry is not None:
                entry.focus_set()
            return
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        self._open_path(p)

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------
    @staticmethod
    def _open_path(p: Path) -> None:
        starter = getattr(os, "startfile", None)
        if starter is None:
            messagebox.showinfo("路径", str(p))
            return
        try:
            starter(str(p))
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc))

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("仍在处理中", "批量处理尚未结束，确定要退出吗？"):
                return
            self.stop_event.set()
        try:
            save_last_used(self._collect_config())
        except Exception:
            pass
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()
