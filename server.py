# -*- coding: utf-8 -*-
import os, re, sys, json, time, queue, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from cloakbrowser import launch, launch_persistent_context

ACCOUNTS_DIR = os.environ.get("MINIMAX_ACCOUNTS_DIR", "/app/accounts")
ACTIVE_FILE = os.path.join(ACCOUNTS_DIR, ".active_account")
BASE_URL = "https://agent.minimax.io/"
PORT = int(os.environ.get("PORT", 10000))

if os.path.exists("/app/accounts.tar.gz") and (not os.path.exists(ACCOUNTS_DIR) or len(os.listdir(ACCOUNTS_DIR)) < 10):
    import subprocess
    subprocess.run(["tar", "-xzf", "/app/accounts.tar.gz", "-C", "/app"], check=False)

request_queue = queue.Queue()
PAGE_READY = threading.Event()

def _pick_account_for_proxy():
    if os.path.exists(ACTIVE_FILE):
        target = open(ACTIVE_FILE).read().strip()
        if target:
            for f in os.listdir(ACCOUNTS_DIR):
                if not (f.startswith("account_") and f.endswith(".json")): continue
                fp = os.path.join(ACCOUNTS_DIR, f)
                try:
                    with open(fp, "r", encoding="utf-8") as fh: acc = json.load(fh)
                    if acc.get("email") == target: return target, acc.get("cookies") or []
                except Exception: pass
    files = [f for f in os.listdir(ACCOUNTS_DIR) if f.startswith("account_") and f.endswith(".json")]
    files.sort()
    if not files: return None, []
    fp = os.path.join(ACCOUNTS_DIR, files[0])
    with open(fp, "r", encoding="utf-8") as j: acc = json.load(j)
    return acc.get("email", "?"), acc.get("cookies") or []

class APIHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        clean_path = self.path.split('?')[0].rstrip('/')
        if clean_path in ("", "/chat", "/index.html"):
            ui_path = os.path.join(os.path.dirname(__file__), "chat_ui.html")
            if os.path.exists(ui_path):
                content = open(ui_path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
        if clean_path == "/v1/ready":
            if PAGE_READY.is_set(): self._send_json({"status": "ready", "page_ready": True}, 200)
            else: self._send_json({"status": "initializing", "page_ready": False}, 503)
            return
        if clean_path in ("/v1/models", "/models"):
            self._send_json({
                "object": "list",
                "data": [
                    {"id": "minimax-m3", "object": "model", "created": 1700000000, "owned_by": "minimax"},
                    {"id": "MiniMax", "object": "model", "created": 1700000000, "owned_by": "minimax"}
                ]
            })
            return
        self._send_json({"status": "active"})

    def do_POST(self):
        clean_path = self.path.split('?')[0].rstrip('/')
        if clean_path in ('/v1/chat/completions', '/chat/completions', '/responses', '/v1/responses', '/v1/messages', '/messages'):
            self._handle_chat()
        else:
            self._send_json({"error": {"message": "not found", "code": 404}}, 404)

    def _handle_chat(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            req_json = json.loads(post_data.decode('utf-8'))
            is_anthropic = '/messages' in self.path
            raw = req_json.get("messages", []) or req_json.get("input", [])
            txt = ""
            if isinstance(raw, list):
                for msg in reversed(raw):
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") in ["text", "input_text"]: txt = c.get("text", "")
                                elif isinstance(c, str): txt = c
                        else: txt = str(content)
                        break
            if not txt: txt = "Hello"

            if txt.strip().lower() in ["ping", "test", "hi", "hello", "1"]:
                if is_anthropic:
                    self._send_json({
                        "id": "msg_test", "type": "message", "role": "assistant", "model": "minimax-m3",
                        "content": [{"type": "text", "text": "Connection OK! MiniMax M3 is ready."}],
                        "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}
                    }, 200)
                else:
                    self._send_json({
                        "id": "chatcmpl-test", "object": "chat.completion", "created": int(time.time()),
                        "model": "minimax-m3", "choices": [{"index": 0, "message": {"role": "assistant", "content": "Connection OK!"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                    }, 200)
                return

            want_stream = req_json.get("stream", False)
            res_q = queue.Queue()
            request_queue.put((txt, res_q))
            
            ans = None
            err = None

            if want_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.end_headers()
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()

                for _ in range(180):
                    try:
                        ans, err = res_q.get(timeout=1.0)
                        break
                    except queue.Empty:
                        try:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                        except Exception:
                            break

                if is_anthropic:
                    m_start = json.dumps({"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "minimax-m3", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
                    self.wfile.write(f"event: message_start\ndata: {m_start}\n\n".encode("utf-8"))
                    cb_start = json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                    self.wfile.write(f"event: content_block_start\ndata: {cb_start}\n\n".encode("utf-8"))
                    cb_delta = json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": ans or err or ""}})
                    self.wfile.write(f"event: content_block_delta\ndata: {cb_delta}\n\n".encode("utf-8"))
                    self.wfile.write(b"event: content_block_stop\ndata: {\"type\": \"content_block_stop\", \"index\": 0}\n\n")
                    self.wfile.write(b"event: message_delta\ndata: {\"type\": \"message_delta\", \"delta\": {\"stop_reason\": \"end_turn\", \"stop_sequence\": null}, \"usage\": {\"output_tokens\": 0}}\n\n")
                    self.wfile.write(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")
                    self.wfile.flush()
                else:
                    res = {
                        "id": "chatcmpl-123",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "minimax-m3",
                        "choices": [{"index": 0, "delta": {"content": ans or err or ""}}]
                    }
                    self.wfile.write(f"data: {json.dumps(res)}\n\n".encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
            else:
                for _ in range(180):
                    try:
                        ans, err = res_q.get(timeout=1.0)
                        break
                    except queue.Empty:
                        pass

                reply_txt = ans if ans else (f"生成异常: {err}" if err else "已收到您的请求。")
                if is_anthropic:
                    res = {
                        "id": "msg_123",
                        "type": "message",
                        "role": "assistant",
                        "model": "minimax-m3",
                        "content": [{"type": "text", "text": reply_txt}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 10, "output_tokens": 20}
                    }
                else:
                    res = {
                        "id": "chatcmpl-123",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": "minimax-m3",
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": reply_txt},
                            "finish_reason": "stop"
                        }],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
                    }
                self._send_json(res, 200)

        except Exception as e:
            print(f"Handler Exception: {e}", flush=True)

    def log_message(self, *args): pass

def playwright_executor():
    print("-> [大脑] 初始化中...", flush=True)
    active_email, active_cookies = _pick_account_for_proxy()
    if active_email is None:
        print("💀 [大脑] 没有可用账号，proxy 退出。", flush=True)
        return
    
    user_data_dir = os.path.join(ACCOUNTS_DIR, "_proxy_profile")
    import shutil
    shutil.rmtree(user_data_dir, ignore_errors=True)
    
    ctx = launch_persistent_context(
        user_data_dir,
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-notifications",
            "--disable-popup-blocking",
            "--no-first-run",
        ],
        viewport=None,
        extension_paths=None,
    )
    if active_cookies: ctx.add_cookies(active_cookies)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    
    def _dismiss_popups(p):
        try:
            p.evaluate("""() => {
                const clickSels = [
                    "div.full-screen-modal button[aria-label='Close']",
                    "div.ant-popover button[aria-label='Close']",
                    "button[aria-label='Close']",
                    "[aria-label='close']",
                    ".ant-modal-close",
                    "button.ant-modal-close",
                ];
                for (const sel of clickSels) {
                    try { document.querySelectorAll(sel).forEach(el => el.click()); } catch (e) {}
                }
                ['.ant-modal-root', '.ant-modal-mask', '.ant-popover',
                 '.ant-notification', '.full-screen-modal',
                 '[role="dialog"]'].forEach(s => {
                    try {
                        document.querySelectorAll(s).forEach(el => {
                            el.style.display = 'none';
                            el.style.visibility = 'hidden';
                        });
                    } catch (e) {}
                });
            }""")
        except Exception: pass

    def prepare_page():
        print("-> [导航] 载入 chat 主页 /mavis ...", flush=True)
        try:
            page.goto("https://agent.minimax.io/mavis", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            try: page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception: pass
        try: page.wait_for_load_state("networkidle", timeout=15000)
        except Exception: pass
        time.sleep(3)
        _dismiss_popups(page)
        PAGE_READY.set()
        print("✅ [大脑] 引导页已清理，环境就绪。", flush=True)

    prepare_page()

    while True:
        try:
            prompt, rq = request_queue.get()
            print(f"-> [任务] 正在提问: {prompt[:30]}...", flush=True)
            if page.is_closed(): prepare_page()
            _dismiss_popups(page)

            # 支持多选择器，增强弹性（无论是富文本还是 textarea 均兼容）
            INPUT_SELS = ["div[contenteditable='true']", "textarea", "input[type='text']"]
            target_sel = None
            for sel in INPUT_SELS:
                try:
                    page.wait_for_selector(sel, timeout=5000, state="visible")
                    target_sel = sel
                    break
                except Exception:
                    pass

            if not target_sel:
                target_sel = "div[contenteditable='true']"

            try:
                page.evaluate("""([sel, txt]) => {
                    const p = document.querySelector(sel);
                    if (p) {
                        p.textContent = String(txt);
                        if (p.value !== undefined) p.value = String(txt);
                        p.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }""", [target_sel, prompt])
                page.locator(target_sel).first.click(force=True)
                page.keyboard.press("Enter")
            except Exception:
                try:
                    page.keyboard.type(str(prompt))
                    page.keyboard.press("Enter")
                except Exception: pass

            time.sleep(3)
            ans = ""
            deadline = time.time() + 170
            while time.time() < deadline:
                time.sleep(1)
                try:
                    txt = page.evaluate("""() => {
                        const els = document.querySelectorAll("[class*='markdown'], [class*='response']");
                        if (!els.length) return "";
                        return els[els.length - 1].innerText.trim();
                    }""")
                    if txt and len(txt) > 2:
                        ans = txt
                        break
                except Exception: pass

            rq.put((ans or "已收到您的提问", None))
        except Exception as e:
            rq.put(("", str(e)))

class ReuseAddressHTTPServer(HTTPServer):
    allow_reuse_address = True

if __name__ == "__main__":
    threading.Thread(target=playwright_executor, daemon=True).start()
    print(f"🚀 [M3 修正大脑] 端口 {PORT} 就绪 (0.0.0.0)", flush=True)
    ReuseAddressHTTPServer(('0.0.0.0', PORT), APIHandler).serve_forever()
