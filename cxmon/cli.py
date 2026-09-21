"""命令行入口：doctor / cookie / courses / probe / monitor / test-alert。"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__, notifier, runtime
from .chaoxing import (
    ACTIVE_LIST_URL,
    COURSE_LIST_URL,
    MOBILE_BASE,
    ChaoxingClient,
    ChaoxingError,
    extract_cookie_string,
    parse_cookie,
)
from .config import default_config_path, load_config, save_config
from .monitor import Monitor, is_ongoing, match_activity, render_voice_text

log = logging.getLogger("cxmon")


# ------------------------------------------------------------------ 基础工具


def setup_console() -> None:
    """控制台按 UTF-8 输出，并打开 ANSI 颜色。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL
    except Exception:
        pass


def setup_logging(cfg: dict, verbose: bool = False, console: bool = True) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt_file = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                                 "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("cxmon")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    log_path = Path(cfg.get("_dir") or ".") / str(cfg.get("log_file") or "monitor.log")
    try:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG if verbose else logging.INFO)
        fh.setFormatter(fmt_file)
        root.addHandler(fh)
    except OSError as exc:
        print(f"[警告] 无法写入日志文件 {log_path}: {exc}", file=sys.stderr)

    if console and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(level)
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        root.addHandler(sh)


def build_client(cfg: dict) -> ChaoxingClient:
    return ChaoxingClient(
        cookie=cfg.get("cookie") or "",
        fid=cfg.get("fid") or "",
        uid=cfg.get("uid") or "",
        timeout=cfg.get("request_timeout") or 10,
        retries=cfg.get("http_retries") or 0,
    )


# --------------------------------------------------------------------- 命令


