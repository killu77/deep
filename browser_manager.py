# browser_manager.py
"""
DeepSeek 反代管理器（纯 API 版）：
- 用 Playwright 登录 + 拦截真实 API 请求
- 提取准确的 Token / Cookie / API 路径
- 之后全部走 HTTP，释放浏览器
"""

import os
import sys
import json
import time
import asyncio
import base64
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
        self._captured_headers: dict = {}
        self._http_client: Optional[httpx.AsyncClient] = None

    async def initialize(self):
        print("🔧 正在初始化...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        await self._login_with_browser()

        if self.logged_in:
            await self._sniff_real_api()
            await self._extract_credentials()

            if self._token and self._api_base:
                await self._close_browser()
                self._http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(300.0, connect=10.0),
                    headers=self._build_headers(),
                    cookies=self._cookies,
                )
                ok = await self._verify_api()
                if ok:
                    print("🎉 API 验证通过！纯 HTTP 模式已就绪。")
                else:
                    print("⚠️ API 验证失败，回退到浏览器模式。")
                    await self._fallback_to_browser()
            else:
                print("⚠️ 未能提取 API 信息，保持浏览器模式。")
                self._http_client = None
        else:
            print("⚠️ 登录未完成。")

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

    async def _sniff_real_api(self):
        """
        在浏览器里发一条测试消息，拦截所有网络请求，
        找到真实的 API 端点和请求头
        """
        print("  → 嗅探真实 API 端点...")

        captured_requests = []

        async def on_request(request):
            url = request.url
            if "chat.deepseek.com" in url and "/api/" in url:
                captured_requests.append({
                    "url": url,
                    "method": request.method,
                    "headers": dict(request.headers),
                    "post_data": request.post_data,
                })

        self.page.on("request", on_request)

        # 等几秒让页面自然发一些 API 请求（加载会话列表等）
        await asyncio.sleep(3)

        # 打印所有捕获的请求
        print(f"  → 捕获到 {len(captured_requests)} 个 API 请求:")
        for req in captured_requests:
            print(f"    {req['method']} {req['url']}")
            # 提取 Authorization header
            if "authorization" in req["headers"]:
                auth_header = req["headers"]["authorization"]
                print(f"    Authorization: {auth_header[:50]}...")

        # 分析捕获结果，提取 API base 和 headers
        for req in captured_requests:
            url = req["url"]
            # 找到 API base（例如 /api/v0/ 或 /api/v1/ 等）
            if "/api/" in url:
                # 提取 base: https://chat.deepseek.com/api/v0
                import re
                match = re.search(r"(https://chat\.deepseek\.com/api/v\d+)", url)
                if match and not self._api_base:
                    self._api_base = match.group(1)
                    print(f"  ✅ 发现 API base: {self._api_base}")

                # 提取真实的 Authorization
                if "authorization" in req["headers"] and not self._token:
                    auth_val = req["headers"]["authorization"]
                    if auth_val.startswith("Bearer "):
                        self._token = auth_val[7:]
                        print(f"  ✅ 从请求头提取到 Token: {self._token[:30]}...")

                # 保存完整的请求头作为参考
                if not self._captured_headers:
                    self._captured_headers = {
                        k: v for k, v in req["headers"].items()
                        if k.lower() not in ("host", "content-length")
                    }

        self.page.remove_listener("request", on_request)

        if not self._api_base:
            print("  ⚠️ 未捕获到 API base，尝试常见路径...")
            # 手动尝试几个可能的路径
            for base in [
                "https://chat.deepseek.com/api/v0",
                "https://chat.deepseek.com/api/v1",
                "https://chat.deepseek.com/api",
            ]:
                self._api_base = base
                print(f"    尝试: {base}")

    async def _extract_credentials(self):
        """从浏览器中提取正确格式的 Token"""
        print("  → 提取登录凭据...")

        # 提取 cookies
        raw_cookies = await self.context.cookies()
        for c in raw_cookies:
            self._cookies[c["name"]] = c["value"]
        print(f"  ✅ 提取到 {len(self._cookies)} 个 Cookie")

        # 如果嗅探阶段已经拿到 token，就不用再提取了
        if self._token:
            print(f"  ✅ Token 已通过请求嗅探获得")
            return

        # 从 localStorage 提取 token（需要正确解析 JSON 包装）
        try:
            token = await self.page.evaluate("""
                () => {
                    // 方法1: 直接找常见 key
                    const keys = ['userToken', 'ds_token', 'token', '_token', 'auth_token'];
                    for (const key of keys) {
                        const raw = localStorage.getItem(key);
                        if (!raw) continue;
                        
                        // 可能是 JSON 包装: {"value":"xxx"} 或纯字符串
                        try {
                            const parsed = JSON.parse(raw);
                            if (parsed && parsed.value) {
                                // value 本身可能也是 JSON 字符串
                                if (typeof parsed.value === 'string') {
                                    return parsed.value;
                                }
                            }
                            if (typeof parsed === 'string') {
                                return parsed;
                            }
                        } catch(e) {
                            // 不是 JSON，就是纯 token
                            return raw;
                        }
                    }
                    
                    // 方法2: 扫描所有 localStorage，找 JWT 样式的值
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        const raw = localStorage.getItem(key);
                        if (!raw) continue;
                        
                        let val = raw;
                        try {
                            const p = JSON.parse(raw);
                            if (p && p.value && typeof p.value === 'string') val = p.value;
                        } catch(e) {}
                        
                        // JWT 或长 token 特征
                        if (val.length > 40 && !val.startsWith('{') && !val.startsWith('[')) {
                            console.log('Found potential token in key:', key);
                            return val;
                        }
                    }
                    
                    return null;
                }
            """)
            if token:
                self._token = token.strip()
                print(f"  ✅ 从 localStorage 提取到 Token: {self._token[:30]}...")
            else:
                print("  ⚠️ localStorage 中未找到 Token")
        except Exception as e:
            print(f"  ⚠️ Token 提取异常: {e}")

    def _build_headers(self) -> dict:
        headers = {
            "Accept": "text/event-stream",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
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

        # 合并嗅探到的真实请求头（优先级更高）
        if self._captured_headers:
            for key in ["x-app-version", "x-client-locale", "x-client-platform",
                        "x-client-version", "x-ds-pow-response"]:
                if key in self._captured_headers:
                    headers[key] = self._captured_headers[key]

        return headers

    async def _verify_api(self) -> bool:
        try:
            # 尝试多个可能的端点
            endpoints = [
                f"{self._api_base}/chat/list_session",
                f"{self._api_base}/chat/session/list",
                f"{self._api_base}/session/list",
            ]
            for endpoint in endpoints:
                try:
                    resp = await self._http_client.get(
                        endpoint,
                        params={"count": 1, "offset": 0},
                    )
                    print(f"  → 尝试 {endpoint}: {resp.status_code}")
                    if resp.status_code == 200:
                        data = resp.json()
                        print(f"    响应: {json.dumps(data, ensure_ascii=False)[:200]}")
                        if data.get("code") == 0 or "data" in data:
                            self._api_base = endpoint.rsplit("/", 2)[0]
                            return True
                    elif resp.status_code != 404:
                        print(f"    响应体: {resp.text[:200]}")
                except Exception as e:
                    print(f"    异常: {e}")

            # 如果都 404，试试 POST
            for endpoint in [
                f"{self._api_base}/chat/create_session",
            ]:
                try:
                    resp = await self._http_client.post(
                        endpoint,
                        json={"agent": "chat"},
                    )
                    print(f"  → 尝试 POST {endpoint}: {resp.status_code}")
                    if resp.status_code == 200:
                        data = resp.json()
                        print(f"    响应: {json.dumps(data, ensure_ascii=False)[:200]}")
                        return True
                    else:
                        print(f"    响应体: {resp.text[:200]}")
                except Exception as e:
                    print(f"    异常: {e}")

            return False
        except Exception as e:
            print(f"  → API 验证异常: {e}")
            return False

    async def _fallback_to_browser(self):
        """API 模式失败，保持浏览器运行，用 UI 操作"""
        print("  ⚠️ 回退到浏览器 UI 模式...")
        # 重新启动浏览器（之前可能已关闭）
        if not self.browser or not self.browser.is_connected():
            await self._login_with_browser()

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
            print("  🔒 浏览器已关闭，内存已释放。")
        except Exception as e:
            print(f"  ⚠️ 关闭浏览器出错: {e}")

    async def is_alive(self) -> bool:
        if self._http_client:
            try:
                return await self._verify_api()
            except Exception:
                return False
        elif self.page and not self.page.is_closed():
            try:
                await self.page.evaluate("() => document.title")
                return True
            except Exception:
                return False
        return False

    async def get_status(self) -> dict:
        return {
            "logged_in": self.logged_in,
            "mode": "http-api" if self._http_client else "browser-ui",
            "api_base": self._api_base or "N/A",
            "has_token": bool(self._token),
            "cookie_count": len(self._cookies),
            "uptime_seconds": time.time() - self.start_time,
            "total_requests": self.total_requests,
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        if self.page and not self.page.is_closed():
            try:
                data = await self.page.screenshot(full_page=False)
                return base64.b64encode(data).decode("utf-8")
            except Exception:
                pass
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

        if self._http_client:
            async for chunk in self._send_via_api(message, req_id):
                yield chunk
        elif self.page and not self.page.is_closed():
            async for chunk in self._send_via_browser(message, req_id):
                yield chunk
        else:
            yield "[错误] 无可用的发送通道"

    async def _send_via_api(self, message: str, req_id: int) -> AsyncGenerator[str, None]:
        """通过 HTTP API 发送"""
        try:
            # 创建会话
            create_resp = await self._http_client.post(
                f"{self._api_base}/chat/create_session",
                json={"agent": "chat"},
            )
            if create_resp.status_code != 200:
                yield f"[错误] 创建会话失败: {create_resp.status_code} {create_resp.text[:200]}"
                return

            session_data = create_resp.json()
            if session_data.get("code") != 0:
                yield f"[错误] 创建会话异常: {json.dumps(session_data, ensure_ascii=False)[:200]}"
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

            # 清理
            try:
                await self._http_client.post(
                    f"{self._api_base}/chat/delete_session",
                    json={"chat_session_id": chat_session_id},
                )
            except Exception:
                pass

            print(f"  [{req_id}] ✅ 完成，长度: {len(full_text)}")

        except Exception as e:
            print(f"  [{req_id}] ❌ {e}")
            import traceback
            traceback.print_exc()
            yield f"[错误] {str(e)}"

    async def _send_via_browser(self, message: str, req_id: int) -> AsyncGenerator[str, None]:
        """浏览器 UI 模式回退（保留原有逻辑的精简版）"""
        try:
            if "chat.deepseek.com" not in self.page.url:
                await self.page.goto("https://chat.deepseek.com/", wait_until="networkidle", timeout=30000)
                await asyncio.sleep(2)

            # 新对话
            try:
                btn = self.page.locator("xpath=//*[contains(text(), '开启新对话')]").first
                await btn.wait_for(state="visible", timeout=5000)
                await btn.click()
                await asyncio.sleep(1)
            except Exception:
                pass

            textarea = self.page.locator("textarea").first
            await textarea.wait_for(state="visible", timeout=10000)
            await textarea.click()
            await textarea.fill(message)
            await asyncio.sleep(0.3)
            await textarea.press("Enter")
            await asyncio.sleep(2)

            last_text = ""
            stable = 0
            for _ in range(1200):
                await asyncio.sleep(0.5)
                result = await self.page.evaluate("""
                    () => {
                        const items = document.querySelectorAll('div[data-virtual-list-item-key]');
                        if (!items.length) return {text:'',done:false};
                        const last = items[items.length-1];
                        const md = last.querySelectorAll('[class*="ds-markdown"]');
                        const text = md.length ? md[md.length-1].textContent||'' : '';
                        const btns = last.querySelectorAll('div[role="button"]');
                        const stop = document.querySelector('[class*="stop"],[class*="square"]');
                        return {text, done: btns.length>0 && !stop};
                    }
                """)
                cur = result.get("text", "").strip()
                if cur and len(cur) > len(last_text):
                    yield cur[len(last_text):]
                    last_text = cur
                    stable = 0
                elif cur == last_text:
                    stable += 1
                if result.get("done") and stable >= 3:
                    break
                if stable >= 60:
                    break

            if not last_text:
                yield "抱歉，未能获取到响应。"

            print(f"  [{req_id}] ✅ 浏览器模式完成，长度: {len(last_text)}")
        except Exception as e:
            yield f"[错误] {str(e)}"

    async def simulate_activity(self):
        self.heartbeat_count += 1
        if self.heartbeat_count % 50 == 0:
            if self._http_client:
                print(f"💓 心跳 #{self.heartbeat_count} - HTTP 模式运行中")

    async def shutdown(self):
        try:
            if self._http_client:
                await self._http_client.aclose()
            await self._close_browser()
            print("🔒 已安全关闭。")
        except Exception as e:
            print(f"⚠️ 关闭出错: {e}")
