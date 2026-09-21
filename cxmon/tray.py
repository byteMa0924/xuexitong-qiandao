"""托盘图标：让"关掉面板后"的监控看得见、找得回。

为什么独立成一个进程：面板是可以关掉/重开的窗口，而托盘需要一个**一直在跑的宿主**。
拆开之后：
  · 关掉面板 → 托盘还在，你能看到"它还在跑"
  · 右键托盘 → 打开面板 / 停止监控 / 退出
  · 鼠标悬停 → 直接显示状态（监控中 · 今日命中 N 次 / 已停止）

纯 ctypes 调用 Windows 的 Shell_NotifyIcon，不需要任何第三方库。
"""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from ctypes import wintypes as w
from pathlib import Path

from . import runtime

log = logging.getLogger("cxmon.tray")

ENTRY = Path(__file__).resolve().parent.parent / "cxmon.py"
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32


# ---------------------------------------------------------------- 函数原型
# ⚠️ 必须显式声明：不声明的话 ctypes 按 32 位 int 推断参数，
# 窗口消息里 lParam 常常是指针大小的大整数，会抛 OverflowError，
# 结果是窗口消息处理静默失败（这次自检就是这么发现的）。
def _declare_prototypes() -> None:
    P, U = ctypes.c_void_p, w.UINT
    user32.DefWindowProcW.argtypes = [P, U, P, P]
    user32.DefWindowProcW.restype = P
    user32.CreateWindowExW.argtypes = [w.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, w.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       P, P, P, P]
    user32.CreateWindowExW.restype = P
    user32.RegisterClassExW.argtypes = [P]
    user32.RegisterClassExW.restype = ctypes.c_ushort
    user32.LoadImageW.argtypes = [P, ctypes.c_wchar_p, U, ctypes.c_int, ctypes.c_int, U]
    user32.LoadImageW.restype = P
    user32.LoadIconW.argtypes = [P, ctypes.c_wchar_p]
    user32.LoadIconW.restype = P
    user32.DestroyWindow.argtypes = [P]
    user32.DestroyWindow.restype = w.BOOL
    user32.SetTimer.argtypes = [P, U, U, P]
    user32.SetTimer.restype = U
    user32.KillTimer.argtypes = [P, U]
    user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), P, U, U]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostMessageW.argtypes = [P, U, P, P]
    user32.PostMessageW.restype = w.BOOL
    user32.CreatePopupMenu.argtypes = []
    user32.CreatePopupMenu.restype = P
    user32.AppendMenuW.argtypes = [P, U, P, ctypes.c_wchar_p]
    user32.AppendMenuW.restype = w.BOOL
    user32.TrackPopupMenu.argtypes = [P, U, ctypes.c_int, ctypes.c_int, ctypes.c_int, P, P]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.DestroyMenu.argtypes = [P]
    user32.DestroyMenu.restype = w.BOOL
    user32.SetForegroundWindow.argtypes = [P]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
    user32.GetCursorPos.restype = w.BOOL
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = P
    user32.FindWindowExW.argtypes = [P, P, ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowExW.restype = P
    user32.ShowWindow.argtypes = [P, ctypes.c_int]
    user32.ShowWindow.restype = w.BOOL
    user32.IsWindowVisible.argtypes = [P]
    user32.IsWindowVisible.restype = w.BOOL
    shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = w.BOOL
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = P

# ---- 常量 ----
WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 1
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202          # 单击（用户要求：单击就打开面板）
WM_LBUTTONDBLCLK = 0x0203
SW_RESTORE = 9
WINDOW_TITLE = "学习通签到监控"
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
IDI_APPLICATION = 32512
TIMER_ID = 1
TOOLTIP_BASE = "学习通签到监控"


class NOTIFYICONDATAW(ctypes.Structure):
    """必须是完整的 SDK 结构 —— Windows 会校验 cbSize。"""
    _fields_ = [
        ("cbSize", w.DWORD),
        ("hWnd", ctypes.c_void_p),
        ("uID", w.UINT),
        ("uFlags", w.UINT),
        ("uCallbackMessage", w.UINT),
        ("hIcon", ctypes.c_void_p),
        ("szTip", w.WCHAR * 128),
        ("dwState", w.DWORD),
        ("dwStateMask", w.DWORD),
        ("szInfo", w.WCHAR * 256),
        ("uVersion", w.UINT),
        ("szInfoTitle", w.WCHAR * 64),
        ("dwInfoFlags", w.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, w.UINT,
                             ctypes.c_void_p, ctypes.c_void_p)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.UINT),
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", ctypes.c_void_p),
    ]


