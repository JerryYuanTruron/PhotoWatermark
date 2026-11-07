"""测试产出目录管理：每次运行使用唯一空目录，不做批量删除。

背景：运行环境对「单轮批量删除」有确认阈值（默认 50 个文件）。测试套件原本在
每个用例前用 shutil.rmtree 清空 _test_runs / _test_out_ui，一轮累计删除上百个文件，
会命中该阈值并等待人工授权，导致自动化验证被动中断。

因此改为「唯一目录 + 不删除任何已有文件」：
  - 目录天然为空，用例断言（如「输出目录只含合格成品」）依然成立；
  - 全程零删除，不需要任何删除授权即可跑完；
  - 旧目录位于项目根 `_` 前缀路径下，已被 .gitignore 忽略，可随时人工清理。
"""

from __future__ import annotations

import itertools
from datetime import datetime
from pathlib import Path

_RUN_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
_SEQ = itertools.count(1)


def fresh_dir(root: str | Path, name: str) -> Path:
    """返回本次运行专属的空目录（已创建，不复用、不删除任何已有内容）。"""
    p = Path(root) / f"run{_RUN_STAMP}" / f"{next(_SEQ):02d}_{name}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_stamp() -> str:
    return _RUN_STAMP
