#!/usr/bin/env python3
"""FreebuffProxy Python 版 - 管理面板 + OpenAI 兼容 API（功能对齐 Go 版前端）

用法:
  python3 server.py --listen :18082 --proxy http://127.0.0.1:8078

环境变量:
  FREEBUFF_AUTH_TOKEN   默认 token（AUTH_TOKENS 为空时用）
  FREEBUFF_ACTING_USER_ID
  CONFIG_FILE           config.json 路径（默认 ./config.json）
"""
import json, os, sys, time, uuid, argparse, threading, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen, ProxyHandler, build_opener
from urllib.error import HTTPError

# ============ 配置 ============
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
DEFAULT_AUTH_TOKEN = os.environ.get("FREEBUFF_AUTH_TOKEN", "")
DEFAULT_ACTING_USER_ID = os.environ.get("FREEBUFF_ACTING_USER_ID", "")
DEFAULT_PROXY = os.environ.get("HTTPS_PROXY", "")

BASE_URL = "https://www.codebuff.com"
AUTH_BACKEND = "https://freebuff.llm.pm"
UA_SESSION = "Bun/1.3.14"
UA_CHAT = "ai-sdk/openai-compatible/0.0.0-test/codebuff ai-sdk/provider-utils/3.0.25 runtime/browser"

# 免费 agents 映射（对齐 Go 版）
AGENT_MAP = {
    "deepseek/deepseek-v4-flash": "base2-free-deepseek-flash",
    "mimo/mimo-v2.5": "base2-free-mimo",
    "minimax/minimax-m2.7": "base2-free-minimax",
    "z-ai/glm-5.1": "base2-free-glm",
}
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
ALL_MODELS = list(AGENT_MAP.keys())

# ============ 配置加载 ============
def load_config():
    cfg = {
        "auth_tokens": [],
        "acting_user_id": DEFAULT_ACTING_USER_ID,
        "api_keys": ["admin123"],
        "proxy": DEFAULT_PROXY,
        "proxy_pool": [],
    }
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        cfg["auth_tokens"] = data.get("AUTH_TOKENS", []) or []
        cfg["acting_user_id"] = data.get("ACTING_USER_ID", cfg["acting_user_id"])
        cfg["api_keys"] = data.get("API_KEYS", cfg["api_keys"]) or ["admin123"]
        cfg["proxy"] = data.get("HTTP_PROXY", cfg["proxy"])
        cfg["proxy_pool"] = data.get("PROXY_POOL", []) or []
    except FileNotFoundError:
        pass
    return cfg

def save_config(cfg):
    data = {
        "AUTH_TOKENS": cfg["auth_tokens"],
        "ACTING_USER_ID": cfg["acting_user_id"],
        "API_KEYS": cfg["api_keys"],
        "HTTP_PROXY": cfg["proxy"],
        "PROXY_POOL": cfg["proxy_pool"],
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

CONFIG = load_config()

# ============ 日志 ============
class LogStore:
    def __init__(self, max_logs=500):
        self.max_logs = max_logs
        self.logs = []
        self.lock = threading.Lock()
    def add(self, level, source, message):
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "level": level, "source": source, "message": message}
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > self.max_logs:
                self.logs = self.logs[-self.max_logs:]
    def all(self):
        with self.lock:
            return list(self.logs)
    def clear(self):
        with self.lock:
            self.logs = []

LOGS = LogStore()

def log_info(source, msg): LOGS.add("info", source, msg)
def log_warn(source, msg): LOGS.add("warn", source, msg)
def log_error(source, msg): LOGS.add("error", source, msg)

