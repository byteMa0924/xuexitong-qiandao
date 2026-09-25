"""提醒通道：语音 / 提示音 / 气泡通知 / Webhook / 打开签到页。

语音走一个常驻的 Windows PowerShell 5.1 子进程（见 voice_worker.ps1）。
必须用 powershell.exe 而不是 pwsh：只有 .NET Framework 版的 PowerShell
自带 System.Speech 程序集，pwsh (PowerShell 7) 加载不了。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .paths import resource

log = logging.getLogger("cxmon.notify")

_HERE = Path(__file__).resolve().parent
# 打包成 exe 后这两个 .ps1 被解包到 _MEIPASS，用 resource() 找才找得到
VOICE_WORKER = resource("voice_worker.ps1")
TOAST_SCRIPT = resource("toast.ps1")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_powershell() -> str:
    """优先用 Windows PowerShell 5.1（有 System.Speech）。"""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.exists():
        return str(candidate)
    return "powershell.exe"


# --------------------------------------------------------------------- 语音


class VoiceSpeaker:
    """常驻 TTS 进程封装；线程安全，失败自动降级为一次性播放。"""

    def __init__(self, voice_name: str = "", rate: int = 1):
        self.voice_name = voice_name or ""
        self.rate = max(-10, min(10, int(rate or 0)))
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # -- 内部：启动 / 重启常驻进程
    def _ensure_proc(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        if not VOICE_WORKER.exists():
            log.error("找不到语音脚本：%s", VOICE_WORKER)
            return False
        args = [find_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(VOICE_WORKER), "-Rate", str(self.rate)]
        if self.voice_name:
            args += ["-Voice", self.voice_name]
        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
            )
            log.debug("语音进程已启动 pid=%s", self._proc.pid)
            return True
        except OSError as exc:
            log.error("启动语音进程失败：%s", exc)
            self._proc = None
            return False

    def speak(self, text: str) -> bool:
        """朗读一段文本。非阻塞（写进常驻进程的 stdin 就返回）。"""
        clean = " ".join(str(text or "").split())
        if not clean:
            return False
        with self._lock:
            if self._ensure_proc():
                try:
                    assert self._proc is not None and self._proc.stdin is not None
                    self._proc.stdin.write((clean + "\n").encode("utf-8"))
                    self._proc.stdin.flush()
                    return True
                except (OSError, ValueError) as exc:
                    log.warning("语音进程写入失败(%s)，改用一次性播放", exc)
                    self._kill_proc()
        # 降级：临时起一个 PowerShell 朗读（文本走环境变量，避免引号转义地狱）
        return self._speak_oneshot(clean)

    def _speak_oneshot(self, text: str) -> bool:
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"try {{ $s.Rate = {self.rate} }} catch {{}}; "
            "if ($env:CXMON_VOICE) { try { $s.SelectVoice($env:CXMON_VOICE) } catch {} }; "
            "$s.Speak($env:CXMON_TEXT); $s.Dispose()"
        )
        env = dict(os.environ, CXMON_TEXT=text, CXMON_VOICE=self.voice_name)
        try:
            subprocess.run(
                [find_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                env=env, timeout=60, creationflags=_CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("一次性语音播放失败：%s", exc)
            return False

    def speak_and_wait(self, text: str) -> bool:
        """同步朗读，念完才返回（自检 / test-alert 用）。"""
        clean = " ".join(str(text or "").split())
        if not clean:
            return False
        return self._speak_oneshot(clean)

    def _kill_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.write(b"__QUIT__\n")
                proc.stdin.flush()
                proc.stdin.close()
            proc.wait(timeout=3)
        except (OSError, ValueError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
        log.debug("语音进程已退出")


def list_voices(timeout: float = 20) -> list[str]:
    """列出系统可用发音人，返回 ["名称 | 语言", ...]。"""
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.GetInstalledVoices() | ForEach-Object { "
        "$_.VoiceInfo.Name + ' | ' + $_.VoiceInfo.Culture }"
    )
    try:
        out = subprocess.run(
            [find_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, timeout=timeout, creationflags=_CREATE_NO_WINDOW,
        )
        text = (out.stdout or b"").decode("utf-8", "replace")
        return [line.strip() for line in text.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("枚举发音人失败：%s", exc)
        return []


# --------------------------------------------------------------------- 提示音


def beep(times: int = 3) -> None:
    """在后台线程里响几声，不阻塞主循环。"""

    def _run():
        try:
            import winsound
        except ImportError:
            return
        for _ in range(max(1, times)):
            try:
                winsound.Beep(1000, 200)
                winsound.Beep(1500, 200)
            except (RuntimeError, OSError, ValueError):
                try:
                    winsound.MessageBeep(winsound.MB_ICONHAND)
                except (RuntimeError, OSError):
                    pass
                break

    threading.Thread(target=_run, name="cxmon-beep", daemon=True).start()


# ------------------------------------------------------------------ 气泡通知


def toast(title: str, message: str, seconds: int = 10) -> None:
    """右下角气泡通知，起独立进程，不等待。"""
    if not TOAST_SCRIPT.exists():
        log.debug("缺少 toast.ps1，跳过气泡通知")
        return
    args = [find_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(TOAST_SCRIPT), "-Title", title, "-Message", message, "-Seconds", str(seconds)]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=_CREATE_NO_WINDOW)
    except OSError as exc:
        log.debug("气泡通知失败：%s", exc)


# ------------------------------------------------------------------- Webhook


def send_webhook(url: str, title: str, body: str, timeout: float = 8) -> bool:
    """推送手机通知。

    - Server酱 Turbo（sctapi.ftqq.com/<key>.send）：推微信，用表单 title/desp
    - Server酱³（<uid>.push.ft07.com/send/<key>.send）：推自有客户端，用 JSON title/desp
    - 钉钉 / 企业微信机器人等：JSON {"msgtype":"text","text":{"content": ...}}

    返回值只表示"服务器接受了"，失败原因会写进日志（避免填错 key 却静默无声）。
    """
    if not url:
        return False
    content = f"{title}\n{body}"
    try:
        if "sctapi.ftqq.com" in url or "sc.ftqq.com" in url:
            data = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
            content_type = "application/x-www-form-urlencoded"
        elif "ft07.com" in url:
            data = json.dumps({"title": title, "desp": body},
                              ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            data = json.dumps({"msgtype": "text", "text": {"content": content}},
                              ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"

        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": content_type})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        log.debug("推送接口返回：%s", raw[:200])

        # 结果判定：Server酱 用 code，钉钉/企业微信 用 errcode，0 都表示成功
        try:
            result = json.loads(raw)
        except ValueError:
            result = None
        if isinstance(result, dict):
            code = result.get("code", result.get("errcode", 0))
            if str(code) not in ("0", "None"):
                log.warning("手机通知被拒绝（code=%s）：%s", code,
                            result.get("message") or result.get("errmsg")
                            or result.get("error") or raw[:120])
                return False
        log.info("已推送手机通知：%s", title)
        return True
    except (OSError, urllib.error.URLError, ValueError) as exc:
        log.warning("手机通知推送失败：%s", exc)
        return False


# -------------------------------------------------------------------- 打开网页


def open_in_browser(url: str) -> bool:
    """用系统默认浏览器打开签到页（签不签由用户自己决定）。"""
    if not url:
        return False
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            import webbrowser
            webbrowser.open(url)
        log.info("已打开签到页：%s", url)
        return True
    except OSError as exc:
        log.warning("打开浏览器失败：%s", exc)
        return False
