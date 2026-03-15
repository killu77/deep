# browser_manager.py

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


class ChatPage:
    def __init__(self, page, page_id: int):
        self.page = page
        self.page_id = page_id
        self.busy = False
        self.request_count = 0
        self.last_used = 0.0

    async def start_new_chat(self):
        if "chat.deepseek.com" not in self.page.url:
            await self.page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(2)

        for sel in [
            "xpath=//*[contains(text(), '开启新对话')]",
            "xpath=//*[contains(text(), '新对话')]",
            "xpath=//*[contains(text(), 'New chat')]",
            "div.ds-icon-button",
            "[class*='new-chat']",
        ]:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(1)
                    return
            except Exception:
                continue

        await self.page.goto(
            "https://chat.deepseek.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)

    async def type_and_send(self, message: str):
        textarea = self.page.locator(
            "textarea[placeholder*='DeepSeek'], "
            "textarea[placeholder*='发送消息'], "
            "textarea, "
            "[contenteditable='true']"
        ).first
        await textarea.wait_for(state="visible", timeout=10000)
        await textarea.click()
        await asyncio.sleep(0.3)

        try:
            await textarea.fill("")
            await asyncio.sleep(0.1)
            await textarea.fill(message)
        except Exception:
            await self.page.evaluate("""
                (text) => {
                    const el = document.querySelector('textarea')
                        || document.querySelector('[contenteditable="true"]');
                    if (!el) return;
                    if (el.tagName === 'TEXTAREA') {
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLTextAreaElement.prototype, 'value'
                        ).set;
                        setter.call(el, text);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    } else {
                        el.innerText = text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }
            """, message)

        await asyncio.sleep(0.5)
        await textarea.press("Enter")
        await asyncio.sleep(1)

    async def read_response_instant(self) -> str:
        """
        立刻读取最后一个对话项的 ds-markdown 文本。
        不等待，不延迟，调用时立刻读。
        """
        try:
            text = await self.page.evaluate("""
                () => {
                    const items = document.querySelectorAll(
                        'div[data-virtual-list-item-key]'
                    );
                    if (items.length === 0) return '';
                    const lastItem = items[items.length - 1];
                    const mdEls = lastItem.querySelectorAll(
                        '[class*="ds-markdown"]'
                    );
                    if (mdEls.length === 0) return '';
                    return mdEls[mdEls.length - 1].textContent || '';
                }
            """)
            return (text or "").strip()
        except Exception:
            return ""

    async def check_button_and_read(self) -> dict:
        """
        一次 evaluate 调用同时检查复制按钮 + 读取文本。
        保证读到的文本和按钮状态是同一瞬间的。
        """
        return await self.page.evaluate("""
            () => {
                const items = document.querySelectorAll(
                    'div[data-virtual-list-item-key]'
                );
                if (items.length === 0) {
                    return {
                        hasButton: false,
                        isGenerating: false,
                        text: '',
                        itemCount: 0,
                    };
                }

                const lastItem = items[items.length - 1];

                // 读文本
                const mdEls = lastItem.querySelectorAll(
                    '[class*="ds-markdown"]'
                );
                let text = '';
                if (mdEls.length > 0) {
                    text = mdEls[mdEls.length - 1].textContent || '';
                }

                // 复制按钮
                const buttons = lastItem.querySelectorAll(
                    'div[role="button"]'
                );
                const hasButton = buttons.length > 0;

                // 正在生成
                const stopBtn = document.querySelector(
                    '[class*="stop"], [class*="square"]'
                );
                const isGenerating = !!stopBtn &&
                    stopBtn.offsetParent !== null;

                return {
                    hasButton: hasButton,
                    isGenerating: isGenerating,
                    text: text,
                    itemCount: items.length,
                };
            }
        """)

    async def is_alive(self) -> bool:
        try:
            if self.page.is_closed():
                return False
            await self.page.evaluate("() => document.title")
            return True
        except Exception:
            return False


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.logged_in = False
        self.start_time = time.time()
        self.heartbeat_count = 0
        self.requests_handled = 0
        self.total_requests = 0

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"
        self._engine = "unknown"

        self._page_count = int(os.getenv("PAGE_COUNT", "3"))
        self._pages: list[ChatPage] = []
        self._page_semaphore: asyncio.Semaphore = None

        self._ready = False
        self._ready_event = asyncio.Event()

    async def wait_until_ready(self, timeout: float = 180.0) -> bool:
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    def _prepare_camoufox_cache(self):
        home_cache = Path.home() / ".cache"
        store_dir = home_cache / "camoufox_store"
        cache_dir = home_cache / "camoufox"
        if store_dir.exists() and any(store_dir.iterdir()):
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(store_dir, cache_dir, dirs_exist_ok=True)
            except Exception:
                pass
        else:
            store_dir.mkdir(parents=True, exist_ok=True)

    def _save_camoufox_cache(self):
        home_cache = Path.home() / ".cache"
        store_dir = home_cache / "camoufox_store"
        cache_dir = home_cache / "camoufox"
        if cache_dir.exists() and any(cache_dir.iterdir()):
            try:
                store_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(cache_dir, store_dir, dirs_exist_ok=True)
            except Exception:
                pass

    async def initialize(self):
        print("🔧 正在初始化浏览器...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        camoufox_ok = False
        try:
            os.environ['CAMOUFOX_NO_UPDATE_CHECK'] = '1'
            self._prepare_camoufox_cache()
            await self._start_with_camoufox()
            camoufox_ok = True
            self._engine = "camoufox"
            self._save_camoufox_cache()
        except Exception as e:
            print(f"⚠️ Camoufox 失败: {e}，回退 Playwright Firefox")
            if hasattr(self, '_camoufox'):
                try:
                    await self._camoufox.__aexit__(None, None, None)
                except Exception:
                    pass

        if not camoufox_ok:
            await self._start_with_playwright()
            self._engine = "playwright-firefox"

        await self._inject_stealth_scripts()

        first_page = await self.context.new_page()
        auth = AuthHandler(first_page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

        if not self.logged_in:
            print("⚠️ 登录可能未完成")
            await first_page.close()
        else:
            print("🎉 登录成功！")
            self._pages.append(ChatPage(first_page, 0))
            print(f"  📄 页面 #0 就绪")

        for i in range(1, self._page_count):
            try:
                page = await self.context.new_page()
                await page.goto(
                    "https://chat.deepseek.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await asyncio.sleep(2)
                self._pages.append(ChatPage(page, i))
                print(f"  📄 页面 #{i} 就绪")
            except Exception as e:
                print(f"  ⚠️ 页面 #{i} 创建失败: {e}")

        actual_count = len(self._pages)
        self._page_semaphore = asyncio.Semaphore(actual_count)

        self._ready = True
        self._ready_event.set()
        print(f"✅ 就绪（引擎: {self._engine}，{actual_count} 个并发页面）")

    async def _start_with_camoufox(self):
        from camoufox.async_api import AsyncCamoufox
        self._camoufox = AsyncCamoufox(headless=self.headless, geoip=False)
        self.browser = await self._camoufox.__aenter__()
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        print("  ✅ Camoufox 已启动")

    async def _start_with_playwright(self):
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )
        except Exception:
            import subprocess
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True, text=True, timeout=120,
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
        print("  ✅ Playwright Firefox 已启动")

    async def _inject_stealth_scripts(self):
        if self._engine == "camoufox":
            await self.context.add_init_script(
                "if(navigator.webdriver!==undefined)"
                "{Object.defineProperty(navigator,'webdriver',{get:()=>undefined})}"
            )
        else:
            await self.context.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                Object.defineProperty(navigator,'languages',{
                    get:()=>['zh-CN','zh','en-US','en']
                });
            """)

    async def _acquire_page(self) -> ChatPage:
        await self._page_semaphore.acquire()
        for cp in self._pages:
            if not cp.busy:
                cp.busy = True
                cp.last_used = time.time()
                return cp
        for _ in range(100):
            await asyncio.sleep(0.1)
            for cp in self._pages:
                if not cp.busy:
                    cp.busy = True
                    cp.last_used = time.time()
                    return cp
        raise RuntimeError("无法获取空闲页面")

    def _release_page(self, cp: ChatPage):
        cp.busy = False
        self._page_semaphore.release()

    async def send_message(self, message: str) -> str:
        full = ""
        async for chunk in self.send_message_stream(message):
            full += chunk
        return full

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        if not self._ready:
            ok = await self.wait_until_ready(timeout=180)
            if not ok:
                yield "[错误] 浏览器初始化超时"
                return

        self.total_requests += 1
        self.requests_handled += 1
        req_id = self.total_requests
        print(f"📨 请求 #{req_id} (长度: {len(message)} 字符)")

        cp = None
        try:
            cp = await asyncio.wait_for(self._acquire_page(), timeout=300)
        except asyncio.TimeoutError:
            yield "[错误] 所有页面忙碌，请稍后重试"
            return
        except Exception as e:
            yield f"[错误] {e}"
            return

        print(f"  [{req_id}] 分配到页面 #{cp.page_id}")

        try:
            cp.request_count += 1

            if not await cp.is_alive():
                print(f"  [{req_id}] 页面 #{cp.page_id} 已死，恢复中...")
                try:
                    new_page = await self.context.new_page()
                    await new_page.goto(
                        "https://chat.deepseek.com/",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await asyncio.sleep(2)
                    cp.page = new_page
                except Exception as e:
                    yield f"[错误] 页面恢复失败: {e}"
                    return

            await cp.start_new_chat()
            await asyncio.sleep(1)
            await cp.type_and_send(message)
            print(f"  [{req_id}] 等待回复...")

            # ═══════════════════════════════════════════
            # 核心逻辑：跟 Selenium 版一样
            # 只等复制按钮出现，按钮出现的那一刻文本就在同一次
            # evaluate 里一起读出来了，不给审查替换的时间窗口
            # ═══════════════════════════════════════════

            max_wait_seconds = 600
            captured_text = ""

            for tick in range(max_wait_seconds * 2):
                await asyncio.sleep(0.5)

                try:
                    # 关键：一次 evaluate 同时检查按钮 + 读文本
                    # 按钮出现的瞬间文本一定是原始内容
                    state = await cp.check_button_and_read()
                except Exception:
                    continue

                has_button = state.get("hasButton", False)
                is_generating = state.get("isGenerating", False)
                text = (state.get("text") or "").strip()
                item_count = state.get("itemCount", 0)

                # 还在生成中，继续等
                if is_generating:
                    if tick > 0 and tick % 40 == 0:
                        print(f"  [{req_id}] ⏳ 生成中... "
                              f"len={len(text)} tick={tick}")
                    continue

                # 复制按钮出现了！立刻抢文本
                if has_button and text:
                    captured_text = text
                    print(f"  [{req_id}] ✅ 复制按钮出现，"
                          f"立刻捕获 {len(text)} 字符")
                    break

                # 没有在生成，也没有按钮，但有文本
                # 可能按钮还没渲染出来，再等几轮
                if not is_generating and text and tick > 10:
                    # 给按钮渲染 2 秒时间
                    await asyncio.sleep(0.3)
                    state2 = await cp.check_button_and_read()
                    if state2.get("hasButton"):
                        captured_text = (state2.get("text") or "").strip()
                        print(f"  [{req_id}] ✅ 延迟捕获 "
                              f"{len(captured_text)} 字符")
                        break

                # 进度日志
                if tick > 0 and tick % 20 == 0:
                    print(
                        f"  [{req_id}] ⏳ tick={tick} "
                        f"len={len(text)} "
                        f"gen={is_generating} "
                        f"btn={has_button} "
                        f"items={item_count}"
                    )

                # 超时
                if tick > 120 and not text:
                    print(f"  [{req_id}] ❌ 60秒无回复")
                    break

            # 输出结果
            if captured_text:
                yield captured_text
                print(f"  [{req_id}] 📊 页面#{cp.page_id} "
                      f"完成: {len(captured_text)} 字符")
            else:
                # 最后一搏：直接读 DOM
                fallback = await cp.read_response_instant()
                if fallback:
                    yield fallback
                    print(f"  [{req_id}] 📋 兜底: {len(fallback)} 字符")
                else:
                    yield "抱歉，未能获取到响应。请稍后重试。"
                    print(f"  [{req_id}] ❌ 完全无响应")

        except Exception as e:
            print(f"  [{req_id}] ❌ {e}")
            import traceback
            traceback.print_exc()
            yield f"[错误] {str(e)}"

        finally:
            if cp:
                self._release_page(cp)

    async def is_alive(self) -> bool:
        if not self._ready or not self._pages:
            return False
        for cp in self._pages:
            if await cp.is_alive():
                return True
        return False

    async def get_status(self) -> dict:
        alive_count = 0
        busy_count = 0
        for cp in self._pages:
            if await cp.is_alive():
                alive_count += 1
            if cp.busy:
                busy_count += 1

        return {
            "browser_alive": alive_count > 0,
            "logged_in": self.logged_in,
            "ready": self._ready,
            "engine": self._engine,
            "mode": "multi-page-instant-capture",
            "has_token": True,
            "cookie_count": 0,
            "page_count": len(self._pages),
            "pages_alive": alive_count,
            "pages_busy": busy_count,
            "pages_idle": alive_count - busy_count,
            "uptime_seconds": time.time() - self.start_time,
            "heartbeat_count": self.heartbeat_count,
            "requests_handled": self.requests_handled,
            "total_requests": self.total_requests,
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        for cp in self._pages:
            try:
                if not cp.page.is_closed():
                    buf = await cp.page.screenshot(full_page=False)
                    return base64.b64encode(buf).decode("utf-8")
            except Exception:
                continue
        return None

    async def simulate_activity(self):
        self.heartbeat_count += 1
        for cp in self._pages:
            try:
                if not cp.page.is_closed() and not cp.busy:
                    import random
                    await cp.page.mouse.move(
                        random.randint(100, 1800),
                        random.randint(100, 900),
                    )
                    await cp.page.evaluate("""
                        () => {
                            document.dispatchEvent(new MouseEvent(
                                'mousemove', {
                                    clientX: Math.random() * window.innerWidth,
                                    clientY: Math.random() * window.innerHeight
                                }
                            ));
                            window.scrollBy(0,
                                Math.random() > 0.5 ? 1 : -1);
                        }
                    """)
            except Exception:
                pass
        if self.heartbeat_count % 10 == 0:
            alive = sum(
                1 for cp in self._pages if not cp.page.is_closed()
            )
            busy = sum(1 for cp in self._pages if cp.busy)
            print(f"💓 心跳 #{self.heartbeat_count} "
                  f"({alive}存活/{busy}忙碌)")

    async def shutdown(self):
        try:
            self._save_camoufox_cache()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 已关闭")
        except Exception as e:
            print(f"⚠️ {e}")
