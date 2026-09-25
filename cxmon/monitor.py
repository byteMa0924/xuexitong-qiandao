"""监控主循环：轮询 → 命中 → 提醒 → 去重 → 落盘。"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from . import notifier, runtime
from .paths import self_command, subprocess_cwd
from .chaoxing import (
    ChaoxingClient,
    ChaoxingError,
    CookieExpired,
    MockTransport,
    NetworkError,
    build_sign_url,
)

log = logging.getLogger("cxmon.monitor")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
STATE_TTL = 24 * 3600  # 状态文件里超过 24 小时的记录直接清理
TRAY_CHECK_INTERVAL = 30  # 每隔多久确认一次托盘图标还在（它是"监控在跑"的落脚点）


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class AuthExpired(Exception):
    """登录信息失效且自动修复失败 —— 监控会原地等待重试，而不是退出。"""


def render_voice_text(alert_cfg: dict, activity: dict) -> str:
    """把 voice_text 模板渲染成最终播报文本。

    模板可用占位符：{course} {name} {activeId} {time}
    """
    template = (alert_cfg or {}).get("voice_text") or "检测到{name}"
    try:
        text = template.format(
            course=activity.get("courseName") or "",
            name=activity.get("name") or "签到",
            activeId=activity.get("activeId") or "",
            time=datetime.now().strftime("%H时%M分"),
        )
    except (KeyError, IndexError, ValueError):
        text = f"检测到{activity.get('name') or '签到'}"
    return " ".join(str(text).split())


def is_ongoing(activity: dict, now: float | None = None, grace_ms: int = 60_000) -> bool:
    """判断活动是否「正在进行中」。

    学习通的 activeList 会把历史活动一起返回（实测 148 条里有 4 月份的老签到），
    不按时间过滤的话，第一次扫描就会把几个月的旧签到全报一遍。
    规则：以时间窗为主（开始时间已到 + 结束时间未过），status 只做兜底参考。
    """
    now_ms = int((now if now is not None else time.time()) * 1000)
    end = activity.get("endTime")
    start = activity.get("startTime")
    if end:
        try:
            if int(end) + grace_ms < now_ms:
                return False
        except (TypeError, ValueError):
            pass
    if start:
        try:
            if int(start) - grace_ms > now_ms:
                return False
        except (TypeError, ValueError):
            pass
    status = activity.get("status")
    if status is not None and not end:
        try:
            if int(status) == 2:      # 2 = 已结束
                return False
        except (TypeError, ValueError):
            pass
    return True


def match_activity(activity: dict, match_cfg: dict) -> bool:
    """判断一个活动是不是我们关心的「签到」。

    命中规则（或关系）：类型命中 types，或名称命中 name_keywords 里任一关键词。
    名称会同时看 name / nameOne（真实签到里 name 常为空、nameOne 才是"位置签到"）。
    两边都为空配置时，退化为"名称里带『签』字就提醒"。
    """
    types = match_cfg.get("types") or []
    keywords = match_cfg.get("name_keywords") or []
    text = " ".join(str(activity.get(key) or "")
                    for key in ("name", "nameOne", "nameTwo", "title"))
    if not types and not keywords:
        return "签" in text

    if types:
        act_type = activity.get("type")
        for wanted in types:
            try:
                if act_type is not None and int(act_type) == int(wanted):
                    return True
            except (TypeError, ValueError):
                if str(act_type) == str(wanted):
                    return True

    if keywords and any(kw and kw in text for kw in keywords):
        return True
    return False


class Monitor:
    def __init__(self, cfg: dict, *, mock: bool = False, once: bool = False,
                 dry_run: bool = False, interval: float | None = None):
        self.cfg = cfg
        self.mock = mock
        self.once = once
        self.dry_run = dry_run
        self.interval = float(interval if interval else cfg.get("poll_interval") or 5.0)
        self.jitter = float(cfg.get("jitter") or 0)

        base_dir = Path(cfg.get("_dir") or ".")
        self.state_path = base_dir / str(cfg.get("state_file") or "state.json")

        alert_cfg = cfg.get("alert") or {}
        self.alert_cfg = alert_cfg
        self.realert_after = float(alert_cfg.get("realert_after") or 0)

        self.client = ChaoxingClient(
            cookie=cfg.get("cookie") or "",
            fid=cfg.get("fid") or "",
            uid=cfg.get("uid") or "",
            timeout=cfg.get("request_timeout") or 10,
            retries=cfg.get("http_retries") or 0,
            transport=MockTransport() if mock else None,
        )
        self._mock = mock
        self._refresh_used = 0                  # Cookie 自动更新次数（防死循环）
        self._auth_notice_at = 0.0              # 上次"登录过期"提醒的时间

        self.targets: list[dict] = []
        self.seen: dict[str, dict] = self._load_state()
        self._stop = threading.Event()
        self._speaker_obj: notifier.VoiceSpeaker | None = None
        self._speaker_lock = threading.Lock()
        self._alert_slots = threading.Semaphore(2)   # 同时最多两路提醒
        self._opened: set[str] = set()
        self._stats = {"scans": 0, "hits": 0, "errors": 0,
                       "net_errors": 0, "round_ok": 0, "round_net_fail": 0,
                       "last_net_error": ""}

        # ---- 网络健康状态：断网是"静默失效"，必须主动告诉你 ----
        self.net_cfg = cfg.get("network") or {}
        self._net_state: str | None = None   # None=正常 / "flaky"=不稳 / "down"=全断
        self._net_down_rounds = 0            # 连续"全军覆没"的轮数
        self._net_flaky_rounds = 0           # 连续"部分失败"的轮数

    # --------------------------------------------------------------- 状态文件

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError) as exc:
            log.warning("状态文件损坏，重新开始：%s", exc)
            return {}
        seen = data.get("seen") if isinstance(data, dict) else None
        if not isinstance(seen, dict):
            return {}
        cutoff = time.time() - STATE_TTL
        return {k: v for k, v in seen.items()
                if isinstance(v, dict) and float(v.get("first_seen") or 0) >= cutoff}

    def _save_state(self) -> None:
        cutoff = time.time() - STATE_TTL
        pruned = {k: v for k, v in self.seen.items()
                  if isinstance(v, dict) and float(v.get("first_seen") or 0) >= cutoff}
        self.seen = pruned
        payload = {"updated_at": _now(), "seen": pruned}
        tmp = self.state_path.with_suffix(".json.tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("写入状态文件失败：%s", exc)

    # ------------------------------------------------------------------ 目标班级

    def resolve_targets(self) -> None:
        """确定要盯的班级：配置了就用配置的，否则自动拉课程列表。"""
        configured = self.cfg.get("targets") or []
        if configured and not self.cfg.get("auto_discover", True):
            self.targets = [self._normalize_target(t) for t in configured]
            log.info("按配置监控 %d 个班级", len(self.targets))
            return

        try:
            courses = self._get_courses_with_retry()
        except CookieExpired as exc:
            # 启动时就失效：先试自动修复，修不好才报错退出
            if self._try_refresh_cookie():
                courses = self.client.get_courses()
            else:
                self._notify_auth_problem(str(exc))
                raise SystemExit(
                    f"登录信息失效，且无法自动获取新的：{exc}\n"
                    "请打开『学习通』电脑客户端登录一次，然后重新开始监控；\n"
                    "或者在控制面板上点「更新 Cookie」。")
        except NetworkError as exc:
            # 起不来是静默失效里最危险的一种：你以为它在盯，其实它没启动
            log.error("拉取课程列表失败（网络不通）：%s", exc)
            if not self.targets:
                self._notify_startup_failure(exc)
                raise SystemExit(
                    f"连不上学习通，无法开始监控：{exc}\n"
                    "已重试 %s 次。请检查网络后重新点「开始监控」。" %
                    (self.net_cfg.get("startup_retries") or 1))
            return
        except ChaoxingError as exc:
            log.error("拉取课程列表失败：%s", exc)
            if not self.targets:
                raise SystemExit("拉不到课程列表，且配置里也没有 targets，无法开始监控。")
            return

        if configured:
            wanted = {(str(t.get("courseId")), str(t.get("classId"))) for t in configured}
            kept = [c for c in courses if (c["courseId"], c["classId"]) in wanted]
            missing = wanted - {(c["courseId"], c["classId"]) for c in kept}
            for item in missing:
                log.warning("配置里的班级在课程列表里找不到：courseId=%s classId=%s", *item)
            self.targets = kept or [self._normalize_target(t) for t in configured]
        else:
            self.targets = courses

        if not self.targets:
            raise SystemExit("没有可监控的班级，请检查 Cookie 或 targets 配置。")

    @staticmethod
    def _normalize_target(item: dict) -> dict:
        return {
            "courseId": str(item.get("courseId") or item.get("courseid") or ""),
            "classId": str(item.get("classId") or item.get("classid") or ""),
            "name": str(item.get("name") or item.get("courseName") or "未命名课程"),
            "fid": str(item.get("fid") or ""),
        }

    def target_label(self, target: dict) -> str:
        for known in self.targets:
            if known["courseId"] == target.get("courseId") and known["classId"] == target.get("classId"):
                return known.get("name") or "未命名课程"
        return target.get("name") or "未命名课程"

    # -------------------------------------------------- 登录过期自动修复

    def _try_refresh_cookie(self) -> bool:
        """登录失效时自动重新获取（读学习通客户端/浏览器里的登录信息并验证）。"""
        if self._mock or self._refresh_used >= 2:
            return False
        self._refresh_used += 1
        from .browser_cookie import auto_update_cookie
        log.warning("登录信息失效，正在尝试自动重新获取（第 %d 次）…", self._refresh_used)
        ok, cand, message = auto_update_cookie(self.cfg, log=log.info)
        if not ok:
            log.error("自动更新失败：%s", message)
            return False
        self.client = ChaoxingClient(
            cookie=self.cfg.get("cookie") or "", fid=self.cfg.get("fid") or "",
            uid=self.cfg.get("uid") or "",
            timeout=self.cfg.get("request_timeout") or 10,
            retries=self.cfg.get("http_retries") or 0,
            transport=None,
        )
        log.warning("登录信息已自动更新：%s", message)
        return True

    def _notify_auth_problem(self, reason: str) -> None:
        """登录修不好时，用语音+弹窗+微信告诉用户该做什么（每小时最多一次）。"""
        from .browser_cookie import COOKIE_FAILURE_ADVICE
        now = time.time()
        if now - self._auth_notice_at < 3600:
            return
        self._auth_notice_at = now
        text = "学习通登录过期了，请打开学习通电脑客户端登录一次，我会自动更新"
        log.error("登录失效且无法自动修复：%s\n%s", reason, COOKIE_FAILURE_ADVICE)
        if self.dry_run:
            return
        try:
            if self.alert_cfg.get("voice", True):
                self._get_speaker().speak(text)
            if self.alert_cfg.get("beep", True):
                notifier.beep(3)
            if self.alert_cfg.get("toast", True):
                notifier.toast("学习通监控：登录已过期", COOKIE_FAILURE_ADVICE[:120])
            webhook = self.alert_cfg.get("webhook") or ""
            if webhook:
                notifier.send_webhook(webhook, "【学习通监控】登录已过期，需要你操作一下",
                                      COOKIE_FAILURE_ADVICE)
        except Exception as exc:                    # 通知失败不影响继续重试
            log.debug("登录过期通知发送失败：%s", exc)

    # ------------------------------------------------------------------ 扫描

    def scan_once(self) -> list[dict]:
        """并发扫描所有班级，返回本轮需要提醒的活动（已打过标记）。

        并发的原因：26 个班串行要 ~3 秒，这点时间会直接叠加到检测延迟上；
        并发后一轮只需几百毫秒，检测延迟就基本等于扫描间隔本身。
        并发不会增加请求总量（每轮每班仍然只请求一次）。
        """
        found: list[dict] = []
        targets = list(self.targets)
        workers = max(1, min(int(self.cfg.get("scan_workers") or 6), len(targets) or 1))
        expired: CookieExpired | None = None
        ok = 0
        net_fail = 0

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cxmon-scan") as pool:
            futures = {pool.submit(self._fetch, t): t for t in targets}
            for future in as_completed(futures):
                target = futures[future]
                label = target.get("name") or target.get("courseId")
                try:
                    activities = future.result()
                except CookieExpired as exc:
                    expired = exc
                    continue
                except NetworkError as exc:
                    # 连不上：单独计数，用来判断"是网络瞎了"还是"只是接口变了"
                    self._stats["errors"] += 1
                    self._stats["net_errors"] += 1
                    self._stats["last_net_error"] = str(exc)
                    net_fail += 1
                    log.warning("[%s] 连不上学习通：%s", label, exc)
                    continue
                except ChaoxingError as exc:
                    self._stats["errors"] += 1
                    log.warning("[%s] 拉取活动列表失败：%s", label, exc)
                    continue
                except Exception as exc:            # 单个班出错不能拖垮整轮
                    self._stats["errors"] += 1
                    log.warning("[%s] 扫描异常：%s", label, exc)
                    continue

                ok += 1
                for activity in activities:
                    if not is_ongoing(activity):
                        continue          # 历史活动（接口会返回几个月前的），不提醒
                    if not match_activity(activity, self.cfg.get("match") or {}):
                        continue
                    activity["courseName"] = label
                    key = str(activity["activeId"])
                    record = self.seen.get(key)
                    if record is None:
                        self.seen[key] = {
                            "activeId": key,
                            "name": activity.get("name"),
                            "course": label,
                            "first_seen": time.time(),
                            "first_seen_at": _now(),
                            "last_alert": 0,
                        }
                        activity["_realert"] = False
                        found.append(activity)
                    elif self.realert_after and \
                            time.time() - float(record.get("last_alert") or 0) >= self.realert_after:
                        activity["_realert"] = True
                        found.append(activity)

        # 记录本轮的网络健康度，供 _check_network_health 判断
        self._stats["round_ok"] = ok
        self._stats["round_net_fail"] = net_fail

        if expired is not None:
            self._stats["errors"] += 1
            if self._try_refresh_cookie():
                return self.scan_once()          # 换成新登录信息，立刻重扫这一轮
            self._notify_auth_problem(str(expired))
            raise AuthExpired(str(expired))
        self._stats["scans"] += 1
        return found

    def _fetch(self, target: dict) -> list[dict]:
        """拉一个班的进行中活动（供线程池调用）。"""
        return self.client.get_active_list(
            target["courseId"], target["classId"], target.get("fid") or self.client.fid)

    # ------------------------------------------------------------- 网络健康提示


    def _push_net(self, title: str, body: str) -> None:
        """推一条网络相关的手机通知（没配 webhook 或关掉提示时什么都不做）。"""
        if self.dry_run or not self.net_cfg.get("notify", True):
            return
        webhook = self.alert_cfg.get("webhook") or ""
        if not webhook:
            return
        try:
            notifier.send_webhook(webhook, title, body)
        except Exception as exc:                 # 告警本身失败绝不能拖垮监控
            log.debug("网络告警推送失败：%s", exc)

    def _check_network_health(self) -> None:
        """每轮扫描后调用：断网/网络不稳时推手机，恢复时也推一条。

        为什么必须推：断网时 26 个班每轮各报一条 warning，但心跳照样打
        "暂无新签到"——从表面看你分不出"真没有签到"和"根本扫不到"。
        没有这条提醒，你会在完全不知情的情况下漏掉整节课的签到。
        """
        if self.mock:
            return
        total = len(self.targets)
        ok = int(self._stats.get("round_ok") or 0)
        net_fail = int(self._stats.get("round_net_fail") or 0)
        if total == 0 or (ok == 0 and net_fail == 0):
            return                # 本轮没有网络类失败（可能是 Cookie 问题），不插手

        down_rounds = max(1, int(self.net_cfg.get("down_rounds") or 3))
        flaky_rounds = max(1, int(self.net_cfg.get("flaky_rounds") or 5))
        detail = str(self._stats.get("last_net_error") or "")

        if net_fail == 0:
            # 完全正常
            self._net_down_rounds = 0
            self._net_flaky_rounds = 0
            if self._net_state is not None:
                log.warning("网络已恢复：本轮 %d/%d 个班级扫描成功", ok, total)
                if self.net_cfg.get("recover_notify", True):
                    self._push_net("【学习通监控】✅ 网络已恢复",
                                   f"已能正常访问学习通（本轮 {ok}/{total} 个班级扫描成功），"
                                   "签到监控继续工作。")
                self._net_state = None
            return

        if ok == 0:
            # 这一轮全军覆没
            self._net_flaky_rounds = 0
            self._net_down_rounds += 1
            if self._net_down_rounds >= down_rounds and self._net_state != "down":
                self._net_state = "down"
                log.error("网络异常：连续 %d 轮 %d 个班级全部连不上",
                          self._net_down_rounds, net_fail)
                self._push_net(
                    "【学习通监控】⚠️ 网络断了，已看不见签到",
                    f"连续 {self._net_down_rounds} 轮都连不上学习通，"
                    f"{total} 个班级一个都没扫到。\n"
                    f"最后一条错误：{detail}\n"
                    "这段时间内的签到不会被发现，请检查网络"
                    "（电脑是否断网 / 睡眠 / Wi-Fi 掉了）。")
            return

        # 部分班级连不上：网络不稳，漏检风险升高
        if self._net_state == "down":
            return                # 从全断变成部分通 = 正在恢复，继续观察
        self._net_down_rounds = 0
        self._net_flaky_rounds += 1
        if self._net_flaky_rounds >= flaky_rounds and self._net_state != "flaky":
            self._net_state = "flaky"
            log.warning("网络不稳定：连续 %d 轮有 %d 个班级连不上",
                        self._net_flaky_rounds, net_fail)
            self._push_net(
                "【学习通监控】⚠️ 网络不稳定",
                f"连续 {self._net_flaky_rounds} 轮有 {net_fail} 个班级连不上"
                f"（本轮成功 {ok}/{total}），漏检风险变高。\n"
                f"最后一条错误：{detail}")

    # ------------------------------------------------------------- 启动时拉课程

    def _get_courses_with_retry(self) -> list[dict]:
        """拉课程列表；网络不通时先重试几次再放弃。

        宿舍网早上刚连上、Wi-Fi 重连的那几秒经常失败，
        而那恰好就是你出门前启动监控的时刻。
        """
        tries = max(1, int(self.net_cfg.get("startup_retries") or 1))
        delay = float(self.net_cfg.get("startup_retry_delay") or 5.0)
        last: NetworkError | None = None
        for attempt in range(tries):
            try:
                return self.client.get_courses()
            except NetworkError as exc:
                last = exc
                if attempt + 1 < tries:
                    log.warning("连不上学习通（第 %d/%d 次），%.0f 秒后重试：%s",
                                attempt + 1, tries, delay, exc)
                    if self._stop.wait(delay):
                        break
        raise last if last is not None else NetworkError("连不上学习通")

    def _notify_startup_failure(self, exc: Exception) -> None:
        """启动时网络不通：推手机告诉你「它根本没在跑」。"""
        self._push_net(
            "【学习通监控】⚠️ 监控没能启动：网络不通",
            "电脑连不上学习通，监控没有开始运行。\n"
            f"原因：{exc}\n"
            "现在没有任何东西在盯签到，请检查网络后重新点「开始监控」。")

    # ------------------------------------------------------------------ 提醒

    def _get_speaker(self) -> notifier.VoiceSpeaker:
        with self._speaker_lock:
            if self._speaker_obj is None:
                self._speaker_obj = notifier.VoiceSpeaker(
                    voice_name=self.alert_cfg.get("voice_name") or "",
                    rate=self.alert_cfg.get("voice_rate") or 0,
                )
            return self._speaker_obj

    def render_text(self, activity: dict, index: int = 0) -> str:
        return render_voice_text(self.alert_cfg, activity)

    def alert(self, activity: dict) -> None:
        """打印醒目横幅并在后台线程里执行提醒动作。"""
        realert = bool(activity.get("_realert"))
        head = "签到仍在进行，再次提醒" if realert else "检测到新签到"
        banner = (
            "\n" + "=" * 62 + "\n"
            f"  !! {head} !!    {_now()}\n"
            f"  课程：{activity.get('courseName')}\n"
            f"  活动：{activity.get('name')}  (type={activity.get('type')}, id={activity.get('activeId')})\n"
            + "=" * 62
        )
        self._stats["hits"] += 1
        print(banner, flush=True)
        log.warning("%s | 课程=%s | 活动=%s | activeId=%s | 本轮扫描耗时 %.2f 秒",
                    "再次提醒" if realert else "新签到",
                    activity.get("courseName"), activity.get("name"), activity.get("activeId"),
                    float(self._stats.get("last_scan") or 0))

        record = self.seen.setdefault(str(activity["activeId"]), {})
        record["last_alert"] = time.time()
        record["last_alert_at"] = _now()
        self._save_state()

        if self.dry_run:
            log.info("--dry-run：跳过语音/提示音/打开页面")
            return
        threading.Thread(target=self._alert_worker, args=(activity,),
                         name="cxmon-alert", daemon=True).start()

    def _alert_worker(self, activity: dict) -> None:
        with self._alert_slots:
            try:
                repeat = max(1, int(self.alert_cfg.get("repeat") or 1))
                gap = float(self.alert_cfg.get("repeat_interval") or 0)
                text = self.render_text(activity)

                # ---- 第一步：把"必须立刻发生"的动作全部发出去 ----
                # 实测教训：原来先念完 3 遍语音（间隔 5 秒）才推送，
                # 结果真实签到检测到 11:11:25、手机 11:11:36 才响，白白丢了 11 秒。
                # 签到窗口只有 1~3 分钟，手机推送最不能等 —— 人不在电脑前全靠它。
                if self.alert_cfg.get("beep", True):
                    notifier.beep(3 if not activity.get("_realert") else 1)

                voice_on = self.alert_cfg.get("voice", True)
                speaker = self._get_speaker() if voice_on else None
                if speaker is not None:
                    speaker.speak(text)          # 第一遍：电脑前的人立刻听到

                if self.alert_cfg.get("toast", True):
                    notifier.toast(
                        "学习通签到提醒",
                        f"{activity.get('courseName')} 发起了{activity.get('name')}，请尽快处理",
                    )

                webhook = self.alert_cfg.get("webhook") or ""
                if webhook:
                    notifier.send_webhook(
                        webhook,
                        f"【学习通签到】{activity.get('courseName')}",
                        f"活动：{activity.get('name')}\n时间：{_now()}\nactiveId：{activity.get('activeId')}\n"
                        f"请尽快打开学习通完成签到。",
                    )

                key = str(activity["activeId"])
                if self.alert_cfg.get("open_url", True) and key not in self._opened:
                    self._opened.add(key)
                    url = build_sign_url(self.alert_cfg.get("sign_url_template") or "", activity)
                    notifier.open_in_browser(url)

                # ---- 第二步：再把语音重复几遍，防止你没听见 ----
                if speaker is not None:
                    for _ in range(repeat - 1):
                        if self._stop.wait(gap):
                            break
                        speaker.speak(text)
            except Exception as exc:  # 提醒失败绝不能拖垮监控
                log.exception("提醒动作异常：%s", exc)

    # ------------------------------------------------------------------ 托盘

    def _ensure_tray(self) -> None:
        """确认托盘图标还在；没了就补一个。

        托盘是"关掉面板之后，它在哪"的唯一可见答案，实测会意外消失
        （日志里只有"已显示"没有"已移除"= 进程被杀或崩溃），所以不能指望它一直在。
        """
        try:
            if runtime.instance_running("tray"):
                return
            subprocess.Popen(self_command("tray"),
                             cwd=subprocess_cwd(), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW)
            log.info("发现托盘图标不存在，已自动补上")
        except Exception as exc:                    # 补不上也不能影响监控
            log.debug("补托盘失败：%s", exc)

    # ------------------------------------------------------------------ 主循环

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._save_state()
        with self._speaker_lock:
            if self._speaker_obj is not None:
                self._speaker_obj.close()
                self._speaker_obj = None

    def run(self) -> int:
        if not self.mock and not self.client.cookies:
            raise SystemExit(
                "还没配置 Cookie。\n"
                "  1) 浏览器登录 https://i.chaoxing.com\n"
                "  2) 运行  python cxmon.py cookie  把 Cookie 粘进来\n"
                "（想先看效果可以运行  python cxmon.py monitor --mock）"
            )

        self.resolve_targets()
        discover_interval = float(self.cfg.get("discover_interval") or 600)
        next_discover = time.time() + discover_interval
        next_tray_check = time.time() + TRAY_CHECK_INTERVAL
        scan_quiet_until = time.time() + 60
        if not self.mock:
            self._ensure_tray()

        log.info("开始监控 %d 个班级，扫描间隔 %.1f 秒%s",
                 len(self.targets), self.interval, "（Mock 演示模式）" if self.mock else "")
        for target in self.targets:
            log.info("  · %s (courseId=%s classId=%s)",
                     target.get("name"), target.get("courseId"), target.get("classId"))

        try:
            while not self._stop.is_set():
                cycle_start = time.monotonic()
                try:
                    found = self.scan_once()
                except AuthExpired:
                    # 登录修不好：不退出，每 5 分钟再试一次 ——
                    # 这样你什么时候去学习通客户端登录一下，监控就自己恢复了。
                    log.warning("登录信息不可用，5 分钟后重试（你也可以现在去客户端登录一次）")
                    self._stop.wait(300)
                    self._refresh_used = 0
                    continue
                except SystemExit:
                    raise
                except Exception as exc:
                    self._stats["errors"] += 1
                    log.exception("扫描异常：%s", exc)
                    found = []

                for activity in found:
                    self.alert(activity)

                # 网络健康检查：断网时"看起来正常"，必须主动推手机告诉你
                self._check_network_health()

                # 记录本轮耗时：检测延迟 = 轮询间隔 + 本轮耗时 + 服务端出现该签到的延迟。
                # 有了这个数，才知道"晚了 18 秒"到底是我们的问题还是网络的问题。
                self._stats["last_scan"] = time.monotonic() - cycle_start

                if self.once:
                    if not found:
                        log.info("单次扫描完成：没有发现进行中的签到")
                    break

                now = time.time()
                if now >= next_discover and self.cfg.get("auto_discover", True):
                    next_discover = now + discover_interval
                    try:
                        self.resolve_targets()
                        log.debug("课程列表已刷新，共 %d 个班级", len(self.targets))
                    except SystemExit:
                        raise
                    except Exception as exc:
                        log.warning("刷新课程列表失败：%s", exc)

                if not self.mock and now >= next_tray_check:
                    next_tray_check = now + TRAY_CHECK_INTERVAL
                    self._ensure_tray()

                if not found and now >= scan_quiet_until:
                    scan_quiet_until = now + 60
                    if self._net_state == "down":
                        # 断网时"暂无新签到"是骗人的，这里必须说实话
                        log.warning("心跳：已扫描 %d 轮，但网络断了（%d 个班级全部连不上），"
                                    "这段期间的签到不会被发现",
                                    self._stats["scans"], len(self.targets))
                    else:
                        log.info("心跳：已扫描 %d 轮 / %d 个班级，最近一轮耗时 %.2f 秒，"
                                 "暂无新签到（累计命中 %d 次）",
                                 self._stats["scans"], len(self.targets),
                                 float(self._stats.get("last_scan") or 0), self._stats["hits"])

                # 周期校正：扫描耗时也算在间隔里，否则实际周期 = 间隔 + 扫描耗时
                elapsed = time.monotonic() - cycle_start
                wait = max(0.0, self.interval - elapsed)
                if self.jitter:
                    wait += random.random() * self.jitter
                self._stop.wait(wait)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

        log.info("监控结束：共扫描 %d 轮，命中 %d 次，错误 %d 次",
                 self._stats["scans"], self._stats["hits"], self._stats["errors"])
        return 0
