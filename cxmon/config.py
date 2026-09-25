"""配置的加载 / 合并 / 保存。

所有配置都在 config.json 里，缺省字段用 DEFAULT_CONFIG 补齐，
所以用户就算只写一个 cookie 也能跑起来。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

DEFAULT_CONFIG = {
    # ---------- 账号 ----------
    "cookie": "",          # 浏览器里复制的完整 Cookie 字符串（必填）
    "fid": "",             # 学校/机构 id，留空则从 Cookie 的 fid 读取
    "uid": "",             # 用户 id，留空则从 Cookie 的 _uid 读取

    # ---------- 轮询 ----------
    "poll_interval": 5.0,      # 每轮扫描间隔（秒），不建议低于 3
    "jitter": 1.5,             # 每轮随机抖动上限（秒），避免请求过于规律
    "scan_workers": 6,         # 并发扫描线程数（26 个班串行要 3 秒，并发后 <1 秒）
    "request_timeout": 10,     # 单个请求超时（秒）
    "http_retries": 2,         # 失败重试次数
    "auto_discover": True,     # 自动拉取「我学的课」列表
    "discover_interval": 600,  # 多久重新拉一次课程列表（秒）

    # 留空 = 监控所有课程；也可以只盯几个班：
    # [{"courseId": "2001", "classId": "1001", "name": "高等数学"}]
    "targets": [],

    # ---------- 什么算「签到」 ----------
    # 实测（2026-09）：学习通 activelist 接口里签到类活动的 type 都是 2
    # （普通签到/位置签到/二维码签到/手势签到），随堂练习是 42、选人是 11、通知是 45。
    # 名字看 nameOne（name 字段是空的）。用 probe 命令可以随时复核。
    "match": {
        "types": [2],                                     # 活动类型白名单
        "name_keywords": ["签到", "签退"],                 # 名称关键词（或关系）
    },

    # ---------- 提醒方式 ----------
    "alert": {
        "voice": True,             # 语音播报
        "voice_name": "",          # 指定发音人，如 "Microsoft Huihui Desktop"；留空用系统默认
        "voice_rate": 1,           # 语速 -10 ~ 10
        "voice_text": "注意，{course}发起了{name}，请尽快打开学习通处理",
        "repeat": 3,               # 播报几遍
        "repeat_interval": 5.0,    # 每遍之间间隔（秒）
        "realert_after": 0,        # >0 表示签到还挂着就每隔这么久再提醒一次；0 = 只提醒一次
        "beep": True,              # 提示音
        "toast": True,             # Windows 右下角气泡通知
        "open_url": False,         # 自动用默认浏览器打开签到页（默认关：你习惯在手机上签）
        "sign_url_template": "https://mobilelearn.chaoxing.com/pptSign/stuSign?activeId={activeId}",
        "webhook": "",             # 可选：Server酱 / 钉钉 / 企业微信机器人地址，推送到手机
    },

    # ---------- 网络异常提示 ----------
    # 断网是最阴的故障：心跳照打、只是"暂无新签到"，看起来一切正常，
    # 其实 26 个班一个都扫不到。所以连续连不上要主动推手机告诉你。
    "network": {
        "notify": True,              # 网络异常时推手机（关掉就只写日志）
        "down_rounds": 3,            # 连续几轮"所有班级都连不上"才告警（3 轮 ≈ 24 秒）
        "flaky_rounds": 5,           # 连续几轮"部分班级连不上"才告警
        "recover_notify": True,      # 网络恢复时也推一条，让你知道又能靠它了
        "startup_retries": 3,        # 启动时拉不到课程列表的重试次数
        "startup_retry_delay": 5.0,  # 每次重试之间的间隔（秒）
    },

    # ---------- 文件 ----------
    "state_file": "state.json",   # 已提醒过的活动记录（去重）
    "log_file": "monitor.log",
}


def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 合并进 base 的副本，dict 递归合并。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def default_config_path() -> Path:
    """配置文件路径：exe 旁边（打包后）或项目根目录（源码运行）。

    打包后不能再用 __file__ —— 那会指向解包出来的临时目录，
    配置写进去程序一退出就没了。
    """
    from .paths import app_dir
    return app_dir() / "config.json"


def load_config(path=None) -> dict:
    """读取配置；文件不存在时返回全默认值（并允许后续 save 创建）。"""
    p = Path(path) if path else default_config_path()
    raw = {}
    if p.exists():
        text = p.read_text(encoding="utf-8-sig").strip()
        if text:
            try:
                raw = json.loads(text)
            except ValueError as exc:
                raise SystemExit(f"配置文件不是合法 JSON: {p}\n  {exc}")
    if not isinstance(raw, dict):
        raise SystemExit(f"配置文件顶层必须是对象: {p}")
    cfg = _deep_merge(DEFAULT_CONFIG, raw)
    cfg["_path"] = str(p)
    cfg["_dir"] = str(p.parent)
    return cfg


def save_config(cfg: dict, path=None) -> Path:
    """写回配置（忽略下划线开头的运行时字段）。"""
    p = Path(path) if path else Path(cfg.get("_path") or default_config_path())
    clean = {k: v for k, v in cfg.items() if not str(k).startswith("_")}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p
