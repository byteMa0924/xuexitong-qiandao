"""图形控制面板（tkinter，Python 自带，零依赖）。

双击 启动监控.bat 打开这个窗口：
  · 一眼看出「监控中 / 已停止」
  · 一个按钮开始/停止，不用记任何命令
  · 顺带显示今日命中次数、最近检测时间、Cookie 状态、最近日志

面板和监控是**两个进程**：关掉面板不影响监控，重开面板照样能看到状态。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import runtime
from .paths import self_command, subprocess_cwd

log = logging.getLogger("cxmon.panel")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
GREEN = "#1a7f37"
GRAY = "#8a8a8a"
RED = "#b42318"
UI_FONT = "Microsoft YaHei UI"


# ------------------------------------------------------------------ 状态读取


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def today_hits(cfg: dict) -> tuple[int, str]:
    """今日命中次数 + 最近一次命中的时间。"""
    state = _read_json(Path(cfg.get("_dir") or ".") / str(cfg.get("state_file") or "state.json"))
    seen = state.get("seen") or {}
    today = dt.date.today()
    count = 0
    latest = 0.0
    for record in seen.values():
        if not isinstance(record, dict):
            continue
        stamp = record.get("first_seen")
        if not stamp:
            continue
        try:
            when = dt.datetime.fromtimestamp(float(stamp))
        except (ValueError, OSError, OverflowError):
            continue
        if when.date() == today:
            count += 1
            latest = max(latest, float(stamp))
    return count, (dt.datetime.fromtimestamp(latest).strftime("%H:%M:%S") if latest else "—")


def log_tail(cfg: dict, lines: int = 7) -> list[str]:
    """日志末尾几行，去掉又长又吵的前缀，只留时间 + 内容。"""
    path = Path(cfg.get("_dir") or ".") / str(cfg.get("log_file") or "monitor.log")
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in content[-60:]:
        if " | " in line:
            head, _, message = line.partition(" | ")
            stamp = head[11:19] if len(head) >= 19 else head
            out.append(f"{stamp}  {message}")
        else:
            out.append(line)
    return out[-lines:]


def last_heartbeat(cfg: dict) -> str:
    path = Path(cfg.get("_dir") or ".") / str(cfg.get("log_file") or "monitor.log")
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "—"
    for line in reversed(content[-400:]):
        if "心跳" in line:
            import re
            match = re.search(r"已扫描 (\d+) 轮 / (\d+) 个班级", line)
            if match:
                return f"{match.group(1)} 轮 / {match.group(2)} 个班"
            return line[-40:]
    return "—"


def cookie_label(cfg: dict) -> str:
    cookie = cfg.get("cookie") or ""
    if not cookie:
        return "未配置（点「更新 Cookie」）"
    jar = {}
    for part in cookie.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            jar[key.strip()] = value.strip()
    uid = cfg.get("uid") or jar.get("_uid") or "?"
    fields = ",".join(c for c in ("_uid", "fid", "vc3", "_d") if jar.get(c))
    return f"已配置  uid={uid}  含 {fields or '无关键字段'}"


# --------------------------------------------------------------------- 面板


class Panel:
    def __init__(self, root: tk.Tk, cfg: dict):
        self.root = root
        self.cfg = cfg
        self.busy = False
        root.title("学习通签到监控")
        root.geometry("470x498")
        root.minsize(430, 380)

        outer = ttk.Frame(root, padding=(18, 16))
        outer.pack(fill="both", expand=True)

        # —— 状态大字 ——
        self.state_var = tk.StringVar(value="正在读取状态 …")
        self.state_lbl = ttk.Label(outer, textvariable=self.state_var,
                                   font=(UI_FONT, 23, "bold"), foreground=GRAY)
        self.state_lbl.pack(anchor="w")

        self.sub_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.sub_var, font=(UI_FONT, 10),
                  foreground="#666666").pack(anchor="w", pady=(2, 16))

        # —— 按钮：主按钮占满一行（一眼看得到该点哪儿），次要按钮等宽并排 ——
        self.toggle_btn = ttk.Button(outer, text="▶   开始监控", command=self.toggle,
                                     padding=(10, 9))
        self.toggle_btn.pack(fill="x")

        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="测试提醒", command=self.test_alert).pack(
            side="left", expand=True, fill="x")
        ttk.Button(bar, text="更新 Cookie", command=self.update_cookie).pack(
            side="left", expand=True, fill="x", padx=(8, 0))

        # —— 运行信息：默认收起，点小按钮才展开 ——
        #   原来一打开面板就摊开 6 行技术信息，很不好看。收起后界面只剩
        #   「状态 + 按钮 + 最近记录」，要排查时再点开。
        toggle_row = ttk.Frame(outer)
        toggle_row.pack(fill="x", pady=(14, 4))
        self.info_btn = ttk.Button(toggle_row, text="▸  运行信息", style="Toolbutton",
                                   takefocus=False, command=self.toggle_info)
        self.info_btn.pack(side="left")
        self.info_visible = False

        self.info_frame = ttk.LabelFrame(outer, text="运行信息", padding=(12, 8))
        self.info_frame.columnconfigure(1, weight=1)
        self.info_vars: dict[str, tk.StringVar] = {}
        rows = [
            ("today", "今日命中"),
            ("latest", "最近一次检测"),
            ("cookie", "Cookie"),
            ("alert", "提醒方式"),
            ("scan", "扫描设置"),
            ("beat", "最近心跳"),
        ]
        for row, (key, label) in enumerate(rows):
            ttk.Label(self.info_frame, text=label + "：", font=(UI_FONT, 10)).grid(
                row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value="—")
            self.info_vars[key] = var
            ttk.Label(self.info_frame, textvariable=var, font=(UI_FONT, 10),
                      foreground="#222222", wraplength=300, justify="left").grid(
                row=row, column=1, sticky="w", pady=2)

        # —— 日志 ——
        self.log_frame = ttk.LabelFrame(outer, text="最近记录", padding=8)
        self.log_frame.pack(fill="both", expand=True, pady=(2, 0))
        self.log_text = tk.Text(self.log_frame, height=5, wrap="word", relief="flat",
                                background="#fbfbfb", foreground="#444444",
                                font=("Consolas", 9), padx=4, pady=2)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

        # —— 文件在哪：一键打开项目文件夹（配置、日志都在里面）——
        path_row = ttk.Frame(outer)
        path_row.pack(fill="x", pady=(10, 0))
        ttk.Button(path_row, text="打开项目文件夹", takefocus=False,
                   command=self.open_folder).pack(side="left")
        self.path_var = tk.StringVar(value=str(cfg.get("_dir") or ""))
        ttk.Label(path_row, textvariable=self.path_var, font=(UI_FONT, 8),
                  foreground="#888888").pack(side="left", padx=(10, 0))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        # 固定一个紧凑的初始尺寸：日志框有 expand=True 会吃掉剩余空间，
        # 所以窗口越小它越紧凑，不会出现一大片空白。
        root.geometry("470x402")
        self.refresh()

    # ------------------------------------------------------------- 状态刷新

    def toggle_info(self) -> None:
        """展开 / 收起「运行信息」。默认收起，界面干净一些。

        展开时让**窗口长高**而不是挤压日志框 —— 日志框有 expand=True，
        如果窗口高度不变，展开信息会把日志压到只剩几个像素。
        """
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        need = max(120, self.info_frame.winfo_reqheight())
        if self.info_visible:
            self.info_frame.pack_forget()
            self.info_btn.configure(text="▸  运行信息")
            self.root.geometry(f"{width}x{max(360, height - need)}")
        else:
            self.info_frame.pack(fill="x", pady=(0, 6), before=self.log_frame)
            self.info_btn.configure(text="▾  收起运行信息")
            self.root.geometry(f"{width}x{height + need}")
        self.info_visible = not self.info_visible

    def snapshot(self) -> dict:
        cfg = self.cfg
        pid = runtime.read_pid(cfg)
        hits, latest = today_hits(cfg)
        alert = cfg.get("alert") or {}
        channels = [name for name, on in (
            ("语音", alert.get("voice", True)),
            ("提示音", alert.get("beep", True)),
            ("弹窗", alert.get("toast", True)),
            ("微信", bool(alert.get("webhook"))),
        ) if on]
        targets = cfg.get("targets") or []
        scope = f"{len(targets)} 个指定班级" if targets else "全部班级"
        return {
            "pid": pid,
            "running": bool(pid),
            "button": "停止监控" if pid else "开始监控",
            "today_hits": hits,
            "latest": latest,
            "cookie": cookie_label(cfg),
            "channels": " + ".join(channels) or "（无）",
            "scan": f"每 {cfg.get('poll_interval')} 秒一轮，并发 {cfg.get('scan_workers') or 6} 路，{scope}",
            "beat": last_heartbeat(cfg),
            "log": log_tail(cfg),
        }

    def refresh(self) -> None:
        snap = self.snapshot()
        if snap["running"]:
            self.state_var.set("●  监控中")
            self.state_lbl.configure(foreground=GREEN)
            self.sub_var.set(f"进程 PID {snap['pid']} · 正在盯着你的课程")
        else:
            self.state_var.set("○  已停止")
            self.state_lbl.configure(foreground=GRAY)
            self.sub_var.set("点下面的「开始监控」即可开始")

        self.info_vars["today"].set(f"{snap['today_hits']} 次")
        self.info_vars["latest"].set(snap["latest"])
        self.info_vars["cookie"].set(snap["cookie"])
        self.info_vars["alert"].set(snap["channels"])
        self.info_vars["scan"].set(snap["scan"])
        self.info_vars["beat"].set(snap["beat"])

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(snap["log"]) or "（还没有日志）")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

        # 主按钮文案永远跟着真实状态走，且永远可点（重复点击由 busy 挡住），
        # 这样不可能再出现"标签和按钮互相矛盾"
        self.toggle_btn.configure(text="■  停止监控" if snap["running"] else "▶  开始监控")
        if self.busy:
            self.sub_var.set("正在处理，请稍候 …")

        self.root.after(2000, self.refresh)

    # --------------------------------------------------------------- 动作

    def toggle(self) -> None:
        """主按钮：没在跑就启动，在跑就停止。永远可点，重入由 busy 挡住。"""
        if self.busy:
            return
        if runtime.read_pid(self.cfg):
            self.stop_monitor()
        else:
            self.start_monitor()

    def start_monitor(self) -> None:
        if runtime.read_pid(self.cfg):
            self.refresh()
            return
        self.busy = True
        problem = ""
        try:
            subprocess.Popen(
                self_command("monitor"),
                cwd=subprocess_cwd(), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW,
            )
            # 等它写下 PID 文件（实测 0.26 秒），最多等 10 秒
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if runtime.read_pid(self.cfg):
                    break
                self.root.update()
                time.sleep(0.15)
            if not runtime.read_pid(self.cfg):
                problem = ("监控进程没有起来。\n\n常见原因：Cookie 已失效或未配置。\n"
                           "点「更新 Cookie」重新获取；具体原因见窗口下方「最近记录」。")
        except OSError as exc:
            problem = f"没能启动监控进程：\n{exc}"
        except Exception as exc:                    # 面板绝不能因为异常卡死
            log.exception("启动监控异常：%s", exc)
            problem = f"启动过程中出错：\n{exc}"
        finally:
            self.busy = False                      # 无论成败都复位，绝不冻结界面
        self.refresh()
        if problem:
            messagebox.showwarning("启动失败", problem)

    def stop_monitor(self) -> None:
        pid = runtime.read_pid(self.cfg)
        if not pid:
            self.refresh()
            return
        if not messagebox.askyesno("停止监控", "确定要停止监控吗？\n\n"
                                              "停止后将不再检测签到，直到你重新开始。"):
            return
        self.busy = True
        problem = ""
        try:
            if not runtime.stop_pid(pid):
                problem = f"没能结束进程 {pid}，请在任务管理器里手动结束。"
            runtime.clear_pid(self.cfg, pid)
        except Exception as exc:
            log.exception("停止监控异常：%s", exc)
            problem = f"停止过程中出错：\n{exc}"
        finally:
            self.busy = False
        self.refresh()
        if problem:
            messagebox.showerror("停止失败", problem)

    def test_alert(self) -> None:
        try:
            subprocess.Popen(self_command("test-alert", "--repeat", "1"),
                             cwd=subprocess_cwd(), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW)
        except OSError as exc:
            messagebox.showerror("测试失败", str(exc))
            return
        messagebox.showinfo("测试提醒", "已经发出一次测试提醒：\n\n"
                                       "· 电脑会念一句话 + 响一声 + 右下角弹窗\n"
                                       "· 如果你配了微信推送，微信也会收到")

    def update_cookie(self) -> None:
        """自动读学习通客户端的 Cookie；失败就提示走手动方式。"""
        if not messagebox.askyesno(
                "更新 Cookie",
                "将自动从「学习通 PC 客户端」读取登录信息。\n\n"
                "前提：客户端里登录着学习通，且客户端已关闭。\n\n继续吗？"):
            return
        result = subprocess.run(
            self_command("browser-cookie", "--browser", "cxstudy"),
            cwd=subprocess_cwd(), capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace", creationflags=_CREATE_NO_WINDOW)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0:
            messagebox.showinfo("更新 Cookie", "已更新并验证成功！\n\n" + output[-600:])
            fresh = _reload(self.cfg)      # 必须在 clear 之前读，否则配置文件路径会丢
            self.cfg.clear()
            self.cfg.update(fresh)
        else:
            messagebox.showwarning(
                "没能自动更新",
                "自动读取失败（可能客户端没登录，或读不到）。\n\n"
                "可以改用手动方式：\n"
                "  1) 浏览器 F12 → 网络 → F5 → 选第一条请求\n"
                "  2) 标头 → 请求标头 → 复制 cookie: 那一行\n"
                "  3) 运行：python cxmon.py cookie  然后粘贴\n\n"
                + output[-400:])
        self.refresh()

    def open_folder(self) -> None:
        """在资源管理器里打开项目文件夹 —— 配置、日志、状态文件都在里面。"""
        target = self.cfg.get("_dir") or ""
        try:
            os.startfile(target)          # noqa: S606  Windows 专用
        except OSError as exc:
            messagebox.showerror("打不开文件夹", f"{target}\n\n{exc}")

    # ------------------------------------------------------------- 托盘

    def ensure_tray(self) -> bool:
        """确保右下角有托盘图标 —— 它就是"这个工具还在后台跑"的落脚点。"""
        if runtime.instance_running("tray"):
            return True
        try:
            subprocess.Popen(self_command("tray"), cwd=subprocess_cwd(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=_CREATE_NO_WINDOW)
        except OSError as exc:
            log.warning("启动托盘失败：%s", exc)
            return False
        for _ in range(30):                       # 最多等 3 秒
            if runtime.instance_running("tray"):
                return True
            time.sleep(0.1)
        return runtime.instance_running("tray")

    def stop_tray(self) -> None:
        pid = runtime.read_tray_pid(self.cfg)
        if pid:
            runtime.stop_pid(pid)
            runtime.clear_tray_pid(self.cfg, pid)

    def on_close(self) -> None:
        """关窗口。

        注意：这里**不会隐藏窗口**。之前那版"隐藏窗口 + 定时叫回来"和关闭对话框互相打架，
        实测会出现"叫回来 4 秒后又被隐藏"。现在改成：关窗口 = 这个面板进程退出，
        托盘图标负责留在右下角，需要时点它重新开面板。
        """
        if runtime.read_pid(self.cfg):
            answer = messagebox.askyesnocancel(
                "关闭面板",
                "监控还在运行，你想怎么办？\n\n"
                "「是」= 停止监控并完全退出（右下角托盘图标也一起消失）\n\n"
                "「否」= 只关这个窗口，监控继续跑。\n"
                "        右下角的托盘图标会留着：单击它就能重新打开面板。\n\n"
                "「取消」= 什么都不做")
            if answer is None:
                return
            if answer:
                pid = runtime.read_pid(self.cfg)
                if pid:
                    runtime.stop_pid(pid)
                    runtime.clear_pid(self.cfg, pid)
                self.stop_tray()
            else:
                self.ensure_tray()                 # 保证关掉后有个看得见的入口
                log.info("面板关闭，监控与托盘继续运行")
        else:
            self.stop_tray()
        self.root.destroy()


def _reload(cfg: dict) -> dict:
    from .config import load_config
    return load_config(cfg.get("_path"))


def main(cfg: dict, selftest: bool = False) -> int:
    # 单实例：用命名互斥体抢锁（无竞态、进程退出自动释放）。
    # 拿不到就说明已经有一个面板了 —— 把它的窗口提到最前面，然后自己退出。
    lock = None
    if not selftest:
        lock = runtime.acquire_single_instance("panel")
        if lock is None:
            from .tray import focus_window_by_title
            if focus_window_by_title():
                print("面板已经开着，已把它切换到最前面。")
            else:
                print("面板已经开着（没找到它的窗口，可能被最小化了）。")
            return 0

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"[失败] 无法创建窗口（{exc}）。如果没有桌面环境，请用命令行："
              f"python cxmon.py monitor", file=sys.stderr)
        return 1
    try:
        app = Panel(root, cfg)
    except Exception as exc:                       # GUI 起不来也要让用户看到原因
        log.exception("面板初始化失败：%s", exc)
        try:
            messagebox.showerror("面板启动失败", str(exc))
        except tk.TclError:
            print(f"[失败] {exc}", file=sys.stderr)
        return 1

    if selftest:
        root.update_idletasks()
        root.update()
        for key, value in app.snapshot().items():
            if key == "log":
                print("log:")
                for line in value:
                    print("   ", line)
            else:
                print(f"{key}: {value}")
        root.destroy()
        return 0

    app.ensure_tray()          # 面板一打开，托盘图标就在，关窗口后它就是入口
    root.mainloop()
    return 0
