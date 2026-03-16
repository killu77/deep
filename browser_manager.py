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

CENSORSHIP_PHRASES = [
    "这个问题我暂时无法回答",
    "让我们换个话题再聊聊吧",
    "我无法回答这个问题",
    "抱歉，我无法",
    "这个话题不太适合讨论",
    "我没法对此进行回答",
    "作为AI助手，我无法",
    "你好，这个问题我暂时无法回答，让我们换个话题再聊聊吧",
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


# ============================================================
# 注入到页面的 JS：拦截 DOM 变化，保护原始文本不被审查替换
# ============================================================
INJECTED_MONITOR_SCRIPT = """
(() => {
    if (window.__dsMonitorInstalled) return;
    window.__dsMonitorInstalled = true;

    // 存储最佳文本（最长的非审查文本）
    window.__dsState = {
        bestText: '',
        currentText: '',
        isGenerating: false,
        hasButton: false,
        itemCount: 0,
        lastUpdateTime: 0,
        generationStarted: false,
        finished: false,
        chunks: [],         // 增量文本队列
        lastYieldedLen: 0,  // 已推送的长度
    };

    const CENSOR_PHRASES = %CENSOR_PHRASES%;

    function isCensored(text) {
        if (!text || text.length > 150) return false;
        return CENSOR_PHRASES.some(p => text.includes(p));
    }

    function readReplyText() {
        const items = document.querySelectorAll('div[data-virtual-list-item-key]');
        if (!items.length) return '';
        const lastItem = items[items.length - 1];

        const allMd = lastItem.querySelectorAll('[class*="ds-markdown"]');
        if (!allMd.length) return '';

        const thinkSelectors = [
            '[class*="think"]', '[class*="Think"]', '[class*="thought"]',
            '[class*="reasoning"]', '[class*="_74c0879"]',
            '[class*="collapse"]', '[class*="Collapse"]', 'details',
        ];
        const thinkContainers = [];
        thinkSelectors.forEach(sel => {
            lastItem.querySelectorAll(sel).forEach(el => thinkContainers.push(el));
        });

        const replyMds = [];
        for (const md of allMd) {
            let inside = false;
            for (const tc of thinkContainers) {
                if (tc.contains(md) && tc !== md) { inside = true; break; }
            }
            if (!inside) replyMds.push(md);
        }

        if (replyMds.length > 0) {
            return replyMds[replyMds.length - 1].innerText || '';
        }
        return allMd[allMd.length - 1].innerText || '';
    }

    function checkState() {
        const items = document.querySelectorAll('div[data-virtual-list-item-key]');
        const itemCount = items.length;
        const stopBtn = document.querySelector('[class*="stop"], [class*="square"]');
        const isGenerating = !!stopBtn && stopBtn.offsetParent !== null;

        let hasButton = false;
        if (items.length > 0) {
            const lastItem = items[items.length - 1];
            hasButton = lastItem.querySelectorAll('div[role="button"]').length > 0;
        }

        const text = readReplyText().trim();

        const s = window.__dsState;
        s.isGenerating = isGenerating;
        s.hasButton = hasButton;
        s.itemCount = itemCount;
        s.currentText = text;

        if (isGenerating && !s.generationStarted) {
            s.generationStarted = true;
        }

        // 审查检测：如果之前有长文本，突然变短且是审查文本
        if (text && s.bestText.length > 50 &&
            text.length < s.bestText.length * 0.5 && isCensored(text)) {
            // 被审查了，不更新 bestText，标记完成
            s.finished = true;
            // 把 bestText 中未推送的部分加入 chunks
            if (s.lastYieldedLen < s.bestText.length) {
                s.chunks.push(s.bestText.substring(s.lastYieldedLen));
                s.lastYieldedLen = s.bestText.length;
            }
            return;
        }

        // 更新最佳文本
        if (text && text.length >= s.bestText.length) {
            s.bestText = text;
        }

        // 推送增量
        if (text.length > s.lastYieldedLen) {
            // 只在生成中或还没出现按钮时推送（避免推送审查文本）
            if (isGenerating || (!hasButton && s.generationStarted) || !isCensored(text)) {
                const newPart = text.substring(s.lastYieldedLen);
                s.chunks.push(newPart);
                s.lastYieldedLen = text.length;
            }
        }

        s.lastUpdateTime = Date.now();

        // 完成检测
        if (hasButton && !isGenerating && s.generationStarted) {
            if (isCensored(text) && s.bestText.length > text.length * 1.5) {
                // 审查替换，用快照
                if (s.lastYieldedLen < s.bestText.length) {
                    s.chunks.push(s.bestText.substring(s.lastYieldedLen));
                    s.lastYieldedLen = s.bestText.length;
                }
            } else {
                if (text.length > s.lastYieldedLen) {
                    s.chunks.push(text.substring(s.lastYieldedLen));
                    s.lastYieldedLen = text.length;
                }
            }
            s.finished = true;
        }
    }

    // 用 MutationObserver 监听 DOM 变化，比轮询更快
    const observer = new MutationObserver(() => {
        try { checkState(); } catch(e) {}
    });

    observer.observe(document.body, {
        childList: true,
        subtree: true,
        characterData: true,
    });

    // 同时保留一个低频轮询作为兜底
    setInterval(() => {
        try { checkState(); } catch(e) {}
    }, 200);

    // 暴露重置方法（每次新请求前调用）
    window.__dsResetMonitor = () => {
        window.__dsState = {
            bestText: '',
            currentText: '',
            isGenerating: false,
            hasButton: false,
            itemCount: 0,
            lastUpdateTime: 0,
            generationStarted: false,
            finished: false,
            chunks: [],
            lastYieldedLen: 0,
        };
    };

    // 暴露获取增量的方法（Python 调用后清空队列）
    window.__dsFlushChunks = () => {
        const s = window.__dsState;
        const data = {
            chunks: s.chunks.splice(0),  // 取出并清空
            finished: s.finished,
            isGenerating: s.isGenerating,
            hasButton: s.hasButton,
            bestLen: s.bestText.length,
            currentLen: s.currentText.length,
            yieldedLen: s.lastYieldedLen,
            itemCount: s.itemCount,
            generationStarted: s.generationStarted,
        };
        return data;
    };

    // 暴露获取完整最佳文本的方法
    window.__dsGetBestText = () => {
        return window.__dsState.bestText;
    };
})();
""".replace('%CENSOR_PHRASES%', json.dumps(CENSORSHIP_PHRASES, ensure_ascii=False))


class ChatPage:
    def __init__(self, page, page_id: int):
        self.page = page
        self.page_id = page_id
        self.busy = False
        self.request_count = 0
        self.last_used = 0.0
        self._monitor_injected = False

    async def ensure_monitor(self):
        """确保监控脚本已注入"""
        if self._monitor_injected:
            # 验证脚本是否还在（页面可能刷新过）
            try:
                installed = await self.page.evaluate("() => !!window.__dsMonitorInstalled")
                if installed:
                    return
            except Exception:
                pass
        try:
            await self.page.evaluate(INJECTED_MONITOR_SCRIPT)
            self._monitor_injected = True
        except Exception as e:
            print(f"  ⚠️ 监控脚本注入失败: {e}")

    async def reset_monitor(self):
        """重置监控状态（新请求前调用）"""
        try:
            await self.page.evaluate("() => { if(window.__dsResetMonitor) window.__dsResetMonitor(); }")
        except Exception:
            pass

    async def flush_chunks(self) -> dict:
        """获取增量文本块"""
        try:
            return await self.page.evaluate("() => window.__dsFlushChunks ? window.__dsFlushChunks() : null")
        except Exception:
            return None

    async def get_best_text(self) -> str:
        """获取最佳快照文本"""
        try:
            text = await self.page.evaluate("() => window.__dsGetBestText ? window.__dsGetBestText() : ''")
            return (text or "").strip()
        except Exception:
            return ""

    async def start_new_chat(self):
        if "chat.deepseek.com" not in self.page.url:
            await self.page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(2)
            self._monitor_injected = False
            return

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
        self._monitor_injected = False

    async def get_conversation_item_count(self) -> int:
        try:
            return await self.page.evaluate("""
                () => document.querySelectorAll('div[data-virtual-list-item-key]').length
            """)
        except Exception:
            return 0

    async def type_and_send(self, message: str):
        textarea = self.page.locator(
            "textarea[placeholder*='DeepSeek'], "
            "textarea[placeholder*='发送消息'], "
            "textarea, "
            "[contenteditable='true']"
        ).first
        await textarea.wait_for(state="visible", timeout=10000)
        await textarea.click()
        await asyncio.sleep(0.2)

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

        await asyncio.sleep(0.3)
        await textarea.press("Enter")
        await asyncio.sleep(0.5)

    async def read_response_text(self) -> str:
        try:
            text = await self.page.evaluate("""
                () => {
                    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
                    if (!items.length) return '';
                    const lastItem = items[items.length - 1];
                    const allMd = lastItem.querySelectorAll('[class*="ds-markdown"]');
                    if (!allMd.length) return '';

                    const thinkSelectors = [
                        '[class*="think"]','[class*="Think"]','[class*="thought"]',
                        '[class*="reasoning"]','[class*="_74c0879"]',
                        '[class*="collapse"]','[class*="Collapse"]','details',
                    ];
                    const thinkContainers = [];
                    thinkSelectors.forEach(sel => {
                        lastItem.querySelectorAll(sel).forEach(el => thinkContainers.push(el));
                    });
                    const replyMds = [];
                    for (const md of allMd) {
                        let inside = false;
                        for (const tc of thinkContainers) {
                            if (tc.contains(md) && tc !== md) { inside = true; break; }
                        }
                        if (!inside) replyMds.push(md);
                    }
                    if (replyMds.length > 0) return replyMds[replyMds.length - 1].innerText || '';
                    return allMd[allMd.length - 1].innerText || '';
                }
            """)
            return (text or "").strip()
        except Exception:
            return ""

    async def check_generation_state(self) -> dict:
        return await self.page.evaluate("""
            () => {
                const items = document.querySelectorAll('div[data-virtual-list-item-key]');
                if (!items.length) return { hasButton:false, isGenerating:false, text:'', itemCount:0 };
                const lastItem = items[items.length - 1];
                const allMd = lastItem.querySelectorAll('[class*="ds-markdown"]');
                let text = '';
                if (allMd.length > 0) {
                    const thinkSelectors = [
                        '[class*="think"]','[class*="Think"]','[class*="thought"]',
                        '[class*="reasoning"]','[class*="_74c0879"]',
                        '[class*="collapse"]','[class*="Collapse"]','details',
                    ];
                    const thinkContainers = [];
                    thinkSelectors.forEach(sel => {
                        lastItem.querySelectorAll(sel).forEach(el => thinkContainers.push(el));
                    });
                    const replyMds = [];
                    for (const md of allMd) {
                        let inside = false;
                        for (const tc of thinkContainers) {
                            if (tc.contains(md) && tc !== md) { inside = true; break; }
                        }
                        if (!inside) replyMds.push(md);
                    }
                    if (replyMds.length > 0) text = replyMds[replyMds.length - 1].innerText || '';
                    else text = allMd[allMd.length - 1].innerText || '';
                }
                const buttons = lastItem.querySelectorAll('div[role="button"]');
                const hasButton = buttons.length > 0;
                const stopBtn = document.querySelector('[class*="stop"], [class*="square"]');
                const isGenerating = !!stopBtn && stopBtn.offsetParent !== null;
                return { hasButton, isGenerating, text, itemCount: items.length };
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

            # 检查页面是否存活
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
                    cp._monitor_injected = False
                except Exception as e:
                    yield f"[错误] 页面恢复失败: {e}"
                    return

            # 开新对话
            await cp.start_new_chat()
            await asyncio.sleep(0.5)

            # 注入监控脚本 & 重置状态
            await cp.ensure_monitor()
            await cp.reset_monitor()

            # 记录基线
            baseline_item_count = await cp.get_conversation_item_count()

            # 发送消息
            await cp.type_and_send(message)
            print(f"  [{req_id}] 消息已发送，等待回复...")

            # ═══════════════════════════════════════════════════
            # 高性能流式读取：
            # 1. JS 端 MutationObserver 实时追踪 DOM 变化
            # 2. Python 端高频 flush 增量文本
            # 3. 审查检测在 JS 端完成，确保快照不丢失
            # ═══════════════════════════════════════════════════

            max_wait = 600
            poll_interval = 0.1  # 100ms 高频轮询，配合 JS 端 MutationObserver
            total_yielded = 0
            finished = False
            new_response_seen = False
            gen_started = False
            no_data_ticks = 0
            btn_stable_ticks = 0
            start_time = time.time()

            while (time.time() - start_time) < max_wait:
                await asyncio.sleep(poll_interval)

                # 从 JS 端获取增量数据
                data = await cp.flush_chunks()

                if data is None:
                    # 监控脚本可能丢失，重新注入
                    await cp.ensure_monitor()
                    no_data_ticks += 1
                    if no_data_ticks > 100:  # 10秒无数据
                        # fallback 到传统读取
                        print(f"  [{req_id}] ⚠️ 监控脚本无响应，切换传统模式")
                        async for chunk in self._fallback_stream(cp, message, req_id, baseline_item_count):
                            yield chunk
                        finished = True
                        break
                    continue

                no_data_ticks = 0
                chunks = data.get("chunks", [])
                is_finished = data.get("finished", False)
                is_generating = data.get("isGenerating", False)
                has_button = data.get("hasButton", False)
                best_len = data.get("bestLen", 0)
                current_len = data.get("currentLen", 0)
                item_count = data.get("itemCount", 0)
                gen_started_js = data.get("generationStarted", False)

                # 检测新回复
                if not new_response_seen:
                    if item_count > baseline_item_count or is_generating or gen_started_js:
                        new_response_seen = True
                        print(f"  [{req_id}] 📬 新回复出现")
                    else:
                        elapsed = time.time() - start_time
                        if elapsed > 30 and not is_generating:
                            print(f"  [{req_id}] ⚠️ 30秒未见回复，重试发送")
                            await cp.reset_monitor()
                            await cp.type_and_send(message)
                            baseline_item_count = item_count
                        if elapsed > 60:
                            print(f"  [{req_id}] ❌ 60秒超时无回复")
                            break
                        continue

                if gen_started_js and not gen_started:
                    gen_started = True
                    print(f"  [{req_id}] 🚀 生成开始")

                # 输出增量
                for chunk in chunks:
                    if chunk:
                        yield chunk
                        total_yielded += len(chunk)

                # 完成检测
                if is_finished:
                    print(f"  [{req_id}] ✅ JS端检测完成 "
                          f"(yielded={total_yielded}, best={best_len})")
                    finished = True
                    break

                # 按钮稳定检测（JS 端没捕获到完成的兜底）
                if has_button and not is_generating and new_response_seen:
                    if current_len > 0:
                        btn_stable_ticks += 1
                        if btn_stable_ticks >= 20:  # 2秒稳定
                            # 确保文本已经全部输出
                            if current_len > total_yielded:
                                # 可能有遗漏，直接读取
                                full_text = await cp.get_best_text()
                                if len(full_text) > total_yielded:
                                    remaining = full_text[total_yielded:]
                                    yield remaining
                                    total_yielded += len(remaining)
                            print(f"  [{req_id}] ✅ 按钮稳定完成 "
                                  f"(yielded={total_yielded})")
                            finished = True
                            break
                    else:
                        btn_stable_ticks += 1
                        if btn_stable_ticks >= 50:  # 5秒有按钮但无文本
                            print(f"  [{req_id}] ⚠️ 有按钮但无文本，尝试直接读取")
                            fallback_text = await cp.read_response_text()
                            if fallback_text:
                                yield fallback_text
                                total_yielded += len(fallback_text)
                            finished = True
                            break
                else:
                    btn_stable_ticks = 0

                # 进度日志
                elapsed = time.time() - start_time
                if int(elapsed) % 20 == 0 and int(elapsed) > 0 and int(elapsed * 10) % 200 < 2:
                    print(f"  [{req_id}] ⏳ {elapsed:.0f}s "
                          f"yielded={total_yielded} best={best_len} "
                          f"gen={is_generating} btn={has_button}")

            # 兜底
            if not finished:
                best_text = await cp.get_best_text()
                if best_text and len(best_text) > total_yielded:
                    remaining = best_text[total_yielded:]
                    yield remaining
                    total_yielded += len(remaining)
                    print(f"  [{req_id}] 📋 兜底快照: {len(best_text)} 字符")
                elif total_yielded == 0:
                    fallback = await cp.read_response_text()
                    if fallback:
                        yield fallback
                        print(f"  [{req_id}] 📋 兜底读取: {len(fallback)} 字符")
                    else:
                        yield "抱歉，未能获取到响应。请稍后重试。"
                        print(f"  [{req_id}] ❌ 完全无响应")

            print(f"  [{req_id}] 📊 页面#{cp.page_id} 完成 "
                  f"(总输出: {total_yielded} 字符, "
                  f"耗时: {time.time()-start_time:.1f}s)")

        except Exception as e:
            print(f"  [{req_id}] ❌ {e}")
            import traceback
            traceback.print_exc()
            yield f"[错误] {str(e)}"

        finally:
            if cp:
                self._release_page(cp)

    async def _fallback_stream(
        self, cp: ChatPage, message: str, req_id: int,
        baseline_item_count: int
    ) -> AsyncGenerator[str, None]:
        """传统轮询模式作为兜底"""
        poll_interval = 0.2
        max_wait = 600
        last_text = ""
        best_snapshot = ""
        yielded_length = 0
        generation_started = False
        new_response_seen = False
        idle_ticks = 0
        btn_stable_ticks = 0
        start_time = time.time()

        while (time.time() - start_time) < max_wait:
            await asyncio.sleep(poll_interval)

            try:
                state = await cp.check_generation_state()
            except Exception:
                continue

            has_button = state.get("hasButton", False)
            is_generating = state.get("isGenerating", False)
            current_text = (state.get("text") or "").strip()
            item_count = state.get("itemCount", 0)

            if not new_response_seen:
                if item_count > baseline_item_count or is_generating:
                    new_response_seen = True
                else:
                    if time.time() - start_time > 60:
                        break
                    continue

            if is_generating and not generation_started:
                generation_started = True

            # 审查检测
            if (current_text and len(best_snapshot) > 50 and
                len(current_text) < len(best_snapshot) * 0.5 and
                _is_censored(current_text)):
                remaining = best_snapshot[yielded_length:]
                if remaining:
                    yield remaining
                return

            if current_text and len(current_text) >= len(best_snapshot):
                best_snapshot = current_text

            if current_text and len(current_text) > yielded_length:
                if is_generating or (not has_button and generation_started):
                    new_content = current_text[yielded_length:]
                    yield new_content
                    yielded_length = len(current_text)
                    idle_ticks = 0
                    btn_stable_ticks = 0

            if has_button and not is_generating:
                if generation_started or (new_response_seen and current_text):
                    btn_stable_ticks += 1
                    if btn_stable_ticks >= 10:
                        if current_text and len(current_text) > yielded_length:
                            yield current_text[yielded_length:]
                        return
            else:
                btn_stable_ticks = 0

            if current_text == last_text:
                idle_ticks += 1
            else:
                idle_ticks = 0
                last_text = current_text

            if (idle_ticks > 150 and not is_generating and
                (generation_started or new_response_seen)):
                if current_text and len(current_text) > yielded_length:
                    yield current_text[yielded_length:]
                return

        # 兜底
        if best_snapshot and yielded_length < len(best_snapshot):
            yield best_snapshot[yielded_length:]
        elif yielded_length == 0:
            fallback = await cp.read_response_text()
            if fallback:
                yield fallback

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
            "mode": "observer-stream-anti-censorship",
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
                            window.scrollBy(0, Math.random() > 0.5 ? 1 : -1);
                        }
                    """)
            except Exception:
                pass
        if self.heartbeat_count % 10 == 0:
            alive = sum(1 for cp in self._pages if not cp.page.is_closed())
            busy = sum(1 for cp in self._pages if cp.busy)
            print(f"💓 心跳 #{self.heartbeat_count} ({alive}存活/{busy}忙碌)")

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
