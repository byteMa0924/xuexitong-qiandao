"""从本机 Chrome / Edge 的 Cookie 数据库里直接读出学习通 Cookie。

为什么需要它：手动去 DevTools 里翻 Cookie 太麻烦，尤其是 HttpOnly 的那几个字段
（vc3 等）在 Console 里用 document.cookie 根本看不到。

实现是纯标准库，不依赖 cryptography：
  * Cookie 数据库  -> 复制到临时目录后用 sqlite3 读（不锁库、不改浏览器数据）
  * 主密钥        -> crypt32.dll  CryptUnprotectData（DPAPI，只有当前 Windows 用户能解）
  * Cookie 密文   -> bcrypt.dll   AES-256-GCM（Windows CNG）

隐私边界：只筛选 host_key 属于 chaoxing.com / xuexitong.com 的记录，
其它网站的 Cookie 一律不读、不打印。所有输出都做掩码处理。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

# --------------------------------------------------------------------- 常量

BROWSERS = {
    # 学习通 PC 客户端是 Electron 程序，自带独立 Cookie 库，且不做 App-Bound 加密，
    # 所以它是最容易被自动读出来的来源 —— 放在最前面优先尝试。
    "cxstudy": ("学习通客户端", Path(os.environ.get("APPDATA", "")) / "cxstudy"),
    "chrome": ("Chrome", Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data"),
    "edge": ("Edge", Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/User Data"),
}
# 学习通相关域名（超星：chaoxing.com，学习通：xuexitong.com）
DOMAIN_FILTER = ("chaoxing.com", "xuexitong.com")

# 接口真正需要的字段，用于判断"这个 profile 的 Cookie 是不是有用的那个"
CORE_FIELDS = ("_uid", "fid", "vc3", "_d")


class CookieReadError(Exception):
    """读取/解密失败，附带给用户看的中文原因。"""


class LockedCookieDB(CookieReadError):
    """Cookie 数据库被正在运行的浏览器独占，读不了（关掉浏览器即可）。"""


PROCESS_NAMES = {"chrome": "chrome.exe", "edge": "msedge.exe", "cxstudy": "cxstudy.exe"}


def browser_process_count(browser_key: str) -> int:
    """该浏览器当前有多少进程在跑（-1 = 查不到）。"""
    exe = PROCESS_NAMES.get(browser_key)
    if not exe or os.name != "nt":
        return -1
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                             capture_output=True, timeout=15,
                             creationflags=0x08000000)
        text = out.stdout.decode("gbk", "replace").lower()
        return text.count(exe)
    except (OSError, subprocess.SubprocessError):
        return -1


def mask(value: str) -> str:
    """掩码显示，避免把凭证打进日志/终端。"""
    value = str(value or "")
    if len(value) <= 6:
        return "*" * len(value) if value else "(空)"
    return f"{value[:2]}{'*' * min(8, len(value) - 4)}{value[-2:]}(len={len(value)})"


# ------------------------------------------------------------------ DPAPI


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_unprotect(data: bytes) -> bytes:
    """CryptUnprotectData：解出当前用户专属密钥。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _DataBlob(len(data), ctypes.cast(ctypes.create_string_buffer(data),
                                               ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise CookieReadError(
            f"DPAPI 解密失败（错误码 {ctypes.GetLastError()}）："
            "通常是以其它用户身份运行导致的，请用你自己的账号运行"
        )
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ------------------------------------------------------------- AES-256-GCM


class _AuthCipherModeInfo(ctypes.Structure):
    """BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO（字段顺序必须与 Windows SDK 一致）。"""
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("dwInfoVersion", wintypes.ULONG),
        ("pbNonce", ctypes.c_void_p),
        ("cbNonce", wintypes.ULONG),
        ("pbAuthData", ctypes.c_void_p),
        ("cbAuthData", wintypes.ULONG),
        ("pbTag", ctypes.c_void_p),
        ("cbTag", wintypes.ULONG),
        ("pbMacContext", ctypes.c_void_p),
        ("cbMacContext", wintypes.ULONG),
        ("cbAAD", wintypes.ULONG),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", wintypes.ULONG),
    ]


def aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    """AES-256-GCM 解密，走 Windows CNG（bcrypt.dll），无第三方依赖。"""
    bcrypt = ctypes.windll.bcrypt
    h_alg = wintypes.HANDLE()
    h_key = wintypes.HANDLE()

    status = bcrypt.BCryptOpenAlgorithmProvider(
        ctypes.byref(h_alg), ctypes.c_wchar_p("AES"), None, 0)
    if status != 0:
        raise CookieReadError(f"BCryptOpenAlgorithmProvider 失败：0x{status & 0xFFFFFFFF:08X}")
    try:
        mode = "ChainingModeGCM".encode("utf-16-le") + b"\x00\x00"
        status = bcrypt.BCryptSetProperty(
            h_alg, ctypes.c_wchar_p("ChainingMode"), mode, len(mode), 0)
        if status != 0:
            raise CookieReadError(f"设置 GCM 模式失败：0x{status & 0xFFFFFFFF:08X}")
        key_len = wintypes.ULONG(len(key) * 8)
        bcrypt.BCryptSetProperty(
            h_alg, ctypes.c_wchar_p("KeyLength"),
            ctypes.byref(key_len), ctypes.sizeof(key_len), 0)

        key_buf = ctypes.create_string_buffer(key, len(key))
        status = bcrypt.BCryptGenerateSymmetricKey(
            h_alg, ctypes.byref(h_key), None, 0, key_buf, len(key), 0)
        if status != 0:
            raise CookieReadError(f"BCryptGenerateSymmetricKey 失败：0x{status & 0xFFFFFFFF:08X}")

        nonce_buf = ctypes.create_string_buffer(nonce, len(nonce))
        tag_buf = ctypes.create_string_buffer(tag, len(tag))
        info = _AuthCipherModeInfo(
            cbSize=ctypes.sizeof(_AuthCipherModeInfo),
            dwInfoVersion=1,
            pbNonce=ctypes.cast(nonce_buf, ctypes.c_void_p),
            cbNonce=len(nonce),
            pbAuthData=None,
            cbAuthData=0,
            pbTag=ctypes.cast(tag_buf, ctypes.c_void_p),
            cbTag=len(tag),
            pbMacContext=None,
            cbMacContext=0,
            cbAAD=0,
            cbData=0,
            dwFlags=0,
        )
        in_buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
        out_buf = ctypes.create_string_buffer(len(ciphertext) + 16)
        out_len = wintypes.ULONG()
        status = bcrypt.BCryptDecrypt(
            h_key, in_buf, len(ciphertext), ctypes.byref(info), None, 0,
            out_buf, len(out_buf), ctypes.byref(out_len), 0)
        if status != 0:
            raise CookieReadError(
                f"AES-GCM 解密失败：0x{status & 0xFFFFFFFF:08X}"
                "（Cookie 可能来自 App-Bound 加密的新版浏览器）")
        return out_buf.raw[:out_len.value]
    finally:
        if h_key:
            bcrypt.BCryptDestroyKey(h_key)
        if h_alg:
            bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


# --------------------------------------------------------------- 读密钥/数据库


def load_master_key(user_data_dir: Path) -> tuple[bytes, str]:
    """从 Local State 读 os_crypt.encrypted_key 并解出 AES 主密钥。"""
    local_state = user_data_dir / "Local State"
    if not local_state.exists():
        raise CookieReadError(f"找不到 {local_state}")
    try:
        data = json.loads(local_state.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        raise CookieReadError(f"读取 Local State 失败：{exc}")
    os_crypt = data.get("os_crypt") or {}
    note = ""
    if os_crypt.get("app_bound_encrypted_key"):
        note = "app-bound"
    encoded = os_crypt.get("encrypted_key")
    if not encoded:
        raise CookieReadError("Local State 里没有 os_crypt.encrypted_key")
    raw = base64.b64decode(encoded)
    if not raw.startswith(b"DPAPI"):
        raise CookieReadError("encrypted_key 前缀不是 DPAPI，无法识别")
    return dpapi_unprotect(raw[5:]), note


def iter_profiles(user_data_dir: Path) -> list[Path]:
    """列出所有 profile 目录。

    两种布局都支持：
      * 浏览器：User Data\\Default、User Data\\Profile 1 …
      * Electron 应用（学习通客户端）：User Data 本身就是 profile
    """
    profiles: list[Path] = []
    if cookie_db_path(user_data_dir) is not None:
        profiles.append(user_data_dir)          # 扁平布局：根目录就是 profile
    default = user_data_dir / "Default"
    if default.exists():
        profiles.append(default)
    for child in sorted(user_data_dir.glob("Profile *")):
        if child.is_dir():
            profiles.append(child)
    return profiles


def cookie_db_path(profile_dir: Path) -> Path | None:
    """Cookie 数据库位置：Chrome/Edge 96+ 在 Network\\Cookies，旧版在 profile 根目录。"""
    for candidate in (profile_dir / "Network" / "Cookies", profile_dir / "Cookies"):
        if candidate.exists():
            return candidate
    return None


def _copy_db(db: Path) -> tuple[Path, Path]:
    """兜底方案：把 Cookie 数据库（含 WAL）复制到临时目录再读。

    仅当浏览器已关闭、或 SQLite 允许复制时可用。
    返回（副本路径, 临时目录）。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="cxmon-cookies-"))
    target = tmp_dir / "Cookies"
    shutil.copy2(db, target)
    for suffix in ("-wal", "-shm", "-journal"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            try:
                shutil.copy2(side, tmp_dir / ("Cookies" + suffix))
            except OSError:
                pass
    return target, tmp_dir


def _open_cookie_db(db: Path) -> tuple[sqlite3.Connection, Path | None]:
    """依次尝试三种打开方式，返回（连接, 需要清理的临时目录或 None）。

    浏览器正在运行时文件被锁，复制会报 WinError 32，
    所以首选直接用 SQLite 只读打开原库（SQLite 自己会读 -wal，能拿到最新 Cookie）。
    """
    errors: list[str] = []
    for attempt in range(3):
        try:
            conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)
            conn.execute("SELECT 1 FROM cookies LIMIT 1").fetchone()
            return conn, None
        except sqlite3.Error as exc:
            errors.append(f"只读打开失败({exc})")
            time.sleep(0.4)

    try:
        target, tmp_dir = _copy_db(db)
        return sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=5), tmp_dir
    except (OSError, sqlite3.Error) as exc:
        errors.append(f"复制副本失败({exc})")

    try:
        conn = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM cookies LIMIT 1").fetchone()
        errors.append("已用 immutable 强读（可能读不到刚写入的最新 Cookie）")
        return conn, None
    except sqlite3.Error as exc:
        errors.append(f"immutable 打开失败({exc})")

    # 三种方式都失败 —— 判断一下是不是「浏览器正在运行、独占了这个库」
    try:
        with open(db, "rb"):
            pass
    except OSError as exc:
        if getattr(exc, "winerror", None) in (32, 33) or exc.errno in (13, 11):
            raise LockedCookieDB(
                "Cookie 数据库被正在运行的浏览器独占（Windows 不允许同时读取）"
            ) from exc
    raise CookieReadError("；".join(errors))


def read_chaoxing_cookies(profile_dir: Path, master_key: bytes) -> tuple[dict, list[str], int]:
    """返回（Cookie 字典, 警告列表, 因 App-Bound 加密而解不开的条数）。只读学习通相关域。"""
    db = cookie_db_path(profile_dir)
    if db is None:
        raise CookieReadError(f"找不到 Cookie 数据库（{profile_dir}\\Network\\Cookies）")
    conn, cleanup = _open_cookie_db(db)
    warnings: list[str] = []
    jar: dict[str, str] = {}
    app_bound = 0
    try:
        where = " OR ".join("host_key LIKE ?" for _ in DOMAIN_FILTER)
        rows = conn.execute(
            f"SELECT host_key, name, value, encrypted_value FROM cookies WHERE {where}",
            tuple(f"%{d}" for d in DOMAIN_FILTER),
        ).fetchall()
    except sqlite3.Error as exc:
        raise CookieReadError(f"查询 cookies 表失败：{exc}")
    finally:
        conn.close()
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)

    for host, name, value, encrypted in rows:
        if value:
            text = value
        else:
            if encrypted[:3] == b"v20":
                app_bound += 1
                if "app-bound" not in warnings:
                    warnings.append("app-bound")
                continue
            text = decrypt_cookie_value(master_key, encrypted, warnings)
        if text is None:
            continue
        # 同名 Cookie 保留先出现的（根域 .chaoxing.com 排在前面）
        jar.setdefault(name, text)
    return jar, warnings, app_bound


def decrypt_cookie_value(master_key: bytes, encrypted: bytes, warnings: list[str]) -> str | None:
    if not encrypted:
        return None
    if encrypted.startswith(b"v20"):
        if "app-bound" not in warnings:
            warnings.append("app-bound")
        return None
    if encrypted[:3] in (b"v10", b"v11"):
        nonce, payload = encrypted[3:15], encrypted[15:]
        if len(payload) <= 16:
            return None
        try:
            plain = aes_gcm_decrypt(master_key, nonce, payload[:-16], payload[-16:])
        except CookieReadError as exc:
            warnings.append(str(exc))
            return None
        return plain.decode("utf-8", "replace")
    # 老格式：整个值由 DPAPI 保护
    try:
        return dpapi_unprotect(encrypted).decode("utf-8", "replace")
    except CookieReadError as exc:
        warnings.append(str(exc))
        return None


# ------------------------------------------------------------------ 对外接口


def _scan(browsers: list[str] | None, log) -> tuple[list[dict], list[str], list[str]]:
    """扫一遍所有浏览器/profile，返回（候选列表, 被占用的标签, App-Bound 加密的标签）。"""
    keys = list(browsers or BROWSERS.keys())
    candidates: list[dict] = []
    locked: list[str] = []
    app_bound: list[str] = []
    for key in keys:
        if key not in BROWSERS:
            continue
        display, user_data = BROWSERS[key]
        if not user_data.exists():
            log(f"[跳过] 没装 {display}")
            continue
        try:
            master_key, enc_note = load_master_key(user_data)
        except CookieReadError as exc:
            log(f"[跳过] {display}：{exc}")
            continue
        for profile in iter_profiles(user_data):
            label = f"{display} / {profile.name}" if profile != user_data else display
            try:
                jar, warnings, blocked = read_chaoxing_cookies(profile, master_key)
            except LockedCookieDB:
                locked.append(label)
                log(f"[占用] {label}：Cookie 数据库被运行中的 {display} 独占，读不了")
                continue
            except CookieReadError as exc:
                log(f"[跳过] {label}：{exc}")
                continue
            if not jar:
                if blocked:
                    app_bound.append(label)
                    log(f"[加密] {label}：库里有 {blocked} 个学习通 Cookie，"
                        f"但都是 App-Bound(v20) 加密，脚本解不开（这是浏览器的反抓取设计）")
                continue
            score = sum(1 for f in CORE_FIELDS if jar.get(f))
            log(f"[发现] {label}：学习通字段 {len(jar)} 个，"
                f"关键字段 {score}/{len(CORE_FIELDS)}（_uid={'有' if jar.get('_uid') else '无'}, "
                f"fid={'有' if jar.get('fid') else '无'}, vc3={'有' if jar.get('vc3') else '无'}）")
            for warning in dict.fromkeys(warnings):
                log(f"        注意：{warning}")
            if blocked:
                log(f"        另有 {blocked} 个 Cookie 是 App-Bound 加密、解不开")
            candidates.append({"label": label, "jar": jar, "score": score, "warnings": warnings})
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates, locked, app_bound


def collect_candidates(browsers: list[str] | None = None, log=print,
                       wait_seconds: float = 0.0, poll: float = 3.0
                       ) -> tuple[list[dict], list[str], list[str]]:
    """收集候选；如果数据库被运行中的浏览器占用，可挂起等待它退出。

    wait_seconds > 0 时，会一直等到读到 Cookie 或超时（期间提示还剩哪些浏览器在跑）。
    返回（候选列表, 仍在占用中的标签, App-Bound 加密无法解密的标签）。
    """
    keys = [k for k in (browsers or BROWSERS.keys()) if k in BROWSERS]
    deadline = time.monotonic() + max(0.0, wait_seconds)
    first = True
    while True:
        candidates, locked, app_bound = _scan(
            keys if keys else None, log if first else (lambda *_: None))
        first = False
        # App-Bound 是死路，等下去也没用；只有"占用"才值得等
        if candidates or not locked or app_bound or time.monotonic() >= deadline:
            return candidates, locked, app_bound

        remaining = int(deadline - time.monotonic())
        running = []
        for key in keys:
            count = browser_process_count(key)
            if count > 0:
                running.append(f"{BROWSERS[key][0]}（{count} 个进程）")
        detail = "、".join(running) if running else "进程看起来已经退出了，再等一下…"
        log(f"[等待] 请完全退出浏览器，退出后本命令会自动继续。"
            f" 还需退出：{detail}  剩余 {remaining} 秒")
        time.sleep(poll)


def jar_to_header(jar: dict) -> str:
    """拼成 Cookie 请求头字符串。"""
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def summarize(jar: dict) -> str:
    """给用户看的安全摘要（全部掩码）。"""
    parts = []
    for field in CORE_FIELDS:
        if field in jar:
            parts.append(f"{field}={mask(jar[field])}")
    return "  ".join(parts) if parts else "（没有关键字段）"


def auto_update_cookie(cfg: dict, log=print, browsers: list[str] | None = None
                       ) -> tuple[bool, dict | None, str]:
    """自动重新获取并**验证** Cookie，成功就写回 config.json。

    这是"登录过期不用你操心"的核心：读候选 → 逐个真的拿去请求学习通 →
    通过才保存。所以返回 True 就一定是能用的登录信息。

    返回（是否成功, 成功的候选, 给人看的原因说明）。
    """
    from .chaoxing import ChaoxingClient, ChaoxingError       # 延迟导入，避免循环依赖
    from .config import save_config

    candidates, locked, app_bound = collect_candidates(browsers, log=log, wait_seconds=0)
    for cand in candidates:
        header = jar_to_header(cand["jar"])
        probe = dict(cfg, cookie=header, fid=cand["jar"].get("fid") or "",
                     uid=cand["jar"].get("_uid") or "")
        try:
            courses = ChaoxingClient(
                cookie=header, fid=probe.get("fid") or "", uid=probe.get("uid") or "",
                timeout=cfg.get("request_timeout") or 10, retries=0).get_courses()
        except (ChaoxingError, SystemExit, OSError) as exc:
            log(f"[自动更新] {cand['label']} 的登录信息已失效：{exc}")
            continue
        except Exception as exc:                              # noqa: BLE001 验证失败不算致命
            log(f"[自动更新] {cand['label']} 验证出错：{exc}")
            continue

        cfg["cookie"] = header
        cfg["uid"] = cand["jar"].get("_uid") or cand["jar"].get("uid") or ""
        cfg["fid"] = cand["jar"].get("fid") or ""
        save_config(cfg)
        return True, cand, f"已用「{cand['label']}」的登录信息更新（能访问 {len(courses)} 个班级）"

    reasons = []
    if locked:
        reasons.append(f"{'、'.join(locked)} 正在运行，读不到它的登录信息（关掉它就行）")
    if app_bound:
        reasons.append("浏览器启用了 App-Bound 加密，脚本读不出来")
    if not candidates and not reasons:
        reasons.append("没找到任何可用的登录信息（学习通客户端里可能也没登录）")
    return False, None, "；".join(reasons)


COOKIE_FAILURE_ADVICE = (
    "登录已过期，监控暂时用不了。\n"
    "请打开『学习通』电脑客户端登录一次，工具会在几分钟内自动获取新的登录信息并继续；\n"
    "如果没装电脑客户端：浏览器登录 i.chaoxing.com，然后点控制面板上的「更新 Cookie」。"
)
