#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simular Box 保活脚本（参考 InstaBox keepalive.py 思路改写）
==========================================================

解决什么问题
------------
Simular Box（https://box.simular.ai）空闲太久会被平台休眠/回收运行时间。
本脚本每 5 分钟通过 MCP 控制通道做一次轻量活动，让平台一直看到"有人在用"；
万一机器已经被休眠，自动调 box_wake 拉起来。

和 InstaBox 版的差异（为什么逻辑更简单）
----------------------------------------
InstaBox 有 activity-lease / suspend / resume / maxRunMinutes 这些接口，
需要在运行窗口跑满前主动 suspend->resume 重置计时。
Simular Box 实测（2026-11-09，OAuth 元数据 + tools/list）没有这些接口，
计费走 credits，所以这里只保留两件事：

  1. 每 5 分钟调一次 box_status（就是一次平台可见的活动信号，和你的
     MCP 客户端走完全相同的通道）；
  2. state != running 时调 box_wake 自动唤醒。

认证模型（重要）
----------------
Simular 的 MCP 走 OAuth：
  - access token 只有 1 小时寿命；
  - 每次用 refresh token 换新 access token 时，旧的 refresh token 会被
    轮换作废（rotation），必须把新 refresh token 存回来；
  - 刷新请求必须带 resource=https://mcp.box.simular.ai/mcp（RFC 8707），
    否则签发的 access token 没有 audience，调接口会 401 invalid_token。

所以本脚本维护自己独立的 token 对（和 Claude Code 的互不影响）：
  - refresh token 存 /etc/simular-keepalive.env（0600）；
  - access token 缓存到 /var/lib/simular-keepalive/token.json，
    快过期才刷新，减少轮换频率。

协议实测记录（写死在代码里的原因）
----------------------------------
  POST https://mcp.box.simular.ai/mcp            # 管理工具在主端点
      -> initialize（拿 Mcp-Session-Id 响应头）
      -> notifications/initialized（202）
      -> tools/call box_status {"box": "cb-..."}   # structuredContent 里是状态
  注意：/mcp/cb-xxx 这个分机器端点上没有 box_* 管理工具，别用错。
  响应可能是 application/json 或 text/event-stream，两种都解析。

用法
----
    # 第一步：授权（任意目录可跑；会打印一个链接让你点一下，拿专属 token）
    python3 simular_keepalive.py authorize

    # 第二步：装成 systemd 定时任务（root）
    sudo python3 simular_keepalive.py install

    # 手动跑一次
    sudo python3 simular_keepalive.py run

    # 只看状态，不动机器
    sudo python3 simular_keepalive.py status

    # 卸载（env 文件保留）
    sudo python3 simular_keepalive.py uninstall

依赖：Python 3.8+，只用标准库。
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

API_BASE = "https://mcp.box.simular.ai"
TOKEN_EP = API_BASE + "/oauth/token"
AUTHORIZE_EP = API_BASE + "/cb/oauth/authorize"
RESOURCE = API_BASE + "/mcp"
CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
DEFAULT_MCP_URL = API_BASE + "/mcp"

UNIT_NAME = "simular-keepalive"
UNIT_DIR = "/etc/systemd/system"
STATE_DIR = "/opt/simular-keepalive"
CACHE_DIR = "/var/lib/simular-keepalive"
ENV_FILE = "/etc/simular-keepalive.env"
LOG_FILE = "/var/log/simular-keepalive.log"

DEFAULTS = {
    "interval": "300",   # systemd timer 间隔（秒）
}

ENV_NAME = "SIMULAR_KEEPALIVE_ENV"  # 覆盖 env 文件路径的环境变量名


def env_file_path():
    return os.environ.get(ENV_NAME) or ENV_FILE


def log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def atomic_write(path, data, mode=None):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)
    if mode is not None:
        os.chmod(path, mode)


