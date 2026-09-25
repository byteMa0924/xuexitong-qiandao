"""打包成 exe 时的入口。

两件事：

1. **没有参数时直接开图形面板** —— 不懂命令行的人双击 exe 就该看到窗口，
   而不是一串英文帮助文本。

2. **补上 stdout/stderr** —— PyInstaller 用 --noconsole 打包后这两个是 None，
   而 cli.py 里有大量 print()，一执行就 AttributeError 崩掉。
   这里把它们换成"黑洞"，print 变成空操作而不是崩溃。

带参数运行时（例如 `cxmon.exe doctor`）行为与 `python cxmon.py doctor` 完全一致。
"""

from __future__ import annotations

import io
import sys


def _ensure_streams() -> None:
    """--noconsole 下 sys.stdout/stderr 是 None，给它一个能吞掉输出的替身。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, io.StringIO())


def main() -> int:
    _ensure_streams()
    argv = sys.argv[1:]
    from cxmon.cli import main as cli_main
    if not argv:
        # 双击 → 打开面板（面板是图形界面，不需要控制台）
        return cli_main(["panel"])
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
