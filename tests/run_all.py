"""全量验证入口：依次运行核心链路测试与 GUI 冒烟测试。

用法：python tests/run_all.py
退出码 0 表示全部通过。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITES = [ROOT / "tests" / "test_core.py", ROOT / "tests" / "test_ui_smoke.py"]


def main() -> int:
    codes = []
    for suite in SUITES:
        print("\n" + "=" * 72)
        print(f"运行 {suite.name}")
        print("=" * 72)
        code = subprocess.call([sys.executable, str(suite)], cwd=str(ROOT))
        codes.append(code)
        print(f"{suite.name} 退出码 = {code}")
    print("\n" + "=" * 72)
    print("全量验证结果：", "通过" if all(c == 0 for c in codes) else "存在失败")
    print("=" * 72)
    return 0 if all(c == 0 for c in codes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
