"""学习通接口客户端（只读）。

用到的两个接口都是公开的移动端 Web 接口，社区里广泛使用：

1. 课程/班级列表
   GET https://mooc1-api.chaoxing.com/mycourse/backclazzdata?view=json&rss=1
2. 某个班级当前「正在进行」的活动列表（签到就在里面）
   GET https://mobilelearn.chaoxing.com/v2/apis/active/student/activelist
       ?fid=<fid>&courseId=<courseId>&classId=<classId>&_=<时间戳>

学习通是私有接口、随时可能改字段，所以这里解析得非常宽容，
并且提供了 MockTransport（离线假数据）和 probe 命令方便排查。
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("cxmon.api")

MOBILE_BASE = "https://mobilelearn.chaoxing.com"
COURSE_LIST_URL = "https://mooc1-api.chaoxing.com/mycourse/backclazzdata?view=json&rss=1"
ACTIVE_LIST_URL = MOBILE_BASE + "/v2/apis/active/student/activelist"

UA = (
    "Mozilla/5.0 (Linux; Android 13; 22021211RC Build/TP1A.220624.014; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/108.0.0.0 Mobile Safari/537.36"
)


class ChaoxingError(Exception):
    """接口调用失败（网络、返回格式等）。"""


class NetworkError(ChaoxingError):
    """连不上学习通：DNS 解析失败、连接超时、网络不可达等。

    单独分出来是为了让上层能区分两种情况：
      * NetworkError —— 网络断了，等一会儿可能会好，值得推手机提醒你；
      * 其他 ChaoxingError —— 被拒绝 / 返回格式不对，等多久都没用。
    不区分的话，断网只能靠翻日志发现，而断网时监控"看起来是正常的"。
    """


class CookieExpired(ChaoxingError):
    """Cookie 无效或已过期，需要重新获取。"""


def extract_cookie_string(text: str) -> str:
    """从用户粘贴的内容里把 Cookie 抠出来。

    兼容四种粘贴方式：
      1. 纯 Cookie 串            `_uid=1; fid=2; vc3=abc`
      2. 请求头整段（含 cookie: 行）
      3. cURL 命令（DevTools 的 Copy as cURL，里面带 -H 'cookie: ...'）
      4. 带引号/换行的混合内容
    """
    body = (text or "").strip()
    if not body:
        return ""
    # 找 cookie: 行（cURL 的 -H 或请求头块都一样能命中）
    match = re.search(r"(?im)^[ \t]*(?:-H[ \t]+)?['\"]?cookie['\"]?[ \t]*:[ \t]*(.+?)[ \t]*$", body)
    if not match:
        match = re.search(r"(?i)\bcookie['\"]?[ \t]*:[ \t]*([^\r\n]+)", body)
    value = match.group(1) if match else body
    value = value.strip().rstrip("\\").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value.strip()


def parse_cookie(text: str) -> dict:
    """把浏览器里复制的 Cookie 字符串解析成 dict。

    容错：允许换行分隔、多余空格、值里带 '='、值被引号包起来。
    """
    jar: dict[str, str] = {}
    if not text:
        return jar
    for part in str(text).replace("\r", "\n").replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        if key:
            jar[key] = value
    return jar


class ChaoxingClient:
    """读接口封装。transport 可注入（用于 Mock / 测试）。"""

    def __init__(self, cookie: str = "", fid: str = "", uid: str = "",
                 timeout: float = 10, retries: int = 2, transport=None):
        self.cookies = parse_cookie(cookie)
        self.fid = str(fid or self.cookies.get("fid") or "")
        self.uid = str(uid or self.cookies.get("_uid") or self.cookies.get("uid") or "")
        self.timeout = float(timeout or 10)
        self.retries = max(0, int(retries or 0))
        self._transport = transport
        self.cookie_header = "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    # ------------------------------------------------------------------ HTTP

    def _default_get(self, url: str) -> str:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "identity",
            "Referer": MOBILE_BASE + "/",
            "X-Requested-With": "com.chaoxing.mobile",
            "Cookie": self.cookie_header,
        })
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")

    def get(self, url: str) -> str:
        """带重试的 GET，返回文本。

        连接层失败（DNS / 超时 / 不可达）抛 NetworkError，
        其他失败抛 ChaoxingError —— 上层据此决定要不要提示"网络异常"。
        """
        last = None
        network = False
        for attempt in range(self.retries + 1):
            try:
                if self._transport is not None:
                    return self._transport(url)
                return self._default_get(url)
            except urllib.error.HTTPError as exc:
                # HTTPError 是 URLError 的子类，必须排在它前面：
                # 服务器有响应 = 网络其实是通的，不算网络故障
                last = f"HTTP {exc.code}"
                network = False
            except urllib.error.URLError as exc:
                last = f"网络不可达 {exc.reason}"
                network = True
            except OSError as exc:
                last = f"网络错误 {exc}"
                network = True
            if attempt < self.retries:
                time.sleep(0.8 * (attempt + 1) + random.random() * 0.3)
        cls = NetworkError if network else ChaoxingError
        raise cls(f"请求失败 {url} （{last}）")

    def get_json(self, url: str):
        text = self.get(url).strip()
        if not text:
            raise CookieExpired("接口返回空内容，Cookie 可能已失效，请重新获取")
        try:
            return json.loads(text)
        except ValueError:
            head = text[:2000].lower()
            if "<html" in head or "passport" in head or "登录" in text[:2000]:
                raise CookieExpired("接口返回登录页面，Cookie 已失效，请重新获取")
            raise ChaoxingError("接口返回的不是 JSON：" + text[:200].replace("\n", " "))

    # ------------------------------------------------------------- 课程列表

    def get_courses(self) -> list[dict]:
        """返回 [{courseId, classId, name, fid}, ...]。"""
        data = self.get_json(COURSE_LIST_URL)
        if not isinstance(data, dict) or "channelList" not in data:
            raise CookieExpired(
                "课程列表为空或格式异常（通常是 Cookie 失效）：" + str(data)[:200]
            )
        courses, seen = [], set()
        for channel in (data.get("channelList") or []):
            if not isinstance(channel, dict):
                continue
            content = channel.get("content") or {}
            course = content.get("course") or {}
            for item in (course.get("data") or []):
                if not isinstance(item, dict):
                    continue
                course_id = str(item.get("id") or "").strip()
                class_id = str(
                    item.get("clazzid") or item.get("classid") or content.get("id") or ""
                ).strip()
                if not course_id or not class_id:
                    continue
                key = (course_id, class_id)
                if key in seen:
                    continue
                seen.add(key)
                courses.append({
                    "courseId": course_id,
                    "classId": class_id,
                    "name": str(item.get("name") or content.get("name") or "未命名课程"),
                    "fid": str(item.get("fid") or self.fid or ""),
                    "teacher": str(item.get("teacherfactor") or ""),
                })
        return courses

    # ------------------------------------------------------- 进行中的活动

    def active_list_url(self, course_id: str, class_id: str, fid: str = "") -> str:
        """拼出活动列表接口地址（probe / 排查时也用得上）。"""
        params = {
            "fid": str(fid or self.fid or ""),
            "courseId": str(course_id),
            "classId": str(class_id),
            "_": str(int(time.time() * 1000)),
        }
        self.last_active_params = params
        return ACTIVE_LIST_URL + "?" + urllib.parse.urlencode(params)

    def get_active_list(self, course_id: str, class_id: str, fid: str = "") -> list[dict]:
        """拉某个班级当前进行中的活动，返回归一化后的列表。

        签到的 activeId 在返回里可能叫 otherId / activeId / id，
        这里都兼容；原始条目放在 raw 里方便 probe 排查。
        """
        url = self.active_list_url(course_id, class_id, fid)
        params = self.last_active_params
        data = self.get_json(url)

        raw_list = []
        payload = data.get("data") if isinstance(data, dict) else None
        if isinstance(payload, dict):
            raw_list = payload.get("activeList") or payload.get("list") or []
        elif isinstance(payload, list):
            raw_list = payload

        out = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            # ⚠️ 关键：真正的活动 ID 在 id 字段。
            #    otherId 是「签到子类型」（1普通/2手势/3二维码/4位置…），不是 ID ——
            #    早期版本曾把它当 activeId 用，会导致所有签到共用同一个 key 而漏报。
            active_id = item.get("id") or item.get("activeId") or item.get("otherId")
            if not active_id:
                continue
            # 人类可读的活动名在 nameOne（签到/位置签到/随堂练习…），name 常常是空的
            label = item.get("name") or item.get("nameOne") or item.get("title") or "活动"
            out.append({
                "activeId": str(active_id),
                "name": str(label),
                "nameOne": str(item.get("nameOne") or ""),
                "otherId": str(item.get("otherId") or ""),
                "type": item.get("type", item.get("activeType")),
                "activeType": item.get("activeType"),
                "ifphoto": item.get("ifphoto"),
                "status": item.get("status"),
                "startTime": item.get("startTime"),
                "endTime": item.get("endTime"),
                "courseId": str(course_id),
                "classId": str(class_id),
                "fid": params["fid"],
                "raw": item,
            })
        return out


def build_sign_url(template: str, activity: dict) -> str:
    """按模板拼签到页地址；模板里认不出的占位符就原样保留。"""
    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    values = _SafeDict(
        activeId=str(activity.get("activeId") or ""),
        courseId=str(activity.get("courseId") or ""),
        classId=str(activity.get("classId") or ""),
        fid=str(activity.get("fid") or ""),
        name=str(activity.get("name") or ""),
    )
    try:
        return str(template).format_map(values)
    except (ValueError, IndexError):
        return str(template)


# --------------------------------------------------------------------- Mock


class MockTransport:
    """离线演示用假接口：模拟 2 个班，第 3 次和第 8 次扫描时各出现一个新签到。

    用来在没有 Cookie 的情况下验证「发现 → 语音 → 去重 → 状态落盘」整条链路。
    """

    def __init__(self, schedule=(3, 8), ttl: float = 180.0, class_id: str = "1001"):
        self.calls = 0
        self.events: list[dict] = []
        self.schedule = list(schedule)
        self.ttl = ttl
        self.class_id = class_id

    def __call__(self, url: str) -> str:
        self.calls += 1
        while self.schedule and self.calls >= self.schedule[0]:
            self.schedule.pop(0)
            self.events.append({"id": f"mock-{len(self.events) + 1}", "at": time.time()})

        if "backclazzdata" in url:
            return json.dumps({
                "channelList": [
                    {"content": {"id": "1001", "course": {"data": [
                        {"id": "2001", "clazzid": "1001", "name": "【模拟】高等数学"}]}}},
                    {"content": {"id": "1002", "course": {"data": [
                        {"id": "2002", "clazzid": "1002", "name": "【模拟】大学英语"}]}}},
                ]
            }, ensure_ascii=False)

        if "activelist" in url:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            class_id = (query.get("classId") or [""])[0]
            now = time.time()
            active = []
            if class_id == self.class_id:
                for event in self.events:
                    if now - event["at"] < self.ttl:
                        active.append({
                            "id": event["id"],
                            "otherId": event["id"],
                            "name": "签到",
                            "type": 0,
                            "ifphoto": 0,
                            "status": 1,
                            "startTime": int(event["at"] * 1000),
                            "endTime": int((event["at"] + self.ttl) * 1000),
                        })
            return json.dumps({"result": 1, "data": {"activeList": active}}, ensure_ascii=False)

        raise ChaoxingError("MockTransport 不认识的地址：" + url)
