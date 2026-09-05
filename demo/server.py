# -*- coding: utf-8 -*-
"""
《信息流知道你》Demo 服务端

零第三方依赖（stdlib only），断网可跑。

    uv run demo/server.py            # 默认 http://0.0.0.0:8000
    uv run demo/server.py --port 9000

  手机端   http://<本机IP>:8000/
  大屏端   http://<本机IP>:8000/screen
  后台     http://<本机IP>:8000/admin（需管理员密钥）
"""

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import secrets
import socket
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import engine
from engine import Library, SessionStore, Feeder, match_ads

BASE = Path(__file__).resolve().parent
SEED_DIR = BASE.parent / "事件库"
STATIC = BASE / "static"

LIB = Library(SEED_DIR)
# 统计文件由 SessionStore 统一写入；路径可由 YUNA_METRICS_PATH 指定。
_metrics_path_env = (os.environ.get("YUNA_METRICS_PATH") or "").strip()
METRICS_PATH = Path(_metrics_path_env).expanduser() if _metrics_path_env else BASE / "aggregate_metrics.json"
STORE = SessionStore(LIB, metrics_path=METRICS_PATH)
FEEDER = Feeder(LIB)
SCREEN_RESPONSE_CACHE_LIMIT = 24

# 现场是开放的局域网服务：单个请求与并发会话都设上限，避免一个失控或
# 恶意的客户端把内存与“累计参与”数字刷爆。事件批量上限远高于正常客户端
# （一批通常 < 50 条），长离线后补报也不会被截断。
MAX_BODY_BYTES = 256 * 1024
MAX_EVENTS_PER_BATCH = 1000
MAX_LIVE_SESSIONS = 200

# 大屏推送：200ms 节流合并（§19.2）
SUBSCRIBERS = []
SUB_LOCK = threading.Lock()
FOCUS = {"sid": None, "locked": False, "page": "auto", "since": 0.0}
# 自动焦点的最短驻留：多人同时在线时，“最近活跃”策略会把大屏变成
# 滚马灯（实测 3 人交错点击 3 秒切换 60 次）。驻留期内保持当前焦点。
FOCUS_MIN_DWELL_S = 15.0

# 管理端认证：凭据只存在进程内，登录态使用短期 HttpOnly Cookie。
# 生产/现场可通过 YUNA_ADMIN_PASSWORD（兼容 ADMIN_PASSWORD）注入固定密钥；
# 未配置时为本次进程随机生成一个密钥，并在启动终端打印，避免公开硬编码默认密码。
ADMIN_COOKIE = "yuna_admin_session"
ADMIN_SESSION_TTL = 8 * 3600
_configured_admin_password = (os.environ.get("YUNA_ADMIN_PASSWORD") or
                               os.environ.get("ADMIN_PASSWORD") or "").strip()
ADMIN_PASSWORD_GENERATED = not bool(_configured_admin_password)
ADMIN_PASSWORD = (_configured_admin_password or secrets.token_urlsafe(18))
_ADMIN_PASSWORD_DIGEST = hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).digest()
ADMIN_SESSIONS = {}
ADMIN_SESSION_LOCK = threading.Lock()