# ============ 用量统计 ============
class UsageStore:
    def __init__(self):
        self.entries = []  # {ts, model, api_key, tokens}
        self.lock = threading.Lock()
    def record(self, model, api_key, tokens):
        with self.lock:
            self.entries.append({"ts": time.time(), "model": model, "api_key": api_key, "tokens": tokens})
            # 只保留 30 天
            cutoff = time.time() - 30*86400
            self.entries = [e for e in self.entries if e["ts"] >= cutoff]
    def summary(self, days=1):
        cutoff = time.time() - days*86400
        by_model, by_key = {}, {}
        with self.lock:
            for e in self.entries:
                if e["ts"] < cutoff: continue
                by_model[e["model"]] = by_model.get(e["model"], 0) + e["tokens"]
                k = e["api_key"] or "(默认)"
                by_key[k] = by_key.get(k, 0) + e["tokens"]
        return {"by_model": by_model, "by_api_key": by_key}

USAGE = UsageStore()

# ============ 模型启用状态 ============
MODEL_ENABLED = {}
MODEL_ENABLED_LOCK = threading.Lock()

# ============ 代理池 ============
class ProxyPool:
    def __init__(self):
        self.entries = []  # {addr, mode, latency_ms, alive, fail_count}
        self.lock = threading.Lock()
        self.current = None
    def load(self, addrs):
        with self.lock:
            self.entries = [{"addr": a, "mode": "unknown", "latency_ms": 0, "alive": True, "fail_count": 0} for a in addrs]
    def list(self):
        with self.lock:
            return list(self.entries)
    def add(self, addrs):
        added = 0
        with self.lock:
            existing = {e["addr"] for e in self.entries}
            for a in addrs:
                a = a.strip()
                if a and ":" in a and a not in existing:
                    self.entries.append({"addr": a, "mode": "unknown", "latency_ms": 0, "alive": True, "fail_count": 0})
                    existing.add(a)
                    added += 1
        return added
    def remove(self, addr):
        with self.lock:
            before = len(self.entries)
            self.entries = [e for e in self.entries if e["addr"] != addr]
            if self.current == addr: self.current = None
            return len(self.entries) < before
    def save(self):
        with self.lock:
            CONFIG["proxy_pool"] = [e["addr"] for e in self.entries]
        save_config(CONFIG)
    def get_proxy(self):
        """选当前代理（优先 full 且延迟低的）"""
        with self.lock:
            if not self.entries: return CONFIG["proxy"]
            best = None
            for e in self.entries:
                if not e["alive"]: continue
                if e["mode"] == "full":
                    if best is None or e["latency_ms"] < best["latency_ms"]:
                        best = e
            if best is None:
                best = next((e for e in self.entries if e["alive"]), None)
            if best is None: return CONFIG["proxy"]
            self.current = best["addr"]
            return "http://" + best["addr"]
    def check_all(self, token):
        """检测所有代理的 full/limited 模式"""
        with self.lock:
            entries = list(self.entries)
        checked = 0
        for e in entries:
            try:
                mode, latency = check_proxy_mode(e["addr"], token)
                with self.lock:
                    for x in self.entries:
                        if x["addr"] == e["addr"]:
                            x["mode"] = mode
                            x["latency_ms"] = latency
                            x["alive"] = True
                            x["fail_count"] = 0
                            break
                checked += 1
            except Exception as ex:
                with self.lock:
                    for x in self.entries:
                        if x["addr"] == e["addr"]:
                            x["alive"] = False
                            x["fail_count"] += 1
                            break
            time.sleep(0.2)
        return checked

PROXY_POOL = ProxyPool()
PROXY_POOL.load(CONFIG["proxy_pool"])

