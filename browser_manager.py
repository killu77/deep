# browser_manager.py
"""
DeepSeek 反代管理器（纯 API 版）：
- 用 Playwright 登录一次，拿到 Cookie/Token
- 之后所有请求走 HTTP API，不再操作浏览器页面
- 天然支持并发，速度极快
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
        self.page = None  # 仅用于登录
        self.logged_in = False
        self.start_time = time.time()
        self.heartbeat_count = 0
        self.total_requests = 0

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"

        # 登录后提取的凭据
        self._cookies: dict = {}
        self._token: str = ""
        self._http_client: Optional[httpx.AsyncClient] = None

        # DeepSeek API 端点
        self.API_BASE = "https://chat.deepseek.com/api/v0"

    async def initialize(self):
        print("🔧 正在初始化...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        # 步骤 1：用浏览器登录，拿 Cookie
        await self._login_with_browser()

        if self.logged_in:
            # 步骤 2：提取 Cookie 和 Token
            await self._extract_credentials()

            # 步骤 3：关闭浏览器，之后全部走 HTTP
            await self._close_browser()

            # 步骤 4：创建 HTTP 客户端
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=10.0),
                headers=self._build_headers(),
                cookies=self._cookies,
            )

            # 验证 API 可用性
            ok = await self._verify_api()
            if ok:
                print("🎉 API 验证通过！已切换到纯 HTTP 模式，浏览器已释放。")
            else:
                print("⚠️ API 验证失败，可能需要检查登录状态。")
        else:
            print("⚠️ 登录未完成。")

    async def _login_with_browser(self):
        """用 Playwright 完成登录流程"""
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

        # 注入反检测
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        self.page = await self.context.new_page()

        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

    async def _extract_credentials(self):
        """从浏览器中提取 Cookie 和 Bearer Token"""
        print("  → 提取登录凭据...")

        # 提取 cookies
        raw_cookies = await self.context.cookies()
        for c in raw_cookies:
            self._cookies[c["name"]] = c["value"]
        print(f"  ✅ 提取到 {len(self._cookies)} 个 Cookie")

        # 尝试从 localStorage 提取 token
        try:
            token = await self.page.evaluate("""
                () => {
                    // DeepSeek 常见的 token 存储位置
                    const keys = [
                        'ds_token', 'token', 'userToken', 
                        '_token', 'auth_token', 'ds-token'
                    ];
                    for (const key of keys) {
                        const val = localStorage.getItem(key);
                        if (val) return val;
                    }
                    // 尝试从 cookie 中获取
                    const match = document.cookie.match(/ds_token=([^;]+)/);
                    if (match) return match[1];
                    return null;
                }
            """)
            if token:
                self._token = token.strip().strip('"')
                print(f"  ✅ 提取到 Token: {self._token[:20]}...")
        except Exception as e:
            print(f"  ⚠️ Token 提取异常: {e}")

        # 如果没有从 localStorage 拿到，从 cookies 中找
        if not self._token:
            for name in ["ds_token", "token", "sessionToken"]:
                if name in self._cookies:
                    self._token = self._cookies[name]
                    print(f"  ✅ 从 Cookie 中提取到 Token ({name})")
                    break

    def _build_headers(self) -> dict:
        """构建 API 请求头"""
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
        return headers

    async def _verify_api(self) -> bool:
        """验证 API 凭据是否有效"""
        try:
            resp = await self._http_client.get(
                f"{self.API_BASE}/chat/list_session",
                params={"count": 1, "offset": 0},
            )
            if resp.status_code == 200:
                data = resp.json()
                print(f"  → API 响应: status={resp.status_code}, code={data.get('code')}")
                return data.get("code") == 0
            else:
                print(f"  → API 返回 {resp.status_code}: {resp.text[:200]}")
                return False
        except Exception as e:
            print(f"  → API 验证异常: {e}")
            return False

    async def _close_browser(self):
        """登录完成后关闭浏览器释放内存"""
        try:
            if self.page and not self.page.is_closed():
                # 保留一张截图作为调试参考
                pass
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
        if not self._http_client:
            return False
        try:
            return await self._verify_api()
        except Exception:
            return False

    async def get_status(self) -> dict:
        return {
            "logged_in": self.logged_in,
            "mode": "http-api",
            "has_token": bool(self._token),
            "cookie_count": len(self._cookies),
            "uptime_seconds": time.time() - self.start_time,
            "total_requests": self.total_requests,
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        # 纯 API 模式没有浏览器页面可截图
        return None

    async def send_message(self, message: str) -> str:
        full = ""
        async for chunk in self.send_message_stream(message):
            full += chunk
        return full

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        """
        通过 DeepSeek HTTP API 发送消息，流式返回。
        天然支持并发，无需锁。
        """
        self.total_requests += 1
        req_id = self.total_requests
        print(f"📨 请求 #{req_id} (长度: {len(message)} 字符)")

        if not self._http_client:
            yield "[错误] HTTP 客户端未初始化"
            return

        try:
            # 步骤 1：创建新会话
            create_resp = await self._http_client.post(
                f"{self.API_BASE}/chat/create_session",
                json={"agent": "chat"},
            )
            if create_resp.status_code != 200:
                yield f"[错误] 创建会话失败: {create_resp.status_code}"
                return

            session_data = create_resp.json()
            if session_data.get("code") != 0:
                yield f"[错误] 创建会话异常: {session_data}"
                return

            chat_session_id = session_data["data"]["biz_data"]["id"]
            print(f"  [{req_id}] 会话已创建: {chat_session_id}")

            # 步骤 2：发送消息（SSE 流式）
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
                "POST",
                f"{self.API_BASE}/chat/completion",
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield f"[错误] API 返回 {resp.status_code}: {body.decode()[:200]}"
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

                    # 提取增量文本
                    choices = data.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")

                    if content:
                        full_text += content
                        yield content

            # 步骤 3：清理会话（可选，防止历史堆积）
            try:
                await self._http_client.post(
                    f"{self.API_BASE}/chat/delete_session",
                    json={"chat_session_id": chat_session_id},
                )
            except Exception:
                pass  # 删除失败不影响功能

            print(f"  [{req_id}] ✅ 完成，回复长度: {len(full_text)}")

        except httpx.ReadTimeout:
            print(f"  [{req_id}] ⏹️ 读取超时")
            yield "[错误] 响应超时，请稍后重试。"
        except Exception as e:
            print(f"  [{req_id}] ❌ {e}")
            import traceback
            traceback.print_exc()
            yield f"[错误] {str(e)}"

    async def simulate_activity(self):
        """心跳：纯 API 模式只需偶尔验证 Token 有效性"""
        self.heartbeat_count += 1
        # 每 50 次心跳验证一次（大约几分钟一次就够了）
        if self.heartbeat_count % 50 == 0:
            ok = await self._verify_api()
            print(f"💓 心跳 #{self.heartbeat_count} - API {'✅' if ok else '❌'}")

    async def shutdown(self):
        try:
            if self._http_client:
                await self._http_client.aclose()
            print("🔒 HTTP 客户端已关闭。")
        except Exception as e:
            print(f"⚠️ 关闭出错: {e}")
