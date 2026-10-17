"""本机面板、凭据与 AutoDL 余额结算。

凭据只活在这个进程的内存里；面板是它唯一的入口，没有文件或环境变量副本。
"""

from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from trainops_base import *
from trainops_graph import budget, forecast, plan_fingerprint, settlement, snapshot
from trainops_specs import price_value, set_instance_price


def api_base():
    return os.environ.get("TRAINOPS_API_BASE", "https://api.autodl.com").rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """带 token 的请求不接受重定向：urllib 会把 Authorization 一起转发到新地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PlanError("AutoDL 接口返回了重定向；为避免 token 被转发到其他地址已中止")


def opener():
    return urllib.request.build_opener(NoRedirect())


def api_post(path, token, timeout=20.0, opener_func=None):
    """唯一的外部调用：只读接口，无请求体。token 只进请求头，绝不写进计划、输出或异常文本。"""
    request = urllib.request.Request(api_base() + path, data=b"", method="POST")
    request.add_header("Authorization", token)
    try:
        with (opener_func or opener)().open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise PlanError(f"AutoDL 接口返回 HTTP {exc.code}；请检查 token 是否正确、是否已完成实名认证") from None
    except PlanError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise PlanError(f"无法连接 AutoDL 接口（{getattr(exc, 'reason', exc)}）；本地计划未受影响") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PlanError(f"AutoDL 接口返回的不是合法 JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise PlanError("AutoDL 接口返回格式异常")
    if payload.get("code") != "Success":
        message = str(payload.get("msg") or "").strip() or "未知错误"
        request_id = str(payload.get("request_id") or "").strip()
        raise PlanError(f"AutoDL 接口返回失败: {message}" + (f"（request_id={request_id}）" if request_id else ""))
    return payload


def milli_to_yuan(value, label):
    """官方约定：余额/代金券都是"千分之一元"的整数；不是整数就说明单位变了，宁可报错也不写错 1000 倍。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"AutoDL 返回的{label}不是数字")
    if not math.isfinite(value) or value < 0:
        raise PlanError(f"AutoDL 返回的{label}异常: {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise PlanError(f"AutoDL 返回的{label} {value!r} 不是整数；官方约定为千分之一元的整数，"
                        "单位可能已变化，请先核对文档")
    return round(value / 1000, 2)


def balance_amounts(payload):
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("assets") is None:
        raise PlanError("AutoDL 余额接口未返回 assets 字段")
    amounts = {"assets": milli_to_yuan(data["assets"], "余额")}
    voucher = data.get("voucher_balance")
    amounts["voucher"] = None if voucher is None else milli_to_yuan(voucher, "代金券")
    return amounts


