"""运行期状态：监控进程的 PID 文件与存活判断。

用途：
  * 防止重复启动（两个监控 = 同一场签到提醒两遍、微信推两条）
  * 让图形面板知道"现在到底有没有在监控"，不管是谁启动的
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from pathlib import Path

STILL_ACTIVE = 259
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 单实例用命名互斥体：进程退出（哪怕崩溃）系统会自动释放，既没有竞态也没有残留文件。
# 之前用"查找窗口"判断，第二个实例在第一个还没画出窗口时会误判，结果开出两份。
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
_mutex_handles: list = []


def _mutex_name(name: str) -> str:
    return f"Local\\cxmon_{name}"


def acquire_single_instance(name: str):
    """抢占单实例锁：成功返回句柄（要一直留着），已有实例在跑则返回 None。"""
    if os.name != "nt":
        return object()
    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    _kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = _kernel32.CreateMutexW(None, 1, _mutex_name(name))
    if not handle:
        return object()                      # 拿不到锁也别拦着用户用
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None
    _mutex_handles.append(handle)            # 留住引用，进程活着期间一直持有
    return handle


def instance_running(name: str) -> bool:
    """另一个进程是否持有该单实例锁。"""
    if os.name != "nt":
        return False
    _kernel32.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
    _kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = _kernel32.OpenMutexW(SYNCHRONIZE, 0, _mutex_name(name))
    if not handle:
        return False
    _kernel32.CloseHandle(handle)
    return True


def pid_file(cfg: dict) -> Path:
    return Path(cfg.get("_dir") or ".") / "monitor.pid"


def tray_pid_file(cfg: dict) -> Path:
    return Path(cfg.get("_dir") or ".") / "tray.pid"


def process_alive(pid: int) -> bool:
    """进程是否真的还在（不只看 PID 文件里有没有内容）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)     # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid(cfg: dict) -> int | None:
    """返回正在运行的监控进程 PID；进程已消失则顺手清理残留文件。"""
    return _read_pid_file(pid_file(cfg))


def _read_pid_file(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if process_alive(pid):
        return pid
    try:
        path.unlink()
    except OSError:
        pass
    return None


def read_tray_pid(cfg: dict) -> int | None:
    return _read_pid_file(tray_pid_file(cfg))


def write_tray_pid(cfg: dict) -> None:
    _write_pid_file(tray_pid_file(cfg))


def clear_tray_pid(cfg: dict, pid: int | None = None) -> None:
    _clear_pid_file(tray_pid_file(cfg), pid)


def _write_pid_file(path: Path) -> None:
    try:
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _clear_pid_file(path: Path, pid: int | None = None) -> None:
    if pid is not None:
        try:
            if int(path.read_text(encoding="utf-8").strip()) != int(pid):
                return
        except (OSError, ValueError):
            return
    try:
        path.unlink()
    except OSError:
        pass


def write_pid(cfg: dict) -> Path:
    path = pid_file(cfg)
    _write_pid_file(path)
    return path


def clear_pid(cfg: dict, pid: int | None = None) -> None:
    """删除 PID 文件；传入 pid 时只在文件里记录的正是自己时才删。"""
    _clear_pid_file(pid_file(cfg), pid)


def stop_pid(pid: int, timeout: float = 6.0) -> bool:
    """结束监控进程。强杀是安全的：去重记录在每次提醒时就已经落盘了。"""
    if not process_alive(pid):
        return True
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"],
                           capture_output=True, timeout=15,
                           creationflags=_CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return False
    else:
        import signal
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.2)
    return not process_alive(pid)
