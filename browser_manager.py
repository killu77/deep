# browser_manager.py
"""
DeepSeek 反代（浏览器驻留模式）
- Playwright 登录后保留浏览器
- 所有 API 请求都在浏览器内通过 page.evaluate(fetch) 执行
- 自动处理 Cookie、PoW 等反爬机制
"""

import os
import sys
import json
import time
import asyncio
import hashlib
from datetime import datetime
from typing import AsyncGenerator, Optional

from auth_handler import AuthHandler


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.logged_in = False
        self.start_time = time.time()
        self.heartbeat_count = 0
        self.total_requests = 0

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"

        self._cookies: dict = {}
        self._token: str = ""
        self._api_base: str = "https://chat.deepseek.com/api/v0"
        self._extra_headers: dict = {}

        # ── 就绪控制 ──
        self._ready = False
        self._ready_event = asyncio.Event()

    # ── 公开方法：等待就绪 ──
    async def wait_until_ready(self, timeout: float = 180.0) -> bool:
        """阻塞等待浏览器初始化完成，返回是否就绪"""
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def initialize(self):
        print("🔧 正在初始化...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        await self._login_with_browser()

        if not self.logged_in:
            raise RuntimeError("❌ 登录失败。")

        await self._capture_real_api()
        await self._extract_credentials()

        if not self._token:
            raise RuntimeError("❌ 无法提取 Token。")

        ok = await self._verify_api()
        if ok:
            print("🎉 API 验证通过，浏览器驻留模式就绪。")
        else:
            print("⚠️ API 验证未通过，但仍继续运行。")

        # ── 标记就绪，唤醒所有等待者 ──
        self._ready = True
        self._ready_event.set()

    async def _login_with_browser(self):
        print("  → 启动浏览器进行登录...")
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )
        except Exception:
            import subprocess
            env = os.environ.copy()
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True, text=True, env=env, timeout=120,
            )
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )

        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
        )
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        self.page = await self.context.new_page()

        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

    async def _capture_real_api(self):
        print("  → 拦截浏览器请求获取认证信息...")

        captured = []

        async def on_request(request):
            if request.resource_type in ("fetch", "xhr"):
                captured.append({
                    "url": request.url,
                    "method": request.method,
                    "headers": dict(request.headers),
                })

        self.page.on("request", on_request)
        await asyncio.sleep(3)

        deepseek_reqs = [r for r in captured if "deepseek.com/api" in r["url"]]
        if not deepseek_reqs:
            print("  → 触发页面操作以捕获 API 请求...")
            try:
                for sel in [
                    "xpath=//*[contains(text(), '开启新对话')]",
                    "xpath=//*[contains(text(), 'New chat')]",
                ]:
                    try:
                        btn = self.page.locator(sel).first
                        if await btn.is_visible(timeout=2000):
                            await btn.click()
                            await asyncio.sleep(1)
                            break
                    except Exception:
                        continue

                textarea = self.page.locator("textarea").first
                await textarea.wait_for(state="visible", timeout=5000)
                await textarea.fill("test")
                await textarea.press("Enter")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"  ⚠️ 触发操作失败: {e}")

        self.page.remove_listener("request", on_request)

        for req in captured:
            if "deepseek.com" not in req["url"]:
                continue
            headers = req["headers"]
            auth_h = headers.get("authorization", "")
            if auth_h.startswith("Bearer ") and not self._token:
                self._token = auth_h[7:]
                print(f"  ✅ Token: {self._token[:30]}...")

            for key in ["x-app-version", "x-client-locale", "x-client-platform",
                         "x-client-version", "x-client-timezone"]:
                if key in headers and key not in self._extra_headers:
                    self._extra_headers[key] = headers[key]

    async def _extract_credentials(self):
        print("  → 提取凭据...")

        raw_cookies = await self.context.cookies()
        for c in raw_cookies:
            self._cookies[c["name"]] = c["value"]
        print(f"  ✅ Cookie: {len(self._cookies)} 个")

        if self._token:
            return

        try:
            token = await self.page.evaluate("""
                () => {
                    function extract(raw) {
                        if (!raw) return null;
                        try {
                            const p = JSON.parse(raw);
                            if (p && typeof p.value === 'string') return p.value;
                            if (typeof p === 'string') return p;
                        } catch(e) {}
                        return raw;
                    }
                    for (const k of ['userToken', 'ds_token', 'token', '_token', 'auth_token']) {
                        const r = localStorage.getItem(k);
                        if (r) {
                            const v = extract(r);
                            if (v && v.length > 20 && !v.startsWith('{') && !v.startsWith('<')) return v;
                        }
                    }
                    return null;
                }
            """)
            if token:
                self._token = token.strip()
                print(f"  ✅ Token(localStorage): {self._token[:30]}...")
        except Exception as e:
            print(f"  ⚠️ {e}")

    async def _refresh_token(self):
        """从浏览器实时获取最新 token"""
        try:
            token = await self.page.evaluate("""
                () => {
                    for (const k of ['userToken', 'ds_token', 'token', '_token', 'auth_token']) {
                        const raw = localStorage.getItem(k);
                        if (!raw) continue;
                        try {
                            const p = JSON.parse(raw);
                            if (p && typeof p.value === 'string' && p.value.length > 20) return p.value;
                            if (typeof p === 'string' && p.length > 20) return p;
                        } catch(e) {}
                        if (raw.length > 20 && !raw.startsWith('{') && !raw.startsWith('<')) return raw;
                    }
                    for (const k of ['userToken', 'ds_token', 'token']) {
                        const raw = sessionStorage.getItem(k);
                        if (raw && raw.length > 20 && !raw.startsWith('{')) return raw;
                    }
                    return null;
                }
            """)
            if token and token.strip():
                new_token = token.strip()
                if new_token != self._token:
                    print(f"  🔄 Token 已更新: {new_token[:30]}...")
                    self._token = new_token
        except Exception as e:
            print(f"  ⚠️ 刷新 token 失败: {e}")

    async def _verify_api(self) -> bool:
        try:
            result = await self.page.evaluate("""
                async (token) => {
                    try {
                        const resp = await fetch('/api/v0/chat_session/create', {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                                'Content-Type': 'application/json',
                                'Authorization': 'Bearer ' + token,
                            },
                            body: JSON.stringify({}),
                        });
                        const data = await resp.json();
                        return {status: resp.status, data: data};
                    } catch(e) {
                        return {error: e.message};
                    }
                }
            """, self._token)

            print(f"  → 验证状态: {result.get('status')}")
            if result.get("status") == 200:
                data = result.get("data", {})
                if data.get("code") == 0:
                    biz = data.get("data", {}).get("biz_data", {})
                    sid = biz.get("id", "")
                    print(f"  → 验证成功，测试会话: {sid}")
                    if sid:
                        try:
                            await self.page.evaluate("""
                                async (args) => {
                                    await fetch('/api/v0/chat_session/delete', {
                                        method: 'POST',
                                        credentials: 'include',
                                        headers: {
                                            'Content-Type': 'application/json',
                                            'Authorization': 'Bearer ' + args.token,
                                        },
                                        body: JSON.stringify({chat_session_id: args.sid}),
                                    });
                                }
                            """, {"token": self._token, "sid": sid})
                        except Exception:
                            pass
                    return True
            return False
        except Exception as e:
            print(f"  → 验证异常: {e}")
            return False

    # ─── 核心发送方法 ───

    async def send_message(self, message: str) -> str:
        full = ""
        async for chunk in self.send_message_stream(message):
            full += chunk
        return full

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        # ── 等待就绪 ──
        if not self._ready:
            print("  ⏳ 请求等待浏览器初始化完成...")
            ok = await self.wait_until_ready(timeout=180)
            if not ok:
                yield "[错误] 浏览器初始化超时，请稍后重试"
                return

        self.total_requests += 1
        req_id = self.total_requests
        print(f"📨 请求 #{req_id} (长度: {len(message)} 字符)")

        if not self.page:
            yield "[错误] 浏览器未就绪"
            return

        try:
            # 每次请求前尝试刷新 token
            await self._refresh_token()

            # ── Step 1: 创建会话 ──
            session_result = await self._api_create_session()

            # 如果 token 失效，尝试仅用 Cookie
            if session_result.get("code") == 40003:
                print(f"  [{req_id}] Token 失效(40003)，尝试仅用 Cookie 认证...")
                self._token = ""
                session_result = await self._api_create_session()
                print(f"  [{req_id}] Cookie-only 结果: code={session_result.get('code')}")

            if session_result.get("error") or session_result.get("code") != 0:
                err_msg = session_result.get("msg", session_result.get("error", "未知错误"))
                print(f"  [{req_id}] ❌ 创建会话失败: {err_msg}")
                yield f"[错误] 创建会话失败: {err_msg}"
                return

            biz_data = session_result["data"]["biz_data"]
            chat_session_id = biz_data["id"]
            print(f"  [{req_id}] 会话: {chat_session_id}")

            # ── Step 2: 获取并求解 PoW ──
            pow_header = await self._solve_pow_challenge(req_id)

            # ── Step 3: 浏览器内 SSE 流式请求 ──
            await self.page.evaluate("""
                () => {
                    window.__ds_stream_chunks = [];
                    window.__ds_stream_done = false;
                    window.__ds_stream_error = null;
                }
            """)

            await self.page.evaluate("""
                (params) => {
                    const {token, session_id, prompt, pow_header} = params;
                    const headers = {
                        'Content-Type': 'application/json',
                        'Accept': 'text/event-stream',
                    };
                    if (token) {
                        headers['Authorization'] = 'Bearer ' + token;
                    }
                    if (pow_header) {
                        headers['x-ds-pow-response'] = pow_header;
                    }

                    fetch('/api/v0/chat/completion', {
                        method: 'POST',
                        credentials: 'include',
                        headers: headers,
                        body: JSON.stringify({
                            chat_session_id: session_id,
                            parent_message_id: null,
                            prompt: prompt,
                            ref_file_ids: [],
                            thinking_enabled: false,
                            search_enabled: false,
                        }),
                    }).then(async (resp) => {
                        if (!resp.ok) {
                            const text = await resp.text();
                            window.__ds_stream_error = 'HTTP ' + resp.status + ': ' + text.substring(0, 500);
                            window.__ds_stream_done = true;
                            return;
                        }

                        const reader = resp.body.getReader();
                        const decoder = new TextDecoder();
                        let buffer = '';

                        while (true) {
                            const {done, value} = await reader.read();
                            if (done) break;

                            buffer += decoder.decode(value, {stream: true});
                            const lines = buffer.split('\\n');
                            buffer = lines.pop();

                            for (const line of lines) {
                                if (line.startsWith('data: ')) {
                                    const data = line.substring(6).trim();
                                    if (data === '[DONE]') {
                                        window.__ds_stream_done = true;
                                        return;
                                    }
                                    try {
                                        const parsed = JSON.parse(data);
                                        const choices = parsed.choices || [];
                                        if (choices.length > 0) {
                                            const delta = choices[0].delta || {};
                                            const content = delta.content || '';
                                            if (content) {
                                                window.__ds_stream_chunks.push(content);
                                            }
                                        }
                                    } catch(e) {}
                                }
                            }
                        }
                        window.__ds_stream_done = true;
                    }).catch((e) => {
                        window.__ds_stream_error = e.message;
                        window.__ds_stream_done = true;
                    });
                }
            """, {
                "token": self._token,
                "session_id": chat_session_id,
                "prompt": message,
                "pow_header": pow_header,
            })

            # ── Step 4: 轮询读取结果 ──
            read_index = 0
            full_text = ""
            max_wait = 300.0
            waited = 0.0
            idle_count = 0

            while waited < max_wait:
                result = await self.page.evaluate("""
                    (fromIndex) => {
                        return {
                            chunks: window.__ds_stream_chunks.slice(fromIndex),
                            total: window.__ds_stream_chunks.length,
                            done: window.__ds_stream_done,
                            error: window.__ds_stream_error,
                        };
                    }
                """, read_index)

                if result.get("error"):
                    err_msg = result["error"]
                    print(f"  [{req_id}] ❌ 流式错误: {err_msg}")
                    yield f"[错误] {err_msg}"
                    break

                new_chunks = result.get("chunks", [])
                for chunk in new_chunks:
                    full_text += chunk
                    yield chunk
                read_index += len(new_chunks)

                if result.get("done"):
                    if not new_chunks:
                        break
                    continue

                if new_chunks:
                    idle_count = 0
                else:
                    idle_count += 1

                sleep_time = 0.05 if idle_count < 20 else 0.2
                await asyncio.sleep(sleep_time)
                waited += sleep_time

            # ── 清理会话 ──
            try:
                await self.page.evaluate("""
                    async (args) => {
                        const headers = {'Content-Type': 'application/json'};
                        if (args.token) headers['Authorization'] = 'Bearer ' + args.token;
                        await fetch('/api/v0/chat_session/delete', {
                            method: 'POST',
                            credentials: 'include',
                            headers: headers,
                            body: JSON.stringify({chat_session_id: args.sid}),
                        });
                    }
                """, {"token": self._token, "sid": chat_session_id})
            except Exception:
                pass

            print(f"  [{req_id}] ✅ 完成，长度: {len(full_text)}")

        except Exception as e:
            print(f"  [{req_id}] ❌ {e}")
            import traceback
            traceback.print_exc()
            yield f"[错误] {str(e)}"

    # ─── 内部辅助方法 ───

    async def _api_create_session(self) -> dict:
        """在浏览器内创建聊天会话"""
        return await self.page.evaluate("""
            async (token) => {
                try {
                    const headers = {'Content-Type': 'application/json'};
                    if (token) headers['Authorization'] = 'Bearer ' + token;
                    const resp = await fetch('/api/v0/chat_session/create', {
                        method: 'POST',
                        credentials: 'include',
                        headers: headers,
                        body: JSON.stringify({}),
                    });
                    return await resp.json();
                } catch(e) {
                    return {error: e.message};
                }
            }
        """, self._token)

    async def _solve_pow_challenge(self, req_id: int) -> str:
        """获取并求解 PoW，返回 header 字符串"""
        pow_result = await self.page.evaluate("""
            async (token) => {
                try {
                    const headers = {'Content-Type': 'application/json'};
                    if (token) headers['Authorization'] = 'Bearer ' + token;
                    const resp = await fetch('/api/v0/chat/create_pow_challenge', {
                        method: 'POST',
                        credentials: 'include',
                        headers: headers,
                        body: JSON.stringify({target_path: '/api/v0/chat/completion'}),
                    });
                    return await resp.json();
                } catch(e) {
                    return {error: e.message};
                }
            }
        """, self._token)

        if pow_result.get("code") != 0:
            print(f"  [{req_id}] ⚠️ PoW challenge 获取失败: {pow_result.get('msg', '')}")
            return ""

        challenge_data = pow_result["data"]["biz_data"]["challenge"]
        algorithm = challenge_data.get("algorithm", "")
        challenge = challenge_data.get("challenge", "")
        salt = challenge_data.get("salt", "")
        difficulty = challenge_data.get("difficulty", 0)

        print(f"  [{req_id}] PoW: algo={algorithm} diff={difficulty}")

        pow_answer = self._solve_pow_python(challenge, salt, difficulty)

        if pow_answer and not pow_answer.get("error"):
            print(f"  [{req_id}] PoW 已解决: nonce={pow_answer['nonce']}")
            return f"{algorithm}_{challenge}_{salt}_{pow_answer['nonce']}"
        else:
            print(f"  [{req_id}] ⚠️ PoW 求解失败")
            return ""

    def _solve_pow_python(self, challenge: str, salt: str, difficulty: int) -> dict:
        """Python SHA3-256 PoW 求解"""
        print(f"    PoW 求解中: difficulty={difficulty} ...")
        start = time.time()
        try:
            for nonce in range(100_000_000):
                input_str = f"{salt}_{nonce}_{challenge}"
                hash_bytes = hashlib.sha3_256(input_str.encode()).digest()

                leading_zeros = 0
                for byte in hash_bytes:
                    if byte == 0:
                        leading_zeros += 8
                    else:
                        b = byte
                        while (b & 0x80) == 0:
                            leading_zeros += 1
                            b <<= 1
                        break
                    if leading_zeros >= difficulty:
                        break

                if leading_zeros >= difficulty:
                    elapsed = time.time() - start
                    print(f"    PoW 求解成功: nonce={nonce}, 耗时={elapsed:.2f}s")
                    return {"nonce": str(nonce), "result": hash_bytes.hex()}

                if nonce > 0 and nonce % 1_000_000 == 0:
                    elapsed = time.time() - start
                    print(f"    PoW 进度: {nonce} 次, 耗时={elapsed:.1f}s")

        except Exception as e:
            print(f"    PoW 异常: {e}")

        return {"error": "PoW failed"}

    async def is_alive(self) -> bool:
        """检查浏览器页面是否存活"""
        if not self._ready or not self.page:
            return False
        try:
            result = await self.page.evaluate("() => document.title")
            return bool(result)
        except Exception:
            return False

    async def get_status(self) -> dict:
        alive = await self.is_alive() if self.page else False
        return {
            "logged_in": self.logged_in,
            "browser_alive": alive,
            "ready": self._ready,
            "mode": "browser-resident",
            "api_base": self._api_base,
            "has_token": bool(self._token),
            "cookie_count": len(self._cookies),
            "uptime_seconds": time.time() - self.start_time,
            "total_requests": self.total_requests,
            "heartbeat_count": self.heartbeat_count,
            "requests_handled": self.total_requests,
            "engine": "playwright-firefox",
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        if not self.page:
            return None
        try:
            buf = await self.page.screenshot(full_page=False)
            import base64
            return base64.b64encode(buf).decode()
        except Exception:
            return None

    async def simulate_activity(self):
        self.heartbeat_count += 1
        if self.page:
            try:
                await self.page.evaluate("() => document.title")
            except Exception:
                pass

    async def shutdown(self):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 已关闭。")
        except Exception as e:
            print(f"⚠️ {e}")