class Credential:
    """面板登记的开发者 Token：只活在面板进程的内存里，进程一退就没了，重开面板要重填。

    面板是唯一入口，也就不存在"凭据文件该放哪"的问题：仓库被当插件安装后，跟着仓库走的
    任何文件都落在插件副本里（Antigravity 会把插件根整个复制到 ~/.gemini/config/plugins/，
    重新安装就换掉那一份），凭据写进去等于跟着副本走。不落盘还顺带保证 Token 不进取数计划、
    不进日志、不进备份。
    读写都上锁：HTTP 线程登记，结算线程取用。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._set_at = None

    def get(self):
        with self._lock:
            return self._token

    def set(self, token):
        """登记或清除（None）；返回登记时间，未登记时为 None。"""
        with self._lock:
            self._token = token or None
            self._set_at = stamp() if self._token else None
            return self._set_at

    def status(self):
        """只报告"配没配、什么时候配的"：值本身不出现在任何响应、输出或异常里。"""
        with self._lock:
            return {"configured": self._token is not None, "set_at": self._set_at}


def check_token(value):
    """面板填进来的 Token：过短过长、带换行或控制字符都拒绝，返回去掉首尾空白后的值。"""
    if not isinstance(value, str):
        raise PlanError("Token 必须是字符串")
    token = value.strip()
    if not token:
        raise PlanError("Token 不能为空；请粘贴控制台 → 设置 → 开发者Token 里的那一串")
    if len(token) > TOKEN_LIMIT:
        raise PlanError(f"Token 超过 {TOKEN_LIMIT} 字符，请核对是否粘错了内容")
    if any(char < " " or char == "\x7f" for char in token):
        raise PlanError("Token 里不能有换行或控制字符，请重新粘贴")
    return token


def set_wallet(plan, changes, observed_at=None):
    """余额只能从这一处写入：无论是接口拉取还是手工登记，观测时间都由这里统一盖章。

    observed_at 用取数那一刻的时间：结算在文件锁外联网，盖章时间必须早于落盘时间。
    """
    plan["wallet"].update(changes)
    plan["wallet"]["observed_at"] = observed_at or stamp()
    return plan["wallet"]


def sanitize_error(exc):
    """last_error 会落盘也会显示：压成单行并截断，别把多行堆栈写进项目文件。"""
    return " ".join(str(exc).split())[:ERROR_LIMIT]


def cycle(store, interval=None, token=None, timeout=20.0):
    """一轮结算：联网在文件锁之外，落盘只用一次 store.change()。

    token 由调用方从面板登记的那份凭据里现取（[Credential]），这里不做任何解析。
    取数失败（断网、401、超时）只记进 last_error 并保留上一份结论，不抛出——结算线程
    不该因为一次断网就死掉；没登记 Token 更不算故障（那是"没开自动余额"）。
    """
    now = utcnow()
    error, amounts, observed_at = "", None, None
    if token:
        try:
            observed_at = stamp(now)
            amounts = balance_amounts(api_post(API_BALANCE_PATH, token, timeout=timeout))
        except PlanError as exc:
            error = sanitize_error(exc)

    def operation(plan):
        monitor = plan["monitor"]
        if amounts is not None:
            set_wallet(plan, amounts, observed_at)
        if interval is not None:
            monitor["interval_minutes"] = interval
        monitor["last_run"] = stamp(now)
        monitor["token_ready"] = bool(token)
        monitor["last_error"] = error
        values = None
        if not error:
            monitor["last_ok"] = stamp(now)
            # 排程与预算在一轮结算里只算一次：结算快照与面板文案共用同一份结果。
            estimates = forecast(plan, now)
            values = budget(plan, now, estimates)
            # 结算与写入同一次 change：revision 就是这份结论对应的版本，之后任何改动都能对上。
            monitor["estimate"] = {"computed_at": stamp(now), "revision": plan["revision"],
                                   "plan_hash": plan_fingerprint(plan),
                                   **{key: values[key] for key in ESTIMATE_KEYS if key in values}}
        return {"wallet": dict(plan["wallet"]), "monitor": dict(monitor),
                "settlement": settlement(plan, now, live_values=values)}

    result = store.change(operation)
    return {"revision": result["revision"], **result["item"]}


def background_monitor(store, interval, credential, timeout=20.0):
    """面板进程内的结算线程：daemon 线程，失败只记进计划，绝不把面板拖死。

    每轮现取凭据：面板上刚登记的 Token 从下一轮起生效，清除后立刻停下联网。
    """
    def loop():
        while True:
            try:
                cycle(store, interval, credential.get(), timeout)
            except (PlanError, OSError, ValueError) as exc:  # 计划被删/写坏也不该掀掉面板
                print(f"TrainOps: 结算失败 {sanitize_error(exc)}", file=sys.stderr, flush=True)
            time.sleep(interval * 60)

    thread = threading.Thread(target=loop, daemon=True, name="trainops-monitor")
    thread.start()
    return thread


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, store, credential, **kwargs):
        self.store = store
        self.credential = credential
        super().__init__(*args, **kwargs)

    def view(self):
        """面板看到的计划：快照 + 凭据状态。状态只有"配没配、什么时候配的"，没有值。"""
        return {**snapshot(self.store.read()), "credentials": self.credential.status()}

    def respond(self, status, data, content_type="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        """只认 127.0.0.1/localhost：外部域名解析到本机也不能读走或改写计划（防 DNS rebinding）。"""
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.server.server_port == 80:
            hosts.update({"127.0.0.1", "localhost"})
        return self.headers.get("Host", "").lower() in hosts

    def do_GET(self):
        if not self.local():
            self.respond(403, {"error": "只允许本机 Host，不能通过外部域名访问计划"})
            return
        try:
            if urlsplit(self.path).path == "/api/plan":
                self.respond(200, self.view())
            elif urlsplit(self.path).path == "/":
                page = Path(__file__).resolve().parent.parent / "web" / "index.html"
                self.respond(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self.respond(404, {"error": "not found"})
        except (PlanError, OSError, OverflowError) as exc:
            self.respond(500, {"error": str(exc)})

    def do_POST(self):
        """面板的两条写路径：登记时价（写计划）与登记 Token（只进本进程内存）。其余全只读。

        页面只监听本机，但请求可能来自别处，所以三道关：Host 白名单、Origin 必须是本机同源、
        Content-Type 必须是 application/json（跨站 <form> 发不出这个类型，跨站 JSON fetch 要先过预检，
        而本服务不答 OPTIONS、不发 CORS 头）。时价照旧只走 store.change()，不新增第二条写计划的路径；
        Token 压根不落盘。
        """
        if not self.local():
            self.respond(403, {"error": "只允许本机 Host，不能通过外部域名修改计划"})
            return
        path = urlsplit(self.path).path
        if path not in (PRICE_PATH, TOKEN_PATH):
            # 在已知的读取路径上写是"方法不允许"，不存在的路径才是 404。
            if path in ("/", "/api/plan"):
                self.reject_write()
            else:
                self.respond(404, {"error": "not found"})
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {
                f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
            self.respond(403, {"error": "只接受本机面板发出的请求"})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self.respond(415, {"error": "面板只接受 application/json"})
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.respond(400, {"error": "请求缺少合法的 Content-Length"})
            return
        if not 0 <= size <= BODY_LIMIT:
            self.respond(413, {"error": f"请求体过大，最多 {BODY_LIMIT} 字节"})
            return
        try:
            payload = json.loads(self.rfile.read(size))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.respond(400, {"error": f"请求体不是合法 JSON：{exc}"})
            return
        if path == TOKEN_PATH:
            self.register_token(payload)
        else:
            self.register_price(payload)

    def register_price(self, payload):
        """登记时价：写进去的必须是"标价"，不是一次对机器的观测。"""
        # 键集必须恰好是这两种之一：多一个键就报错，免得"顺手改点别的"从这里溜进计划。
        keys = set(payload) if isinstance(payload, dict) else set()
        if keys not in ({"id", "price"}, {"id", "no_gpu_price"}):
            self.respond(400, {"error": '请求体必须恰好是 {"id": 实例编号, "price": 元/小时或 null}'
                                        '（无卡费率用 no_gpu_price）'})
            return
        field = "price" if "price" in keys else "no_gpu_price"
        try:
            plan = self.store.read()
        except (PlanError, OSError, OverflowError) as exc:  # 计划损坏是服务端问题，不能报成 400
            self.respond(500, {"error": str(exc)})
            return
        try:
            instance_id = payload["id"]
            text(instance_id, "实例编号", required=True)
            price = price_value(payload[field])
            find(plan, "instances", instance_id)
        except PlanError as exc:
            self.respond(400, {"error": str(exc)})
            return
        try:
            self.store.change(lambda current: set_instance_price(current, instance_id, field, price))
            self.respond(200, self.view())
        except (PlanError, OSError, OverflowError) as exc:
            self.respond(500, {"error": str(exc)})

    def register_token(self, payload):
        """登记 Token：只存进本进程内存，没有第二处副本。null 表示清除。

        登记后立刻结算一轮：填错了要当场看见"接口拒绝"，而不是等下一个结算周期。
        """
        keys = set(payload) if isinstance(payload, dict) else set()
        if keys != {"token"}:
            self.respond(400, {"error": '请求体必须恰好是 {"token": "开发者 Token 或 null"}'})
            return
        try:
            raw = payload["token"]
            token = None if raw is None else check_token(raw)
        except PlanError as exc:
            self.respond(400, {"error": str(exc)})
            return
        self.credential.set(token)
        try:
            cycle(self.store, token=self.credential.get())
        except (PlanError, OSError, OverflowError) as exc:
            # 凭据已经登记好了，这里失败的是结算本身；照实回报，别让用户以为 Token 没存上。
            self.respond(500, {"error": f"Token 已登记，但本轮结算失败：{exc}"})
            return
        self.respond(200, self.view())

    def reject_write(self):
        self.respond(405, {"error": "只有时价与 Token 能在面板上登记，其余修改请通过 agent/CLI"})

    do_PUT = do_PATCH = do_DELETE = reject_write

    def log_message(self, format, *args):
        pass


def server(store, credential, port=8765):
    return ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, store=store, credential=credential))



__all__ = [
    "api_base", "NoRedirect", "opener", "api_post", "milli_to_yuan", "balance_amounts",
    "Credential", "check_token", "set_wallet", "sanitize_error", "cycle", "background_monitor",
    "Handler", "server",
]
