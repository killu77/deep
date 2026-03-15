# browser_manager.py
"""
浏览器生命周期管理器（Playwright Firefox + 页面池并发版）
- 支持 N 个并发请求（N = 页面池大小）
- 每个页面独立工作，互不阻塞
"""

import os
import sys
import time
import asyncio
import base64
from datetime import datetime
from typing import AsyncGenerator, Optional

from auth_handler import AuthHandler


class BrowserPage:
    """封装单个浏览器页面的状态"""
    def __init__(self, page, context, page_id: int):
        self.page = page
        self.context = context
        self.page_id = page_id
        self.busy = False
        self.requests_handled = 0


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.logged_in = False
        self.start_time = time.time()
        self.heartbeat_count = 0
        self.total_requests = 0

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"

        # 页面池配置
        self.pool_size = int(os.getenv("BROWSER_POOL_SIZE", "3"))
        self._page_pool: list[BrowserPage] = []
        self._pool_lock = asyncio.Lock()
        self._pool_semaphore: Optional[asyncio.Semaphore] = None

        # 保留一个"主页面"用于登录、截图、心跳
        self.page = None
        self.context = None

    async def initialize(self):
        print("🔧 正在初始化浏览器...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        await self._start_browser()

        # 用主 context 登录
        self.context = await self._create_context()
        self.page = await self.context.new_page()

        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

        if self.logged_in:
            print("🎉 登录成功！")
            # 获取登录后的 cookies
            cookies = await self.context.cookies()
            # 初始化页面池，每个页面注入相同 cookies
            await self._init_page_pool(cookies)
            print(f"🏊 页面池已就绪，并发容量: {self.pool_size}")
        else:
            print("⚠️ 登录可能未完成，请检查 /screenshot 端点。")

    async def _start_browser(self):
        print("  → 启动 Playwright Firefox...")
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )
        except Exception as launch_error:
            print(f"  ⚠️ Firefox 启动失败: {launch_error}，尝试重新安装...")
            import subprocess
            env = os.environ.copy()
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True, text=True, env=env, timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Playwright Firefox 安装失败: {result.stderr}")
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )

        print("  ✅ Playwright Firefox 已启动。")

    async def _create_context(self):
        """创建一个带有反检测脚本的浏览器上下文"""
        context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
        )
        stealth_js = """
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
        await context.add_init_script(stealth_js)
        return context

    async def _init_page_pool(self, cookies: list):
        """初始化页面池，每个页面创建独立 context 并注入 cookies"""
        self._pool_semaphore = asyncio.Semaphore(self.pool_size)

        for i in range(self.pool_size):
            try:
                ctx = await self._create_context()
                await ctx.add_cookies(cookies)
                page = await ctx.new_page()

                # 导航到 DeepSeek
                await page.goto(
                    "https://chat.deepseek.com/",
                    wait_until="networkidle",
                    timeout=30000,
                )
                await asyncio.sleep(1)

                bp = BrowserPage(page=page, context=ctx, page_id=i)
                self._page_pool.append(bp)
                print(f"  📄 页面 #{i} 已就绪")
            except Exception as e:
                print(f"  ⚠️ 页面 #{i} 初始化失败: {e}")

        if not self._page_pool:
            print("  ❌ 页面池为空，回退到主页面单线程模式")
            self.pool_size = 0

    async def _acquire_page(self) -> Optional[BrowserPage]:
        """从池中获取一个空闲页面（阻塞等待直到有空闲的）"""
        if not self._pool_semaphore:
            return None

        await self._pool_semaphore.acquire()

        async with self._pool_lock:
            for bp in self._page_pool:
                if not bp.busy:
                    bp.busy = True
                    return bp

        # 不应到达这里
        self._pool_semaphore.release()
        return None

    async def _release_page(self, bp: BrowserPage):
        """归还页面到池中"""
        async with self._pool_lock:
            bp.busy = False
        self._pool_semaphore.release()

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
        async with self._pool_lock:
            busy_count = sum(1 for bp in self._page_pool if bp.busy)
        return {
            "browser_alive": alive,
            "logged_in": self.logged_in,
            "engine": "playwright-firefox",
            "pool_size": self.pool_size,
            "pool_busy": busy_count,
            "pool_free": self.pool_size - busy_count,
            "uptime_seconds": time.time() - self.start_time,
            "heartbeat_count": self.heartbeat_count,
            "total_requests": self.total_requests,
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
        """
        发送消息并以流式方式返回响应。
        从页面池获取页面，支持多请求并发。
        """
        self.total_requests += 1
        req_id = self.total_requests
        print(f"📨 请求 #{req_id} (长度: {len(message)} 字符)")
        print(f"  → 内容预览: {message[:100]}...")

        bp = await self._acquire_page()

        # 如果页面池不可用，回退到主页面单线程
        if bp is None:
            print(f"  ⚠️ 请求 #{req_id} 回退到主页面模式")
            async for chunk in self._send_on_page(self.page, message, req_id):
                yield chunk
            return

        try:
            bp.requests_handled += 1
            print(f"  → 请求 #{req_id} 分配到页面 #{bp.page_id} (该页面第 {bp.requests_handled} 次使用)")

            # 检查页面是否还活着
            try:
                await bp.page.evaluate("() => document.title")
            except Exception:
                print(f"  ⚠️ 页面 #{bp.page_id} 已失效，尝试重建...")
                try:
                    cookies = await self.context.cookies()
                    await bp.context.close()
                    bp.context = await self._create_context()
                    await bp.context.add_cookies(cookies)
                    bp.page = await bp.context.new_page()
                    await bp.page.goto(
                        "https://chat.deepseek.com/",
                        wait_until="networkidle",
                        timeout=30000,
                    )
                    await asyncio.sleep(1)
                    print(f"  ✅ 页面 #{bp.page_id} 已重建")
                except Exception as rebuild_err:
                    print(f"  ❌ 页面 #{bp.page_id} 重建失败: {rebuild_err}")
                    yield "[错误] 浏览器页面不可用，请稍后重试。"
                    return

            async for chunk in self._send_on_page(bp.page, message, req_id):
                yield chunk
        finally:
            await self._release_page(bp)
            print(f"  🔄 请求 #{req_id} 已释放页面 #{bp.page_id}")

    async def _send_on_page(
        self, page, message: str, req_id: int
    ) -> AsyncGenerator[str, None]:
        """在指定页面上发送消息并流式返回响应"""
        try:
            # 确保在 DeepSeek 页面
            if "chat.deepseek.com" not in page.url:
                await page.goto(
                    "https://chat.deepseek.com/",
                    wait_until="networkidle",
                    timeout=30000,
                )
                await asyncio.sleep(2)

            # 步骤 1：开启新对话
            print(f"  [{req_id}] 正在开启新的对话...")
            try:
                new_chat_btn = page.locator(
                    "xpath=//*[contains(text(), '开启新对话')]"
                ).first
                await new_chat_btn.wait_for(state="visible", timeout=5000)
                await new_chat_btn.click()
                await asyncio.sleep(1)
                print(f"  [{req_id}] ✅ 已开启新对话")
            except Exception as e:
                print(f"  [{req_id}] ⚠️ 未找到'开启新对话'按钮: {e}")
                try:
                    icon_btn = page.locator(
                        "div.ds-icon-button, [class*='new-chat']"
                    ).first
                    if await icon_btn.is_visible(timeout=2000):
                        await icon_btn.click()
                        await asyncio.sleep(1)
                except Exception:
                    pass

            # 步骤 2：定位输入框并输入
            textarea = page.locator(
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
            print(f"  [{req_id}] 已输入消息，长度: {len(message)}")

            # 步骤 3：发送
            await textarea.press("Enter")
            print(f"  [{req_id}] 消息已发送，等待模型响应...")
            await asyncio.sleep(2)

            # 步骤 4：等待回复完成
            last_text = ""
            stable_count = 0
            max_wait_seconds = 600
            response_started = False

            for tick in range(max_wait_seconds * 2):
                await asyncio.sleep(0.5)

                result = await page.evaluate("""
                    () => {
                        const items = document.querySelectorAll('div[data-virtual-list-item-key]');
                        if (items.length === 0) {
                            return { text: '', done: false, itemCount: 0, hasButton: false };
                        }
                        
                        const lastItem = items[items.length - 1];
                        
                        const mdEls = lastItem.querySelectorAll('[class*="ds-markdown"]');
                        let text = '';
                        if (mdEls.length > 0) {
                            text = mdEls[mdEls.length - 1].textContent || '';
                        }
                        
                        const buttons = lastItem.querySelectorAll('div[role="button"]');
                        const hasButton = buttons.length > 0;
                        
                        const stopBtn = document.querySelector('[class*="stop"], [class*="square"]');
                        const isGenerating = !!stopBtn;
                        
                        return { 
                            text: text, 
                            done: hasButton && !isGenerating,
                            itemCount: items.length, 
                            hasButton: hasButton,
                            isGenerating: isGenerating
                        };
                    }
                """)

                current_text = result.get("text", "").strip()
                is_done = result.get("done", False)
                is_generating = result.get("isGenerating", False)

                if current_text and not response_started:
                    response_started = True
                    print(f"  [{req_id}] 检测到回复开始（消息数: {result.get('itemCount')}）")

                if response_started and current_text:
                    if len(current_text) > len(last_text):
                        new_part = current_text[len(last_text):]
                        last_text = current_text
                        stable_count = 0
                        yield new_part
                    elif current_text == last_text:
                        stable_count += 1

                    if is_done and stable_count >= 3:
                        print(f"  [{req_id}] ✅ 响应完成")
                        break

                if tick > 0 and tick % 20 == 0:
                    print(
                        f"  [{req_id}] ⏳ 等待中... tick={tick}, "
                        f"textLen={len(current_text)}, "
                        f"generating={is_generating}, "
                        f"hasButton={result.get('hasButton')}, "
                        f"stable={stable_count}"
                    )

                if stable_count >= 60:
                    print(f"  [{req_id}] ⏹️ 响应超时（文本 30 秒无变化）。")
                    break

            # 兜底处理
            if not last_text:
                fallback_text = await page.evaluate("""
                    () => {
                        const allMd = document.querySelectorAll('[class*="ds-markdown"]');
                        if (allMd.length > 0) {
                            return allMd[allMd.length - 1].textContent || '';
                        }
                        return '';
                    }
                """)
                if fallback_text and fallback_text.strip():
                    print(f"  [{req_id}] ⚠️ 兜底获取回复，长度: {len(fallback_text)}")
                    yield fallback_text.strip()
                else:
                    print(f"  [{req_id}] ❌ 完全未能获取到响应")
                    yield "抱歉，未能获取到响应。请稍后重试。"

            print(f"  [{req_id}] 📊 最终回复长度: {len(last_text)} 字符")

        except Exception as e:
            error_msg = f"发送消息时出错: {str(e)}"
            print(f"  [{req_id}] ❌ {error_msg}")
            import traceback
            traceback.print_exc()
            yield f"[错误] {error_msg}"

    async def simulate_activity(self):
        """心跳：对主页面和池中所有空闲页面模拟活动"""
        self.heartbeat_count += 1
        pages_to_ping = []

        if self.page and not self.page.is_closed():
            pages_to_ping.append(("main", self.page))

        async with self._pool_lock:
            for bp in self._page_pool:
                if not bp.busy and bp.page and not bp.page.is_closed():
                    pages_to_ping.append((f"pool-{bp.page_id}", bp.page))

        for label, page in pages_to_ping:
            try:
                import random
                x = random.randint(100, 1800)
                y = random.randint(100, 900)
                await page.mouse.move(x, y)
                await page.evaluate("""
                    () => {
                        document.dispatchEvent(new MouseEvent('mousemove', {
                            clientX: Math.random() * window.innerWidth,
                            clientY: Math.random() * window.innerHeight
                        }));
                        window.scrollBy(0, Math.random() > 0.5 ? 1 : -1);
                        window.dispatchEvent(new Event('focus'));
                    }
                """)
            except Exception:
                pass

        if self.heartbeat_count % 10 == 0:
            print(f"💓 心跳 #{self.heartbeat_count} - 存活页面: {len(pages_to_ping)}")

    async def shutdown(self):
        try:
            for bp in self._page_pool:
                try:
                    await bp.context.close()
                except Exception:
                    pass
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 浏览器已安全关闭。")
        except Exception as e:
            print(f"⚠️ 关闭浏览器时出错: {e}")