def _today_hits(cfg: dict) -> int:
    """今日本工具提醒过几次（只数状态文件里今天的记录，避免依赖面板模块）。"""
    path = Path(cfg.get("_dir") or ".") / str(cfg.get("state_file") or "state.json")
    try:
        seen = (json.loads(path.read_text(encoding="utf-8-sig") or "{}") or {}).get("seen") or {}
    except (OSError, ValueError):
        return 0
    today, count = dt.date.today(), 0
    for record in seen.values():
        if isinstance(record, dict) and record.get("first_seen"):
            try:
                if dt.datetime.fromtimestamp(float(record["first_seen"])).date() == today:
                    count += 1
            except (ValueError, OSError, OverflowError):
                pass
    return count


_declare_prototypes()


def focus_window_by_title(title: str = WINDOW_TITLE, attempts: int = 20) -> bool:
    """把已经打开的**可见**面板窗口显示到最前面。

    两个坑（都是实测踩出来的）：
      1. 托盘进程也有一个同名窗口（隐藏的宿主窗口），所以必须过滤"不可见"的；
      2. 第二个实例启动时，第一个可能还没来得及画出窗口，所以要重试几百毫秒。
    """
    for _ in range(max(1, attempts)):
        hwnd = user32.FindWindowW(None, title)
        while hwnd:
            if user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetForegroundWindow(hwnd)
                return True
            # 同名但不可见（大概率是托盘的隐藏宿主窗口），继续找下一个
            hwnd = user32.FindWindowExW(None, hwnd, None, title)
        time.sleep(0.15)
    return False


