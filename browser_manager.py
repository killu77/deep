# browser_manager.py
"""
DeepSeek 反代（纯 API 版）
- Playwright 登录拿 Token
- 浏览器内 fetch 探测真实 API 路径（POST + Content-Type 校验）
- 全部走 HTTP，释放浏览器
"""

import os
import sys
import json
import time
import asyncio
import httpx
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
        self._api_base: str = ""
        self._http_client: Optional[httpx.AsyncClient] = None
        self._extra_headers: dict = {}

    async def initialize(self):
        print("🔧 正在初始化...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        await self._login_with_browser()

        if not self.logged_in:
            raise RuntimeError("❌ 登录失败，无法继续。")

        # 在浏览器同源环境下探测 + 拦截真实 API
        await self._discover_api()
        await self._extract_credentials()

        if not self._token:
            raise RuntimeError("❌ 无法提取 Token。")
        if not self._api_base:
            raise RuntimeError("❌ 无法确定 API 路径。")

        await self._close_browser()

        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            headers=self._build_headers(),
            cookies=self._cookies,
        )

        ok = await self._verify_api()
        if ok:
            print("🎉 API 验证通过，纯 HTTP 模式就绪。")
        else:
            raise RuntimeError("❌ API 验证失败。请检查日志。")

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

    async def _discover_api(self):
        """
        方案一：在浏览器内 fetch 探测（POST + JSON Content-Type 检查）
        方案二：拦截页面自然发出的 XHR/Fetch 请求
        两个方案同时执行
        """
        print("  → 发现 API 路径...")

        # ── 方案二先启动：拦截页面请求 ──
        captured = []

        async def on_request(request):
            url = request.url
            if "deepseek.com" in url and request.resource_type in ("fetch", "xhr"):
                captured.append({
                    "url": url,
                    "method": request.method,
                    "headers": dict(request.headers),
                })

        self.page.on("request", on_request)

        # ── 方案一：浏览器内 fetch 探测 ──
        probe_result = await self.page.evaluate("""
            async () => {
                const results = {};

                const bases = [
                    '/api/v0', '/api/v1', '/api',
                    '/v0', '/v1',
                    '/backend-api/v0', '/backend-api/v1', '/backend-api',
                ];

                // POST 端点（更可靠，不会被 SPA fallback 骗到）
                const postEndpoints = [
                    {path: '/chat/create_session', body: JSON.stringify({agent:'chat'})},
                    {path: '/chat/session/create', body: JSON.stringify({agent:'chat'})},
                    {path: '/chat/list_session', body: JSON.stringify({count:1,offset:0})},
                    {path: '/chat/session/list', body: JSON.stringify({count:1,offset:0})},
                ];

                // GET 端点（需验证 content-type）
                const getEndpoints = [
                    '/chat/list_session?count=1&offset=0',
                    '/chat/session/list?count=1&offset=0',
                    '/user/info',
                    '/user/current',
                ];

                // 先试 POST
                for (const base of bases) {
                    for (const ep of postEndpoints) {
                        const url = base + ep.path;
                        try {
                            const resp = await fetch(url, {
                                method: 'POST',
                                credentials: 'include',
                                headers: {'Content-Type': 'application/json'},
                                body: ep.body,
                            });
                            const ct = resp.headers.get('content-type') || '';
                            const status = resp.status;
                            let body = '';
                            try { body = await resp.text(); } catch(e) {}

                            const isJson = ct.includes('application/json');
                            results[`POST ${url}`] = {status, isJson, ct, body: body.substring(0, 300)};

                            // 200 + JSON = 找到了
                            if (status === 200 && isJson) {
                                return {
                                    success: true,
                                    api_base: base,
                                    method: 'POST',
                                    path: ep.path,
                                    endpoint: url,
                                    status, body: body.substring(0, 500),
                                    all: results,
                                };
                            }
                        } catch(e) {
                            results[`POST ${url}`] = {error: e.message};
                        }
                    }
                }

                // 再试 GET（但必须验证 content-type 是 JSON）
                for (const base of bases) {
                    for (const ep of getEndpoints) {
                        const url = base + ep;
                        try {
                            const resp = await fetch(url, {
                                method: 'GET',
                                credentials: 'include',
                            });
                            const ct = resp.headers.get('content-type') || '';
                            const status = resp.status;
                            let body = '';
                            try { body = await resp.text(); } catch(e) {}

                            const isJson = ct.includes('application/json');
                            results[`GET ${url}`] = {status, isJson, ct, body: body.substring(0, 300)};

                            if (status === 200 && isJson) {
                                return {
                                    success: true,
                                    api_base: base,
                                    method: 'GET',
                                    path: ep.split('?')[0],
                                    endpoint: url,
                                    status, body: body.substring(0, 500),
                                    all: results,
                                };
                            }
                        } catch(e) {
                            results[`GET ${url}`] = {error: e.message};
                        }
                    }
                }

                return {success: false, all: results};
            }
        """)

        # 处理探测结果
        print(f"  → Fetch 探测结果:")
        if probe_result.get("success"):
            self._api_base = f"https://chat.deepseek.com{probe_result['api_base']}"
            print(f"  ✅ API base: {self._api_base}")
            print(f"     命中: {probe_result['method']} {probe_result['endpoint']}")
            print(f"     响应: {probe_result.get('body', '')[:200]}")
        else:
            all_r = probe_result.get("all", {})
            for key, val in all_r.items():
                s = val.get("status", "ERR")
                ct = val.get("ct", "")
                ij = val.get("isJson", False)
                b = val.get("body", val.get("error", ""))[:80]
                print(f"    {key}: {s} json={ij} ct={ct} → {b}")

        # ── 处理方案二：拦截到的请求 ──
        await asyncio.sleep(2)  # 多等一下
        self.page.remove_listener("request", on_request)

        if captured:
            print(f"  → 拦截到 {len(captured)} 个 XHR/Fetch 请求:")
            for req in captured:
                print(f"    {req['method']} {req['url']}")
                auth_h = req["headers"].get("authorization", "")
                if auth_h:
                    print(f"      Auth: {auth_h[:60]}...")
                    if auth_h.startswith("Bearer ") and not self._token:
                        self._token = auth_h[7:]
                        print(f"  ✅ 从拦截请求拿到 Token")

                # 如果探测失败，从拦截请求中提取 API base
                if not self._api_base and "/api/" in req["url"]:
                    import re
                    for pattern in [
                        r"(https://[^/]+/api/v\d+)",
                        r"(https://[^/]+/api)",
                    ]:
                        m = re.search(pattern, req["url"])
                        if m:
                            self._api_base = m.group(1)
                            print(f"  ✅ 从拦截请求提取 API base: {self._api_base}")
                            break

                for key in ["x-app-version", "x-client-locale", "x-client-platform",
                             "x-client-version", "x-ds-pow-response"]:
                    if key in req["headers"] and key not in self._extra_headers:
                        self._extra_headers[key] = req["headers"][key]

        # ── 最终手段：如果什么都没找到，强制触发一次对话 ──
        if not self._api_base:
            print("  → 常规探测失败，尝试在浏览器中发一条消息来捕获 API...")
            await self._force_capture_via_chat()

    async def _force_capture_via_chat(self):
        """在浏览器中实际发一条消息，拦截所有请求"""
        captured = []

        async def on_req(request):
            url = request.url
            if "deepseek.com" in url and request.resource_type in ("fetch", "xhr"):
                captured.append({
                    "url": url,
                    "method": request.method,
                    "headers": dict(request.headers),
                    "post_data": request.post_data,
                })

        self.page.on("request", on_req)

        try:
            # 找到输入框，输入 "hi"，发送
            textarea = self.page.locator("textarea").first
            await textarea.wait_for(state="visible", timeout=10000)
            await textarea.fill("hi")
            await asyncio.sleep(0.3)
            await textarea.press("Enter")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"  ⚠️ 发送测试消息失败: {e}")
        finally:
            self.page.remove_listener("request", on_req)

        print(f"  → 强制对话拦截到 {len(captured)} 个请求:")
        for req in captured:
            print(f"    {req['method']} {req['url']}")
            if req.get("post_data"):
                print(f"      Body: {str(req['post_data'])[:100]}")

            auth_h = req["headers"].get("authorization", "")
            if auth_h and auth_h.startswith("Bearer ") and not self._token:
                self._token = auth_h[7:]
                print(f"  ✅ Token 已捕获")

            if not self._api_base:
                import re
                # 匹配 completion 或 session 相关路径
                for pattern in [
                    r"(https://[^/]+/api/v\d+)",
                    r"(https://[^/]+/api)",
                    r"(https://[^/]+/v\d+)(?=/chat/)",
                ]:
                    m = re.search(pattern, req["url"])
                    if m:
                        self._api_base = m.group(1)
                        print(f"  ✅ API base: {self._api_base}")
                        break

            for key in ["x-app-version", "x-client-locale", "x-client-platform",
                         "x-client-version", "x-ds-pow-response"]:
                if key in req["headers"] and key not in self._extra_headers:
                    self._extra_headers[key] = req["headers"][key]

    async def _extract_credentials(self):
        print("  → 提取登录凭据...")

        raw_cookies = await self.context.cookies()
        for c in raw_cookies:
            self._cookies[c["name"]] = c["value"]
        print(f"  ✅ 提取到 {len(self._cookies)} 个 Cookie")

        if self._token:
            print(f"  ✅ Token 已有: {self._token[:30]}...")
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
                    const keys = ['userToken', 'ds_token', 'token', '_token', 'auth_token'];
                    for (const k of keys) {
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
                print(f"  ✅ Token: {self._token[:30]}...")
            else:
                print("  ⚠️ localStorage 中未找到 Token")
        except Exception as e:
            print(f"  ⚠️ Token 提取异常: {e}")

        if not self._token:
            for name in ["ds_token", "token", "sessionToken"]:
                if name in self._cookies:
                    self._token = self._cookies[name]
                    print(f"  ✅ 用 Cookie '{name}' 作为 Token")
                    break

    def _build_headers(self) -> dict:
        headers = {
            "Accept": "text/event-stream",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://chat.deepseek.com",
            "Referer": "https://chat.deepseek.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        headers.update(self._extra_headers)
        return headers

    async def _verify_api(self) -> bool:
        """用实际创建会话来验证"""
        try:
            resp = await self._http_client.post(
                f"{self._api_base}/chat/create_session",
                json={"agent": "chat"},
            )
            print(f"  → 验证 POST {self._api_base}/chat/create_session: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                print(f"    响应: {json.dumps(data, ensure_ascii=False)[:200]}")
                if data.get("code") == 0:
                    try:
                        sid = data["data"]["biz_data"]["id"]
                        await self._http_client.post(
                            f"{self._api_base}/chat/delete_session",
                            json={"chat_session_id": sid},
                        )
                    except Exception:
                        pass
                    return True
            else:
                print(f"    响应: {resp.text[:300]}")
            return False
        except Exception as e:
            print(f"  → 验证异常: {e}")
            return False

    async def _close_browser(self):
        try:
            if self.context:
                await self.context.close()
                self.context = None
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            self.page = None
            print("  🔒 浏览器已关闭。")
        except Exception as e:
            print(f"  ⚠️ 关闭浏览器出错: {e}")

    async def is_alive(self) -> bool:
        if not self._http_client:
            return False
        try:
            resp = await self._http_client.post(
                f"{self._api_base}/chat/create_session",
                json={"agent": "chat"},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    try:
                        sid = data["data"]["biz_data"]["id"]
                        await self._http_client.post(
                            f"{self._api_base}/chat/delete_session",
                            json={"chat_session_id": sid},
                        )
                    except Exception:
                        pass
                    return True
            return False
        except Exception:
            return False

    async def get_status(self) -> dict:
        return {
            "logged_in": self.logged_in,
            "mode": "http-api",
            "api_base": self._api_base,
            "has_token": bool(self._token),
            "cookie_count": len(self._cookies),
            "uptime_seconds": time.time() - self.start_time,
            "total_requests": self.total_requests,
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        return None

    async def send_message(self, message: str) -> str:
        full = ""
        async for chunk in self.send_message_stream(message):
            full += chunk
        return full

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        self.total_requests += 1
        req_id = self.total_requests
        print(f"📨 请求 #{req_id} (长度: {len(message)} 字符)")

        if not self._http_client:
            yield "[错误] HTTP 客户端未初始化"
            return

        try:
            create_resp = await self._http_client.post(
                f"{self._api_base}/chat/create_session",
                json={"agent": "chat"},
            )
            if create_resp.status_code != 200:
                yield f"[错误] 创建会话失败: {create_resp.status_code} {create_resp.text[:200]}"
                return

            session_data = create_resp.json()
            if session_data.get("code") != 0:
                yield f"[错误] {json.dumps(session_data, ensure_ascii=False)[:200]}"
                return

            chat_session_id = session_data["data"]["biz_data"]["id"]
            print(f"  [{req_id}] 会话: {chat_session_id}")

            payload = {
                "chat_session_id": chat_session_id,
                "parent_message_id": 0,
                "prompt": message,
                "ref_file_ids": [],
                "thinking_enabled": False,
                "search_enabled": False,
            }

            full_text = ""
            async with self._http_client.stream(
                "POST", f"{self._api_base}/chat/completion", json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield f"[错误] {resp.status_code}: {body.decode()[:200]}"
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices", [])
                    if choices:
                        content = choices[0].get("delta", {}).get("content", "")
                        if content:
                            full_text += content
                            yield content

            try:
                await self._http_client.post(
                    f"{self._api_base}/chat/delete_session",
                    json={"chat_session_id": chat_session_id},
                )
            except Exception:
                pass

            print(f"  [{req_id}] ✅ 完成，长度: {len(full_text)}")

        except httpx.ReadTimeout:
            yield "[错误] 响应超时"
        except Exception as e:
            print(f"  [{req_id}] ❌ {e}")
            import traceback
            traceback.print_exc()
            yield f"[错误] {str(e)}"

    async def simulate_activity(self):
        pass  # 纯 API 模式不需要心跳

    async def shutdown(self):
        try:
            if self._http_client:
                await self._http_client.aclose()
            print("🔒 已关闭。")
        except Exception as e:
            print(f"⚠️ {e}")