def load_env(path=None):
    """读 KEY=VALUE 文件 -> dict"""
    path = path or env_file_path()
    if not os.path.exists(path):
        return {}
    cfg = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def save_env(cfg, path=None):
    path = path or env_file_path()
    order = ["SIMULAR_REFRESH_TOKEN", "SIMULAR_MCP_URL", "SIMULAR_BOX_ID"]
    lines = ["# Simular Box keepalive 配置（自动维护，勿手工编辑 refresh token）"]
    for k in order:
        if k in cfg:
            lines.append("%s=%s" % (k, cfg[k]))
    for k in sorted(cfg):
        if k not in order:
            lines.append("%s=%s" % (k, cfg[k]))
    atomic_write(path, "\n".join(lines) + "\n", 0o600)


# ---------------------------------------------------------------- HTTP 基础

def http_req(url, data=None, headers=None, timeout=30):
    """返回 (status, headers_dict, body_str)。data 为已编码 bytes。"""
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), (e.read() or b"").decode("utf-8", "replace")


def parse_mcp_body(headers, body, want_id):
    """MCP 响应可能是 application/json 或 SSE，取出 want_id 对应的那条 JSON。"""
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "")
    candidates = []
    if "text/event-stream" in ctype or body.lstrip().startswith("event:"):
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                candidates.append(line[5:].strip())
    else:
        candidates.append(body)
    for raw in candidates:
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if obj.get("id") == want_id or want_id is None:
            return obj
    return None


def mcp_post(url, token, session, payload, timeout=30):
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    st, hs, body = http_req(url, json.dumps(payload).encode(), headers, timeout)
    return st, hs, body


class McpClient:
    def __init__(self, mcp_url, token):
        self.url = mcp_url
        self.token = token
        self.session = None
        self.next_id = 1

    def _rpc(self, method, params=None, notify=False):
        i = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": i, "method": method}
        if params is not None:
            payload["params"] = params
        st, hs, body = mcp_post(self.url, self.token, self.session, payload)
        if st == 401:
            raise RuntimeError("MCP 401 invalid_token（access token 无效/过期）")
        if st not in (200, 202):
            raise RuntimeError("MCP HTTP %s: %s" % (st, body[:200]))
        if notify:
            return None
        obj = parse_mcp_body(hs, body, i)
        if obj is None:
            raise RuntimeError("MCP 响应解析失败: %s" % body[:200])
        if "error" in obj:
            raise RuntimeError("MCP error: %s" % json.dumps(obj["error"])[:200])
        return obj["result"]

    def connect(self):
        sid_header = None
        payload = {
            "jsonrpc": "2.0", "id": self.next_id, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "simular-keepalive", "version": "1.1"},
            },
        }
        i = self.next_id
        self.next_id += 1
        st, hs, body = mcp_post(self.url, self.token, None, payload)
        if st != 200:
            raise RuntimeError("initialize HTTP %s: %s" % (st, body[:200]))
        obj = parse_mcp_body(hs, body, i)
        if obj is None or "result" not in obj:
            raise RuntimeError("initialize 解析失败: %s" % body[:200])
        for k, v in hs.items():
            if k.lower() == "mcp-session-id":
                sid_header = v
                break
        if not sid_header:
            raise RuntimeError("响应里没有 Mcp-Session-Id")
        self.session = sid_header
        self._rpc("notifications/initialized", notify=True)
        return obj["result"].get("serverInfo", {})

    def call_tool(self, name, arguments=None):
        r = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if r.get("isError"):
            raise RuntimeError("tool %s 失败: %s" % (name, json.dumps(r.get("content"))[:200]))
        if isinstance(r.get("structuredContent"), dict):
            return r["structuredContent"]
        for c in r.get("content") or []:
            if c.get("type") == "text":
                try:
                    return json.loads(c["text"])
                except ValueError:
                    return {"text": c["text"]}
        return {}


# ---------------------------------------------------------------- token 管理