def check_proxy_mode(addr, token):
    """通过代理检测 freebuff session 模式（full/limited）"""
    proxy = "http://" + addr
    start = time.time()
    ph = ProxyHandler({"https": proxy, "http": proxy})
    opener = build_opener(ph)
    req = Request(BASE_URL + "/api/v1/freebuff/session", method="DELETE")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", UA_SESSION)
    try:
        opener.open(req, timeout=15)
    except HTTPError as e:
        if e.code not in (200, 202, 204):
            pass
    latency = int((time.time() - start) * 1000)
    # 创建 session 检测 accessTier
    req2 = Request(BASE_URL + "/api/v1/freebuff/session", method="POST", data=b"{}")
    if token:
        req2.add_header("Authorization", "Bearer " + token)
    req2.add_header("User-Agent", UA_SESSION)
    req2.add_header("Content-Type", "application/json")
    req2.add_header("x-freebuff-model", DEFAULT_MODEL)
    try:
        resp = opener.open(req2, timeout=15)
        data = json.loads(resp.read())
        tier = data.get("accessTier", "limited")
        # 清理刚创建的 session
        try:
            opener.open(Request(BASE_URL + "/api/v1/freebuff/session", method="DELETE"), timeout=10)
        except Exception:
            pass
        return "full" if tier == "full" else "limited", latency
    except HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        if "country_blocked" in body or "anonymous_network" in body:
            return "blocked", latency
        return "limited", latency

# ============ 上游 API ============
def get_opener(proxy=None):
    p = proxy or CONFIG["proxy"]
    ph = ProxyHandler({"https": p, "http": p}) if p else ProxyHandler({})
    return build_opener(ph)

def api_request(method, path, body=None, ua=UA_SESSION, headers=None, token=None, proxy=None, timeout=120):
    opener = get_opener(proxy)
    url = BASE_URL + path
    req = Request(url, method=method)
    req.add_header("Authorization", "Bearer " + (token or (CONFIG["auth_tokens"][0] if CONFIG["auth_tokens"] else DEFAULT_AUTH_TOKEN)))
    req.add_header("User-Agent", ua)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if body is not None:
        req.data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        resp = opener.open(req, timeout=timeout)
        data = resp.read()
        if not data: return {}
        return json.loads(data)
    except HTTPError as e:
        data = e.read().decode("utf-8", "replace")
        log_error("upstream", f"{method} {path} -> {e.code}: {data[:300]}")
        raise

def create_session(model=DEFAULT_MODEL, token=None, proxy=None):
    resp = api_request("POST", "/api/v1/freebuff/session",
                       headers={"x-freebuff-model": model}, token=token, proxy=proxy)
    return resp

def get_session(instance_id, token=None, proxy=None):
    resp = api_request("GET", "/api/v1/freebuff/session",
                       headers={"x-freebuff-compact-session": "1", "x-freebuff-instance-id": instance_id},
                       token=token, proxy=proxy)
    return resp

def wait_for_active(instance_id, token=None, proxy=None, max_wait=300):
    start = time.time()
    while time.time() - start < max_wait:
        state = get_session(instance_id, token=token, proxy=proxy)
        if state.get("status") == "active":
            return state
        time.sleep(5)
    raise TimeoutError("session not active")

def start_run(agent_id, token=None, acting_user_id=None, proxy=None):
    resp = api_request("POST", "/api/v1/agent-runs",
                       body={"action": "START", "agentId": agent_id, "ancestorRunIds": []},
                       ua=UA_SESSION,
                       headers={"x-freebuff-acting-user-id": acting_user_id or CONFIG["acting_user_id"]},
                       token=token, proxy=proxy)
    run_id = resp.get("runId")
    if not run_id:
        raise RuntimeError(f"start run failed: {resp}")
    return run_id

