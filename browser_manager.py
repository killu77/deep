# browser_manager.py
"""
浏览器生命周期管理器（Render 优化版）：
- 优先使用 Playwright Firefox（轻量、启动快）
- Camoufox 作为可选备选
- 针对低内存环境优化
"""

import os
import sys
import time
import json
import asyncio
import base64
from pathlib import Path
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
        self.requests_handled = 0
        self._lock = asyncio.Lock()
        self._camoufox_cm = None

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"
        # 默认用 playwright，设置 USE_CAMOUFOX=true 才用 camoufox
        self.prefer_camoufox = os.getenv("USE_CAMOUFOX", "false").lower() == "true"
        self._engine = "unknown"

    async def initialize(self):
        """初始化浏览器"""
        print("🔧 正在初始化浏览器...")

        if self.prefer_camoufox:
            # 用户明确要求 Camoufox
            try:
                await self._start_with_camoufox()
                self._engine = "camoufox"
                print("  ✅ Camoufox 启动成功")
            except Exception as e:
                print(f"  ⚠️ Camoufox 启动失败: {e}")
                await self._cleanup_camoufox()
                print("  → 回退到 Playwright Firefox...")
                await self._start_with_playwright()
                self._engine = "playwright-firefox"
        else:
            # 默认：直接用 Playwright（更快更稳）
            try:
                await self._start_with_playwright()
                self._engine = "playwright-firefox"
                print("  ✅ Playwright Firefox 启动成功")
            except Exception as e:
                print(f"  ⚠️ Playwright 启动失败: {e}")
                raise

        # 注入反检测脚本
        await self._inject_stealth_scripts()

        # Cookie 注入登录
        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

        if self.logged_in:
            print("🎉 登录成功！浏览器会话已建立。")
        else:
            print("⚠️ 登录可能未完成，请检查 /screenshot 端点。")

    async def _cleanup_camoufox(self):
        """安全清理 Camoufox 资源"""
        for resource_name, resource in [
            ("page", self.page),
            ("context", self.context),
        ]:
            try:
                if resource and not getattr(resource, 'is_closed', lambda: True)():
                    await resource.close()
            except Exception:
                pass
        try:
            if self._camoufox_cm:
                await self._camoufox_cm.__aexit__(None, None, None)
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        self._camoufox_cm = None

    async def _start_with_camoufox(self):
        """使用 Camoufox 启动"""
        print("  → 使用 Camoufox 反指纹浏览器...")
        os.environ['CAMOUFOX_NO_UPDATE_CHECK'] = '1'

        from camoufox.async_api import AsyncCamoufox

        self._camoufox_cm = AsyncCamoufox(
            headless=self.headless,
            geoip=False,
        )

        self.browser = await self._camoufox_cm.__aenter__()
        await asyncio.sleep(3)

        if not self.browser.is_connected():
            raise RuntimeError("Camoufox 启动后立即断开")

        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        await asyncio.sleep(1)
        self.page = await self.context.new_page()
        print("  ✅ Camoufox 浏览器已启动。")

    async def _start_with_playwright(self):
        """使用 Playwright Firefox 启动（针对低内存优化）"""
        print("  → 启动 Playwright Firefox...")
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()

        launch_args = [
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",  # 关键：避免 /dev/shm 不够用
        ]

        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless,
                args=launch_args,
                timeout=90000,  # 90 秒超时，给 Render 足够时间
            )
        except Exception as launch_error:
            print(f"  ⚠️ Firefox 首次启动失败: {launch_error}")
            import subprocess
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Playwright Firefox 安装失败: {result.stderr}")
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless,
                args=launch_args,
                timeout=90000,
            )

        # 使用较小的 viewport 节省内存
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
        )
        self.page = await self.context.new_page()
        print("  ✅ Playwright Firefox 已启动。")

    async def _inject_stealth_scripts(self):
        """注入反检测脚本"""
        if self._engine == "camoufox":
            await self.context.add_init_script("""
                if (navigator.webdriver !== undefined) {
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                }
            """)
            print("  🛡️ Camoufox 模式：最小化反检测")
        else:
            await self.context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { 
                    get: () => ['zh-CN', 'zh', 'en-US', 'en'] 
                });
                const originalQuery = window.navigator.permissions.query;
                if (originalQuery) {
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                            Promise.resolve({ state: Notification.permission }) :
                            originalQuery(parameters)
                    );
                }
            """)
            print("  🛡️ Firefox 模式：注入反检测脚本")

    async def is_alive(self) -> bool:
        try:
            if not self.page or self.page.is_closed():
                return False
            if not self.browser or not self.browser.is_connected():
                return False
            await self.page.evaluate("() => document.title")
            return True
        except Exception:
            return False

    async def get_status(self) -> dict:
        alive = await self.is_alive()
        return {
            "browser_alive": alive,
            "logged_in": self.logged_in,
            "engine": self._engine,
            "uptime_seconds": time.time() - self.start_time,
            "heartbeat_count": self.heartbeat_count,
            "requests_handled": self.requests_handled,
            "current_url": self.page.url if self.page and not self.page.is_closed() else "N/A",
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        try:
            if not self.page or self.page.is_closed():
                return None
            screenshot_bytes = await self.page.screenshot(full_page=False)
            return base64.b64encode(screenshot_bytes).decode("utf-8")
        except Exception as e:
            print(f"❌ 截图失败: {e}")
            return None

    async def send_message(self, message: str) -> str:
        full_response = ""
        async for chunk in self.send_message_stream(message):
            full_response += chunk
        return full_response

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        async with self._lock:
            self.requests_handled += 1
            print(f"📨 处理第 {self.requests_handled} 个请求 (长度: {len(message)} 字符)")
            print(f"  → 内容预览: {message[:100]}...")

            try:
                current_url = self.page.url if self.page else ""
                if "chat.deepseek.com" not in current_url:
                    await self.page.goto(
                        "https://chat.deepseek.com/",
                        wait_until="networkidle",
                        timeout=30000,
                    )
                    await asyncio.sleep(2)

                # 开启新对话
                print("  → 正在开启新的对话...")
                try:
                    new_chat_btn = self.page.locator(
                        "xpath=//*[contains(text(), '开启新对话')]"
                    ).first
                    await new_chat_btn.wait_for(state="visible", timeout=5000)
                    await new_chat_btn.click()
                    await asyncio.sleep(1)
                    print("  ✅ 已开启新对话")
                except Exception:
                    try:
                        icon_btn = self.page.locator(
                            "div.ds-icon-button, [class*='new-chat']"
                        ).first
                        if await icon_btn.is_visible(timeout=2000):
                            await icon_btn.click()
                            await asyncio.sleep(1)
                    except Exception:
                        pass

                # 输入消息
                textarea = self.page.locator(
                    "textarea[placeholder*='DeepSeek'], "
                    "textarea[placeholder*='发送消息'], "
                    "textarea, "
                    "[contenteditable='true']"
                ).first
                await textarea.wait_for(state="visible", timeout=10000)
                await textarea.click()
                await asyncio.sleep(0.3)
                await textarea.fill("")
                await asyncio.sleep(0.2)
                await textarea.fill(message)
                await asyncio.sleep(0.5)
                print(f"  → 已输入消息，长度: {len(message)}")

                await textarea.press("Enter")
                print("  → 消息已发送，等待响应...")
                await asyncio.sleep(2)

                # 流式读取响应
                last_text = ""
                stable_count = 0
                max_wait_seconds = 600
                response_started = False

                for tick in range(max_wait_seconds * 2):
                    await asyncio.sleep(0.5)

                    result = await self.page.evaluate("""
                        () => {
                            const items = document.querySelectorAll('div[data-virtual-list-item-key]');
                            if (items.length === 0)
                                return { text: '', done: false, itemCount: 0, hasButton: false };
                            const lastItem = items[items.length - 1];
                            const mdEls = lastItem.querySelectorAll('[class*="ds-markdown"]');
                            let text = '';
                            if (mdEls.length > 0)
                                text = mdEls[mdEls.length - 1].textContent || '';
                            const buttons = lastItem.querySelectorAll('div[role="button"]');
                            const hasButton = buttons.length > 0;
                            const stopBtn = document.querySelector('[class*="stop"], [class*="square"]');
                            const isGenerating = !!stopBtn;
                            return { text, done: hasButton && !isGenerating, itemCount: items.length, hasButton, isGenerating };
                        }
                    """)

                    current_text = result.get("text", "").strip()
                    is_done = result.get("done", False)

                    if current_text and not response_started:
                        response_started = True
                        print(f"  → 回复开始")

                    if response_started and current_text:
                        if len(current_text) > len(last_text):
                            new_part = current_text[len(last_text):]
                            last_text = current_text
                            stable_count = 0
                            yield new_part
                        elif current_text == last_text:
                            stable_count += 1
                        if is_done and stable_count >= 3:
                            print("  ✅ 响应完成")
                            break

                    if tick > 0 and tick % 20 == 0:
                        print(f"  ⏳ tick={tick}, len={len(current_text)}, stable={stable_count}")

                    if stable_count >= 60:
                        print("  ⏹️ 超时（30秒无变化）")
                        break

                if not last_text:
                    fallback_text = await self.page.evaluate("""
                        () => {
                            const allMd = document.querySelectorAll('[class*="ds-markdown"]');
                            return allMd.length > 0 ? (allMd[allMd.length - 1].textContent || '') : '';
                        }
                    """)
                    if fallback_text and fallback_text.strip():
                        yield fallback_text.strip()
                    else:
                        yield "抱歉，未能获取到响应。请稍后重试。"

                print(f"  📊 回复长度: {len(last_text)} 字符")

            except Exception as e:
                print(f"  ❌ {e}")
                import traceback
                traceback.print_exc()
                yield f"[错误] {e}"

    async def simulate_activity(self):
        if not self.page or self.page.is_closed():
            return
        try:
            self.heartbeat_count += 1
            import random
            await self.page.mouse.move(random.randint(100, 1200), random.randint(100, 600))
            await self.page.evaluate("""() => {
                document.dispatchEvent(new MouseEvent('mousemove', {
                    clientX: Math.random() * window.innerWidth,
                    clientY: Math.random() * window.innerHeight
                }));
                window.dispatchEvent(new Event('focus'));
            }""")
            if self.heartbeat_count % 10 == 0:
                print(f"💓 心跳 #{self.heartbeat_count}")
        except Exception as e:
            print(f"⚠️ 心跳异常: {e}")

    async def shutdown(self):
        for label, close_fn in [
            ("page", lambda: self.page.close() if self.page and not self.page.is_closed() else None),
            ("context", lambda: self.context.close() if self.context else None),
        ]:
            try:
                coro = close_fn()
                if coro:
                    await coro
            except Exception:
                pass

        try:
            if self._camoufox_cm:
                await self._camoufox_cm.__aexit__(None, None, None)
                self._camoufox_cm = None
            elif self.browser:
                await self.browser.close()
        except Exception:
            pass

        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass

        print("🔒 浏览器已安全关闭。")