def refresh_access_token(refresh_token):
    """用 refresh token 换新 access token。返回 (access_token, expires_in, 新refresh_token或None)。"""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
        "resource": RESOURCE,
    }).encode()
    st, _, resp = http_req(TOKEN_EP, body, {"Content-Type": "application/x-www-form-urlencoded"})
    try:
        d = json.loads(resp)
    except ValueError:
        raise RuntimeError("token endpoint 返回异常 HTTP %s: %s" % (st, resp[:200]))
    if st != 200 or "access_token" not in d:
        raise RuntimeError("refresh 失败 HTTP %s: %s（refresh token 可能已失效，重跑 authorize）"
                           % (st, json.dumps(d)[:200]))
    return d["access_token"], int(d.get("expires_in", 3600)), d.get("refresh_token")


def get_access_token(cfg):
    """优先用缓存的 access token，快过期才刷新；刷新后轮换出的新 rt 立刻写回 env。"""
    path = env_file_path()
    cache = os.path.join(CACHE_DIR, "token.json") if path == ENV_FILE \
        else path + ".token.json"
    if os.path.exists(cache):
        try:
            with open(cache, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("access_token") and d.get("expires_at", 0) > time.time() + 300:
                return d["access_token"]
        except (ValueError, OSError):
            pass
    rt = cfg.get("SIMULAR_REFRESH_TOKEN")
    if not rt:
        raise RuntimeError("env 里没有 SIMULAR_REFRESH_TOKEN，先跑 authorize")
    at, expires_in, new_rt = refresh_access_token(rt)
    if new_rt and new_rt != rt:
        cfg["SIMULAR_REFRESH_TOKEN"] = new_rt
        save_env(cfg)  # 轮换的新 refresh token 必须立刻落盘
        log("refresh token 已轮换并写回 %s" % env_file_path())
    atomic_write(cache, json.dumps({
        "access_token": at,
        "expires_at": time.time() + expires_in - 120,
    }), 0o600)
    return at


def reset_token_cache():
    path = env_file_path()
    cache = os.path.join(CACHE_DIR, "token.json") if path == ENV_FILE \
        else path + ".token.json"
    if os.path.exists(cache):
        os.remove(cache)


# ---------------------------------------------------------------- 授权流程

class _CallbackHandler(BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        q = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(q.query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>授权成功，可以关闭这个页面了。</h2>".encode())

    def log_message(self, *a):
        pass


def cmd_authorize(args):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    port = args.port or 0
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    port = server.server_address[1]
    q = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": "http://localhost:%d/callback" % port,
        "scope": "boxes",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    })
    url = "%s?%s" % (AUTHORIZE_EP, q)
    print("=" * 60)
    print("1. 在浏览器里打开下面的链接并完成授权：")
    print(url)
    print("2. 授权后浏览器会跳到 localhost:%d（页面能打开就是成功）" % port)
    print("   等待回调中（最多 5 分钟）……")
    print("=" * 60)

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + getattr(args, "wait", 300)
    while time.time() < deadline:
        if _CallbackHandler.result:
            break
        time.sleep(0.5)
    server.shutdown()
    res = _CallbackHandler.result
    if not res or "code" not in res:
        raise SystemExit("没等到回调，超时退出。重跑 authorize 再试一次。")
    if res.get("state") != state:
        raise SystemExit("state 不匹配，可能是过期回调，重跑 authorize。")
    if res.get("error"):
        raise SystemExit("授权失败: %s" % res["error"])

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": res["code"],
        "redirect_uri": "http://localhost:%d/callback" % port,
        "code_verifier": verifier,
        "resource": RESOURCE,
    }).encode()
    st, _, resp = http_req(TOKEN_EP, body, {"Content-Type": "application/x-www-form-urlencoded"})
    d = json.loads(resp)
    if st != 200 or "access_token" not in d:
        raise SystemExit("换 token 失败 HTTP %s: %s" % (st, resp[:300]))

    cfg = {
        "SIMULAR_REFRESH_TOKEN": d.get("refresh_token", ""),
        "SIMULAR_MCP_URL": DEFAULT_MCP_URL,
    }
    if args.box_id:
        cfg["SIMULAR_BOX_ID"] = args.box_id
    path = env_file_path()
    if path == ENV_FILE and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        path = os.path.join(os.path.expanduser("~"), "simular-keepalive.env")
        os.environ[ENV_NAME] = path
        print("非 root 运行，env 写到 %s（install 时会自动搬到 /etc）" % path)
    save_env(cfg, path)
    reset_token_cache()
    print("已拿到专属 token，写入 %s" % path)
    print("scope: %s" % d.get("scope"))
    print("下一步：sudo python3 %s install" % os.path.abspath(__file__))


