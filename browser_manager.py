# browser_manager.py
"""
浏览器生命周期管理器（Cookie 注入版）：
- 支持 Cookie 注入登录
- 修复响应提取逻辑，与本地 deepseek_proxy.py 一致
- 每次请求开启新对话，确保上下文由 prompt 自行携带
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

        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

        if self.logged_in:
            print("🎉 登录成功！浏览器会话已建立。")
        else:
            print("⚠️ 登录可能未完成，请检查 /screenshot 端点。")

    async def _start_with_camoufox(self):
        print("  → 使用 Camoufox 反指纹浏览器...")
        from camoufox.async_api import AsyncCamoufox

        try:
            self._camoufox = AsyncCamoufox(headless=self.headless, geoip=False)
        except TypeError:
            self._camoufox = AsyncCamoufox(headless=self.headless, geoip=False)

        self.browser = await self._camoufox.__aenter__()
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self.page = await self.context.new_page()
        print("  ✅ Camoufox 浏览器已启动。")

    async def _start_with_playwright(self):
        print("  → 回退到 Playwright Firefox...")
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )
        except Exception as launch_error:
            print(f"  ⚠️ Firefox 启动失败: {launch_error}")
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
        """
        发送消息并以流式方式返回响应。
        
        完全对齐本地 deepseek_proxy.py 的逻辑：
        1. 每次请求前点击"开启新对话"
        2. 使用粘贴方式输入长文本（fill 模拟粘贴）
        3. 通过等待"复制按钮可点击"来判断回复完成
        4. 使用精确的 XPath 风格选择器提取回复
        """
        async with self._lock:
            self.requests_handled += 1
            print(f"📨 处理第 {self.requests_handled} 个请求 (长度: {len(message)} 字符)")
            print(f"  → 内容预览: {message[:100]}...")

            try:
                # ====== 确保在 DeepSeek 页面 ======
                if "chat.deepseek.com" not in self.page.url:
                    await self.page.goto(
                        "https://chat.deepseek.com/",
                        wait_until="networkidle",
                        timeout=30000,
                    )
                    await asyncio.sleep(2)

                # ====== 步骤 1：点击"开启新对话"（与本地版本完全一致）======
                print("  → 正在开启新的对话以保证上下文干净...")
                try:
                    new_chat_btn = self.page.locator(
                        "xpath=//*[contains(text(), '开启新对话')]"
                    ).first
                    await new_chat_btn.wait_for(state="visible", timeout=5000)
                    await new_chat_btn.click()
                    await asyncio.sleep(1)
                    print("  ✅ 已开启新对话")
                except Exception as e:
                    print(f"  ⚠️ 未找到'开启新对话'按钮: {e}")
                    # 备用方案：尝试找新建对话的图标按钮
                    try:
                        icon_btn = self.page.locator(
                            "div.ds-icon-button, [class*='new-chat']"
                        ).first
                        if await icon_btn.is_visible(timeout=2000):
                            await icon_btn.click()
                            await asyncio.sleep(1)
                    except Exception:
                        pass

                # ====== 步骤 2：定位输入框并输入消息 ======
                # 与本地版本对齐：等待 textarea 出现
                textarea = self.page.locator(
                    "textarea[placeholder*='DeepSeek'], "
                    "textarea[placeholder*='发送消息'], "
                    "textarea, "
                    "[contenteditable='true']"
                ).first
                await textarea.wait_for(state="visible", timeout=10000)
                await textarea.click()
                await asyncio.sleep(0.3)

                # 清空输入框
                await textarea.fill("")
                await asyncio.sleep(0.2)

                # 使用 fill 输入完整内容（相当于粘贴，不是逐字输入）
                await textarea.fill(message)
                await asyncio.sleep(0.5)
                print(f"  → 已输入消息，长度: {len(message)}")

                # ====== 步骤 3：发送消息 ======
                # 按 Enter 发送
                await textarea.press("Enter")
                print("  → 消息已发送，等待模型响应...")
                await asyncio.sleep(2)

                # ====== 步骤 4：等待回复完成 ======
                # 核心策略（与本地版本一致）：
                # 等待最后一个对话项中出现可点击的按钮（复制按钮）
                # 这是回复完成的最可靠标志

                last_text = ""
                stable_count = 0
                max_wait_seconds = 600  # 与本地版本一致，最多等 10 分钟
                response_started = False

                for tick in range(max_wait_seconds * 2):
                    await asyncio.sleep(0.5)

                    # 提取最后一个对话项中的回复内容
                    result = await self.page.evaluate("""
                        () => {
                            // 与本地版本一致的选择器：
                            // 本地版本用的 XPath: ((//div[@data-virtual-list-item-key])[last()]//div[contains(@class, 'ds-markdown')])[last()]
                            
                            const items = document.querySelectorAll('div[data-virtual-list-item-key]');
                            if (items.length === 0) {
                                return { text: '', done: false, itemCount: 0, hasButton: false };
                            }
                            
                            const lastItem = items[items.length - 1];
                            
                            // 获取最后一个 ds-markdown 元素的文本
                            const mdEls = lastItem.querySelectorAll('[class*="ds-markdown"]');
                            let text = '';
                            if (mdEls.length > 0) {
                                text = mdEls[mdEls.length - 1].textContent || '';
                            }
                            
                            // 检查复制按钮是否可点击（回复完成的标志）
                            // 本地版本的 XPath: ((//div[@data-virtual-list-item-key])[last()]//div[@role='button'])[1]
                            const buttons = lastItem.querySelectorAll('div[role="button"]');
                            const hasButton = buttons.length > 0;
                            
                            // 额外检查：是否正在生成中
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

                    # 检测到回复内容开始出现
                    if current_text and not response_started:
                        response_started = True
                        print(f"  → 检测到回复开始（消息数: {result.get('itemCount')}）")

                    if response_started and current_text:
                        # 有新增内容，流式输出
                        if len(current_text) > len(last_text):
                            new_part = current_text[len(last_text):]
                            last_text = current_text
                            stable_count = 0
                            yield new_part
                        elif current_text == last_text:
                            stable_count += 1

                        # 判断是否完成
                        if is_done and stable_count >= 3:
                            print("  ✅ 响应完成（检测到复制按钮可用）。")
                            break

                    # 进度日志
                    if tick > 0 and tick % 20 == 0:
                        print(
                            f"  ⏳ 等待中... tick={tick}, "
                            f"textLen={len(current_text)}, "
                            f"generating={is_generating}, "
                            f"hasButton={result.get('hasButton')}, "
                            f"stable={stable_count}"
                        )

                    # 超长时间没有变化
                    if stable_count >= 60:
                        print("  ⏹️ 响应超时（文本 30 秒无变化）。")
                        break

                # ====== 兜底处理 ======
                if not last_text:
                    fallback_text = await self.page.evaluate("""
                        () => {
                            const allMd = document.querySelectorAll(
                                '[class*="ds-markdown"]'
                            );
                            if (allMd.length > 0) {
                                return allMd[allMd.length - 1].textContent || '';
                            }
                            return '';
                        }
                    """)
                    if fallback_text and fallback_text.strip():
                        print(f"  ⚠️ 使用兜底方式获取到回复，长度: {len(fallback_text)}")
                        yield fallback_text.strip()
                    else:
                        print("  ❌ 完全未能获取到响应")
                        try:
                            ss = await self.take_screenshot_base64()
                            if ss:
                                print(f"  📸 调试截图 base64 长度: {len(ss)}")
                        except Exception:
                            pass
                        yield "抱歉，未能获取到响应。请稍后重试。"

                print(f"  📊 最终回复长度: {len(last_text)} 字符")

            except Exception as e:
                error_msg = f"发送消息时出错: {str(e)}"
                print(f"  ❌ {error_msg}")
                import traceback
                traceback.print_exc()
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
