#!/usr/bin/env python
"""学习通签到监控 —— 命令行入口。

用法示例：
    python cxmon.py doctor --speak     # 环境自检 + 试听语音
    python cxmon.py cookie             # 粘贴 Cookie
    python cxmon.py monitor            # 开始监控
    python cxmon.py monitor --mock     # 离线演示（不需要 Cookie）

只做「检测 + 提醒」，不代替签到。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cxmon.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
