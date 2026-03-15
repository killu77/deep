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

# 审查替换的固定提示语列表，检测到这些就说明内容被替换了
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

    async def get_conversation_item_count(self) -> int:
        """获取当前对话项数量"""
        try:
            return await self.page.evaluate("""
                () => {
                    return document.querySelectorAll(
                        'div[data-virtual-list-item-key]'
                    ).length;
                }
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

    async def read_response_text(self) -> str:
        try:
            text = await self.page.evaluate("""
                () => {
                    const items = document.querySelectorAll(
                        'div[data-virtual-list-item-key]'
                    );
                    if (items.length === 0) return '';
                    const lastItem = items[items.length - 1];

                    const allMd = lastItem.querySelectorAll('[class*="ds-markdown"]');
                    if (allMd.length === 0) return '';

                    const thinkContainerSelectors = [
                        '[class*="think"]',
                        '[class*="Think"]',
                        '[class*="thought"]',
                        '[class*="reasoning"]',
                        '[class*="_74c0879"]',
                        '[class*="collapse"]',
                        '[class*="Collapse"]',
                        'details',
                    ];

                    const thinkContainers = [];
                    for (const sel of thinkContainerSelectors) {
                        const els = lastItem.querySelectorAll(sel);
                        els.forEach(el => thinkContainers.push(el));
                    }

                    const replyMds = [];
                    for (const md of allMd) {
                        let insideThink = false;
                        for (const tc of thinkContainers) {
                            if (tc.contains(md) && tc !== md) {
                                insideThink = true;
                                break;
                            }
                        }
                        if (!insideThink) {
                            replyMds.push(md);
                        }
                    }

                    if (replyMds.length === 0) {
                        return allMd[allMd.length - 1].innerText || '';
                    }

                    return replyMds[replyMds.length - 1].innerText || '';
                }
            """)
            return (text or "").strip()
        except Exception:
            return ""

    async def check_generation_state(self) -> dict:
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

                const allMd = lastItem.querySelectorAll('[class*="ds-markdown"]');
                let text = '';

                if (allMd.length > 0) {
                    const thinkContainerSelectors = [
                        '[class*="think"]',
                        '[class*="Think"]',
                        '[class*="thought"]',
                        '[class*="reasoning"]',
                        '[class*="_74c0879"]',
                        '[class*="collapse"]',
                        '[class*="Collapse"]',
                        'details',
                    ];

                    const thinkContainers = [];
                    for (const sel of thinkContainerSelectors) {
                        const els = lastItem.querySelectorAll(sel);
                        els.forEach(el => thinkContainers.push(el));
                    }

                    const replyMds = [];
                    for (const md of allMd) {
                        let insideThink = false;
                        for (const tc of thinkContainers) {
                            if (tc.contains(md) && tc !== md) {
                                insideThink = true;
                                break;
                            }
                        }
                        if (!insideThink) {
                            replyMds.push(md);
                        }
                    }

                    if (replyMds.length > 0) {
                        text = replyMds[replyMds.length - 1].innerText || '';
                    } else {
                        text = allMd[allMd.length - 1].innerText || '';
                    }
                }

                const buttons = lastItem.querySelectorAll(
                    'div[role="button"]'
                );
                const hasButton = buttons.length > 0;

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


def _is_censored(text: str) -> bool:
    """检查文本是否是审查替换后的固定提示语"""
    text = text.strip()
    if not text:
        return False
    for phrase in CENSORSHIP_PHRASES:
        if phrase in text:
            if len(text) < 100:
                return True
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

            # ========== 新对话 & 记录基线 ==========
            await cp.start_new_chat()
            await asyncio.sleep(1)

            # 记录发送前的对话项数量，用于判断新回复是否出现
            baseline_item_count = await cp.get_conversation_item_count()
            print(f"  [{req_id}] 基线对话项数: {baseline_item_count}")

            await cp.type_and_send(message)
            print(f"  [{req_id}] 等待回复...")

            # ═══════════════════════════════════════════════════
            # 核心逻辑：
            # 1. 先等新的对话项出现（排除残留状态的干扰）
            # 2. 流式轮询，持续读取文本并输出增量
            # 3. 每次都保存快照，检测到审查替换时使用上次快照
            # 4. 复制按钮出现后结束
            # ═══════════════════════════════════════════════════

            max_wait_seconds = 600
            poll_interval = 0.3

            last_text = ""
            best_snapshot = ""
            yielded_length = 0
            generation_started = False
            new_response_appeared = False  # 新回复对话项是否已出现
            idle_ticks = 0
            btn_with_text_ticks = 0  # 有按钮+有文本+非生成中 的连续次数
            finished = False

            for tick in range(int(max_wait_seconds / poll_interval)):
                await asyncio.sleep(poll_interval)

                try:
                    state = await cp.check_generation_state()
                except Exception:
                    continue

                has_button = state.get("hasButton", False)
                is_generating = state.get("isGenerating", False)
                current_text = (state.get("text") or "").strip()
                item_count = state.get("itemCount", 0)

                # ---- 等待新回复出现 ----
                # 发送消息后，对话项数量应该增加（用户消息+助手回复）
                if not new_response_appeared:
                    if item_count > baseline_item_count:
                        new_response_appeared = True
                        print(f"  [{req_id}] 📬 新对话项出现 "
                              f"({baseline_item_count} -> {item_count})")
                        # 重置：新回复出现后，之前的按钮状态都不算
                        has_button = False
                    elif is_generating:
                        # 即使 item_count 没变，如果检测到生成中也认为开始了
                        new_response_appeared = True
                        print(f"  [{req_id}] 📬 检测到生成开始")
                    else:
                        # 新回复还没出现，忽略当前状态（可能是上一轮残留）
                        elapsed = tick * poll_interval
                        if tick > 0 and tick % int(20 / poll_interval) == 0:
                            print(f"  [{req_id}] ⏳ {elapsed:.0f}s "
                                  f"等待新回复... items={item_count} "
                                  f"baseline={baseline_item_count}")
                        # 如果等了很久还没出现新对话项，可能消息没发出去
                        if tick * poll_interval > 30 and not is_generating:
                            print(f"  [{req_id}] ⚠️ 30秒未见新对话项，"
                                  f"尝试重新发送")
                            try:
                                await cp.type_and_send(message)
                                baseline_item_count = item_count
                            except Exception as e:
                                print(f"  [{req_id}] ❌ 重发失败: {e}")
                        if tick * poll_interval > 60:
                            print(f"  [{req_id}] ❌ 60秒无新对话项，放弃")
                            break
                        continue

                # ---- 跟踪生成是否开始 ----
                if is_generating and not generation_started:
                    generation_started = True
                    print(f"  [{req_id}] 🚀 生成开始")

                # ---- 审查检测 ----
                if (current_text and
                    len(best_snapshot) > 50 and
                    len(current_text) < len(best_snapshot) * 0.5 and
                    _is_censored(current_text)):
                    print(f"  [{req_id}] 🛡️ 检测到审查替换！"
                          f"当前={len(current_text)} vs 快照={len(best_snapshot)}")
                    remaining = best_snapshot[yielded_length:]
                    if remaining:
                        yield remaining
                    print(f"  [{req_id}] 📊 使用快照完成: "
                          f"{len(best_snapshot)} 字符")
                    finished = True
                    break

                # ---- 更新快照 ----
                if current_text and len(current_text) >= len(best_snapshot):
                    best_snapshot = current_text

                # ---- 流式输出增量 ----
                if current_text and len(current_text) > yielded_length:
                    if is_generating or (not has_button and generation_started):
                        new_content = current_text[yielded_length:]
                        yield new_content
                        yielded_length = len(current_text)
                        last_text = current_text
                        idle_ticks = 0
                        btn_with_text_ticks = 0

                # ---- 完成检测（主逻辑）----
                if has_button and not is_generating:
                    if generation_started:
                        # 经典路径：生成曾开始，现已停止，有按钮
                        if (_is_censored(current_text) and
                            len(best_snapshot) > len(current_text) * 1.5):
                            remaining = best_snapshot[yielded_length:]
                            if remaining:
                                yield remaining
                            print(f"  [{req_id}] 🛡️ 完成时检测到审查，"
                                  f"使用快照 {len(best_snapshot)} 字符")
                        else:
                            if current_text and len(current_text) > yielded_length:
                                remaining = current_text[yielded_length:]
                                yield remaining
                                yielded_length = len(current_text)
                            print(f"  [{req_id}] ✅ 正常完成 "
                                  f"{max(yielded_length, len(current_text))} 字符")
                        finished = True
                        break
                    elif new_response_appeared and current_text:
                        # 新路径：新回复已出现，有文本，有按钮，但从未检测到
                        # isGenerating（可能生成太快，错过了生成状态窗口）
                        btn_with_text_ticks += 1
                        # 等几个 tick 确认文本稳定不变
                        if btn_with_text_ticks >= int(3.0 / poll_interval):
                            if (_is_censored(current_text) and
                                len(best_snapshot) > len(current_text) * 1.5):
                                remaining = best_snapshot[yielded_length:]
                                if remaining:
                                    yield remaining
                                print(f"  [{req_id}] 🛡️ 快速完成检测到审查，"
                                      f"使用快照 {len(best_snapshot)} 字符")
                            else:
                                if current_text and len(current_text) > yielded_length:
                                    remaining = current_text[yielded_length:]
                                    yield remaining
                                    yielded_length = len(current_text)
                                print(f"  [{req_id}] ✅ 快速完成（未捕获生成状态）"
                                      f" {max(yielded_length, len(current_text))} 字符")
                            finished = True
                            break
                    else:
                        btn_with_text_ticks = 0
                else:
                    btn_with_text_ticks = 0

                # ---- 文本不变计数 ----
                if current_text == last_text:
                    idle_ticks += 1
                else:
                    idle_ticks = 0
                    last_text = current_text

                # ---- 进度日志 ----
                elapsed = tick * poll_interval
                if tick > 0 and tick % int(20 / poll_interval) == 0:
                    print(
                        f"  [{req_id}] ⏳ {elapsed:.0f}s "
                        f"len={len(current_text)} "
                        f"gen={is_generating} "
                        f"btn={has_button} "
                        f"snapshot={len(best_snapshot)} "
                        f"started={generation_started} "
                        f"newResp={new_response_appeared}"
                    )

                # ---- 超时检测 ----
                if (tick * poll_interval > 60 and
                    not generation_started and
                    not current_text and
                    new_response_appeared):
                    print(f"  [{req_id}] ❌ 60秒无回复内容")
                    break

                # 长时间无新内容且不在生成中
                if (idle_ticks > int(30 / poll_interval) and
                    not is_generating and
                    (generation_started or new_response_appeared)):
                    if current_text:
                        remaining = current_text[yielded_length:]
                        if remaining:
                            yield remaining
                        print(f"  [{req_id}] ⏰ 超时完成 "
                              f"{len(current_text)} 字符")
                        finished = True
                        break

            # ---- 最终兜底 ----
            if not finished:
                if best_snapshot and yielded_length < len(best_snapshot):
                    remaining = best_snapshot[yielded_length:]
                    if remaining:
                        yield remaining
                    print(f"  [{req_id}] 📋 兜底快照: "
                          f"{len(best_snapshot)} 字符")
                elif yielded_length == 0:
                    fallback = await cp.read_response_text()
                    if fallback:
                        yield fallback
                        print(f"  [{req_id}] 📋 兜底读取: "
                              f"{len(fallback)} 字符")
                    else:
                        yield "抱歉，未能获取到响应。请稍后重试。"
                        print(f"  [{req_id}] ❌ 完全无响应")

            print(f"  [{req_id}] 📊 页面#{cp.page_id} 请求完成")

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
            "mode": "stream-with-anti-censorship",
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
