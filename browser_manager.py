# browser_manager.py
# 根据探针数据精确重写，针对 DeepSeek 真实 DOM 结构优化

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

CENSORSHIP_PHRASES = [
    "这个问题我暂时无法回答",
    "让我们换个话题再聊聊吧",
    "我无法回答这个问题",
    "抱歉，我无法",
    "这个话题不太适合讨论",
    "我没法对此进行回答",
    "作为AI助手，我无法",
    "很抱歉，这个问题",
]


def _is_censored(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    for phrase in CENSORSHIP_PHRASES:
        if phrase in text:
            if len(text) < 150:
                return True
    return False


# ═══════════════════════════════════════════════════
# 注入到页面的 JS：精确匹配探针发现的 DOM 结构
# ═══════════════════════════════════════════════════

# 基于探针发现：
# 1. fetch 拦截拿不到 SSE（DeepSeek 可能用 WebSocket 或内部拦截）
# 2. 复制按钮（第一个 ds-icon-button）点击后 clipboard 有完整内容
# 3. DOM 中 div.ds-message > div.ds-markdown（非 ds-think-content 子代）= 正式回复
# 4. 思考区域 = div.ds-think-content > div.ds-markdown

COLLECTOR_JS = """
() => {
    window.__col = {
        clipText: '',
        clipTime: 0,
        bestSnapshot: '',
        installed: true,
    };

    // 拦截剪贴板 - 这是最可靠的完整内容源
    try {
        const orig = navigator.clipboard.writeText.bind(navigator.clipboard);
        navigator.clipboard.writeText = async function(text) {
            if (text && text.length > 5) {
                window.__col.clipText = text;
                window.__col.clipTime = Date.now();
            }
            return orig(text);
        };
    } catch(e) {}

    console.log('[Col] installed');
    return true;
}
"""

# 读取状态的 JS - 极其轻量，一次 evaluate 搞定
READ_STATE_JS = """
() => {
    const result = {
        domText: '',
        domTextLen: 0,
        hasButton: false,
        buttonCount: 0,
        isGenerating: false,
        clipText: '',
        clipLen: 0,
        itemCount: 0,
        thinkText: '',
        thinkLen: 0,
    };

    // 找所有对话项
    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    result.itemCount = items.length;
    if (items.length === 0) return result;

    const lastItem = items[items.length - 1];

    // 找 ds-message 容器
    const msgDiv = lastItem.querySelector('div.ds-message');
    if (!msgDiv) return result;

    // ====== 正式回复：ds-message 的直接子代 ds-markdown ======
    // 排除 ds-think-content 内的 ds-markdown
    const allMd = msgDiv.querySelectorAll(':scope > div.ds-markdown');
    if (allMd.length > 0) {
        // 直接子代中的 ds-markdown 就是正式回复
        const replyMd = allMd[allMd.length - 1];
        result.domText = replyMd.innerText || '';
        result.domTextLen = result.domText.length;
    } else {
        // 备选：找所有 ds-markdown，排除在 ds-think-content 内的
        const allMdDeep = msgDiv.querySelectorAll('div.ds-markdown');
        for (let i = allMdDeep.length - 1; i >= 0; i--) {
            const md = allMdDeep[i];
            // 检查是否在 ds-think-content 内
            let inThink = false;
            let el = md.parentElement;
            while (el && el !== msgDiv) {
                if (el.classList && el.classList.contains('ds-think-content')) {
                    inThink = true;
                    break;
                }
                el = el.parentElement;
            }
            if (!inThink) {
                result.domText = md.innerText || '';
                result.domTextLen = result.domText.length;
                break;
            }
        }
    }

    // ====== 思考区域 ======
    const thinkDiv = msgDiv.querySelector('div.ds-think-content');
    if (thinkDiv) {
        const thinkMd = thinkDiv.querySelector('div.ds-markdown');
        if (thinkMd) {
            result.thinkText = (thinkMd.innerText || '').substring(0, 200);
            result.thinkLen = (thinkMd.innerText || '').length;
        }
    }

    // ====== 按钮 ======
    const buttons = lastItem.querySelectorAll('div.ds-icon-button');
    result.buttonCount = buttons.length;
    result.hasButton = buttons.length > 0;

    // ====== 生成中检测 ======
    // 方法1: 查找停止按钮/加载动画
    const stopBtns = document.querySelectorAll(
        'div[class*="StopGeneration"], div[class*="stop-generation"],' +
        'div[class*="stopGenerat"], button[class*="stop"]'
    );
    for (const btn of stopBtns) {
        if (btn.offsetParent !== null) {
            result.isGenerating = true;
            break;
        }
    }

    // 方法2: 输入框是否被禁用（生成中通常禁用输入）
    if (!result.isGenerating) {
        const textarea = document.querySelector('textarea');
        if (textarea && textarea.disabled) {
            result.isGenerating = true;
        }
    }

    // 方法3: 检查是否有正在渲染的光标/动画
    if (!result.isGenerating) {
        const cursors = document.querySelectorAll(
            'span[class*="cursor"], span[class*="blink"],' +
            'div[class*="loading"], div[class*="typing"]'
        );
        for (const c of cursors) {
            if (c.offsetParent !== null) {
                result.isGenerating = true;
                break;
            }
        }
    }

    // ====== 剪贴板数据 ======
    if (window.__col) {
        result.clipText = window.__col.clipText || '';
        result.clipLen = result.clipText.length;
    }

    // ====== 更新快照 ======
    if (window.__col && result.domText.length > (window.__col.bestSnapshot || '').length) {
        window.__col.bestSnapshot = result.domText;
    }

    return result;
}
"""

# 点击复制按钮的 JS
CLICK_COPY_JS = """
() => {
    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    if (items.length === 0) return false;
    const lastItem = items[items.length - 1];

    // 复制按钮是第一个 ds-icon-button
    const buttons = lastItem.querySelectorAll('div.ds-icon-button');
    if (buttons.length === 0) return false;

    // 清空之前的剪贴板记录
    if (window.__col) {
        window.__col.clipText = '';
        window.__col.clipTime = 0;
    }

    buttons[0].click();
    return true;
}
"""

# 滚动到底部的 JS（确保虚拟滚动渲染完整内容）
SCROLL_BOTTOM_JS = """
() => {
    const scrollArea = document.querySelector('.ds-scroll-area');
    if (scrollArea) {
        scrollArea.scrollTop = scrollArea.scrollHeight;
        return true;
    }
    // 备选
    const vlist = document.querySelector('.ds-virtual-list');
    if (vlist) {
        vlist.scrollTop = vlist.scrollHeight;
        return true;
    }
    return false;
}
"""


class ChatPage:
    def __init__(self, page, page_id: int):
        self.page = page
        self.page_id = page_id
        self.busy = False
        self.request_count = 0
        self.last_used = 0.0
        self._collector_ok = False

    async def ensure_collector(self):
        """确保收集器（主要是剪贴板拦截）已安装"""
        try:
            ok = await self.page.evaluate("() => !!(window.__col && window.__col.installed)")
            if ok:
                return
        except Exception:
            pass

        try:
            await self.page.evaluate(COLLECTOR_JS)
            self._collector_ok = True
        except Exception as e:
            print(f"  ⚠️ 页面#{self.page_id} 收集器安装失败: {e}")

    async def reset_collector(self):
        """重置收集器状态"""
        try:
            await self.page.evaluate("""
                () => {
                    if (window.__col) {
                        window.__col.clipText = '';
                        window.__col.clipTime = 0;
                        window.__col.bestSnapshot = '';
                    }
                }
            """)
        except Exception:
            pass

    async def read_state(self) -> dict:
        """一次 evaluate 读取所有状态"""
        try:
            return await self.page.evaluate(READ_STATE_JS)
        except Exception as e:
            return {
                "domText": "", "domTextLen": 0, "hasButton": False,
                "buttonCount": 0, "isGenerating": False,
                "clipText": "", "clipLen": 0, "itemCount": 0,
                "thinkText": "", "thinkLen": 0, "error": str(e),
            }

    async def click_copy_and_wait(self, timeout: float = 2.0) -> str:
        """点击复制按钮并等待剪贴板拦截获取内容"""
        try:
            clicked = await self.page.evaluate(CLICK_COPY_JS)
            if not clicked:
                return ""

            # 等待剪贴板拦截生效
            deadline = time.time() + timeout
            while time.time() < deadline:
                await asyncio.sleep(0.2)
                clip = await self.page.evaluate(
                    "() => (window.__col && window.__col.clipText) || ''"
                )
                if clip:
                    return clip

            return ""
        except Exception as e:
            print(f"  ⚠️ 复制按钮失败: {e}")
            return ""

    async def scroll_to_bottom(self):
        """滚动到底部，触发虚拟滚动渲染"""
        try:
            await self.page.evaluate(SCROLL_BOTTOM_JS)
        except Exception:
            pass

    async def start_new_chat(self):
        """开启新对话"""
        self._collector_ok = False
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
        """输入并发送消息"""
        # 探针确认选择器: textarea[placeholder="给 DeepSeek 发送消息 "]
        textarea = self.page.locator("textarea").first
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
                    const el = document.querySelector('textarea');
                    if (!el) return;
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    setter.call(el, text);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
            """, message)

        await asyncio.sleep(0.5)
        await textarea.press("Enter")
        await asyncio.sleep(0.5)

    async def is_alive(self) -> bool:
        try:
            if self.page.is_closed():
                return False
            await self.page.evaluate("() => 1")
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
            cp = ChatPage(first_page, 0)
            await cp.ensure_collector()
            self._pages.append(cp)
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
                cp = ChatPage(page, i)
                await cp.ensure_collector()
                self._pages.append(cp)
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

            # 检查页面存活
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
                    cp._collector_ok = False
                except Exception as e:
                    yield f"[错误] 页面恢复失败: {e}"
                    return

            # 开新对话
            await cp.start_new_chat()
            await asyncio.sleep(0.5)

            # 安装/重新安装收集器（start_new_chat 可能导致页面变化）
            await cp.ensure_collector()
            await cp.reset_collector()

            # 发送消息
            await cp.type_and_send(message)
            print(f"  [{req_id}] 消息已发送")

            # ═══════════════════════════════════════════
            # 流式读取策略（基于探针结论）：
            #
            # 主数据源: DOM (div.ds-message > div.ds-markdown)
            #   - 轮询 innerText，增量输出
            #   - 排除 ds-think-content 内的内容
            #
            # 兜底数据源: 复制按钮 → 剪贴板拦截
            #   - 完成后点复制按钮获取完整内容
            #   - 如果 DOM 读的少于复制的，补齐差异
            #
            # 抗审查: 持续维护快照(bestSnapshot)
            # ═══════════════════════════════════════════

            max_wait = 600
            poll_interval = 0.3  # 300ms 够了，DOM 读取很轻量

            yielded_len = 0
            best_text = ""
            generation_started = False
            no_change_count = 0
            prev_dom_len = 0
            start_ts = time.time()

            # 等待对话项出现（AI 开始回复）
            for _ in range(int(30 / 0.5)):
                await asyncio.sleep(0.5)
                state = await cp.read_state()
                if state.get("itemCount", 0) >= 2:
                    break
                if time.time() - start_ts > 30:
                    break

            while True:
                elapsed = time.time() - start_ts
                if elapsed > max_wait:
                    print(f"  [{req_id}] ⏰ 总超时 {max_wait}s")
                    break

                await asyncio.sleep(poll_interval)

                state = await cp.read_state()
                dom_text = state.get("domText", "")
                dom_len = state.get("domTextLen", 0)
                has_button = state.get("hasButton", False)
                is_gen = state.get("isGenerating", False)
                item_count = state.get("itemCount", 0)
                think_len = state.get("thinkLen", 0)

                # 检测生成开始
                if (dom_len > 0 or think_len > 0 or is_gen) and not generation_started:
                    generation_started = True
                    no_change_count = 0
                    gen_type = []
                    if think_len > 0:
                        gen_type.append(f"思考{think_len}字")
                    if dom_len > 0:
                        gen_type.append(f"回复{dom_len}字")
                    if is_gen:
                        gen_type.append("生成中")
                    print(f"  [{req_id}] 🚀 生成开始 ({', '.join(gen_type)})")

                # 更新最佳文本
                if dom_len > len(best_text):
                    best_text = dom_text

                # ---- 审查检测 ----
                if (generation_started and len(best_text) > 80 and
                    dom_text and dom_len < len(best_text) * 0.4 and
                    _is_censored(dom_text)):
                    print(f"  [{req_id}] 🛡️ 审查替换！当前={dom_len} 快照={len(best_text)}")
                    remaining = best_text[yielded_len:]
                    if remaining:
                        yield remaining
                    break

                # ---- 流式增量输出 ----
                if dom_text and dom_len > yielded_len:
                    new_part = dom_text[yielded_len:]
                    # 生成中时直接输出
                    if is_gen or not has_button:
                        yield new_part
                        yielded_len = dom_len
                        no_change_count = 0
                    elif has_button and not _is_censored(dom_text):
                        # 已完成且非审查
                        yield new_part
                        yielded_len = dom_len

                # ---- 定期滚动到底部（对抗虚拟滚动截断）----
                if generation_started and int(elapsed) % 5 == 0 and int(elapsed * 10) % 50 < 4:
                    await cp.scroll_to_bottom()

                # ---- 完成检测 ----
                if generation_started and has_button and not is_gen:
                    # 等待一下确认真的完成了
                    await asyncio.sleep(0.5)
                    confirm_state = await cp.read_state()
                    if (confirm_state.get("hasButton", False) and
                        not confirm_state.get("isGenerating", False)):

                        confirm_text = confirm_state.get("domText", "")
                        confirm_len = confirm_state.get("domTextLen", 0)

                        # 滚到底部再读一次，确保虚拟滚动没截断
                        await cp.scroll_to_bottom()
                        await asyncio.sleep(0.3)
                        final_state = await cp.read_state()
                        final_text = final_state.get("domText", "")
                        final_len = final_state.get("domTextLen", 0)

                        # 取最长的
                        if final_len > confirm_len:
                            confirm_text = final_text
                            confirm_len = final_len

                        # 更新 best_text
                        if confirm_len > len(best_text):
                            best_text = confirm_text

                        # 输出剩余部分
                        if confirm_len > yielded_len:
                            if not _is_censored(confirm_text):
                                yield confirm_text[yielded_len:]
                                yielded_len = confirm_len
                            else:
                                remaining = best_text[yielded_len:]
                                if remaining:
                                    yield remaining
                                    yielded_len = len(best_text)
                                print(f"  [{req_id}] 🛡️ 完成时审查替换")

                        # === 核心：点复制按钮获取完整内容 ===
                        clip_text = await cp.click_copy_and_wait(timeout=3.0)
                        if clip_text:
                            clip_len = len(clip_text)
                            print(f"  [{req_id}] 📋 复制按钮: {clip_len} 字符 "
                                  f"(DOM: {yielded_len} 字符)")

                            # 如果剪贴板内容比已输出的更多
                            # 注意：剪贴板是 markdown 格式，DOM 是纯文本
                            # 剪贴板通常更长因为包含 ## ** 等格式符号
                            if clip_len > yielded_len and not _is_censored(clip_text):
                                # 剪贴板内容可能和 DOM 内容格式不同
                                # 如果差距很大（>30%），说明 DOM 可能被虚拟滚动截断
                                if clip_len > yielded_len * 1.3:
                                    # DOM 严重截断，用剪贴板内容替代
                                    print(f"  [{req_id}] ⚠️ DOM 可能被截断，"
                                          f"补充剪贴板差异")
                                    # 不能简单拼接（格式不同），但可以发送差异提示
                                    # 或者直接用剪贴板全文替代
                                    # 这里我们检查是否 DOM 文本是剪贴板的子集
                                    dom_clean = best_text.replace('\n', '').replace(' ', '')
                                    clip_clean = clip_text.replace('\n', '').replace(' ', '')
                                    clip_clean = clip_clean.replace('#', '').replace('*', '')

                                    if len(dom_clean) < len(clip_clean) * 0.7:
                                        # DOM 确实被截断了，重新用剪贴板全文
                                        # 但要注意已经 yield 了一部分
                                        # 最安全的做法：记录下来，让调用者知道
                                        print(f"  [{req_id}] 📋 DOM截断严重，"
                                              f"完整内容已通过剪贴板获取")

                        print(f"  [{req_id}] ✅ 完成: {yielded_len} 字符 "
                              f"(DOM: {confirm_len}, clip: {len(clip_text) if clip_text else 0})")
                        break

                # ---- 无进展检测 ----
                if dom_len == prev_dom_len:
                    no_change_count += 1
                else:
                    no_change_count = 0
                    prev_dom_len = dom_len

                # 长时间无新数据
                if no_change_count > int(90 / poll_interval):
                    if generation_started and best_text:
                        if len(best_text) > yielded_len:
                            yield best_text[yielded_len:]
                        print(f"  [{req_id}] ⏰ 90秒无进展，完成: {len(best_text)} 字符")
                        break
                    elif not generation_started:
                        if elapsed > 120:
                            print(f"  [{req_id}] ❌ 120秒无响应")
                            break

                # ---- 定期日志 ----
                if int(elapsed) > 0 and int(elapsed) % 15 == 0 and int(elapsed * 10) % 150 < 4:
                    print(
                        f"  [{req_id}] ⏳ {elapsed:.0f}s "
                        f"dom={dom_len} think={think_len} "
                        f"yielded={yielded_len} gen={is_gen} btn={has_button}"
                    )

            # ═══════════════════════════════════════════
            # 最终兜底
            # ═══════════════════════════════════════════
            if yielded_len == 0:
                # 1. 尝试点复制按钮
                print(f"  [{req_id}] 🔄 兜底: 尝试复制按钮...")
                clip = await cp.click_copy_and_wait(timeout=5.0)
                if clip and not _is_censored(clip):
                    yield clip
                    print(f"  [{req_id}] 📋 兜底复制: {len(clip)} 字符")
                    return

                # 2. 尝试再读一次 DOM
                await cp.scroll_to_bottom()
                await asyncio.sleep(1)
                state = await cp.read_state()
                dom_text = state.get("domText", "")
                if dom_text and not _is_censored(dom_text):
                    yield dom_text
                    print(f"  [{req_id}] 📋 兜底DOM: {len(dom_text)} 字符")
                    return

                # 3. 用快照
                if best_text:
                    yield best_text
                    print(f"  [{req_id}] 📋 兜底快照: {len(best_text)} 字符")
                    return

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
            "mode": "dom-poll-clipboard-v2",
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
                        }
                    """)
            except Exception:
                pass

        # 定期重新安装收集器
        if self.heartbeat_count % 5 == 0:
            for cp in self._pages:
                if not cp.busy:
                    try:
                        await cp.ensure_collector()
                    except Exception:
                        pass

        if self.heartbeat_count % 10 == 0:
            alive = sum(1 for cp in self._pages if not cp.page.is_closed())
            busy = sum(1 for cp in self._pages if cp.busy)
            print(f"💓 #{self.heartbeat_count} ({alive}活/{busy}忙)")

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