def finish_run(run_id, token=None, acting_user_id=None, proxy=None, status="completed", steps=None):
    if steps is None:
        steps = [{"id": str(uuid.uuid4()), "stepNumber": 1, "credits": 0,
                  "childRunIds": [], "messageId": None, "status": "completed",
                  "startTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}]
    api_request("POST", "/api/v1/agent-runs",
                body={"action": "FINISH", "runId": run_id, "status": status,
                      "totalSteps": len(steps), "directCredits": 0, "totalCredits": 0,
                      "steps": steps},
                ua=UA_SESSION,
                headers={"x-freebuff-acting-user-id": acting_user_id or CONFIG["acting_user_id"]},
                token=token, proxy=proxy)

def chat_completion(prompt, model=DEFAULT_MODEL, stream=False, token=None, acting_user_id=None, proxy=None):
    """完整协议: DELETE 旧 session → sleep(1) → 建新 → 轮询 active → START run → chat → FINISH run"""
    # 0. 结束所有旧 session
    try:
        api_request("DELETE", "/api/v1/freebuff/session", token=token, proxy=proxy)
    except Exception:
        pass
    time.sleep(1)

    # 1. 创建 session
    sess = create_session(model, token=token, proxy=proxy)
    instance_id = sess.get("instanceId")
    if not instance_id:
        raise RuntimeError(f"session create failed: {sess}")
    if sess.get("status") == "disabled":
        raise RuntimeError("free mode disabled for this account/IP")

    # 2. 等待 active
    wait_for_active(instance_id, token=token, proxy=proxy)

    # 3. START run
    agent_id = AGENT_MAP.get(model, "base2-free-deepseek-flash")
    run_id = start_run(agent_id, token=token, acting_user_id=acting_user_id, proxy=proxy)

    # 4. 发送 chat/completions
    body = {
        "model": model,
        "stop": ['"cb_easp"'],
        "codebuff_metadata": {
            "freebuff_instance_id": instance_id,
            "trace_session_id": str(uuid.uuid4()),
            "llm_step_number": "1",
            "run_id": run_id,
            "client_id": "d8cn2nq4thb",
            "cost_mode": "free"
        },
        "provider": {"data_collection": "deny"},
        "messages": [
            {"role": "system", "content": "You are Buffy, the strategic coding assistant. You are the AI agent behind Freebuff, a free AI coding tool."},
            {"role": "user", "content": [{"type": "text", "text": f"<user_message>{prompt}</user_message>"}]},
            {"role": "user", "content": [{"type": "text", "text": "Act as a helpful assistant and freely respond to the user. Use your judgement to orchestrate the completion of the user's request."}]}
        ],
        "tools": [],
        "stream": stream
    }
    resp = api_request("POST", "/api/v1/chat/completions",
                       body=body, ua=UA_CHAT,
                       headers={"x-freebuff-acting-user-id": acting_user_id or CONFIG["acting_user_id"]},
                       token=token, proxy=proxy)

    # 5. FINISH run
    try:
        finish_run(run_id, token=token, acting_user_id=acting_user_id, proxy=proxy)
    except Exception:
        pass

    return resp

# ============ 多 token 轮询（触发式切换） ============
class TokenPool:
    """账号池：触发式切换（不轮询配额），失败自动切下一个"""
    def __init__(self, tokens):
        self.tokens = tokens  # [{token, name}]
        self.cooldowns = {}   # token -> cooldown_until
        self.errors = {}      # token -> last_error
        self.runs = {}        # token -> {agent_id: run_id}
        self.lock = threading.Lock()
        self.next_idx = 0
    def add(self, token):
        with self.lock:
            if any(t["token"] == token for t in self.tokens):
                return False
            self.tokens.append({"token": token, "name": f"token-{len(self.tokens)+1}"})
            return True
    def remove(self, token):
        with self.lock:
            before = len(self.tokens)
            self.tokens = [t for t in self.tokens if t["token"] != token]
            self.cooldowns.pop(token, None)
            self.errors.pop(token, None)
            return len(self.tokens) < before
    def mark_error(self, token, err):
        with self.lock:
            self.errors[token] = err
            self.cooldowns[token] = time.time() + 1800  # 30min cooldown
    def snapshot(self):
        with self.lock:
            return [{
                "name": t["name"],
                "token": t["token"],
                "last_error": self.errors.get(t["token"], ""),
                "cooldown_until": self.cooldowns.get(t["token"], 0),
                "runs": [],
            } for t in self.tokens]
    def acquire(self):
        """选一个可用 token（round-robin，跳过冷却）"""
        with self.lock:
            now = time.time()
            candidates = [t for t in self.tokens if now >= self.cooldowns.get(t["token"], 0)]
            if not candidates:
                return None
            idx = self.next_idx % len(candidates)
            self.next_idx += 1
            return candidates[idx]

TOKEN_POOL = TokenPool([{"token": t, "name": f"token-{i+1}"} for i, t in enumerate(CONFIG["auth_tokens"])])

# ============ HTTP Handler ============
class FreebuffHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- 辅助 ----
    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, data):
        body = data.encode() if isinstance(data, str) else data
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _js(self, data):
        body = data.encode() if isinstance(data, str) else data
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0: return {}
        raw = self.rfile.read(length)
        try: return json.loads(raw)
        except Exception: return {}

    def _check_key(self):
        """API key 校验（管理面板不校验，/v1/ 校验）"""
        if not self.path.startswith("/v1/"):
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:]
        else:
            key = self.headers.get("X-API-Key", "")
        return key in CONFIG["api_keys"]

    # ---- 路由 ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._html(WEBUI_HTML)
        elif path == "/webui.js":
            self._js(WEBUI_JS)
        elif path == "/v1/models":
            self.handle_models()
        elif path == "/api/webui/status":
            self._json(200, {
                "ok": True,
                "uptime_sec": int(time.time() - START_TIME),
                "models": len(ALL_MODELS),
                "tokens": len(TOKEN_POOL.tokens),
                "api_keys": len(CONFIG["api_keys"]),
                "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(START_TIME)),
            })
        elif path == "/api/webui/tokens":
            self._json(200, TOKEN_POOL.snapshot())
        elif path == "/api/webui/proxy/pool":
            self._json(200, {"entries": PROXY_POOL.list()})
        elif path == "/api/webui/apis":
            self._json(200, {"base_url": f"http://{self.headers.get('Host', 'localhost')}", "apis": CONFIG["api_keys"]})
        elif path == "/api/webui/usage":
            days = int(self.path_query("days", "1") or 1)
            self._json(200, USAGE.summary(days))
        elif path == "/api/webui/logs":
            self._json(200, {"logs": LOGS.all(), "total": len(LOGS.all())})
        elif path == "/api/webui/models":
            self.handle_webui_models()
        else:
            self._json(404, {"error": "not found"})

    def path_query(self, key, default=None):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return q.get(key, [default])[0]

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._check_key():
            self._json(401, {"error": {"message": "invalid api key", "type": "authentication_error"}})
            return
        if path == "/v1/chat/completions":
            self.handle_chat()
        elif path == "/api/webui/auth/code":
            self.handle_auth_code()
        elif path == "/api/webui/auth/status":
            self.handle_auth_status()
        elif path == "/api/webui/auth/import":
            self.handle_auth_import()
        elif path == "/api/webui/auth/account/delete":
            self.handle_auth_delete()
        elif path == "/api/webui/proxy/pool":
            self.handle_proxy_pool()
        elif path == "/api/webui/proxy/refresh":
            self.handle_proxy_refresh()
        elif path == "/api/webui/proxy/select":
            self._json(200, {"selected": PROXY_POOL.get_proxy(), "current": PROXY_POOL.current})
        elif path == "/api/webui/models/toggle":
            self.handle_model_toggle()
        elif path == "/api/webui/apis/create":
            self.handle_api_create()
        elif path == "/api/webui/apis/delete":
            self.handle_api_delete()
        elif path == "/api/webui/settings/password":
            self.handle_password()
        elif path == "/api/webui/logs/clear":
            LOGS.clear()
            self._json(200, {"ok": True})
        elif path == "/api/webui/probe":
            self.handle_probe()
        else:
            self._json(404, {"error": "not found"})

    # ---- 核心: chat ----
    def handle_chat(self):
        body = self._body()
        model = body.get("model", DEFAULT_MODEL)
        messages = body.get("messages", [])
        stream = body.get("stream", False)
        prompt = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, list):
                    c = "".join(x.get("text", "") for x in c if x.get("type") == "text")
                prompt = c
                break
        # API key -> 对应 token（简化：轮询所有 token）
        api_key = self.headers.get("Authorization", "").replace("Bearer ", "")
        start = time.time()
        token_entry = TOKEN_POOL.acquire()
        if token_entry is None:
            self._json(502, {"error": {"message": "no healthy upstream auth token available (all cooling down)", "type": "server_error"}})
            return
        token = token_entry["token"]
        try:
            resp = chat_completion(prompt, model=model, stream=stream, token=token,
                                   acting_user_id=CONFIG["acting_user_id"],
                                   proxy=PROXY_POOL.get_proxy())
            # 用量记录
            usage = resp.get("usage", {})
            USAGE.record(model, api_key, usage.get("total_tokens", 0))
            log_info("chat", f"model={model} tokens={usage.get('total_tokens',0)} cost={usage.get('cost',0)}")
            self._json(200, resp)
        except HTTPError as e:
            TOKEN_POOL.mark_error(token, f"HTTP {e.code}")
            log_error("chat", f"model={model} HTTP {e.code}: {e}")
            self._json(502, {"error": {"message": f"upstream error {e.code}", "type": "server_error"}})
        except Exception as e:
            TOKEN_POOL.mark_error(token, str(e))
            log_error("chat", f"model={model}: {e}")
            self._json(502, {"error": {"message": str(e), "type": "server_error"}})

    # ---- 模型 ----
    def handle_models(self):
        results = []
        with MODEL_ENABLED_LOCK:
            for m in ALL_MODELS:
                enabled = MODEL_ENABLED.get(m, True)
                if not enabled: continue
                results.append({"id": m, "object": "model", "created": 0, "owned_by": "freebuff"})
        self._json(200, {"object": "list", "data": results})

    def handle_webui_models(self):
        results = []
        with MODEL_ENABLED_LOCK:
            for m in ALL_MODELS:
                enabled = MODEL_ENABLED.get(m, True)
                results.append({"id": m, "enabled": enabled, "status": "unknown"})
        self._json(200, {"models": results})

    def handle_model_toggle(self):
        body = self._body()
        model, enabled = body.get("model"), body.get("enabled")
        if not model:
            self._json(400, {"error": "model required"})
            return
        with MODEL_ENABLED_LOCK:
            MODEL_ENABLED[model] = bool(enabled)
        self._json(200, {"ok": True})

    def handle_probe(self):
        """探测所有模型延迟（acquire session）"""
        results = []
        for model in ALL_MODELS:
            start = time.time()
            token_entry = TOKEN_POOL.acquire()
            if token_entry is None:
                results.append({"model": model, "status": "error", "error": "no token available"})
                continue
            try:
                chat_completion("ping", model=model, token=token_entry["token"],
                                acting_user_id=CONFIG["acting_user_id"], proxy=PROXY_POOL.get_proxy())
                results.append({"model": model, "status": "ok", "latency_ms": int((time.time()-start)*1000)})
            except Exception as e:
                results.append({"model": model, "status": "error", "error": str(e)})
        self._json(200, results)

    # ---- 认证 ----
    def handle_auth_code(self):
        try:
            req = Request(AUTH_BACKEND + "/api/code", method="POST")
            resp = urlopen(req, timeout=30)
            data = json.loads(resp.read())
            self._json(200, data)
        except Exception as e:
            self._json(502, {"error": f"upstream call failed: {e}"})

    def handle_auth_status(self):
        body = self._body()
        try:
            req = Request(AUTH_BACKEND + "/api/status", method="POST",
                          data=json.dumps(body).encode())
            req.add_header("Content-Type", "application/json")
            resp = urlopen(req, timeout=30)
            data = json.loads(resp.read())
            self._json(200, data)
        except Exception as e:
            self._json(502, {"error": f"upstream call failed: {e}"})

    def handle_auth_import(self):
        body = self._body()
        token = body.get("authToken", "")
        if not token:
            self._json(400, {"error": "authToken required"})
            return
        added = TOKEN_POOL.add(token)
        if added:
            CONFIG["auth_tokens"].append(token)
            save_config(CONFIG)
            log_info("auth", f"added token via OAuth: {body.get('email', '')}")
        self._json(200, {"ok": True, "added": added, "total": len(TOKEN_POOL.tokens),
                         "token": token[:8] + "***"})

    def handle_auth_delete(self):
        body = self._body()
        token = body.get("token", "")
        if not token:
            self._json(400, {"error": "token required"})
            return
        removed = TOKEN_POOL.remove(token)
        CONFIG["auth_tokens"] = [t for t in CONFIG["auth_tokens"] if t != token]
        save_config(CONFIG)
        self._json(200, {"ok": removed})

    # ---- 代理池 ----
    def handle_proxy_pool(self):
        body = self._body()
        action = body.get("action")
        if action == "add":
            addrs = body.get("addrs", [])
            n = PROXY_POOL.add(addrs)
            self._json(200, {"ok": True, "added": n})
        elif action == "remove":
            ok = PROXY_POOL.remove(body.get("addr", ""))
            self._json(200, {"ok": ok})
        elif action == "save":
            PROXY_POOL.save()
            self._json(200, {"ok": True})
        else:
            self._json(400, {"error": "unknown action"})

    def handle_proxy_refresh(self):
        token = CONFIG["auth_tokens"][0] if CONFIG["auth_tokens"] else DEFAULT_AUTH_TOKEN
        checked = PROXY_POOL.check_all(token)
        self._json(200, {"ok": True, "checked": checked})

    # ---- API keys ----
    def handle_api_create(self):
        body = self._body()
        key = body.get("key", "").strip()
        if not key:
            self._json(400, {"error": "key required"})
            return
        if key in CONFIG["api_keys"]:
            self._json(200, {"ok": True, "added": False})
            return
        CONFIG["api_keys"].append(key)
        save_config(CONFIG)
        self._json(200, {"ok": True, "added": True})

    def handle_api_delete(self):
        body = self._body()
        key = body.get("key", "")
        if key in CONFIG["api_keys"]:
            CONFIG["api_keys"].remove(key)
            save_config(CONFIG)
        self._json(200, {"ok": True})

    # ---- 密码 ----
    def handle_password(self):
        body = self._body()
        old, new = body.get("old_password", ""), body.get("new_password", "")
        if not new:
            self._json(400, {"error": "new password required"})
            return
        if old not in CONFIG["api_keys"]:
            self._json(403, {"error": "旧密码错误"})
            return
        CONFIG["api_keys"] = [new] + [k for k in CONFIG["api_keys"] if k != old]
        save_config(CONFIG)
        self._json(200, {"ok": True})

    def log_message(self, fmt, *args):
        pass

