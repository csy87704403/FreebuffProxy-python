#!/usr/bin/env python3
"""Freebuff API 代理 - 完整复刻官方 CLI 协议.

用法:
  # 转发模式 (OpenAI 兼容)
  python3 freebuff_api.py --listen :18082 --proxy http://127.0.0.1:8078

  # 直接调用
  python3 freebuff_api.py --proxy http://127.0.0.1:8078 \\
    --prompt "请只回复「连接正常」四个字"
"""
import json, os, sys, time, uuid, argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen, ProxyHandler, build_opener
from urllib.error import HTTPError

AUTH = os.environ.get("FREEBUFF_AUTH_TOKEN", "ad16adfb-ed64-4fc3-aafa-3b0e3acdc8a8")
ACTING_USER_ID = os.environ.get("FREEBUFF_ACTING_USER_ID", "da6ec7f1-7bb8-43c8-a73f-2fcc5f937a1e")
PROXY = os.environ.get("HTTPS_PROXY", "http://127.0.0.1:8078")
BASE_URL = "https://www.codebuff.com"
UA_SESSION = "Bun/1.3.14"
UA_CHAT = "ai-sdk/openai-compatible/0.0.0-test/codebuff ai-sdk/provider-utils/3.0.25 runtime/browser"

def get_opener():
    ph = ProxyHandler({"https": PROXY}) if PROXY else ProxyHandler({})
    return build_opener(ph)

def api_request(method, path, body=None, ua=UA_SESSION, headers=None):
    opener = get_opener()
    url = BASE_URL + path
    req = Request(url, method=method)
    req.add_header("Authorization", "Bearer " + AUTH)
    req.add_header("User-Agent", ua)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        resp = opener.open(req, timeout=120)
        data = resp.read()
        return json.loads(data)
    except HTTPError as e:
        data = e.read().decode("utf-8", "replace")
        print(f"ERROR {e.code}: {data[:500]}", file=sys.stderr)
        raise

def create_session(model="deepseek/deepseek-v4-flash"):
    """创建 freebuff session."""
    resp = api_request("POST", "/api/v1/freebuff/session",
                       headers={"x-freebuff-model": model})
    print(f"[session] status={resp.get('status')} instanceId={resp.get('instanceId')}")
    return resp

def get_session(instance_id):
    """轮询 session 状态."""
    resp = api_request("GET", "/api/v1/freebuff/session",
                       headers={
                           "x-freebuff-compact-session": "1",
                           "x-freebuff-instance-id": instance_id,
                       })
    return resp

def wait_for_active(instance_id, max_wait=300):
    """等待 session 变为 active."""
    start = time.time()
    while time.time() - start < max_wait:
        state = get_session(instance_id)
        if state.get("status") == "active":
            return state
        print(f"[session] status={state.get('status')}, waiting...", file=sys.stderr)
        time.sleep(5)
    raise TimeoutError("session not active")

def start_run(agent_id="base2-free-deepseek-flash"):
    """START agent run, 返回 runId."""
    resp = api_request("POST", "/api/v1/agent-runs",
                       body={"action": "START", "agentId": agent_id, "ancestorRunIds": []},
                       ua=UA_SESSION,
                       headers={"x-freebuff-acting-user-id": ACTING_USER_ID})
    run_id = resp.get("runId")
    print(f"[run] runId={run_id}")
    return run_id

def finish_run(run_id, status="completed", steps=None):
    """FINISH agent run."""
    if steps is None:
        steps = [{"id": str(uuid.uuid4()), "stepNumber": 1, "credits": 0,
                  "childRunIds": [], "messageId": None, "status": "completed",
                  "startTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}]
    api_request("POST", "/api/v1/agent-runs",
                body={"action": "FINISH", "runId": run_id, "status": status,
                      "totalSteps": len(steps), "directCredits": 0, "totalCredits": 0,
                      "steps": steps},
                ua=UA_SESSION,
                headers={"x-freebuff-acting-user-id": ACTING_USER_ID})

def chat_completion(prompt, model="deepseek/deepseek-v4-flash", stream=False):
    """发送 chat/completions 请求 (完整协议)."""
    # 0. 结束所有旧 session (避免 409 model_mismatch)
    try:
        api_request("DELETE", "/api/v1/freebuff/session")
    except Exception:
        pass
    import time as _t; _t.sleep(1)

    # 1. 创建 session
    sess = create_session(model)
    instance_id = sess["instanceId"]

    # 2. 等待 active
    wait_for_active(instance_id)

    # 3. START run
    agent_map = {
        "deepseek/deepseek-v4-flash": "base2-free-deepseek-flash",
        "mimo/mimo-v2.5": "base2-free-mimo",
    }
    agent_id = agent_map.get(model, "base2-free-deepseek-flash")
    run_id = start_run(agent_id)

    # 4. 发送 chat/completions
    body = {
        "model": model,
        "stop": ["\"cb_easp\""],
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
                       headers={"x-freebuff-acting-user-id": ACTING_USER_ID})

    # 5. FINISH run
    finish_run(run_id)

    return resp

# ---- HTTP Server (OpenAI 兼容) ----
class FreebuffHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            prompt = body.get("messages", [{}])[-1].get("content", "")
            if isinstance(prompt, list):
                prompt = prompt[0].get("text", "") if prompt else ""
            model = body.get("model", "deepseek/deepseek-v4-flash")
            try:
                resp = chat_completion(prompt, model=model)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(resp).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": str(e)}}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[server] {fmt % args}", file=sys.stderr)

def main():
    parser = argparse.ArgumentParser(description="Freebuff API 代理")
    parser.add_argument("--listen", default=":18082", help="监听地址")
    parser.add_argument("--proxy", default="http://127.0.0.1:8078", help="代理地址")
    parser.add_argument("--prompt", help="直接调用模式: 发送 prompt 并打印回复")
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash", help="模型名称")
    args = parser.parse_args()

    global PROXY
    PROXY = args.proxy

    if args.prompt:
        resp = chat_completion(args.prompt, model=args.model)
        print(json.dumps(resp, ensure_ascii=False, indent=1))
        return

    host, port = "0.0.0.0", int(args.listen.lstrip(":"))
    print(f"Freebuff API listening on {args.listen}", file=sys.stderr)
    HTTPServer((host, port), FreebuffHandler).serve_forever()

if __name__ == "__main__":
    main()