def cmd_doctor(args, cfg: dict) -> int:
    print("=== 环境自检 ===")
    print(f"Python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"操作系统    : {os.name} / {sys.platform}")
    print(f"配置文件    : {cfg['_path']}"
          f"{'' if Path(cfg['_path']).exists() else '  [不存在，将使用默认值]'}")
    print(f"日志文件    : {Path(cfg['_dir']) / str(cfg.get('log_file'))}")
    print(f"打包版本    : cxmon {__version__}")

    jar = parse_cookie(cfg.get("cookie") or "")
    uid = cfg.get("uid") or jar.get("_uid") or ""
    fid = cfg.get("fid") or jar.get("fid") or ""
    if jar:
        keys = ", ".join(sorted(jar)[:8])
        print(f"Cookie      : 已配置 {len(jar)} 个字段（{keys} …）")
        print(f"uid / fid   : {uid or '[缺 _uid]'} / {fid or '[缺 fid]'}")
        for need in ("_uid", "fid"):
            if need not in jar and not cfg.get(need.strip("_")):
                print(f"  [警告] Cookie 里没有 {need}，接口可能返回空数据")
    else:
        print("Cookie      : [未配置]  → 运行  python cxmon.py cookie")

    ps = notifier.find_powershell()
    print(f"PowerShell  : {ps}  {'[OK]' if Path(ps).exists() else '[找不到，语音会失败]'}")
    voices = notifier.list_voices()
    if voices:
        print(f"系统发音人  : {len(voices)} 个")
        for voice in voices:
            print(f"    · {voice}")
    else:
        print("系统发音人  : [枚举失败] 语音可能不可用")

    print("连通性      : 正在测试 mobilelearn.chaoxing.com …")
    try:
        req = urllib.request.Request(MOBILE_BASE + "/", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            print(f"    HTTP {resp.status}  可以访问")
    except Exception as exc:
        print(f"    [失败] {exc}")
        print("    如果这里失败，本工具无法工作：检查网络/代理/校园网。")

    if args.speak:
        print("语音测试    : 正在朗读 …")
        speaker = notifier.VoiceSpeaker(cfg["alert"].get("voice_name") or "",
                                        cfg["alert"].get("voice_rate") or 0)
        ok = speaker.speak_and_wait("语音测试成功，学习通签到监控已就绪")
        speaker.close()
        print(f"    朗读{'成功' if ok else '失败'}")
    else:
        print("语音测试    : 跳过（加 --speak 可实测一次）")
    return 0


def cmd_cookie(args, cfg: dict) -> int:
    if args.value:
        text = args.value
    elif args.from_file:
        text = Path(args.from_file).read_text(encoding="utf-8-sig").strip()
    else:
        print("请粘贴完整 Cookie 后按回车（Ctrl+C 取消）")
        print("  支持直接粘贴 Cookie 串、整段请求头、或 DevTools 的 Copy as cURL 内容")
        try:
            text = sys.stdin.readline()
        except KeyboardInterrupt:
            print("\n已取消")
            return 1
    raw = text.strip()
    jar = parse_cookie(extract_cookie_string(raw))
    if not jar:
        print("[失败] 没解析出任何 Cookie 字段，确认复制的是 `名称=值; 名称=值` 这种格式")
        return 2

    cfg["cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
    cfg["uid"] = jar.get("_uid") or jar.get("uid") or cfg.get("uid") or ""
    cfg["fid"] = jar.get("fid") or cfg.get("fid") or ""
    path = save_config(cfg)
    print(f"[OK] 已保存到 {path}")
    print(f"     字段数：{len(jar)}   uid={cfg['uid'] or '?'}   fid={cfg['fid'] or '?'}")
    if "_uid" not in jar and "uid" not in jar:
        print("     [警告] Cookie 里没有 _uid，接口可能拉不到数据")

    if args.no_verify:
        return 0
    print("正在用这个 Cookie 拉课程列表验证 …")
    try:
        courses = build_client(cfg).get_courses()
    except (ChaoxingError, SystemExit) as exc:
        print(f"[失败] {exc}")
        print("       Cookie 可能复制不完整或已过期，重新登录后再复制一次。")
        return 3
    print(f"[OK] Cookie 有效，共发现 {len(courses)} 个班级：")
    for course in courses[:10]:
        print(f"     · {course['name']}  (courseId={course['courseId']}, classId={course['classId']})")
    if len(courses) > 10:
        print(f"     … 其余 {len(courses) - 10} 个见  python cxmon.py courses")
    return 0


def cmd_courses(args, cfg: dict) -> int:
    try:
        courses = build_client(cfg).get_courses()
    except (ChaoxingError, SystemExit) as exc:
        print(f"[失败] {exc}")
        return 3
    print(f"共 {len(courses)} 个班级：")
    for i, course in enumerate(courses, 1):
        print(f"{i:>3}. {course['name']}")
        print(f"     courseId={course['courseId']}  classId={course['classId']}  fid={course['fid']}")
    print("\n只想盯其中几个班，就把对应的 courseId/classId 填进 config.json 的 targets。")
    return 0


def cmd_probe(args, cfg: dict) -> int:
    """把接口原始返回打出来，方便确认字段名有没有变。"""
    client = build_client(cfg)
    targets = [{"courseId": str(t.get("courseId")), "classId": str(t.get("classId")),
                "name": str(t.get("name") or "")} for t in (cfg.get("targets") or [])]
    if not targets:
        print("拉取课程列表 …")
        try:
            targets = client.get_courses()
        except (ChaoxingError, SystemExit) as exc:
            print(f"[失败] {exc}")
            return 3
    print(f"课程列表接口 : {COURSE_LIST_URL}")
    print(f"活动列表接口 : {ACTIVE_LIST_URL}")
    limit = len(targets) if args.all else min(3, len(targets))
    for target in targets[:limit]:
        url = client.active_list_url(target["courseId"], target["classId"], target.get("fid") or "")
        print("\n" + "-" * 62)
        print(f"班级：{target.get('name')}  courseId={target['courseId']} classId={target['classId']}")
        print(f"URL ：{url}")
        try:
            text = client.get(url)
            data = json.loads(text)
        except (ChaoxingError, ValueError) as exc:
            print(f"[失败] {exc}")
            continue
        normalized = client.get_active_list(target["courseId"], target["classId"],
                                           target.get("fid") or "")
        print(f"进行中的活动：{len(normalized)} 个")
        for act in normalized:
            hit = match_activity(act, cfg.get("match") or {})
            stay = "进行中" if is_ongoing(act) else "已结束/未开始"
            print(f"  · activeId={act['activeId']} type={act['type']} "
                  f"名称={act.get('nameOne') or act['name']} subType={act.get('otherId') or '-'} "
                  f"[{stay}] → {'会提醒' if hit else '忽略'}"
                  f"{'' if hit and stay == '进行中' else '（' + ('类型/名称不匹配' if not hit else '不在进行中') + '）'}")
        if args.raw:
            print("原始返回：")
            print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
    return 0


def manual_steps() -> str:
    """手动从 DevTools 复制 Cookie 的步骤（浏览器自己解密，绕开 App-Bound）。"""
    return "\n".join([
        "    1) 切到那个已登录学习通的标签页（停在 i.chaoxing.com 页面上）",
        "    2) 按 F12 打开开发者工具",
        "    3) 点顶部的「网络 / Network」标签",
        "    4) 按 F5 刷新页面，左边会出现一串请求",
        "    5) 点左边列表里【最上面那一条】（通常是 i.chaoxing.com）",
        "    6) 右边点「标头 / Headers」，往下滚到「请求标头 / Request Headers」",
        "    7) 找到以 cookie: 开头的那一行 → 三击选中 → Ctrl+C",
        "    8) 运行  python cxmon.py cookie  然后粘贴、回车",
        "    小技巧：第 5 步也可以在请求上右键 → 复制 → 以 cURL 格式复制，",
        "            把整段 cURL 粘进来，本工具一样能自动识别。",
    ])


def cmd_browser_cookie(args, cfg: dict) -> int:
    """自动从本机 Chrome / Edge 读取学习通 Cookie（推荐方式）。

    浏览器运行时 Cookie 库被独占，所以：
      * --wait-close 会挂着等浏览器退出，退出后自动继续
      * 拿到候选后会真的拿去请求学习通验证，验证不过（Cookie 过期）就继续等，
        这样"去 Edge 登录一次 → 关掉 Edge"这种流程可以一把过
    """
    from .browser_cookie import collect_candidates, jar_to_header, summarize

    browsers = None if args.browser == "auto" else [args.browser]
    say = log.info

    print("正在从浏览器读取学习通 Cookie …")
    print("（只读 chaoxing.com / xuexitong.com 域，不改动浏览器数据，也不打印明文）")
    deadline = time.monotonic() + (args.wait_seconds if args.wait_close else 0.0)
    if args.wait_close:
        print(f"（最多等 {int(args.wait_seconds)} 秒，你退出浏览器后会自动继续，无需重跑）")

    last_error = ""
    while True:
        remaining = max(0.0, deadline - time.monotonic()) if args.wait_close else 0.0
        candidates, locked, app_bound = collect_candidates(browsers, log=say,
                                                          wait_seconds=remaining)

        if not candidates:
            if args.wait_close and locked and not app_bound and time.monotonic() < deadline:
                continue          # collect_candidates 内部已等待并打印过进度
            print("\n[失败] 没拿到学习通 Cookie。")
            if app_bound:
                print("  原因：浏览器把 Cookie 换成了 App-Bound 加密（密文前缀 v20）。")
                print("  这是 Chrome/Edge 127+ 的反抓取设计：密钥绑死在浏览器程序上，")
                print("  只有浏览器自己解得开 —— 关掉浏览器、复制数据库都没用。")
                print("  唯一可行办法：让浏览器自己把 Cookie 交出来（DevTools）。步骤：")
                print(manual_steps())
            elif locked:
                print("  原因：Cookie 数据库正被运行中的浏览器独占（Windows 不允许同时读取）。")
                print("  解决办法：完全退出浏览器后重跑本命令，或加 --wait-close 挂着等。")
                print(f"  （被占用的库：{'、'.join(locked)}）")
            else:
                print("  可能原因：")
                print("    1) 浏览器里还没登录学习通 → 打开 https://i.chaoxing.com 登录后重试")
                print("    2) 用的是本脚本不支持的浏览器（目前支持 Chrome / Edge）")
                print("    3) 浏览器启用了 App-Bound 加密 → 请用下面的手动办法")
                print(manual_steps())
            return 4

        if args.list:
            print(f"\n共 {len(candidates)} 个候选（未验证、未保存）：")
            for cand in candidates:
                print(f"  · {cand['label']}  关键字段 {cand['score']}/4  {summarize(cand['jar'])}")
            return 0

        print(f"\n共 {len(candidates)} 个候选，逐个拿去请求学习通验证 …")
        for cand in candidates:
            header = jar_to_header(cand["jar"])
            print(f"\n--- {cand['label']} ---")
            print(f"    字段摘要：{summarize(cand['jar'])}")
            probe_cfg = dict(cfg, cookie=header,
                             uid=cand["jar"].get("_uid") or "", fid=cand["jar"].get("fid") or "")
            try:
                courses = build_client(probe_cfg).get_courses()
            except (ChaoxingError, SystemExit) as exc:
                last_error = str(exc)
                print(f"    验证失败：{last_error[:120]}")
                continue

            print(f"    [OK] 验证通过，共 {len(courses)} 个班级")
            if args.no_save:
                print("    --no-save：按要求不写入配置")
                return 0
            cfg["cookie"] = header
            cfg["uid"] = cand["jar"].get("_uid") or cand["jar"].get("uid") or ""
            cfg["fid"] = cand["jar"].get("fid") or ""
            path = save_config(cfg)
            print(f"    [OK] 已写入 {path}  (uid={cfg['uid']}, fid={cfg['fid']})")
            print("    你的班级：")
            for course in courses[:10]:
                print(f"      · {course['name']}  (courseId={course['courseId']}, "
                      f"classId={course['classId']})")
            if len(courses) > 10:
                print(f"      … 其余 {len(courses) - 10} 个见  python cxmon.py courses")
            print("\n下一步：python cxmon.py monitor")
            return 0

        # 候选全都验证不过 —— 如果还允许等，就继续等用户重新登录
        if args.wait_close and time.monotonic() < deadline:
            remain = int(deadline - time.monotonic())
            print(f"\n[继续等待] 上面的 Cookie 都已失效。请在浏览器里重新登录 "
                  f"https://i.chaoxing.com ，然后退出浏览器（剩余 {remain} 秒）…")
            log.info("候选 Cookie 全部失效，继续等待重新登录（剩余 %s 秒）", remain)
            time.sleep(5)
            continue

        print("\n[失败] 所有候选都没通过验证（Cookie 大多已过期）。")
        if last_error:
            print(f"       最后一次服务端回应：{last_error[:120]}")
        print("       请在浏览器里重新登录 https://i.chaoxing.com ，再运行一次本命令。")
        return 3


def cmd_monitor(args, cfg: dict) -> int:
    existing = runtime.read_pid(cfg)
    if existing and not args.force:
        print(f"[已有监控在运行] PID {existing}，这次不会重复启动。")
        print("  同时开两个的话，同一场签到会提醒两遍、微信也会推两条。")
        print("  想查看状态或停止：双击 启动监控.bat 打开控制面板")
        print("  确实要再开一个：加 --force 参数")
        return 0

    runtime.write_pid(cfg)
    atexit.register(runtime.clear_pid, cfg, os.getpid())
    try:
        monitor = Monitor(cfg, mock=args.mock, once=args.once, dry_run=args.dry_run,
                          interval=args.interval)
        return monitor.run()
    finally:
        runtime.clear_pid(cfg, os.getpid())


def cmd_tray(args, cfg: dict) -> int:
    """启动托盘图标（让关掉面板后也能看见、能找回）。"""
    from . import tray
    return tray.main(cfg, selftest=args.selftest)


def cmd_panel(args, cfg: dict) -> int:
    """打开图形控制面板（给不熟悉命令行的使用者）。"""
    from . import panel
    return panel.main(cfg, selftest=args.selftest)


def cmd_test_alert(args, cfg: dict) -> int:
    """不联网，只把整条提醒链路走一遍。"""
    alert = cfg["alert"]
    activity = {
        "activeId": "test-0001",
        "name": args.name,
        "type": 0,
        "courseName": args.course,
    }
    text = render_voice_text(alert, activity)
    print("=" * 62)
    print("  这是一次提醒测试")
    print(f"  课程：{activity['courseName']}")
    print(f"  活动：{activity['name']}")
    print(f"  播报文本：{text}")
    print("=" * 62)

    if args.dry_run:
        print("--dry-run：不实际播报")
        return 0

    repeat = max(1, int(args.repeat if args.repeat else 1))
    if alert.get("voice", True):
        speaker = notifier.VoiceSpeaker(alert.get("voice_name") or "", alert.get("voice_rate") or 0)
        for i in range(repeat):
            print(f"  朗读第 {i + 1}/{repeat} 遍 …")
            speaker.speak_and_wait(text)
        speaker.close()
    if alert.get("beep", True):
        notifier.beep(2)
    if alert.get("toast", True):
        notifier.toast("学习通签到提醒（测试）", f"{activity['courseName']} 发起了{activity['name']}")
    if alert.get("webhook"):
        notifier.send_webhook(alert["webhook"], "【测试】学习通签到提醒", text)
    if alert.get("open_url"):
        from .chaoxing import build_sign_url
        notifier.open_in_browser(build_sign_url(alert.get("sign_url_template") or "", activity))
    print("测试完成。如果没听到声音，检查系统音量和默认播放设备，或运行 doctor --speak。")
    return 0


# --------------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cxmon",
        description="学习通签到监控：发现新签到立刻语音提醒（只提醒，不代签）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用：
  python cxmon.py doctor --speak     环境自检 + 试听语音
  python cxmon.py cookie             粘贴 Cookie
  python cxmon.py monitor            开始监控
  python cxmon.py monitor --mock     离线看效果（无需 Cookie）
""")
    parser.add_argument("-c", "--config", default=None, help="配置文件路径（默认 ./config.json）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=f"cxmon {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("doctor", help="检查环境、Cookie、发音人、网络连通性")
    p.add_argument("--speak", action="store_true", help="顺便实测一次语音")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("cookie", help="粘贴/更新 Cookie 并验证")
    p.add_argument("--value", help="直接给 Cookie 字符串（不推荐，会留在命令历史里）")
    p.add_argument("--from-file", help="从文件读取 Cookie")
    p.add_argument("--no-verify", action="store_true", help="只保存不验证")
    p.set_defaults(func=cmd_cookie)

    p = sub.add_parser("browser-cookie",
                       help="自动从 Chrome/Edge 读取学习通 Cookie（最省事）")
    p.add_argument("--browser", choices=["auto", "cxstudy", "chrome", "edge"], default="auto",
                   help="指定来源，默认自动（cxstudy=学习通 PC 客户端）")
    p.add_argument("--list", action="store_true", help="只列出找到的候选，不验证也不保存")
    p.add_argument("--no-save", action="store_true", help="验证通过但先不写入配置")
    p.add_argument("--wait-close", action="store_true",
                   help="浏览器没关时挂起等待，你退出浏览器后脚本自动继续")
    p.add_argument("--wait-seconds", type=float, default=300.0,
                   help="--wait-close 的最长等待秒数（默认 300）")
    p.set_defaults(func=cmd_browser_cookie)

    p = sub.add_parser("courses", help="列出所有班级及其 courseId/classId")
    p.set_defaults(func=cmd_courses)

    p = sub.add_parser("probe", help="打印活动列表接口的原始返回，排查字段变化")
    p.add_argument("--all", action="store_true", help="所有班级都探一遍（默认前 3 个）")
    p.add_argument("--raw", action="store_true", help="打印完整原始 JSON")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("monitor", help="开始监控（核心命令）")
    p.add_argument("--mock", action="store_true", help="离线演示模式，不联网、不需要 Cookie")
    p.add_argument("--once", action="store_true", help="只扫一轮就退出")
    p.add_argument("--dry-run", action="store_true", help="只打印不发声、不弹窗、不打开页面")
    p.add_argument("--interval", type=float, help="覆盖配置里的扫描间隔（秒）")
    p.add_argument("--force", action="store_true", help="已有监控在跑时仍要再开一个（一般不要）")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("panel", help="打开图形控制面板（推荐给不熟悉命令行的使用者）")
    p.add_argument("--selftest", action="store_true", help="只构建界面并读一次状态后退出（自检用）")
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("tray", help="只启动右下角托盘图标（面板关了它还在）")
    p.add_argument("--selftest", action="store_true", help="注册一次托盘图标后退出（自检用）")
    p.set_defaults(func=cmd_tray)

    p = sub.add_parser("test-alert", help="测试提醒链路（语音/提示音/气泡/推送）")
    p.add_argument("--course", default="【测试】高等数学", help="测试用的课程名")
    p.add_argument("--name", default="签到", help="测试用的活动名")
    p.add_argument("--repeat", type=int, default=1, help="朗读几遍")
    p.add_argument("--dry-run", action="store_true", help="只显示将要播报的文本")
    p.set_defaults(func=cmd_test_alert)
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0

    config_path = Path(args.config) if args.config else default_config_path()
    cfg = load_config(config_path)
    # 面板可能由 pythonw 启动（没有控制台），这时别装控制台日志处理器
    setup_logging(cfg, verbose=args.verbose,
                  console=not (getattr(args, "cmd", "") == "panel" and sys.stdout is None))
    log.debug("配置文件：%s", cfg["_path"])
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except SystemExit as exc:
        message = exc.code if isinstance(exc.code, str) else ""
        if message:
            print(f"\n{message}".rstrip())
            # 同时写进日志：面板启动监控时 stdout 是丢弃的，只有日志能看到失败原因
            log.error("%s", " ".join(str(message).split()))
        return int(exc.code) if isinstance(exc.code, int) else 1
    except ChaoxingError as exc:
        log.error("%s", exc)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
