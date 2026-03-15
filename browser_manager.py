# browser_manager.py
"""
浏览器生命周期管理器（Cookie 注入版）：
- 支持 Cookie 注入登录（不再依赖模拟输入）
- 修复缓存保存 Errno 16 问题
- 修复 Camoufox 启动参数
"""

import os
import sys
import time
import json
import asyncio
import base64
import shutil
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

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"
        self._engine = "unknown"

    def _prepare_camoufox_cache(self):
        home_cache = Path.home() / ".cache"
        store_dir = home_cache / "camoufox_store"
        cache_dir = home_cache / "camoufox"

        if store_dir.exists() and any(store_dir.iterdir()):
            print(f"  📦 从持久存储恢复 Camoufox 缓存...")
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(store_dir, cache_dir, dirs_exist_ok=True)
                print(f"  ✅ 缓存已恢复，大小: {self._dir_size(cache_dir):.0f} MB")
            except Exception as e:
                print(f"  ⚠️ 恢复缓存失败: {e}，将重新下载")
        else:
            print(f"  📦 未找到持久缓存，Camoufox 将首次下载...")
            store_dir.mkdir(parents=True, exist_ok=True)

    def _save_camoufox_cache(self):
        """修复：使用增量复制，避免 Errno 16"""
        home_cache = Path.home() / ".cache"
        store_dir = home_cache / "camoufox_store"
        cache_dir = home_cache / "camoufox"

        if cache_dir.exists() and any(cache_dir.iterdir()):
            try:
                store_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(cache_dir, store_dir, dirs_exist_ok=True)
                print(f"  💾 Camoufox 缓存已保存到持久存储 ({self._dir_size(store_dir):.0f} MB)")
            except PermissionError as e:
                print(f"  ⚠️ 保存缓存权限不足: {e}")
            except OSError as e:
                print(f"  ⚠️ 保存缓存失败（非致命）: {e}")

    @staticmethod
    def _dir_size(path: Path) -> float:
        total = 0
        try:
            for f in path.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        except Exception:
            pass
        return total / (1024 * 1024)

    async def initialize(self):
        """初始化浏览器并完成登录。"""
        print("🔧 正在初始化浏览器...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        camoufox_succeeded = False
        try:
            os.environ['CAMOUFOX_NO_UPDATE_CHECK'] = '1'
            self._prepare_camoufox_cache()

            from camoufox.async_api import AsyncCamoufox
            self._camoufox_cls = AsyncCamoufox
            await self._start_with_camoufox()
            camoufox_succeeded = True
            self._engine = "camoufox"
            self._save_camoufox_cache()
        except Exception as e:
            print(f"⚠️ Camoufox 启动失败: {e}")
            print("⚠️ 将回退到 Playwright Firefox...")
            if hasattr(self, '_camoufox'):
                try:
                    await self._camoufox.__aexit__(None, None, None)
                except:
                    pass
            camoufox_succeeded = False

        if not camoufox_succeeded:
            await self._start_with_playwright()
            self._engine = "playwright-firefox"

        await self._inject_stealth_scripts()

        # ====== 改动：使用 Cookie 注入方式登录，传入 context ======
        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

        if self.logged_in:
            print("🎉 登录成功！浏览器会话已建立。")
        else:
            print("⚠️ 登录可能未完成，请检查 /screenshot 端点。")

    async def _start_with_camoufox(self):
        """使用 Camoufox 启动浏览器。"""
        print("  → 使用 Camoufox 反指纹浏览器...")

        from camoufox.async_api import AsyncCamoufox

        try:
            self._camoufox = AsyncCamoufox(
                headless=self.headless,
                geoip=False,
            )
        except TypeError:
            self._camoufox = AsyncCamoufox(
                headless=self.headless,
                geoip=False,
            )

        self.browser = await self._camoufox.__aenter__()

        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self.page = await self.context.new_page()
        print("  ✅ Camoufox 浏览器已启动。")

    async def _start_with_playwright(self):
        """回退方案：使用标准 Playwright Firefox。"""
        print("  → 回退到 Playwright Firefox...")

        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless,
                args=["--no-sandbox"],
            )
        except Exception as launch_error:
            print(f"  ⚠️ Firefox 启动失败: {launch_error}")
            import subprocess
            env = os.environ.copy()
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True, text=True, env=env, timeout=120
            )
            if result.returncode != 0:
                raise RuntimeError(f"Playwright Firefox 安装失败: {result.stderr}")
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless,
                args=["--no-sandbox"],
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
        self.page = await self.context.new_page()
        print("  ✅ Playwright Firefox 已启动。")

    async def _inject_stealth_scripts(self):
        if self._engine == "camoufox":
            minimal_js = """
            if (navigator.webdriver !== undefined) {
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            }
            """
            await self.context.add_init_script(minimal_js)
            print("  🛡️ Camoufox 模式：使用最小化反检测脚本")
        else:
            firefox_stealth_js = """
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
            """
            await self.context.add_init_script(firefox_stealth_js)
            print("  🛡️ Firefox 模式：注入兼容的反检测脚本")

    async def is_alive(self) -> bool:
        try:
            if not self.page or self.page.is_closed():
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
            print(f"📨 处理第 {self.requests_handled} 个请求: {message[:50]}...")

            try:
                if "chat.deepseek.com" not in self.page.url:
                    await self.page.goto("https://chat.deepseek.com/", wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(2)

                try:
                    new_chat_btn = self.page.locator("div.ds-icon-button, [class*='new-chat']").first
                    if await new_chat_btn.is_visible(timeout=2000):
                        await new_chat_btn.click()
                        await asyncio.sleep(1)
                except Exception:
                    pass

                textarea = self.page.locator("textarea, [contenteditable='true'], #chat-input").first
                await textarea.wait_for(state="visible", timeout=10000)
                await textarea.click()
                await asyncio.sleep(0.3)
                await textarea.fill(message)
                await asyncio.sleep(0.5)

                send_btn = self.page.locator(
                    "div[class*='send'], button[class*='send'], "
                    "[data-testid='send-button'], "
                    "div.ds-icon-button[role='button']"
                ).last

                if await send_btn.is_visible(timeout=3000):
                    await send_btn.click()
                else:
                    await textarea.press("Enter")

                print("  → 消息已发送，等待响应...")
                await asyncio.sleep(1)

                last_text = ""
                stable_count = 0
                max_wait_seconds = 120

                for _ in range(max_wait_seconds * 2):
                    await asyncio.sleep(0.5)

                    current_text = await self.page.evaluate("""
                        () => {
                            const selectors = [
                                '.ds-markdown.ds-markdown--block',
                                '[class*="message-content"]',
                                '[class*="assistant"]',
                                '.markdown-body'
                            ];
                            for (const sel of selectors) {
                                const elements = document.querySelectorAll(sel);
                                if (elements.length > 0) {
                                    const lastEl = elements[elements.length - 1];
                                    return lastEl.textContent || '';
                                }
                            }
                            return '';
                        }
                    """)

                    if current_text and len(current_text) > len(last_text):
                        new_part = current_text[len(last_text):]
                        last_text = current_text
                        stable_count = 0
                        yield new_part
                    elif current_text and current_text == last_text:
                        stable_count += 1
                        is_generating = await self.page.evaluate("""
                            () => {
                                const loadingEls = document.querySelectorAll(
                                    '[class*="loading"], [class*="generating"], ' +
                                    '[class*="thinking"], .ds-loading'
                                );
                                return loadingEls.length > 0;
                            }
                        """)
                        if not is_generating and stable_count >= 6:
                            print("  ✅ 响应完成。")
                            break

                    if stable_count >= 20:
                        print("  ⏹️ 响应超时（文本无变化）。")
                        break

                if not last_text:
                    yield "抱歉，未能获取到响应。请稍后重试。"

            except Exception as e:
                error_msg = f"发送消息时出错: {str(e)}"
                print(f"  ❌ {error_msg}")
                yield f"[错误] {error_msg}"

    async def simulate_activity(self):
        if not self.page or self.page.is_closed():
            return
        try:
            self.heartbeat_count += 1
            import random
            x = random.randint(100, 1800)
            y = random.randint(100, 900)
            await self.page.mouse.move(x, y)
            await self.page.evaluate("""
                () => {
                    document.dispatchEvent(new MouseEvent('mousemove', {
                        clientX: Math.random() * window.innerWidth,
                        clientY: Math.random() * window.innerHeight
                    }));
                    window.scrollBy(0, Math.random() > 0.5 ? 1 : -1);
                    window.dispatchEvent(new Event('focus'));
                }
            """)
            if self.heartbeat_count % 10 == 0:
                print(f"💓 心跳 #{self.heartbeat_count} - 页面: {self.page.url[:60]}...")
        except Exception as e:
            print(f"⚠️ 心跳异常: {e}")

    async def shutdown(self):
        try:
            self._save_camoufox_cache()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 浏览器已安全关闭。")
        except Exception as e:
            print(f"⚠️ 关闭浏览器时出错: {e}")
