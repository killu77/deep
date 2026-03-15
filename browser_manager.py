# browser_manager.py
"""
DeepSeek 反代（初始化脚本拦截模式）
- 用 addInitScript 在页面最早期 patch fetch/XMLHttpRequest
- 真正逐 chunk 实时读取 SSE，不等完整响应
- 敏感内容在被替换前就已经捕获
- 适配 DeepSeek 自有 SSE 格式（fragments）
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


# 注入到页面最早期的 JS 脚本
INIT_INTERCEPT_SCRIPT = """
(() => {
    // 防止重复注入
    if (window.__ds_interceptor_installed) return;
    window.__ds_interceptor_installed = true;

    // 全局捕获存储
    window.__ds_chunks = [];
    window.__ds_stream_done = false;
    window.__ds_stream_error = null;
    window.__ds_capture_active = false;

    // 重置函数
    window.__ds_reset = () => {
        window.__ds_chunks = [];
        window.__ds_stream_done = false;
        window.__ds_stream_error = null;
    };

    // 解析 SSE 行，支持 DeepSeek 自有格式和 OpenAI 格式
    function parseSSELine(line) {
        line = line.trim();
        if (!line.startsWith('data: ')) return null;
        const dataStr = line.substring(6).trim();

        if (dataStr === '[DONE]') return { type: 'done' };

        try {
            const parsed = JSON.parse(dataStr);

            // DeepSeek 自有格式：v.response.fragments[].content
            if (parsed.v && parsed.v.response) {
                const resp = parsed.v.response;
                const fragments = resp.fragments || [];
                let text = '';
                for (const frag of fragments) {
                    if (frag.content) text += frag.content;
                }
                if (text) return { type: 'content', data: text, format: 'deepseek' };
            }

            // OpenAI 格式：choices[0].delta.content
            if (parsed.choices && parsed.choices.length > 0) {
                const delta = parsed.choices[0].delta || {};
                if (delta.content) return { type: 'content', data: delta.content, format: 'openai' };
            }

            return null;
        } catch (e) {
            return null;
        }
    }

    // 处理 SSE 流的通用函数
    async function processSSEStream(reader) {
        const decoder = new TextDecoder();
        let buffer = '';
        let lastFullText = '';

        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\\n');
                buffer = lines.pop(); // 保留不完整行

                for (const line of lines) {
                    const result = parseSSELine(line);
                    if (!result) continue;

                    if (result.type === 'done') {
                        window.__ds_stream_done = true;
                        return;
                    }

                    if (result.type === 'content') {
                        if (result.format === 'deepseek') {
                            // DeepSeek 格式：每次推送的是完整累积文本
                            // 我们只取增量部分
                            const fullText = result.data;
                            if (fullText.length > lastFullText.length) {
                                const delta = fullText.substring(lastFullText.length);
                                window.__ds_chunks.push(delta);
                                lastFullText = fullText;
                            } else if (fullText !== lastFullText) {
                                // 文本被替换了（敏感内容审查），记录完整的新文本
                                window.__ds_chunks.push('[CONTENT_REPLACED]');
                                lastFullText = fullText;
                            }
                        } else {
                            // OpenAI 格式：每次是增量
                            window.__ds_chunks.push(result.data);
                        }
                    }
                }
            }
        } catch (e) {
            window.__ds_stream_error = e.message;
        }
        window.__ds_stream_done = true;
    }

    // ══════════ 拦截 fetch ══════════
    const _originalFetch = window.fetch;
    window.fetch = async function(...args) {
        const response = await _originalFetch.apply(this, args);
        const url = (typeof args[0] === 'string') ? args[0] : (args[0]?.url || '');

        if (window.__ds_capture_active && url.includes('/chat/completion')) {
            const contentType = response.headers.get('content-type') || '';
            if (contentType.includes('text/event-stream') || contentType.includes('application/json')) {
                console.log('[DS Intercept] 捕获到 completion 响应');
                // clone 出来读取，原始返回给页面
                const cloned = response.clone();
                const reader = cloned.body.getReader();
                processSSEStream(reader);
            }
        }
        return response;
    };

    // ══════════ 拦截 XMLHttpRequest ══════════
    const _originalXHROpen = XMLHttpRequest.prototype.open;
    const _originalXHRSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        this.__ds_url = url;
        return _originalXHROpen.call(this, method, url, ...rest);
    };

    XMLHttpRequest.prototype.send = function(body) {
        if (window.__ds_capture_active && this.__ds_url && this.__ds_url.includes('/chat/completion')) {
            console.log('[DS Intercept] XHR 捕获到 completion 请求');
            let xhrBuffer = '';
            let xhrLastFullText = '';

            this.addEventListener('progress', function() {
                try {
                    const text = this.responseText || '';
                    const newData = text.substring(xhrBuffer.length);
                    xhrBuffer = text;

                    const lines = newData.split('\\n');
                    for (const line of lines) {
                        const result = parseSSELine(line);
                        if (!result) continue;

                        if (result.type === 'done') {
                            window.__ds_stream_done = true;
                            return;
                        }

                        if (result.type === 'content') {
                            if (result.format === 'deepseek') {
                                const fullText = result.data;
                                if (fullText.length > xhrLastFullText.length) {
                                    window.__ds_chunks.push(fullText.substring(xhrLastFullText.length));
                                    xhrLastFullText = fullText;
                                }
                            } else {
                                window.__ds_chunks.push(result.data);
                            }
                        }
                    }
                } catch (e) {}
            });

            this.addEventListener('loadend', function() {
                window.__ds_stream_done = true;
            });

            this.addEventListener('error', function() {
                window.__ds_stream_error = 'XHR error';
                window.__ds_stream_done = true;
            });
        }
        return _originalXHRSend.call(this, body);
    };

    // ══════════ 拦截 EventSource ══════════
    const _OriginalEventSource = window.EventSource;
    if (_OriginalEventSource) {
        window.EventSource = function(url, config) {
            const es = new _OriginalEventSource(url, config);
            if (window.__ds_capture_active && url.includes('/chat/completion')) {
                console.log('[DS Intercept] EventSource 捕获');
                let esLastFullText = '';

                es.addEventListener('message', function(event) {
                    const result = parseSSELine('data: ' + event.data);
                    if (!result) return;
                    if (result.type === 'done') { window.__ds_stream_done = true; return; }
                    if (result.type === 'content') {
                        if (result.format === 'deepseek') {
                            if (result.data.length > esLastFullText.length) {
                                window.__ds_chunks.push(result.data.substring(esLastFullText.length));
                                esLastFullText = result.data;
                            }
                        } else {
                            window.__ds_chunks.push(result.data);
                        }
                    }
                });
                es.addEventListener('error', () => { window.__ds_stream_done = true; });
            }
            return es;
        };
        window.EventSource.prototype = _OriginalEventSource.prototype;
        Object.defineProperty(window.EventSource, 'CONNECTING', { value: 0 });
        Object.defineProperty(window.EventSource, 'OPEN', { value: 1 });
        Object.defineProperty(window.EventSource, 'CLOSED', { value: 2 });
    }

    console.log('[DS Intercept] 全协议拦截器已安装 (fetch + XHR + EventSource)');
})();
"""


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
        self.total_requests = 0

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"
        self._engine = "unknown"

        self._lock = asyncio.Lock()
        self._ready = False
        self._ready_event = asyncio.Event()

    # ── 就绪控制 ──

    async def wait_until_ready(self, timeout: float = 180.0) -> bool:
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    # ── Camoufox 缓存 ──

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

    # ── 初始化 ──

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

        # 登录
        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

        if not self.logged_in:
            print("⚠️ 登录可能未完成")
        else:
            print("🎉 登录成功！")

        self._ready = True
        self._ready_event.set()
        print(f"✅ 就绪（引擎: {self._engine}，模式: 初始化脚本拦截）")

    async def _start_with_camoufox(self):
        print("  → Camoufox...")
        from camoufox.async_api import AsyncCamoufox
        self._camoufox = AsyncCamoufox(headless=self.headless, geoip=False)
        self.browser = await self._camoufox.__aenter__()
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        # 在创建页面之前就注入拦截脚本
        await self.context.add_init_script(INIT_INTERCEPT_SCRIPT)
        self.page = await self.context.new_page()
        print("  ✅ Camoufox 已启动（拦截器已预注入）")

    async def _start_with_playwright(self):
        print("  → Playwright Firefox...")
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
        # 在创建页面之前就注入拦截脚本
        await self.context.add_init_script(INIT_INTERCEPT_SCRIPT)
        self.page = await self.context.new_page()
        print("  ✅ Playwright Firefox 已启动（拦截器已预注入）")

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

    async def _ensure_interceptor_active(self):
        """确保拦截器在当前页面中仍然有效"""
        try:
            installed = await self.page.evaluate(
                "() => window.__ds_interceptor_installed === true"
            )
            if not installed:
                print("  ⚠️ 拦截器丢失，重新注入...")
                await self.page.evaluate(INIT_INTERCEPT_SCRIPT)
        except Exception:
            try:
                await self.page.evaluate(INIT_INTERCEPT_SCRIPT)
            except Exception as e:
                print(f"  ❌ 重新注入拦截器失败: {e}")

    # ── 新对话 ──

    async def _start_new_chat(self):
        if "chat.deepseek.com" not in self.page.url:
            await self.page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(2)

        selectors = [
            "xpath=//*[contains(text(), '开启新对话')]",
            "xpath=//*[contains(text(), '新对话')]",
            "xpath=//*[contains(text(), 'New chat')]",
            "div.ds-icon-button",
            "[class*='new-chat']",
        ]
        for sel in selectors:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(1)
                    print("  ✅ 新对话")
                    return
            except Exception:
                continue

        await self.page.goto(
            "https://chat.deepseek.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)

    # ── 输入并发送 ──

    async def _type_and_send(self, message: str):
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
        print(f"  → 已发送 ({len(message)} 字符)")
        await asyncio.sleep(1)

    # ══════════════════════════════════════════════════════
    # 核心发送
    # ══════════════════════════════════════════════════════

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

        async with self._lock:
            self.total_requests += 1
            self.requests_handled += 1
            req_id = self.total_requests
            print(f"📨 请求 #{req_id} (长度: {len(message)} 字符)")

            if not self.page:
                yield "[错误] 浏览器未就绪"
                return

            try:
                # 开启新对话
                await self._start_new_chat()
                await asyncio.sleep(1)

                # 确保拦截器有效
                await self._ensure_interceptor_active()

                # 重置捕获状态 + 开启捕获
                await self.page.evaluate("""
                    () => {
                        window.__ds_reset();
                        window.__ds_capture_active = true;
                    }
                """)

                # 发送消息
                await self._type_and_send(message)

                # ── 实时轮询读取 ──
                print(f"  [{req_id}] 等待 SSE 流...")
                full_text = ""
                read_index = 0
                max_wait = 600.0
                waited = 0.0
                idle_count = 0
                stream_started = False

                while waited < max_wait:
                    result = await self.page.evaluate("""
                        (fromIndex) => ({
                            chunks: window.__ds_chunks.slice(fromIndex),
                            total: window.__ds_chunks.length,
                            done: window.__ds_stream_done,
                            error: window.__ds_stream_error,
                        })
                    """, read_index)

                    if result.get("error"):
                        err = result["error"]
                        print(f"  [{req_id}] ❌ 流错误: {err}")
                        if not full_text:
                            yield f"[错误] {err}"
                        break

                    new_chunks = result.get("chunks", [])
                    if new_chunks:
                        if not stream_started:
                            stream_started = True
                            print(f"  [{req_id}] 流开始")

                        for chunk in new_chunks:
                            if chunk == '[CONTENT_REPLACED]':
                                print(f"  [{req_id}] ⚠️ 检测到内容被替换（审查）")
                                continue
                            full_text += chunk
                            yield chunk
                        read_index += len(new_chunks)
                        idle_count = 0

                    if result.get("done"):
                        if not new_chunks:
                            break
                        continue

                    if not new_chunks:
                        idle_count += 1

                    sleep_time = 0.05 if idle_count < 50 else 0.2
                    await asyncio.sleep(sleep_time)
                    waited += sleep_time

                    # 超时检查
                    if not stream_started and waited > 90:
                        print(f"  [{req_id}] ⚠️ 90秒未收到流数据，尝试 DOM 兜底")
                        fallback = await self._dom_fallback()
                        if fallback:
                            yield fallback
                            full_text = fallback
                        else:
                            yield "[错误] 等待响应超时"
                        break

                    if idle_count > 0 and idle_count % 200 == 0:
                        print(f"  [{req_id}] ⏳ idle={idle_count}, waited={waited:.0f}s")

                # 关闭捕获
                await self.page.evaluate(
                    "() => { window.__ds_capture_active = false; }"
                )

                # 最终兜底
                if not full_text:
                    await asyncio.sleep(2)
                    fallback = await self._dom_fallback()
                    if fallback:
                        print(f"  [{req_id}] 🔄 DOM 兜底，长度: {len(fallback)}")
                        yield fallback
                        full_text = fallback

                print(f"  [{req_id}] ✅ 完成，长度: {len(full_text)}")

            except Exception as e:
                try:
                    await self.page.evaluate(
                        "() => { window.__ds_capture_active = false; }"
                    )
                except Exception:
                    pass
                print(f"  [{req_id}] ❌ {e}")
                import traceback
                traceback.print_exc()
                yield f"[错误] {str(e)}"

    async def _dom_fallback(self) -> str:
        try:
            text = await self.page.evaluate("""
                () => {
                    const items = document.querySelectorAll(
                        'div[data-virtual-list-item-key]'
                    );
                    if (items.length > 0) {
                        const last = items[items.length - 1];
                        const md = last.querySelectorAll('[class*="ds-markdown"]');
                        if (md.length > 0)
                            return md[md.length - 1].textContent || '';
                    }
                    const allMd = document.querySelectorAll('[class*="ds-markdown"]');
                    if (allMd.length > 0)
                        return allMd[allMd.length - 1].textContent || '';
                    return '';
                }
            """)
            return (text or "").strip()
        except Exception:
            return ""

    # ── 其他 ──

    async def is_alive(self) -> bool:
        try:
            if not self._ready or not self.page or self.page.is_closed():
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
            "ready": self._ready,
            "engine": self._engine,
            "mode": "init-script-intercept",
            "has_token": True,
            "cookie_count": 0,
            "uptime_seconds": time.time() - self.start_time,
            "heartbeat_count": self.heartbeat_count,
            "requests_handled": self.requests_handled,
            "total_requests": self.total_requests,
            "current_url": (
                self.page.url
                if self.page and not self.page.is_closed()
                else "N/A"
            ),
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        try:
            if not self.page or self.page.is_closed():
                return None
            buf = await self.page.screenshot(full_page=False)
            return base64.b64encode(buf).decode("utf-8")
        except Exception:
            return None

    async def simulate_activity(self):
        if not self.page or self.page.is_closed():
            return
        try:
            self.heartbeat_count += 1
            import random
            await self.page.mouse.move(
                random.randint(100, 1800),
                random.randint(100, 900),
            )
            await self.page.evaluate("""
                () => {
                    document.dispatchEvent(new MouseEvent('mousemove', {
                        clientX: Math.random() * window.innerWidth,
                        clientY: Math.random() * window.innerHeight
                    }));
                    window.scrollBy(0, Math.random() > 0.5 ? 1 : -1);
                }
            """)
            if self.heartbeat_count % 10 == 0:
                print(f"💓 心跳 #{self.heartbeat_count}")
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
            print("🔒 已关闭")
        except Exception as e:
            print(f"⚠️ {e}")