def _parse_bool(value, default=False):
    """解析 API 布尔字段，避免把字符串 ``\"false\"`` 当成真值。

    正常客户端发送 JSON boolean；为兼容表单/手工调用，明确的
    ``true/false``（以及常见的 1/0、on/off）字符串也可接受。其它类型
    一律回退到调用方给出的安全默认值，不让任意 truthy 对象改变可见性。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("true", "1", "yes", "on"):
            return True
        if token in ("false", "0", "no", "off"):
            return False
    return bool(default)


def _password_matches(candidate):
    if not isinstance(candidate, str):
        return False
    digest = hashlib.sha256(candidate.encode("utf-8")).digest()
    return hmac.compare_digest(digest, _ADMIN_PASSWORD_DIGEST)


# 登录失败限速：不记录来源（与“不采集 IP”的承诺一致），用全局滑动窗口。
# 现场只有一个后台终端，全局窗口足以挡住暴力尝试；成功登录即清零。
ADMIN_FAIL_WINDOW_S = 300
ADMIN_FAIL_LIMIT = 10
_admin_failed_logins = []


def _new_admin_session():
    token = secrets.token_urlsafe(32)
    expires = time.time() + ADMIN_SESSION_TTL
    with ADMIN_SESSION_LOCK:
        now = time.time()
        for old, until in list(ADMIN_SESSIONS.items()):
            if until <= now:
                ADMIN_SESSIONS.pop(old, None)
        ADMIN_SESSIONS[token] = expires
    return token


def _admin_session_valid(token):
    if not token:
        return False
    now = time.time()
    with ADMIN_SESSION_LOCK:
        expires = ADMIN_SESSIONS.get(token)
        if expires is None:
            return False
        if expires <= now:
            ADMIN_SESSIONS.pop(token, None)
            return False
        return True


def _revoke_admin_session(token):
    if token:
        with ADMIN_SESSION_LOCK:
            ADMIN_SESSIONS.pop(token, None)


def _admin_cookie(value, max_age):
    cookie = SimpleCookie()
    cookie[ADMIN_COOKIE] = value
    morsel = cookie[ADMIN_COOKIE]
    morsel["path"] = "/"
    morsel["max-age"] = str(max_age)
    morsel["httponly"] = True
    morsel["samesite"] = "Strict"
    return morsel.OutputString()


def broadcast():
    payload = screen_state()
    with SUB_LOCK:
        for q in SUBSCRIBERS:
            # 队列只承载“最新状态”；尤其是观众关闭大屏展示后，
            # 不能让旧的画像帧排在撤回通知之前继续发送。
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(payload)
            except queue.Full:
                # 消费线程恰好在清理与写入之间填入一帧时，放弃本轮；
                # 下一次 200ms 广播会再次发送最新状态。
                pass


def pusher():
    while True:
        time.sleep(0.2)
        if SUBSCRIBERS:
            broadcast()


def screen_state():
    visible = []
    for s in STORE.active(limit=16):
        with s.lock:
            if not s.deleted and s.show_on_screen:
                visible.append(s)
    active = visible[:8]
    focus_sid = FOCUS["sid"]
    focus = STORE.get(focus_sid) if focus_sid else None
    focus_finished = False
    if focus:
        with focus.lock:
            if focus.deleted or not focus.show_on_screen:
                focus = None
            else:
                focus_finished = bool(focus.finished)
    now = time.time()
    dwell_expired = now - FOCUS["since"] >= FOCUS_MIN_DWELL_S
    if focus is None or (not FOCUS["locked"] and active and
                         (focus_finished or focus not in active or
                          (dwell_expired and focus is not active[0]))):
        # 锁定的会话被删除或主动退出大屏后，自动切到下一位时同时解除
        # 旧锁定状态，避免响应里出现“locked: true”却实际指向新会话。
        if focus is None and FOCUS["locked"]:
            FOCUS["locked"] = False
        prev_sid = FOCUS["sid"]
        focus = active[0] if active else None
        FOCUS["sid"] = focus.sid if focus else None
        if FOCUS["sid"] != prev_sid:
            FOCUS["since"] = now
        # Stale lock state is meaningless after the locked session expires or
        # is deleted; do not broadcast "locked: true" with an empty focus.
        if focus is None:
            FOCUS["locked"] = False

    # active() 已经保护了会话字典；汇总快照由 SessionStore 在同一把容器锁下
    # 生成白名单快照，避免创建/删除/改反馈恰好发生在广播线程时读到半次更新。
    # 公共大屏只显示已同意展示的在场人数；选择不展示的参与者不应被
    # 旁观者从“在场”数字反推出存在。
    online = len(visible)
    aggregate = STORE.aggregate_stats()
    def other_row(s):
        with s.lock:
            if s.deleted or not s.show_on_screen:
                return None
            top_d, top_score, _ = s.top_domain()
            return {"codename": s.codename,
                    "clicks": s.click_count,
                    "top": engine.DOMAIN_CN.get(top_d, "—") if top_d else "—",
                    "score": round(top_score, 2),
                    "finished": s.finished}

    data = {
        "page": FOCUS["page"],
        "online": online,
        # 待机页只需要两个公开汇总值；会话明细和其余累计字段留在后台。
        "agg": {"participants": aggregate["participants_total"],
                "feedback_good_rate": aggregate["accuracy_rate"]},
        "focus": None,
        "others": [row for row in
                   (other_row(s) for s in active if focus is None or s.sid != focus.sid)
                   if row is not None],
    }
    if focus:
        focus_payload = build_focus(focus)
        for unused_key in ("session_id", "screen_index", "sub_tags",
                           "subtag_evidence", "last_hit", "last_click",
                           "refresh_count", "converged", "finished",
                           "top_domain", "top_score"):
            focus_payload.pop(unused_key, None)
        focus_payload["domain_evidence"] = {
            domain: [{key: row[key] for key in
                      ("title", "action", "domain_cn", "theme")}
                     for row in rows]
            for domain, rows in focus_payload["domain_evidence"].items()
        }
        cross = focus_payload.get("cross_domain")
        if cross:
            focus_payload["cross_domain"] = {
                "axis": cross["axis"], "pole_cn": cross["pole_cn"],
                "evidence": [{key: row[key] for key in
                              ("title", "action", "domain_cn")}
                             for row in cross["evidence"]],
            }
        data["focus"] = focus_payload
    return data


def build_focus(s):
    with s.lock:
        p = s.profile()
        dom_ev = {d: s.evidence_for(d) for d in list(p["domains"])[:3]}
        tag_ev = {t: s.evidence_for(t) for t in list(p["sub_tags"])[:3]}
        cross = None
        for axis, t in p["traits"].items():
            if t["pole"] and t["cross_domain"]:
                cross = {"axis": axis, "axis_cn": t["axis_cn"], "pole_cn": t["pole_cn"],
                         "value": t["value"],
                         "evidence": s.trait_evidence_for(axis)}
                break
        p["domain_evidence"] = dom_ev
        p["subtag_evidence"] = tag_ev
        p["cross_domain"] = cross
        p["summary"] = s.summary_text()
        matched_ads = match_ads(s, LIB, top=1) if p["domains"] else []
        p["ads"] = [{"title": ad["title"], "body": ad["body"],
                     "reasons": ad["reasons"]} for ad in matched_ads]
        return p


def result_payload(s):
    p = build_focus(s)
    p["trait_evidence"] = {a: s.trait_evidence_for(a)
                           for a, t in p["traits"].items() if t["pole"]}
    p["ads"] = [{"title": ad["title"], "body": ad["body"],
                 "reasons": ad["reasons"]}
                for ad in match_ads(s, LIB, top=1)]
    p["privacy"] = {
        "collected": ["本次体验代号", "点击、停留和划过",
                      "本次行为侧写"],
        "retention": "本次体验数据将在 2 小时后自动删除，也可以随时手动删除",
        # 结果页只返回可直接操作的建议。
        "actions": [
             {"n": 1, "title": "关闭个性化推荐",
              "desc": "在相关服务的隐私或推荐设置里，选择不针对个人特征的内容。"},
             {"n": 2, "title": "删除浏览记录",
              "desc": "清理浏览和搜索记录，也可以通过相关服务提供的入口删除个人信息。"},
            {"n": 3, "title": "按需开启权限",
             "desc": "通讯录、位置、相册等权限，只在需要时开启。"},
        ],
        # 为什么可能看错。
        "misread": [
            {"title": "样本太少", "desc": "本次浏览时间较短，结论可能不稳定。"},
            {"title": "行为不等于意图", "desc": "看优惠不等于价格敏感，可能只是随手一点。"},
            {"title": "不了解当时情况", "desc": "它不知道你当时的目的和心情，也可能把偶然点击当成偏好。"},
        ],
    }
    p["bubble_demo"] = bubble_demo(s)
    p["feedback"] = s.feedback
    # 轴标签以 taxonomy 为单一事实来源随结果下发；前端只作渲染兜底，
    # 避免事件库改轴后结果页还停留在手抄的旧文案。
    p["axes"] = {axis: {"axis_cn": engine.AXIS_CN[axis],
                        "pro": engine.TRAIT_CN[engine.POLE_NAME[axis][0]],
                        "con": engine.TRAIT_CN[engine.POLE_NAME[axis][1]]}
                 for axis in engine.AXES}
    # 只有完整结果已成功拼装后才标记完成；若中途出现内容数据异常，手机端还能
    # 通过「再生成一次」重试，而不会把半成品提前标记为已完成。
    s.finished = True
    for internal_key in ("session_id", "updated_at", "screen_index", "hot",
                         "last_hit", "last_click", "refresh_count", "finished",
                         "top_domain", "top_score"):
        p.pop(internal_key, None)
    return p


def bubble_demo(s):
    top_d, _, _ = s.top_domain()
    before = [{"domain": d, "domain_cn": name} for d, name in engine.DOMAIN_CN.items()]
    after = []
    if top_d:
        items = [c for c in LIB.feed_pool if c["domain"] == top_d][:8]
        after = [{"title": c["title"], "domain_cn": c.get("domain_cn", engine.DOMAIN_CN[top_d])}
                 for c in items]
    return {"before": before, "after": after}


def discover_cards(n=8):
    """「发现」栏：不针对个人特征的固定列表（个保法第二十四条）。

    每个域按 content_id 序取一条、域序固定，因此**所有人、任何时刻看到的都一样**，
    与会话状态完全无关：不读画像、不标记 seen、不推进屏序。
    注意这里只是「不个性化排序」，观众在这一栏里的点击照常上报、照常计入画像 ——
    二十四条管的是「推荐」，不是「记录」，结果页会就这一点专门说明。
    """
    by_dom = {}
    for c in sorted(LIB.feed_pool, key=lambda x: x["content_id"]):
        by_dom.setdefault(c["domain"], []).append(c)
    out, i = [], 0
    while len(out) < n:
        added = False
        for d in engine.DOMAIN_CN:                 # 固定域序
            lst = by_dom.get(d) or []
            if i < len(lst):
                out.append(lst[i])
                added = True
                if len(out) >= n:
                    break
        if not added:
            break
        i += 1
    return out


# 进程启动时冻结一次顺序；请求本身不再重新读取会话或重新排序，
# 因而不同设备在同一进程内拿到的「发现」切片严格一致。
DISCOVER_SNAPSHOT = tuple(discover_cards())
# ``discover`` 不写入 session.seen，但其中的卡片确实对观众可见、可以被
# 打开。把 ID 冻结成集合，供正文预取的短期上下文登记做来源校验；否则任意
# 客户端只要把库里的一个 ID 传给 ``open=1``，就能把它伪造成“最近点击”。
DISCOVER_IDS = frozenset(c["content_id"] for c in DISCOVER_SNAPSHOT)


# SessionStore initializes these aggregate counters.  Keep them on the store
# rather than creating ad-hoc module attributes so the same container can be
# reused by offline callers and by the HTTP handlers.


def remember_feedback(s, value):
    # SessionStore 统一处理“最终反馈一次计数”以及原子写入；保留这个
    # 小包装是为了兼容 server.py 既有调用点和离线导入方。
    return STORE.remember_feedback(s, value)


class Handler(BaseHTTPRequestHandler):
    server_version = "FeedKnowsYou/1.0"
    protocol_version = "HTTP/1.1"
    # 读写都走 socket 超时：半开的大屏 SSE 连接不会把线程永久挂在
    # write 上；SSE 每 15s 有 ping，正常连接远碰不到这个上限。
    timeout = 120

    def log_message(self, fmt, *args):        # §6.3 服务器日志不记录 IP 与设备信息
        pass

    # ---------------------------------------------------------- helpers

    def _json(self, obj, code=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, code=200, headers=None):
        # 静态目录只允许服务自身的文件，拒绝 /static/../ 之类的路径穿越。
        try:
            root = STATIC.resolve()
            path = (STATIC / name).resolve()
            path.relative_to(root)
        except (OSError, ValueError, TypeError):
            return self._json({"error": "not found"}, 404)
        if not path.is_file():
            return self._json({"error": "not found"}, 404)
        body = path.read_bytes()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or "javascript" in ctype:
            ctype += "; charset=utf-8"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if not n:
            return {}
        if n > MAX_BODY_BYTES:
            # 不读入超大 body；标记关闭连接，避免 keep-alive 把未读的
            # 剩余字节解析成下一个请求。
            self.close_connection = True
            return None
        raw = self.rfile.read(n).decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            # 登录接口也接受最简单的 form-urlencoded 请求，便于现场手工调用。
            form = parse_qs(raw, keep_blank_values=True)
            return {key: values[-1] for key, values in form.items()}

    def _sid(self, q, body=None):
        sid = (q.get("sid") or [None])[0] or (body or {}).get("session_id")
        if not isinstance(sid, str) or not sid:
            return None
        return STORE.get(sid) if sid else None

    def _admin_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(ADMIN_COOKIE)
        return morsel.value if morsel else None

    def _is_admin(self):
        return _admin_session_valid(self._admin_token())

    def _require_admin(self):
        if self._is_admin():
            return True
        self._json({"error": "admin_auth_required"}, 401)
        return False

    def _admin_login(self, body):
        with ADMIN_SESSION_LOCK:
            now = time.time()
            while _admin_failed_logins and now - _admin_failed_logins[0] > ADMIN_FAIL_WINDOW_S:
                _admin_failed_logins.pop(0)
            if len(_admin_failed_logins) >= ADMIN_FAIL_LIMIT:
                return self._json({"error": "too_many_attempts"}, 429)
        if not isinstance(body, dict) or not _password_matches(body.get("password")):
            with ADMIN_SESSION_LOCK:
                _admin_failed_logins.append(time.time())
            return self._json({"error": "invalid_credentials"}, 401)
        with ADMIN_SESSION_LOCK:
            _admin_failed_logins.clear()
        token = _new_admin_session()
        return self._json({"ok": True, "authenticated": True}, headers={
            "Set-Cookie": _admin_cookie(token, ADMIN_SESSION_TTL),
        })

    def _admin_logout(self):
        _revoke_admin_session(self._admin_token())
        return self._json({"ok": True}, headers={
            "Set-Cookie": _admin_cookie("", 0),
        })

    # ---------------------------------------------------------- routes

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p == "/" or p == "/m":
            return self._file("mobile.html")
        if p == "/screen":
            return self._file("screen.html")
        if p in ("/admin", "/admin/"):
            # 返回登录壳但保留 401 状态；未认证时不泄漏任何看板或会话数据。
            return self._file("admin.html", 200 if self._is_admin() else 401)
        if p.startswith("/static/"):
            return self._file(p[len("/static/"):])

        if p == "/api/stream":
            return self.sse()

        if p == "/api/screen":
            s = self._sid(q)
            if not s:
                return self._json({"error": "session_gone"}, 404)
            mode = str((q.get("mode") or ["next"])[0]).strip().lower()
            if mode not in ("next", "refresh"):
                return self._json({"error": "invalid_request"}, 400)
            raw_request_id = (q.get("request_id") or [None])[0]
            request_id = None
            if raw_request_id is not None:
                request_id = str(raw_request_id).strip()
                if (not request_id or len(request_id) > 96 or
                        any(not (ch.isascii() and (ch.isalnum() or ch in "-_"))
                            for ch in request_id)):
                    return self._json({"error": "invalid_request"}, 400)
            anchor_id = str((q.get("anchor") or [""])[0]).strip() or None
            anchor_seq = (q.get("anchor_seq") or [None])[0]
            # 事件上报与推荐请求可能由不同的 HTTP worker 同时处理；把“取批次、
            # 推进屏序、生成锚点摘要”放在同一把会话锁里，且把客户端点击序号
            # 交给引擎判断，确保响应中的 personalized_from 与实际排序依据是
            # 同一个（而不是被旧请求覆盖的）最近点击。
            with s.lock:
                if s.deleted:
                    return self._json({"error": "session_gone"}, 404)
                cache_key = (mode, request_id) if request_id else None
                cached = (s.screen_response_cache.get(cache_key)
                          if cache_key is not None else None)
                if cached is not None:
                    payload = cached
                else:
                    if mode == "refresh" and s.screen_index <= 0:
                        return self._json({"error": "refresh_not_ready"}, 409)
                    if mode == "refresh":
                        cards = FEEDER.build_refresh(s, anchor_id=anchor_id,
                                                     anchor_seq=anchor_seq)
                        # 刷新不推进屏序：返回当前屏号，让 payload 与大屏/
                        # 画像里的 screen_index 保持同一个语义。
                        screen_index = s.screen_index
                    else:
                        cards = FEEDER.build_screen(s, anchor_id=anchor_id,
                                                    anchor_seq=anchor_seq)
                        screen_index = s.screen_index
                        # 空批次不是完成了一屏；保留屏序让客户端重试时不
                        # 跳过体验阶段，也避免失败请求消耗结束条件。
                        if cards:
                            s.screen_index += 1
                    anchor = FEEDER.anchor_info(s, anchor_id, anchor_seq)
                    personalized_from = ({"content_id": anchor["content_id"],
                                          "title": anchor["title"]}
                                         if anchor else None)
                    payload = {"screen_index": screen_index,
                               "cards": [LIB.card(c) for c in cards],
                               "converged": s.converged(),
                               "personalized_from": personalized_from,
                               "refresh_count": s.refresh_count}
                    # 只缓存客户端能正常使用的非空批次。空素材仍允许同一
                    # request_id 稍后重试，以便内容库恢复后重新生成。
                    if cache_key is not None and cards:
                        s.screen_response_cache[cache_key] = payload
                        while len(s.screen_response_cache) > SCREEN_RESPONSE_CACHE_LIMIT:
                            oldest = next(iter(s.screen_response_cache))
                            s.screen_response_cache.pop(oldest, None)
            return self._json(payload)

        if p == "/api/content":
            s = self._sid(q)
            cid = (q.get("cid") or [""])[0]
            c = LIB.contents.get(cid)
            if not s:
                return self._json({"error": "session_gone"}, 404)
            if not c:
                return self._json({"error": "content_unavailable"}, 404)
            # 正文请求通常比批量 events 更早到达。仅在手机端明确带上
            # open=1 时登记短期推荐上下文；这不计入画像分数，后续真正
            # 的 content_click/content_review 事件仍是唯一计分来源。open_seq
            # 与事件 seq 共用同一条会话序列，防止正文请求乱序回写旧锚点。
            with s.lock:
                if s.deleted:
                    return self._json({"error": "session_gone"}, 404)
                if (q.get("open") or [""])[0] == "1":
                    # 正文 GET 只是点击事件抵达前的“先到信号”。它只能为本会话
                    # 已投放的首页卡，或固定「发现」切片里的卡登记上下文；仍然
                    # 返回正文以保持旧客户端兼容，但未授权的 ID 不影响推荐。
                    context_allowed = cid in s.seen or cid in DISCOVER_IDS
                    if context_allowed:
                        props = {"position": (q.get("position") or [0])[0],
                                 "feed": (q.get("feed") or ["home"])[0]}
                        s.remember_context(cid, props,
                                           (q.get("open_ts") or [None])[0],
                                           (q.get("open_seq") or [None])[0])
            return self._json({"body": c["body"]})

        if p == "/api/discover":
            # 不个性化的对照信息流：无需会话，任何人拿到的都一样
            return self._json({"cards": [LIB.card(c) for c in DISCOVER_SNAPSHOT]})

        if p == "/api/profile":
            s = self._sid(q)
            if not s:
                return self._json({"error": "session_gone"}, 404)
            with s.lock:
                if s.deleted:
                    return self._json({"error": "session_gone"}, 404)
                current_profile = s.profile()
                payload = {key: current_profile[key] for key in
                           ("domains", "sub_tags", "traits", "top_domain", "converged")}
            return self._json(payload)

        if p == "/api/result":
            s = self._sid(q)
            if not s:
                return self._json({"error": "session_gone"}, 404)
            try:
                with s.lock:
                    if s.deleted:
                        return self._json({"error": "session_gone"}, 404)
                    out = result_payload(s)
            except Exception:
                # 结果页有专门的失败出口；不要让一次坏数据把 HTTP worker
                # 直接断掉，也不要把内部异常细节回传给现场设备。
                return self._json({"error": "result_failed"}, 500)
            broadcast()
            return self._json(out)

        if p.startswith("/api/admin/") and not self._require_admin():
            return

        if p == "/api/admin/stats":
            # 先拿一个稳定的会话引用快照，再逐会话加锁读取画像；不在
            # STORE.lock 内等待 Session.lock，避免与事件请求反向嵌套。
            # gc() 会清掉过期的内存会话，先执行后再取得当前会话快照。
            STORE.gc()
            with STORE.lock:
                ss = list(STORE.sessions.values())
            session_rows = []
            done_count = 0
            converge_count = 0
            click_total = 0
            live_count = 0
            for s in ss:
                with s.lock:
                    if s.deleted:
                        continue
                    live_count += 1
                    finished = bool(s.finished)
                    if finished:
                        done_count += 1
                        converge_count += 1 if s.converged() else 0
                    click_total += s.click_count
                    session_rows.append({"sid": s.sid, "codename": s.codename,
                                         "clicks": s.click_count, "events": s.event_count,
                                         "persona": s.persona(), "finished": finished,
                                         "show_on_screen": s.show_on_screen})
            aggregate = STORE.aggregate_stats()
            return self._json({
                "online": live_count, "finished": done_count,
                "participants": aggregate["participants_total"],
                "participants_total": aggregate["participants_total"],
                "online_avg_clicks": (round(click_total / live_count, 2)
                                      if live_count else 0),
                "converge_rate": round(converge_count / done_count, 3) if done_count else 0,
                "feedback_count": aggregate["feedback_total"],
                "feedback_good_rate": aggregate["accuracy_rate"],
                "accuracy_rate": aggregate["accuracy_rate"],
                "persistence_enabled": aggregate["persistence_enabled"],
                "persistence_healthy": aggregate["persistence_healthy"],
                "kappa": engine.KAPPA,
                "focus": FOCUS,
                "sessions": session_rows,
                "library": {"contents": len(LIB.contents), "ads": len(LIB.ads),
                            "probes": len(LIB.probes),
                            "pairs": sum(len(v) for v in LIB.pairs.values())},
            })

        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        body = self._body()
        if body is None:
            return self._json({"error": "payload_too_large"}, 413)
        if not isinstance(body, dict):
            body = {}
        p = u.path

        if p == "/api/admin/login":
            return self._admin_login(body)

        if p == "/api/admin/logout":
            return self._admin_logout()

        if p.startswith("/api/admin/") and not self._require_admin():
            return

        if p == "/api/session":
            raw_request_id = body.get("request_id")
            request_id = None
            if raw_request_id is not None:
                if not isinstance(raw_request_id, str):
                    return self._json({"error": "invalid_request"}, 400)
                request_id = raw_request_id.strip()
                if (len(request_id) < 24 or len(request_id) > 96 or
                        any(not (ch.isascii() and (ch.isalnum() or ch in "-_"))
                            for ch in request_id)):
                    return self._json({"error": "invalid_request"}, 400)
            show_on_screen = _parse_bool(body.get("show_on_screen"), True)
            s = STORE.create(None, show_on_screen, request_id=request_id)
            if s is None:
                # 并发会话已达上限；体验不可用，但不算客户端错误。
                return self._json({"error": "session_limit"}, 503)
            broadcast()
            return self._json({"session_id": s.sid, "codename": s.codename,
                               "show_on_screen": s.show_on_screen})

        if p == "/api/events":
            s = self._sid(q, body)
            if not s:
                return self._json({"error": "session_gone"}, 404)
            events = body.get("events", [])
            if not isinstance(events, list):
                events = []
            elif len(events) > MAX_EVENTS_PER_BATCH:
                events = events[:MAX_EVENTS_PER_BATCH]
            with s.lock:
                if s.deleted:
                    return self._json({"error": "session_gone"}, 404)
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    try:
                        s.on_event(ev)
                    except (TypeError, ValueError, KeyError, AttributeError):
                        continue
            broadcast()
            return self._json({"ok": True})

        if p == "/api/admin/focus":
            requested_sid = body.get("sid")
            FOCUS["sid"] = requested_sid if isinstance(requested_sid, str) and STORE.get(requested_sid) else None
            FOCUS["locked"] = _parse_bool(body.get("locked"), False)
            if FOCUS["sid"] is None:
                FOCUS["locked"] = False
            FOCUS["since"] = time.time()
            broadcast()
            return self._json({"ok": True, "focus": FOCUS})

        if p == "/api/admin/page":
            requested = body.get("page", "auto")
            FOCUS["page"] = requested if requested in ("auto", "standby") else "auto"
            broadcast()
            return self._json({"ok": True, "page": FOCUS["page"]})

        if p == "/api/session/visibility":
            s = self._sid(q, body)
            if not s:
                return self._json({"error": "session_gone"}, 404)
            with s.lock:
                if s.deleted:
                    return self._json({"error": "session_gone"}, 404)
                s.show_on_screen = _parse_bool(body.get("show_on_screen"),
                                               s.show_on_screen)
                if not s.show_on_screen and FOCUS["sid"] == s.sid:
                    FOCUS["sid"] = None
                    FOCUS["locked"] = False
            broadcast()
            return self._json({"ok": True, "show_on_screen": s.show_on_screen})

        if p == "/api/feedback":
            s = self._sid(q, body)
            if not s:
                return self._json({"error": "session_gone"}, 404)
            with s.lock:
                if s.deleted:
                    return self._json({"error": "session_gone"}, 404)
                if not s.finished:
                    return self._json({"error": "feedback_not_ready"}, 409)
                ok = remember_feedback(s, body.get("value"))
                feedback = s.feedback
            if not ok:
                return self._json({"error": "invalid_feedback"}, 400)
            # 反馈接口只回显本会话的最终选择。跨场次的累计人数/认可率
            # 仅在必要的后台或待机汇总中使用，避免任意体验者读取其它
            # 参与者的统计，即使这些数字本身不含会话编号。
            return self._json({"ok": True, "feedback": feedback})

        if p == "/api/admin/kappa":
            try:
                engine.KAPPA = max(0.3, float(body.get("kappa", 2.0)))
            except (TypeError, ValueError):
                return self._json({"error": "bad value"}, 400)
            broadcast()
            return self._json({"ok": True, "kappa": engine.KAPPA})

        if p == "/api/admin/reset":
            n = STORE.reset()
            FOCUS["sid"] = None
            FOCUS["locked"] = False
            broadcast()
            return self._json({"ok": True, "cleared": n,
                               "metrics_preserved": True})

        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/admin/") and u.path != "/api/admin/session":
            if not self._require_admin():
                return

        if u.path == "/api/admin/session":
            if not self._require_admin():
                return
            sid = (q.get("sid") or [None])[0]
            ok = STORE.delete(sid) if sid else False
            if FOCUS["sid"] == sid:
                FOCUS["sid"] = None
                FOCUS["locked"] = False
            broadcast()
            return self._json({"deleted": ok})

        if u.path == "/api/session":
            sid = (q.get("sid") or [None])[0]
            ok = STORE.delete(sid) if sid else False
            if FOCUS["sid"] == sid:
                FOCUS["sid"] = None
                FOCUS["locked"] = False
            broadcast()
            return self._json({"deleted": ok})
        return self._json({"error": "not found"}, 404)

    # ---------------------------------------------------------- SSE

    def sse(self):
        q = queue.Queue(maxsize=8)
        with SUB_LOCK:
            SUBSCRIBERS.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            q.put_nowait(screen_state())
            while True:
                try:
                    data = q.get(timeout=15)
                    chunk = "data: %s\n\n" % json.dumps(data, ensure_ascii=False)
                except queue.Empty:
                    chunk = ": ping\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with SUB_LOCK:
                if q in SUBSCRIBERS:
                    SUBSCRIBERS.remove(q)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class FeedServer(ThreadingHTTPServer):
    # socketserver 默认 backlog=5：现场"全员同时扫码"的建连风暴会被直接
    # 拒绝（实测 200 个并发新建连接约一半 ConnectionRefused）。调大等待队列。
    request_queue_size = 128
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    threading.Thread(target=pusher, daemon=True).start()
    srv = FeedServer((args.host, args.port), Handler)
    ip = lan_ip()
    print("《信息流知道你》Demo 已启动 —— 断网可跑")
    print("  内容库：兴趣+探针 %d 条，模拟广告 %d 条，成对探针 %d 组"
          % (len(LIB.contents), len(LIB.ads), sum(len(v) for v in LIB.pairs.values())))
    print("  手机端  http://%s:%d/" % (ip, args.port))
    print("  大屏端  http://%s:%d/screen" % (ip, args.port))
    print("  后  台  http://%s:%d/admin" % (ip, args.port))
    if ADMIN_PASSWORD_GENERATED:
        print("  后台登录密钥（本次启动随机生成）：%s" % ADMIN_PASSWORD)
    else:
        print("  后台登录密钥：已读取 YUNA_ADMIN_PASSWORD / ADMIN_PASSWORD")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")


if __name__ == "__main__":
    main()
