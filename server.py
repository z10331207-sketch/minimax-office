# -*- coding: utf-8 -*-
import os, re, sys, json, time, queue, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from cloakbrowser import launch, launch_persistent_context

import os
ACCOUNTS_DIR = os.environ.get("MINIMAX_ACCOUNTS_DIR", "/Users/apple/Desktop/google/M3/accounts")
ACTIVE_FILE = os.path.join(ACCOUNTS_DIR, ".active_account")
BASE_URL = "https://agent.minimax.io/"
LOGIN_URL = "https://agent.minimax.io/login?sso=1"
PORT = int(os.environ.get("PORT", 10000))

# 解压账号包
if os.path.exists("accounts.tar.gz") and not os.path.exists(ACCOUNTS_DIR):
    import subprocess
    subprocess.run(["tar", "-xzf", "accounts.tar.gz"], check=False)
request_queue = queue.Queue()

# page 是否就绪 — keep_alive 用 /v1/ready 等这个
# (HTTP server 启动 1s 就 listen,但 Chromium + nav SSO + 引导页清理要 20-30s)
PAGE_READY = threading.Event()


def _pick_account_for_proxy():
    """
    proxy 启动时按 .active_account 选账号；如果没设则 fallback 到 files[0]。

    返回: (email, cookies_list) 或 (None, [])
    """
    # 1. 优先按 .active_account 选
    if os.path.exists(ACTIVE_FILE):
        target = open(ACTIVE_FILE).read().strip()
        if target:
            for f in os.listdir(ACCOUNTS_DIR):
                if not (f.startswith("account_") and f.endswith(".json")):
                    continue
                fp = os.path.join(ACCOUNTS_DIR, f)
                try:
                    with open(fp, "r", encoding="utf-8") as fh:
                        acc = json.load(fh)
                except Exception:
                    continue
                if acc.get("email") == target:
                    print(f"-> [大脑] 按 .active_account 选择: {target}", flush=True)
                    return target, acc.get("cookies") or []
            # active 指向的账号不存在
            print(f"⚠️  [大脑] .active_account 指向 {target} 但找不到对应文件，fallback 到第一个账号", flush=True)

    # 2. Fallback: 取第一个 account_*.json
    files = [f for f in os.listdir(ACCOUNTS_DIR)
             if f.startswith("account_") and f.endswith(".json")]
    files.sort()
    if not files:
        print("❌ [大脑] accounts 目录没有可用账号 JSON！", flush=True)
        return None, []
    fp = os.path.join(ACCOUNTS_DIR, files[0])
    with open(fp, "r", encoding="utf-8") as j:
        acc = json.load(j)
    email = acc.get("email", "?")
    print(f"-> [大脑] .active_account 未设，按字典序选第一个: {email} ({files[0]})", flush=True)
    return email, acc.get("cookies") or []

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
    def do_OPTIONS(self): self._send_json({})
    def do_GET(self):
        # /v1/ready — 等 playwright 真的把 page 准备好后再返回 200
        # (HTTP server 启动 1s 就 listen,但 Chromium + nav SSO + 引导页清理要 20-30s)
        if self.path == "/v1/ready":
            if PAGE_READY.is_set():
                self._send_json({"status": "ready", "page_ready": True}, 200)
            else:
                self._send_json({"status": "initializing", "page_ready": False}, 503)
            return
        if self.path in ("/v1/models", "/models"):
            self._send_json({
                "object": "list",
                "data": [
                    {"id": "minimax-m3", "object": "model", "created": 1700000000, "owned_by": "minimax"},
                    {"id": "MiniMax", "object": "model", "created": 1700000000, "owned_by": "minimax"}
                ]
            })
            return
        if self.path in ("/", "/chat", "/index.html"):
            ui_path = os.path.join(os.path.dirname(__file__), "chat_ui.html")
            if os.path.exists(ui_path):
                content = open(ui_path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
        # /  — 简单 liveness ping(永远 active,只要 HTTP server 在跑就 200)
        self._send_json({"status": "active"})

    def do_POST(self):
        if self.path in ('/v1/chat/completions', '/responses', '/v1/responses', '/v1/messages', '/messages'):
            self._handle_chat()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_chat(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            req_json = json.loads(post_data.decode('utf-8'))
            print(f"=== INCOMING REQUEST ({self.path}) ===\n{json.dumps(req_json, indent=2)}\n=======================", flush=True)
            
            is_anthropic = '/messages' in self.path
            raw = req_json.get("messages", []) or req_json.get("input", [])
            txt = ""
            if isinstance(raw, list):
                for msg in reversed(raw):
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") in ["text", "input_text"]:
                                    txt = c.get("text", "")
                                elif isinstance(c, str):
                                    txt = c
                        else:
                            txt = str(content)
                        break
            
            if not txt:
                txt = "Hello"

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
                    if err:
                        self.wfile.write(f"event: error\ndata: {json.dumps({'error': {'message': err}})}\n\n".encode("utf-8"))
                    elif ans is not None:
                        m_start = json.dumps({"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "minimax-m3", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
                        self.wfile.write(f"event: message_start\ndata: {m_start}\n\n".encode("utf-8"))
                        cb_start = json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                        self.wfile.write(f"event: content_block_start\ndata: {cb_start}\n\n".encode("utf-8"))
                        cb_delta = json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": ans}})
                        self.wfile.write(f"event: content_block_delta\ndata: {cb_delta}\n\n".encode("utf-8"))
                        self.wfile.write(b"event: content_block_stop\ndata: {\"type\": \"content_block_stop\", \"index\": 0}\n\n")
                        self.wfile.write(b"event: message_delta\ndata: {\"type\": \"message_delta\", \"delta\": {\"stop_reason\": \"end_turn\", \"stop_sequence\": null}, \"usage\": {\"output_tokens\": 0}}\n\n")
                        self.wfile.write(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")
                    self.wfile.flush()
                else:
                    if err:
                        self.wfile.write(f"data: {json.dumps({'error': err})}\n\n".encode("utf-8"))
                    elif ans is not None:
                        res = {
                            "id": "chatcmpl-123",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": "minimax-m3",
                            "choices": [{"index": 0, "delta": {"content": ans}}]
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

                if err:
                    self._send_json({"error": {"message": err, "type": "upstream_error"}}, 500)
                else:
                    if is_anthropic:
                        res = {
                            "id": "msg_123",
                            "type": "message",
                            "role": "assistant",
                            "model": "minimax-m3",
                            "content": [{"type": "text", "text": ans or ""}],
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}
                        }
                    else:
                        res = {
                            "id": "chatcmpl-123",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": "minimax-m3",
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": ans or ""},
                                "finish_reason": "stop"
                            }],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                        }
                    self._send_json(res, 200)

        except Exception as e:
            print(f"Handler Exception: {e}", flush=True)

    def log_message(self, *args): pass

def _write_protocol_allow_preference(user_data_dir):
    pref_dir = os.path.join(user_data_dir, "Default")
    os.makedirs(pref_dir, exist_ok=True)
    pref_file = os.path.join(pref_dir, "Preferences")
    
    data = {}
    if os.path.exists(pref_file):
        try:
            with open(pref_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
            
    if "protocol_handler" not in data:
        data["protocol_handler"] = {}
    if "allowed_origin_protocol_pairs" not in data["protocol_handler"]:
        data["protocol_handler"]["allowed_origin_protocol_pairs"] = {}
        
    data["protocol_handler"]["allowed_origin_protocol_pairs"]["https://agent.minimax.io"] = {
        "minimax": True
    }
    
    try:
        with open(pref_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"-> [大脑] [警告] 写入 Preferences 失败: {e}", flush=True)

def playwright_executor():
    print("-> [大脑] 初始化中...", flush=True)
    active_email, active_cookies = _pick_account_for_proxy()
    if active_email is None:
        print("💀 [大脑] 没有可用账号，proxy 退出。", flush=True)
        return
    
    user_data_dir = os.path.join(ACCOUNTS_DIR, "_proxy_profile")
    
    # [核心修复] 每次启动代理时,强制清空旧的浏览器配置文件,防止前一个账号的 localStorage/sessionStorage 等残留
    import shutil
    shutil.rmtree(user_data_dir, ignore_errors=True)
    
    _write_protocol_allow_preference(user_data_dir)
    
    ctx = launch_persistent_context(
        user_data_dir,
        headless=True,
        args=[
            "--disable-notifications",
            "--disable-popup-blocking",
            "--no-first-run",
        ],
        # 严格按 /Users/apple/Desktop/ok/无痕浏览器/launch_browser.py:54 用 viewport=None
        # (no_viewport=True 跟 viewport=None 不一定等价 — cloakbrowser 是 wrapper,行为可能不同)
        viewport=None,
        # 传 None 让 CloakBrowser 用默认扩展加载逻辑(虽然没装扩展,但传了避免内部分支)
        extension_paths=None,
    )
    if active_cookies: ctx.add_cookies(active_cookies)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    
    def _dismiss_popups(p):
        """关掉所有弹窗/遮罩 — 参考 account_manager_gui.py 的智能防御
        landing page 上有 full-screen-modal 遮挡 chat 入口,必须关掉才能发 prompt。"""
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
                let n = 0;
                for (const sel of clickSels) {
                    try {
                        document.querySelectorAll(sel).forEach(el => {
                            try { el.click(); n++; } catch (e) {}
                        });
                    } catch (e) {}
                }
                // 隐藏常见遮罩(modal 弹窗、antd 组件)
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
                return n;
            }""")
        except Exception as e:
            print(f"   ⚠️ 关弹窗失败(可能 page navigation 中): {e}", flush=True)

    def prepare_page():
        # 切号后 proxy 重启, 直接跳 /mavis 强制 server 创建 chat session
        # (2026-06 实测:跳根 https://agent.minimax.io/ 会落 landing page,
        #  必须主动跳 /mavis 才能进 chat 主页)
        print("-> [导航] 载入 chat 主页 /mavis ...", flush=True)
        try:
            page.goto("https://agent.minimax.io/mavis", wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            print(f"   ⚠️  goto /mavis 失败: {e}, 改跳根", flush=True)
            try:
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20000)
            except Exception as e2:
                print(f"   ⚠️  goto 根也失败: {e2}", flush=True)
        # 等 navigation 真正完成
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        time.sleep(2)  # 给 chat 主页 SPA 一点时间渲染
        print(f"   当前 URL: {page.url}", flush=True)
        # 关掉 landing page 的 modal 弹窗(不关看不到 chat 入口)
        _dismiss_popups(page)
        time.sleep(1)
        # 暴力破解引导页(老版逻辑保留,兜底)
        try:
            page.evaluate("""() => {
                document.querySelectorAll('button, a, div[role="button"]').forEach(el => {
                    const t = el.innerText.trim();
                    if (t.match(/Try it now|Start|Got it|OK|了解|开始|确定|继续|跳过|Skip|Agent Team|Ask anything|New task|Switch to classic/i)) {
                        el.click();
                    }
                });
            }""")
        except Exception as e:
            print(f"   清理引导页失败(可能 page 还在 navigation): {e}", flush=True)

        # [修复 2026-07-08] 严格健康检查 + retry — 之前 try/except 太宽松,把
        # "browser has been closed" 吞了还假装 ready,导致 Codex 转发过来后
        # page.evaluate 一直抛异常永远拿不到回复。
        # 现在真验证 page 还活着 + URL 对得上,给 SPA navigation 稳定时间,
        # 最多 retry 3 次(每次间 wait_for_load_state networkidle + sleep)。
        # 3 次都失败再 raise 让整个 proxy 进程退出,由 m3_switcher 重启。
        last_err = None
        for attempt in range(1, 4):
            print(f"   [健康检查] 第 {attempt}/3 次...", flush=True)
            try:
                # 等 navigation 稳定(避免 evaluate 时正在 navigate)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                if page.is_closed():
                    raise RuntimeError("page.is_closed()")
                actual_url = page.evaluate("() => location.href")
                if not actual_url or "agent.minimax.io" not in actual_url:
                    raise RuntimeError(f"page on wrong URL: {actual_url!r}")
                actual_title = page.evaluate("() => document.title")
                print(f"   [健康检查] OK url={actual_url} title={actual_title[:40]!r}", flush=True)
                last_err = None
                break
            except Exception as e:
                last_err = e
                print(f"   ⚠️ 健康检查失败 (attempt {attempt}/3): {type(e).__name__}: {str(e)[:120]}", flush=True)
                if attempt < 3:
                    time.sleep(3)
        if last_err:
            print(f"   ❌ 健康检查 3 次都失败,最后错误: {last_err}", flush=True)
            print(f"   ❌ raise 让 proxy 退出,m3_switcher 会自动重启", flush=True)
            # [修复] 必须显式 raise 带异常,否则 'raise' 不带参数在 for 循环外
            # 抛 RuntimeError: No active exception to reraise,proxy 进程不会退出
            raise RuntimeError(f"prepare_page health check failed: {last_err}")

        print("✅ [大脑] 引导页已清理，环境就绪。", flush=True)
        # 标记 page 就绪 — keep_alive 的 /v1/ready 会立即返回 200
        PAGE_READY.set()

    prepare_page()

    while True:
        try:
            prompt, rq = request_queue.get()
            print(f"-> [任务] 正在提问: {prompt[:30]}...", flush=True)
            if page.is_closed(): prepare_page()

            # 每轮发送前再清一次弹窗(防止新弹窗)
            _dismiss_popups(page)

            # 等 textarea 出现 — Tiptap 富文本编辑器(2026-06 实测 selector)
            # 旧的 data-testid="message-textarea" 已被 M3 UI 移除
            TEXTAREA_SEL = "div[contenteditable='true']"
            try:
                page.wait_for_selector(TEXTAREA_SEL, timeout=15000, state="visible")
            except Exception as e:
                print(f"   ⚠️ 等 textarea 超时 15s: {type(e).__name__}", flush=True)
                rq.put(("", f"textarea not ready: {type(e).__name__}"))
                continue

            # JS 强灌文本(Tiptap 用 textContent)
            try:
                page.evaluate("""([txt]) => {
                    const p = document.querySelector("div[contenteditable='true']");
                    if (p) {
                        p.textContent = String(txt);
                        p.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }""", [prompt])
                page.locator(TEXTAREA_SEL).first.click(force=True)
                page.keyboard.press("Enter")
            except Exception:
                try:
                    page.keyboard.type(str(prompt))
                    page.keyboard.press("Enter")
                except Exception:
                    pass

            # 等 navigation + 网络稳定
            print("   - [等待] navigation + 网络稳定...", flush=True)
            time.sleep(3) # Give it 3 seconds to navigate
            stable_deadline = time.time() + 15
            last_url = ""
            stable_count = 0
            while time.time() < stable_deadline:
                try:
                    state = page.evaluate("() => document.readyState")
                    if state == "complete":
                        cur_url = page.url
                        if cur_url == last_url:
                            stable_count += 1
                            if stable_count >= 2:
                                break
                        else:
                            stable_count = 0
                            last_url = cur_url
                except Exception as e:
                    time.sleep(0.5)
                    continue
                time.sleep(0.5)
            time.sleep(1)
            # 再 dismiss 一次弹窗(可能在 navigation 过程中又冒出来)
            _dismiss_popups(page)
            print(f"   page URL: {page.url}", flush=True)

            # 闪电高频抓取
            # 用 page.wait_for_function 智能轮询,Playwright 内部处理 navigation 期间
            # 的 "Execution context destroyed" 异常,不会像 600 轮 evaluate 那样
            # 一直抛异常被吞掉导致 0 字符
            # selector 列表(优先级高的在前):
            #   [class*='markdown']  ← 当前 M3 UI 真正命中的 class(2026-06 probe 确认)
            #   [class*='response'] / [class*='assistant']  ← 兜底
            #   .markdown-body / .prose / .chat-msg-content  ← 旧版 UI 兼容
            print("   - [监控] 等 LLM 真的回复(智能轮询)...", flush=True)
            ans = ""
            try:
                # 第一步：等「停止生成」按钮出现（表示开始生成了）
                # 然后等它消失（表示生成完毕）
                # minimax stop button: button with SVG square icon, or aria-label contains stop/pause
                # 策略：轮询检查发送按钮是否重新可用（enabled state），最多等 170s
                deadline = time.time() + 170
                generation_started = False
                while time.time() < deadline:
                    try:
                        state = page.evaluate("""() => {
                            try {
                                // 检查是否有 stop/pause 按钮（生成中）
                                const stopBtns = document.querySelectorAll(
                                    'button[aria-label*="stop" i], button[aria-label*="pause" i], ' +
                                    'button[aria-label*="停止" i], button[aria-label*="停"] , ' +
                                    'button[data-testid*="stop" i]'
                                );
                                const hasStop = stopBtns.length > 0;
                                if (hasStop) return 'generating';

                                // 检查 textarea 是否可交互（可以的话说明生成完毕）
                                const ta = document.querySelector("div[contenteditable='true']");
                                const taReady = ta && !ta.hasAttribute('disabled');

                                // 检查最后一条消息是否有实质内容
                                const mdEls = document.querySelectorAll("[class*='markdown']");
                                if (mdEls.length === 0) return 'waiting';
                                const last = mdEls[mdEls.length - 1];
                                const txt = (last.innerText || last.textContent || '').trim();
                                if (txt.length > 5 && taReady) return 'done:' + txt.substring(0, 50);
                                return 'waiting';
                            } catch(e) { return 'error:' + e.message; }
                        }""")
                        if not generation_started and state == 'generating':
                            generation_started = True
                        if state.startswith('done:'):
                            break
                        # 如果生成已开始但现在不是generating也不是done，再等一秒确认
                        if generation_started and state == 'waiting':
                            time.sleep(1.5)
                            break
                    except Exception:
                        pass
                    time.sleep(1.0)

                # 取最后一条 markdown 块的内容（去掉思考块）
                ans = page.evaluate("""() => {
                    try {
                        // 取所有 markdown 元素，选最后一个（最新的 assistant 消息）
                        const mdEls = document.querySelectorAll("[class*='markdown']");
                        if (!mdEls.length) return '';
                        const last = mdEls[mdEls.length - 1];
                        // 克隆并移除思考块容器
                        const clone = last.cloneNode(true);
                        const thinkSels = [
                            "[class*='think']", "[class*='reasoning']",
                            "[class*='thought']", "[class*='chain-of-thought']",
                            "details", "summary"
                        ];
                        for (const ts of thinkSels) {
                            clone.querySelectorAll(ts).forEach(el => el.remove());
                        }
                        const txt = (clone.innerText || clone.textContent || '').trim();
                        // 如果去掉思考块后为空，直接返回原始内容（fallback）
                        if (!txt) return (last.innerText || last.textContent || '').trim();
                        return txt;
                    } catch(e) { return ''; }
                }""")
            except Exception as e:
                print(f"   ⚠️ 等/取 LLM 回复失败: {type(e).__name__}: {str(e)[:100]}", flush=True)
                ans = ""

            print(f"-> [成功] 提取到 {len(ans)} 字: {repr(ans[:60])}", flush=True)
            rq.put((ans, None))
        except Exception as e:
            rq.put(("", str(e)))

class ReuseAddressHTTPServer(HTTPServer):
    allow_reuse_address = True

if __name__ == "__main__":
    threading.Thread(target=playwright_executor, daemon=True).start()
    print(f"🚀 [M3 修正大脑] 端口 {PORT} 就绪 (0.0.0.0)", flush=True)
    ReuseAddressHTTPServer(('0.0.0.0', PORT), APIHandler).serve_forever()