# ============ 静态资源 ============
WEBUI_HTML = ""  # 在下方赋值
WEBUI_JS = ""    # 在下方赋值

def load_static():
    global WEBUI_HTML, WEBUI_JS
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "webui.html")) as f:
        WEBUI_HTML = f.read()
    with open(os.path.join(base, "webui.js")) as f:
        WEBUI_JS = f.read()

START_TIME = time.time()

def main():
    global CONFIG_FILE, CONFIG, PROXY_POOL, TOKEN_POOL
    parser = argparse.ArgumentParser(description="FreebuffProxy Python 版")
    parser.add_argument("--listen", default=":18082")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--config", default=CONFIG_FILE)
    args = parser.parse_args()

    CONFIG_FILE = args.config
    CONFIG = load_config()
    if args.proxy:
        CONFIG["proxy"] = args.proxy
    PROXY_POOL.load(CONFIG["proxy_pool"])
    TOKEN_POOL = TokenPool([{"token": t, "name": f"token-{i+1}"} for i, t in enumerate(CONFIG["auth_tokens"])])

    load_static()
    host, port = "0.0.0.0", int(args.listen.lstrip(":"))
    log_info("server", f"FreebuffProxy Python listening on {args.listen}")
    print(f"FreebuffProxy Python listening on {args.listen}", file=sys.stderr)
    HTTPServer((host, port), FreebuffHandler).serve_forever()

if __name__ == "__main__":
    main()