# ---------------------------------------------------------------- 主功能

def pick_box_arg(cfg):
    return {"box": cfg["SIMULAR_BOX_ID"]} if cfg.get("SIMULAR_BOX_ID") else {}


def do_run(cfg, quiet=False):
    at = get_access_token(cfg)
    cli = McpClient(cfg.get("SIMULAR_MCP_URL") or DEFAULT_MCP_URL, at)
    cli.connect()
    m = cli.call_tool("box_status", pick_box_arg(cfg))
    state = m.get("state")
    bid = m.get("box_id") or cfg.get("SIMULAR_BOX_ID") or "(default)"
    saved = m.get("last_saved_at_ms") or 0
    age = "%.0f min" % ((time.time() * 1000 - saved) / 60000) if saved else "?"
    log("state=%s box=%s last_saved=%s 前" % (state, bid, age), )
    if state != "running":
        log("机器不在运行态，调 box_wake 唤醒 …")
        w = cli.call_tool("box_wake", pick_box_arg(cfg))
        log("wake 结果: %s" % json.dumps(w, ensure_ascii=False)[:200])
        m2 = cli.call_tool("box_status", pick_box_arg(cfg))
        log("唤醒后 state=%s" % m2.get("state"))
    else:
        log("ok（本次活动信号已发送）")


def do_status(cfg):
    at = get_access_token(cfg)
    cli = McpClient(cfg.get("SIMULAR_MCP_URL") or DEFAULT_MCP_URL, at)
    info = cli.connect()
    print("server     :", info.get("name"), info.get("version"))
    m = cli.call_tool("box_status", pick_box_arg(cfg))
    saved = m.get("last_saved_at_ms") or 0
    t = datetime.fromtimestamp(saved / 1000, tz=timezone.utc).astimezone() if saved else None
    print("box_id     :", m.get("box_id"))
    print("name       :", m.get("name"))
    print("state      :", m.get("state"))
    print("image      :", m.get("image"), "(latest: %s)" % m.get("latest_image"))
    print("last_saved :", t.strftime("%Y-%m-%d %H:%M:%S") if t else "?")
    print("view_url   :", m.get("view_url"))


# ---------------------------------------------------------------- install

SYSTEMD_SERVICE = """[Unit]
Description=Simular Box keepalive (activity + auto-wake)
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile={env}
ExecStart=/usr/bin/python3 {script} run
StandardOutput=append:{log}
StandardError=append:{log}
"""

SYSTEMD_TIMER = """[Unit]
Description=Run Simular keepalive every {sec} seconds

[Timer]
OnBootSec=60
OnUnitActiveSec={sec}
AccuracySec=10
Unit={name}.service

[Install]
WantedBy=timers.target
"""


