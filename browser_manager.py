"""
浏览器生命周期管理器：
- 使用 Camoufox (反指纹 Firefox) 通过 Playwright 启动
- 管理登录、会话保持、消息发送
- 提供截图、状态查询等调试功能
"""

import os
import sys
import time
import json
import asyncio
import base64
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
        self._lock = asyncio.Lock()  # 防止并发操作浏览器

        # 凭据
        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")

    async def initialize(self):
        """初始化浏览器并完成登录。"""
        print("🔧 正在初始化 Camoufox 浏览器...")

        # 先尝试使用 Camoufox
        camoufox_succeeded = False
        try:
            # 设置环境变量禁用 Camoufox 的更新检查（避免 GitHub API 限流）
            os.environ['CAMOUFOX_NO_UPDATE_CHECK'] = '1'
            from camoufox.async_api import AsyncCamoufox
            self._camoufox_cls = AsyncCamoufox
            await self._start_with_camoufox()
            camoufox_succeeded = True
        except Exception as e:
            print(f"⚠️ Camoufox 启动失败: {e}")
            print("⚠️ 将回退到 Playwright Firefox...")
            # 清理可能的部分资源
            if hasattr(self, '_camoufox'):
                try:
                    await self._camoufox.__aexit__(None, None, None)
                except:
                    pass
            camoufox_succeeded = False

        # 如果 Camoufox 失败，回退到 Playwright
        if not camoufox_succeeded:
            await self._start_with_playwright()

        # 执行登录
        auth = AuthHandler(self.page)
        self.logged_in = await auth.login(self.email, self.password)

        if self.logged_in:
            print("🎉 登录成功！浏览器会话已建立。")
        else:
            print("⚠️ 登录可能未完成，但浏览器保持运行。请检查 /screenshot 端点。")

    async def _start_with_camoufox(self):
        """使用 Camoufox 启动浏览器。"""
        print("  → 使用 Camoufox 反指纹浏览器...")

        from camoufox.async_api import AsyncCamoufox

        # Camoufox 启动参数
        self._camoufox = AsyncCamoufox(
            headless=True,
            geoip=False,  # HF 环境中禁用 GeoIP
        )
        self.browser = await self._camoufox.__aenter__()

        # 创建上下文和页面
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self.page = await self.context.new_page()

        # 注入反检测脚本
        await self._inject_stealth_scripts()
        print("  ✅ Camoufox 浏览器已启动。")

    async def _start_with_playwright(self):
        """回退方案：使用标准 Playwright Firefox。"""
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.firefox.launch(
            headless=True,
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
        await self._inject_stealth_scripts()
        print("  ✅ Playwright Firefox 已启动。")

    async def _inject_stealth_scripts(self):
        """注入反检测 JavaScript 脚本。"""
        stealth_js = """
        // 隐藏 webdriver 标志
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        
        // 伪造 plugins
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' }
            ]
        });
        
        // 伪造 languages
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
        
        // 隐藏自动化相关属性
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
        
        // 伪造 chrome 对象 (某些检测会查看)
        if (!window.chrome) {
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
        }
        
        // 覆盖 permissions query
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );

        console.log('[Stealth] 反检测脚本已注入');
        """
        await self.context.add_init_script(stealth_js)

    async def is_alive(self) -> bool:
        """检查浏览器是否仍然存活。"""
        try:
            if not self.page or self.page.is_closed():
                return False
            # 尝试执行一个简单的 JS 来验证页面响应
            await self.page.evaluate("() => document.title")
            return True
        except Exception:
            return False

    async def get_status(self) -> dict:
        """获取当前状态信息。"""
        alive = await self.is_alive()
        return {
            "browser_alive": alive,
            "logged_in": self.logged_in,
            "uptime_seconds": time.time() - self.start_time,
            "heartbeat_count": self.heartbeat_count,
            "requests_handled": self.requests_handled,
            "current_url": self.page.url if self.page and not self.page.is_closed() else "N/A",
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        """截取当前页面截图，返回 Base64 字符串。"""
        try:
            if not self.page or self.page.is_closed():
                return None
            screenshot_bytes = await self.page.screenshot(full_page=False)
            return base64.b64encode(screenshot_bytes).decode("utf-8")
        except Exception as e:
            print(f"❌ 截图失败: {e}")
            return None

    async def send_message(self, message: str) -> str:
        """发送消息并等待完整响应（非流式）。"""
        full_response = ""
        async for chunk in self.send_message_stream(message):
            full_response += chunk
        return full_response

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        """
        发送消息并流式返回响应。
        核心逻辑：
        1. 在输入框中输入消息
        2. 点击发送按钮
        3. 监听 DOM 变化，实时捕获 AI 的响应文本
        """
        async with self._lock:  # 确保同一时间只有一个对话
            self.requests_handled += 1
            print(f"📨 处理第 {self.requests_handled} 个请求: {message[:50]}...")

            try:
                # 确保在聊天页面
                if "chat.deepseek.com" not in self.page.url:
                    await self.page.goto("https://chat.deepseek.com/", wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(2)

                # 尝试开始新对话（点击 "新对话" 按钮，如果存在的话）
                try:
                    new_chat_btn = self.page.locator("div.ds-icon-button, [class*='new-chat']").first
                    if await new_chat_btn.is_visible(timeout=2000):
                        await new_chat_btn.click()
                        await asyncio.sleep(1)
                except Exception:
                    pass

                # 找到输入框并输入消息
                textarea = self.page.locator("textarea, [contenteditable='true'], #chat-input").first
                await textarea.wait_for(state="visible", timeout=10000)
                await textarea.click()
                await asyncio.sleep(0.3)

                # 清空并输入（使用 fill 更可靠）
                await textarea.fill(message)
                await asyncio.sleep(0.5)

                # 找到并点击发送按钮
                # DeepSeek 的发送按钮通常是一个带有特定 class 的 div
                send_btn = self.page.locator(
                    "div[class*='send'], button[class*='send'], "
                    "[data-testid='send-button'], "
                    "div.ds-icon-button[role='button']"
                ).last
                
                if await send_btn.is_visible(timeout=3000):
                    await send_btn.click()
                else:
                    # 备选方案：按 Enter 发送
                    await textarea.press("Enter")

                print("  → 消息已发送，等待响应...")
                await asyncio.sleep(1)

                # 监听响应生成
                last_text = ""
                stable_count = 0
                max_wait_seconds = 120

                for _ in range(max_wait_seconds * 2):  # 每 0.5 秒检查一次
                    await asyncio.sleep(0.5)

                    # 获取最后一个助手消息的文本
                    current_text = await self.page.evaluate("""
                        () => {
                            // 尝试多种选择器来获取最后一个 AI 回复
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
                        # 有新内容，yield 增量部分
                        new_part = current_text[len(last_text):]
                        last_text = current_text
                        stable_count = 0
                        yield new_part
                    elif current_text and current_text == last_text:
                        stable_count += 1
                        # 检查是否有"正在生成"的标志
                        is_generating = await self.page.evaluate("""
                            () => {
                                // 检查是否还有加载动画或生成中的标志
                                const loadingEls = document.querySelectorAll(
                                    '[class*="loading"], [class*="generating"], ' +
                                    '[class*="thinking"], .ds-loading'
                                );
                                return loadingEls.length > 0;
                            }
                        """)
                        if not is_generating and stable_count >= 6:
                            # 文本已稳定 3 秒且无生成标志，认为完成
                            print("  ✅ 响应完成。")
                            break
                    
                    if stable_count >= 20:  # 最多等 10 秒无变化
                        print("  ⏹️ 响应超时（文本无变化）。")
                        break

                if not last_text:
                    yield "抱歉，未能获取到响应。请稍后重试。"

            except Exception as e:
                error_msg = f"发送消息时出错: {str(e)}"
                print(f"  ❌ {error_msg}")
                # 截图保存错误现场
                screenshot = await self.take_screenshot_base64()
                if screenshot:
                    print(f"  📸 错误截图已保存（可通过 /screenshot 端点查看）")
                yield f"[错误] {error_msg}"

    async def simulate_activity(self):
        """模拟用户活动，保持会话活跃。"""
        if not self.page or self.page.is_closed():
            return

        try:
            self.heartbeat_count += 1

            # 1. 模拟鼠标移动
            import random
            x = random.randint(100, 1800)
            y = random.randint(100, 900)
            await self.page.mouse.move(x, y)

            # 2. 注入心跳脚本
            await self.page.evaluate("""
                () => {
                    // 触发一些 DOM 事件，模拟用户交互
                    document.dispatchEvent(new MouseEvent('mousemove', {
                        clientX: Math.random() * window.innerWidth,
                        clientY: Math.random() * window.innerHeight
                    }));
                    
                    // 轻微滚动
                    window.scrollBy(0, Math.random() > 0.5 ? 1 : -1);
                    
                    // 触发 focus 事件
                    window.dispatchEvent(new Event('focus'));
                    document.dispatchEvent(new Event('visibilitychange'));
                    
                    console.log('[Keepalive] 心跳 - ' + new Date().toISOString());
                }
            """)

            if self.heartbeat_count % 10 == 0:
                print(f"💓 心跳 #{self.heartbeat_count} - 页面: {self.page.url[:60]}...")

        except Exception as e:
            print(f"⚠️ 心跳异常: {e}")

    async def shutdown(self):
        """安全关闭浏览器。"""
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 浏览器已安全关闭。")
        except Exception as e:
            print(f"⚠️ 关闭浏览器时出错: {e}")
