"""路径解析：让同一份代码在「源码运行」和「打包成 exe」两种方式下都对。

打包（PyInstaller）之后有两件事会变，必须在这里统一处理，否则会出很隐蔽的 bug：

  1. ``__file__`` 指向解包出来的临时目录（``sys._MEIPASS``）。
     如果 config.json / state.json / monitor.log 还按 ``__file__`` 去找，
     就会写进临时目录，程序一退出就被清空 —— 表现为「配置填了、重启就没了」。

  2. ``cxmon.py`` 这个入口文件在 exe 里不存在。
     托盘、面板原来是用 ``[sys.executable, "cxmon.py", "tray"]`` 拉起子进程的，
     打包后必须变成 ``[exe 自己, "tray"]``。

所以规矩是：
  * 可写文件（配置/状态/日志/pid）用 :func:`app_dir`，永远在 exe 旁边；
  * 随程序分发的只读资源（.ps1 / .ico）用 :func:`resource`，可能被解包到临时目录；
  * 要重新拉起自己，用 :func:`self_command`。
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """可写目录：exe 所在目录（打包后）或项目根目录（源码运行）。

    配置、状态文件、日志、pid 文件都放这里 —— 用户能看见、能备份、能改。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource(name: str) -> Path:
    """随程序分发的只读资源（toast.ps1、voice_worker.ps1、app-icon.ico）。

    打包后这些文件在 ``sys._MEIPASS`` 里；源码运行时就在 ``cxmon/`` 目录下。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / name
    return Path(__file__).resolve().parent / name


def self_command(*args: str) -> list[str]:
    """重新拉起自己的命令行（托盘、面板、监控子进程都靠它）。

    源码运行：``python cxmon.py <args>``
    打包运行：``cxmon.exe <args>``（没有 cxmon.py 这个文件了）
    """
    if is_frozen():
        return [sys.executable, *args]
    return [sys.executable, str(app_dir() / "cxmon.py"), *args]


def subprocess_cwd() -> str:
    """子进程的工作目录：永远是 app_dir，保证配置文件位置一致。"""
    return str(app_dir())


def default_icon() -> Path:
    """托盘图标：优先用 exe/项目根目录旁边的，找不到就用打包进去的那份。

    源码运行时图标在项目根目录；打包后它被放进 _MEIPASS，
    所以两个位置都要试。
    """
    beside = app_dir() / "app-icon.ico"
    if beside.exists():
        return beside
    return resource("app-icon.ico")