def cmd_install(args):
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise SystemExit("install 需要 root：sudo python3 %s install" % os.path.abspath(__file__))

    src_env = env_file_path()
    cfg = load_env(src_env)
    if not cfg.get("SIMULAR_REFRESH_TOKEN") and src_env == ENV_FILE:
        # authorize 以普通用户跑时，env 落在 ~/simular-keepalive.env，搬上来
        home_env = os.path.join(os.path.expanduser("~"), "simular-keepalive.env")
        if os.path.exists(home_env):
            cfg = load_env(home_env)
            src_env = home_env
    if args.refresh_token:
        cfg["SIMULAR_REFRESH_TOKEN"] = args.refresh_token
    if not cfg.get("SIMULAR_REFRESH_TOKEN"):
        raise SystemExit("没有 refresh token：先跑 authorize（或 install --refresh-token xxx）")
    if args.box_id:
        cfg["SIMULAR_BOX_ID"] = args.box_id
    if not cfg.get("SIMULAR_MCP_URL"):
        cfg["SIMULAR_MCP_URL"] = DEFAULT_MCP_URL
    save_env(cfg, ENV_FILE)

    os.makedirs(STATE_DIR, exist_ok=True)
    script_path = os.path.join(STATE_DIR, "simular_keepalive.py")
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as f:
        atomic_write(script_path, f.read(), 0o755)
    os.makedirs(CACHE_DIR, exist_ok=True)

    interval = int(args.interval or DEFAULTS["interval"])
    service = SYSTEMD_SERVICE.format(env=ENV_FILE, script=script_path, log=LOG_FILE)
    timer = SYSTEMD_TIMER.format(sec=interval, name=UNIT_NAME)
    for name, content in (("%s.service" % UNIT_NAME, service), ("%s.timer" % UNIT_NAME, timer)):
        atomic_write(os.path.join(UNIT_DIR, name), content, 0o644)

    def run(cmd):
        subprocess.run(cmd, check=True)

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "%s.timer" % UNIT_NAME])
    subprocess.run(["systemctl", "start", "%s.service" % UNIT_NAME], check=False)
    time.sleep(2)
    subprocess.run(["systemctl", "status", "%s.timer" % UNIT_NAME,
                    "--no-pager", "-n", "0"], check=False)
    print("\n装好了。看日志：  tail -f %s" % LOG_FILE)
    print("手动跑一次：      sudo python3 %s run" % script_path)
    print("停掉：            sudo systemctl disable --now %s.timer" % UNIT_NAME)


def cmd_uninstall(args):
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise SystemExit("uninstall 需要 root")
    subprocess.run(["systemctl", "disable", "--now", "%s.timer" % UNIT_NAME], check=False)
    for name in ("%s.service" % UNIT_NAME, "%s.timer" % UNIT_NAME):
        p = os.path.join(UNIT_DIR, name)
        if os.path.exists(p):
            os.remove(p)
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    print("已卸载（%s 里的脚本和 %s 保留，可手动删）" % (STATE_DIR, env_file_path()))


def main():
    ap = argparse.ArgumentParser(
        description="Simular Box 保活：定期 MCP 活动 + 自动唤醒",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""例子:
  python3 simular_keepalive.py authorize
  sudo python3 simular_keepalive.py install
  sudo python3 simular_keepalive.py run
  sudo python3 simular_keepalive.py status
""")
    ap.add_argument("action", nargs="?", default="run",
                    choices=["run", "status", "authorize", "install", "uninstall"])
    ap.add_argument("--env-file", default=None, help="覆盖 env 文件路径")
    ap.add_argument("--box-id", default=None, help="指定 box id（默认 cb-xxx，可留空）")
    ap.add_argument("--refresh-token", default=None, help="install 时直接提供 refresh token")
    ap.add_argument("--interval", default=None, help="systemd timer 间隔秒数，默认 300")
    ap.add_argument("--port", type=int, default=None, help="authorize 本地回调端口（默认随机）")
    ap.add_argument("--wait", type=int, default=300, help="authorize 等回调的秒数（默认 300）")
    args = ap.parse_args()

    if args.env_file:
        os.environ[ENV_NAME] = args.env_file

    if args.action == "authorize":
        return cmd_authorize(args)
    if args.action == "install":
        return cmd_install(args)
    if args.action == "uninstall":
        return cmd_uninstall(args)

    cfg = load_env()
    try:
        if args.action == "status":
            do_status(cfg)
        else:
            do_run(cfg)
    except RuntimeError as e:
        log("错误: %s" % e)
        sys.exit(1)
    except urllib.error.URLError as e:
        log("网络错误: %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