class TrayIcon:
    """托盘图标 + 右键菜单。menu 动作通过 actions 队列交给外部处理。"""

    def __init__(self, cfg: dict, icon_path: Path | None = None):
        self.cfg = cfg
        self.icon_path = icon_path or (Path(__file__).resolve().parent.parent / "app-icon.ico")
        self.actions: queue.Queue[str] = queue.Queue()
        self.hwnd = None
        self.hicon = None
        self._wndproc_ref = None        # 必须留引用，否则回调被 GC 掉会崩
        self._running = False
        self._last_tip = ""

    # ------------------------------------------------------------- 低层封装

    def _load_icon(self):
        if self.icon_path.exists():
            handle = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if handle:
                return handle
            log.warning("图标加载失败，用系统默认图标：%s", self.icon_path)
        return user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))

    def _nid(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = WM_TRAY_CALLBACK
        data.hIcon = self.hicon
        data.szTip = self._last_tip or TOOLTIP_BASE
        return data

    def _notify(self, message: int, flags: int) -> bool:
        data = self._nid(flags)
        ok = bool(shell32.Shell_NotifyIconW(message, ctypes.byref(data)))
        if not ok:
            log.warning("Shell_NotifyIcon 调用失败（message=%s, 错误码=%s）",
                        message, kernel32.GetLastError())
        return ok

    def update_tooltip(self, text: str) -> None:
        text = text[:127]
        if text == self._last_tip:
            return
        self._last_tip = text
        if self._running:
            self._notify(NIM_MODIFY, NIF_TIP | NIF_ICON)

    # ---------------------------------------------------------------- 回调

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY_CALLBACK:
                event = int(lparam or 0) & 0xFFFF
                if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                elif event == WM_LBUTTONUP:      # 单击就打开面板（按用户要求）
                    self.actions.put("open")
            elif msg == WM_TIMER and wparam == TIMER_ID:
                self.actions.put("refresh")
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
        except Exception as exc:                      # 回调里绝不能抛异常
            log.exception("托盘回调异常：%s", exc)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self) -> None:
        running = bool(runtime.read_pid(self.cfg))
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, 1, "打开面板（也可以直接单击图标）")
        user32.AppendMenuW(menu, MF_STRING, 2, "停止监控" if running else "开始监控")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 3, "退出（停止监控并关闭托盘）")
        point = w.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)         # 不设的话菜单点外面不消失
        chosen = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                       point.x, point.y, 0, self.hwnd, None)
        user32.DestroyMenu(menu)
        if chosen == 1:
            self.actions.put("open")
        elif chosen == 2:
            self.actions.put("stop" if running else "start")
        elif chosen == 3:
            self.actions.put("quit")

    # ------------------------------------------------------------ 生命周期

    def start(self) -> bool:
        """创建隐藏窗口并挂上托盘图标；返回是否成功（不阻塞）。"""
        hinst = kernel32.GetModuleHandleW(None)
        class_name = f"cxmonTrayWnd_{os.getpid()}"
        self._wndproc_ref = WNDPROC(self._on_message)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            log.warning("RegisterClassEx 失败，错误码=%s", kernel32.GetLastError())
        self.hwnd = user32.CreateWindowExW(0, class_name, TOOLTIP_BASE, 0,
                                           0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            log.error("CreateWindowEx 失败，错误码=%s", kernel32.GetLastError())
            return False
        self.hicon = self._load_icon()
        self._running = True
        if not self._notify(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP):
            self._running = False
            return False
        user32.SetTimer(self.hwnd, TIMER_ID, 2000, None)   # 每 2 秒刷新提示
        log.info("托盘图标已显示")
        return True

    def run(self) -> int:
        """阻塞式消息循环（在专属进程里跑）。"""
        if not self.start():
            return 1
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        return 0

    def stop(self) -> None:
        if self._running:
            self._notify(NIM_DELETE, 0)
            self._running = False
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        log.info("托盘图标已移除")


# ------------------------------------------------------------------ 主流程


def _start_panel(cfg: dict) -> None:
    """打开面板：已经有窗口就把它提到最前面，否则开一个新的。"""
    if focus_window_by_title():
        log.info("面板窗口已存在，已把它提到最前面")
        return
    subprocess.Popen([sys.executable, str(ENTRY), "panel"], cwd=str(ENTRY.parent),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=_CREATE_NO_WINDOW)


def _start_monitor(cfg: dict) -> None:
    if runtime.read_pid(cfg):
        return
    subprocess.Popen([sys.executable, str(ENTRY), "monitor"], cwd=str(ENTRY.parent),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=_CREATE_NO_WINDOW)
    log.info("已按托盘菜单开始监控")


def _stop_monitor(tray: TrayIcon) -> None:
    pid = runtime.read_pid(tray.cfg)
    if not pid:
        tray.update_tooltip(f"{TOOLTIP_BASE} · 已停止")
        return
    runtime.stop_pid(pid)
    runtime.clear_pid(tray.cfg, pid)
    log.info("已按托盘菜单停止监控（PID %s）", pid)


def _tooltip(cfg: dict) -> str:
    pid = runtime.read_pid(cfg)
    if pid:
        return f"{TOOLTIP_BASE} · 监控中（今日命中 {_today_hits(cfg)} 次）"
    return f"{TOOLTIP_BASE} · 已停止（右键可开始）"


def main(cfg: dict, selftest: bool = False) -> int:
    # 单实例：命名互斥体，避免出现两个托盘图标
    lock = None
    if not selftest:
        lock = runtime.acquire_single_instance("tray")
        if lock is None:
            log.info("托盘已经在运行，本次退出")
            return 0
    runtime.write_tray_pid(cfg)

    tray = TrayIcon(cfg)
    if selftest:
        ok = tray.start()
        print("托盘图标注册:", "成功" if ok else "失败")
        print("窗口句柄:", tray.hwnd)
        tray.update_tooltip(_tooltip(cfg))
        print("工具提示:", tray._last_tip)
        print("菜单动作分发测试:", end=" ")
        tray.actions.put("open")
        print(tray.actions.get())
        tray.stop()
        runtime.clear_tray_pid(cfg)
        return 0 if ok else 1

    tray.update_tooltip(_tooltip(cfg))

    def handle_actions() -> None:
        while True:
            action = tray.actions.get()
            try:
                if action == "refresh":
                    tray.update_tooltip(_tooltip(cfg))
                elif action == "open":
                    _start_panel(cfg)
                elif action == "stop":
                    _stop_monitor(tray)
                    tray.update_tooltip(_tooltip(cfg))
                elif action == "start":
                    _start_monitor(cfg)
                    time.sleep(1)
                    tray.update_tooltip(_tooltip(cfg))
                elif action == "quit":
                    _stop_monitor(tray)
                    break
            except Exception as exc:
                log.exception("处理托盘动作失败：%s", exc)
        tray.stop()
        user32.PostMessageW(tray.hwnd, WM_DESTROY, 0, 0)

    threading.Thread(target=handle_actions, name="cxmon-tray-actions", daemon=True).start()
    try:
        return tray.run()
    except Exception as exc:                       # 托盘崩了要在日志里留下痕迹
        log.exception("托盘消息循环异常退出：%s", exc)
        return 1
    finally:
        runtime.clear_tray_pid(cfg)
        log.info("托盘进程结束")
