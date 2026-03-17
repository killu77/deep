# browser_manager.py
# 基于探针 probe_20260316_111934.json 精确数据编写
# 核心策略：DOM 轮询 + 复制按钮(剪贴板拦截) + 审查快照

import re
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


# ═══════════════════════════════════════════════════════════════
# 注入脚本：只做剪贴板拦截（探针证实 SSE 拦截无效）
# ═══════════════════════════════════════════════════════════════
INSTALL_CLIPBOARD_HOOK_JS = """
() => {
    if (window.__clipHooked) return 'already';
    window.__clipData = { text: '', time: 0 };
    try {
        const orig = navigator.clipboard.writeText.bind(navigator.clipboard);
        navigator.clipboard.writeText = async function(text) {
            if (text && text.length > 5) {
                window.__clipData = { text: text, time: Date.now() };
            }
            return orig(text);
        };
        window.__clipHooked = true;
        return 'ok';
    } catch(e) {
        return 'fail:' + e.message;
    }
}
"""

# 在 browser_manager.py 顶部，CENSORSHIP_PHRASES 后面添加

import re

def _html_to_markdown(html: str) -> str:
    """
    轻量 HTML→Markdown 转换
    专门针对 DeepSeek 的 ds-markdown 输出结构优化
    不需要任何第三方库
    """
    if not html:
        return ""

    text = html

    # ══ 预处理：移除残留的按钮/工具栏文本 ══
    text = re.sub(r'<div[^>]*class="[^"]*(?:copy|toolbar|code-header|actions)[^"]*"[^>]*>.*?</div>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<button[^>]*>.*?</button>', '', text, flags=re.DOTALL)

    # ══ 代码块 ══
    # DeepSeek 代码块结构: <pre><code class="language-xxx">...</code></pre>
    def replace_code_block(m):
        attrs = m.group(1) or ''
        code = m.group(2)
        # 提取语言
        lang_match = re.search(r'class="[^"]*language-(\w+)', attrs)
        lang = lang_match.group(1) if lang_match else ''
        # 解码 HTML 实体
        code = _decode_entities(code)
        # 移除内部标签
        code = re.sub(r'<[^>]+>', '', code)
        return f'\n```{lang}\n{code}\n```\n'

    text = re.sub(r'<pre[^>]*>\s*<code([^>]*)>(.*?)</code>\s*</pre>', replace_code_block, text, flags=re.DOTALL)

    # 独立的 <code>（行内代码）
    def replace_inline_code(m):
        code = _decode_entities(m.group(1))
        code = re.sub(r'<[^>]+>', '', code)
        return f'`{code}`'

    text = re.sub(r'<code[^>]*>(.*?)</code>', replace_inline_code, text, flags=re.DOTALL)

    # ══ 标题 ══
    for i in range(6, 0, -1):
        text = re.sub(
            rf'<h{i}[^>]*>(.*?)</h{i}>',
            lambda m, level=i: f'\n{"#" * level} {_strip_tags(m.group(1))}\n',
            text, flags=re.DOTALL
        )

    # ══ 加粗/斜体 ══
    text = re.sub(r'<strong[^>]*>(.*?)</strong>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<b[^>]*>(.*?)</b>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<em[^>]*>(.*?)</em>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<i[^>]*>(.*?)</i>', r'*\1*', text, flags=re.DOTALL)

    # ══ 链接 ══
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL)

    # ══ 列表 ══
    # 有序列表
    def replace_ol(m):
        content = m.group(1)
        items = re.findall(r'<li[^>]*>(.*?)</li>', content, flags=re.DOTALL)
        result = '\n'
        for idx, item in enumerate(items, 1):
            item_text = _strip_tags(item).strip()
            # 处理多行列表项
            lines = item_text.split('\n')
            result += f'{idx}. {lines[0]}\n'
            for line in lines[1:]:
                if line.strip():
                    result += f'   {line.strip()}\n'
        return result + '\n'

    text = re.sub(r'<ol[^>]*>(.*?)</ol>', replace_ol, text, flags=re.DOTALL)

    # 无序列表
    def replace_ul(m):
        content = m.group(1)
        items = re.findall(r'<li[^>]*>(.*?)</li>', content, flags=re.DOTALL)
        result = '\n'
        for item in items:
            item_text = _strip_tags(item).strip()
            lines = item_text.split('\n')
            result += f'- {lines[0]}\n'
            for line in lines[1:]:
                if line.strip():
                    result += f'  {line.strip()}\n'
        return result + '\n'

    text = re.sub(r'<ul[^>]*>(.*?)</ul>', replace_ul, text, flags=re.DOTALL)

    # ══ 引用块 ══
    def replace_blockquote(m):
        content = _strip_tags(m.group(1)).strip()
        lines = content.split('\n')
        return '\n' + '\n'.join(f'> {line}' for line in lines) + '\n'

    text = re.sub(r'<blockquote[^>]*>(.*?)</blockquote>', replace_blockquote, text, flags=re.DOTALL)

    # ══ 段落和换行 ══
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<p[^>]*>(.*?)</p>', lambda m: f'\n{m.group(1)}\n', text, flags=re.DOTALL)
    text = re.sub(r'<div[^>]*>(.*?)</div>', lambda m: f'\n{m.group(1)}\n', text, flags=re.DOTALL)

    # ══ 水平线 ══
    text = re.sub(r'<hr[^>]*/?\s*>', '\n---\n', text)

    # ══ 表格 ══
    def replace_table(m):
        table_html = m.group(0)
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, flags=re.DOTALL)
        if not rows:
            return _strip_tags(table_html)

        result_rows = []
        for row in rows:
            cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row, flags=re.DOTALL)
            cells = [_strip_tags(c).strip() for c in cells]
            result_rows.append('| ' + ' | '.join(cells) + ' |')

        if len(result_rows) >= 1:
            # 第一行后加分隔线
            header = result_rows[0]
            col_count = header.count('|') - 1
            separator = '| ' + ' | '.join(['---'] * col_count) + ' |'
            return '\n' + header + '\n' + separator + '\n' + '\n'.join(result_rows[1:]) + '\n'

        return '\n'.join(result_rows)

    text = re.sub(r'<table[^>]*>.*?</table>', replace_table, text, flags=re.DOTALL)

    # ══ 清理残留标签 ══
    text = re.sub(r'<span[^>]*>(.*?)</span>', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)

    # ══ 解码 HTML 实体 ══
    text = _decode_entities(text)

    # ══ 清理空行 ══
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    return text


def _strip_tags(html: str) -> str:
    """移除 HTML 标签，保留文本"""
    # 但保留 strong/em 的 markdown 标记
    text = re.sub(r'<strong[^>]*>(.*?)</strong>', r'**\1**', html, flags=re.DOTALL)
    text = re.sub(r'<em[^>]*>(.*?)</em>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<code[^>]*>(.*?)</code>', r'`\1`', text, flags=re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    return _decode_entities(text)


def _decode_entities(text: str) -> str:
    """解码 HTML 实体"""
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&quot;', '"')
    text = text.replace('&#39;', "'")
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&#x27;', "'")
    text = text.replace('&#x2F;', '/')
    return text


# ═══════════════════════════════════════════════════════════════
# 一次性读取所有状态（基于探针确认的精确结构）
#
# 探针确认的 DOM 结构:
#   div[data-virtual-list-item-key]
#     └ div.ds-message._63c77b1
#         ├ div._74c0879 (折叠容器)
#         │   └ div.ds-think-content._767406f
#         │       └ div.ds-markdown  ← 思考文本(排除)
#         └ div.ds-markdown  ← 正式回复(目标)
#
# 完成标志: item className 含 "_43c05b5" (生成中没有这个类)
# 按钮位置: item 内 div.ds-flex._965abe9 > div.ds-icon-button
# 复制按钮: 第一个 ds-icon-button, SVG 含 "M6.14923"
# ═══════════════════════════════════════════════════════════════
READ_STATE_JS = """
() => {
    const R = {
        domText: '',
        domLen: 0,
        domHtml: '',
        thinkLen: 0,
        hasButton: false,
        buttonCount: 0,
        isComplete: false,
        isGenerating: false,
        itemCount: 0,
        clipText: '',
        clipLen: 0,
    };

    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    R.itemCount = items.length;
    if (items.length === 0) return R;

    const lastItem = items[items.length - 1];
    const itemClass = lastItem.className || '';

    R.isComplete = itemClass.includes('_43c05b5');

    const msgDiv = lastItem.querySelector('div.ds-message');
    if (!msgDiv) return R;

    // ══ 找正式回复的 ds-markdown ══
    let replyMd = null;
    const directChildren = msgDiv.children;
    for (let i = directChildren.length - 1; i >= 0; i--) {
        const child = directChildren[i];
        if (child.tagName === 'DIV' &&
            child.classList.contains('ds-markdown') &&
            !child.classList.contains('ds-think-content')) {
            replyMd = child;
            break;
        }
    }

    if (replyMd) {
        // ══ 提取 HTML（用于转 Markdown）══
        // 先克隆，移除不需要的元素
        const clone = replyMd.cloneNode(true);

        // 移除引用角标
        clone.querySelectorAll('span.ds-markdown-cite').forEach(cite => {
            const parentA = cite.closest('a');
            (parentA || cite).remove();
        });

        // 移除搜索来源栏
        clone.querySelectorAll('.ffdab56b, .ddbfd84f').forEach(el => el.remove());

        // 移除代码块中的"复制""下载"按钮
        clone.querySelectorAll('div.ds-markdown-code-copy-button, button[class*="copy"], div[class*="code-header"], div[class*="toolbar"]').forEach(el => el.remove());

        // 移除所有 class*="copy" 或 class*="download" 的按钮元素
        clone.querySelectorAll('[class*="copy-btn"], [class*="download"], [class*="code-actions"]').forEach(el => el.remove());

        R.domHtml = clone.innerHTML;

        // 纯文本用于长度比较和审查检测
        R.domText = clone.innerText || '';
        R.domLen = R.domText.length;
    }

    // ══ 思考区域 ══
    const thinkDiv = msgDiv.querySelector('div.ds-think-content');
    if (thinkDiv) {
        const thinkMd = thinkDiv.querySelector('div.ds-markdown');
        if (thinkMd) {
            R.thinkLen = (thinkMd.textContent || '').length;
        }
    }

    // ══ 按钮 ══
    const btnContainer = lastItem.querySelector('div._965abe9');
    if (btnContainer) {
        const btns = btnContainer.querySelectorAll('div.ds-icon-button');
        R.buttonCount = btns.length;
        R.hasButton = btns.length > 0;
    } else {
        const btns = lastItem.querySelectorAll('div.ds-icon-button');
        R.buttonCount = btns.length;
        R.hasButton = btns.length >= 3;
    }

    // ══ 生成中检测 ══
    R.isGenerating = !R.isComplete && R.itemCount >= 2;
    if (!R.isGenerating) {
        const thinkAnim = lastItem.querySelector('span.e4b3a110');
        if (thinkAnim) {
            const style = thinkAnim.getAttribute('style') || '';
            if (style.includes('running')) {
                R.isGenerating = true;
            }
        }
    }

    // ══ 剪贴板 ══
    if (window.__clipData) {
        R.clipText = window.__clipData.text || '';
        R.clipLen = R.clipText.length;
    }

    return R;
}
"""


# 点击复制按钮
# 探针确认: 复制按钮 = _965abe9 容器内第一个 ds-icon-button
# SVG path 以 "M6.14923" 开头
CLICK_COPY_JS = """
() => {
    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    if (items.length === 0) return 'no-items';
    const lastItem = items[items.length - 1];

    // 清空之前的剪贴板
    if (window.__clipData) {
        window.__clipData = { text: '', time: 0 };
    }

    // 在 _965abe9 容器内找第一个按钮
    const btnContainer = lastItem.querySelector('div._965abe9');
    if (btnContainer) {
        const firstBtn = btnContainer.querySelector('div.ds-icon-button');
        if (firstBtn) {
            firstBtn.click();
            return 'clicked-965';
        }
    }

    // 备选: 通过 SVG path 找复制按钮
    const allBtns = lastItem.querySelectorAll('div.ds-icon-button');
    for (const btn of allBtns) {
        const path = btn.querySelector('svg path');
        if (path) {
            const d = path.getAttribute('d') || '';
            if (d.startsWith('M6.14923')) {
                btn.click();
                return 'clicked-svg';
            }
        }
    }

    // 最后备选: 找父级是 _965abe9 或 _54866f7 的按钮
    for (const btn of allBtns) {
        const parentClass = btn.parentElement?.className || '';
        if (parentClass.includes('_965abe9') || parentClass.includes('_54866f7')) {
            btn.click();
            return 'clicked-parent';
        }
    }

    return 'not-found';
}
"""

# 滚动到底部
SCROLL_BOTTOM_JS = """
() => {
    // 探针确认: 滚动容器是 div._765a5cd.ds-scroll-area
    const sa = document.querySelector('.ds-scroll-area');
    if (sa) { sa.scrollTop = sa.scrollHeight; return true; }
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
        self._hook_installed = False

    async def ensure_clipboard_hook(self):
        """安装/重装剪贴板拦截"""
        try:
            hooked = await self.page.evaluate("() => !!window.__clipHooked")
            if hooked:
                return
        except Exception:
            pass
        try:
            result = await self.page.evaluate(INSTALL_CLIPBOARD_HOOK_JS)
            self._hook_installed = (result in ('ok', 'already'))
        except Exception as e:
            print(f"  ⚠️ P#{self.page_id} 剪贴板 hook 失败: {e}")

    async def reset_clip(self):
        try:
            await self.page.evaluate("() => { if(window.__clipData) window.__clipData = {text:'',time:0}; }")
        except Exception:
            pass

    async def read_state(self) -> dict:
        try:
            return await self.page.evaluate(READ_STATE_JS)
        except Exception as e:
            return {
                "domText": "", "domLen": 0, "thinkLen": 0,
                "hasButton": False, "buttonCount": 0,
                "isComplete": False, "isGenerating": False,
                "itemCount": 0, "clipText": "", "clipLen": 0,
                "error": str(e),
            }

    async def click_copy_and_wait(self, timeout: float = 3.0) -> str:
        try:
            result = await self.page.evaluate(CLICK_COPY_JS)
            if result == 'not-found' or result == 'no-items':
                return ""

            deadline = time.time() + timeout
            while time.time() < deadline:
                await asyncio.sleep(0.2)
                clip = await self.page.evaluate(
                    "() => (window.__clipData && window.__clipData.text) || ''"
                )
                if clip:
                    return clip
            return ""
        except Exception as e:
            print(f"  ⚠️ 复制失败: {e}")
            return ""

    async def scroll_to_bottom(self):
        try:
            await self.page.evaluate(SCROLL_BOTTOM_JS)
        except Exception:
            pass

    async def start_new_chat(self):
        self._hook_installed = False
        if "chat.deepseek.com" not in (self.page.url or ""):
            await self.page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded", timeout=30000,
            )
            await asyncio.sleep(2)

        # 探针确认: 新对话按钮
        for sel in [
            "xpath=//*[contains(text(), '开启新对话')]",
            "xpath=//*[contains(text(), '新对话')]",
            "xpath=//*[contains(text(), 'New chat')]",
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
            wait_until="domcontentloaded", timeout=30000,
        )
        await asyncio.sleep(3)

    async def type_and_send(self, message: str):
        # 探针确认: textarea placeholder="给 DeepSeek 发送消息 "
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
        self._camoufox_ctx = None

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
            print(f"⚠️ Camoufox 失败: {e}，回退 Playwright")
            if self._camoufox_ctx:
                try:
                    await self._camoufox_ctx.__aexit__(None, None, None)
                except Exception:
                    pass
                self._camoufox_ctx = None

        if not camoufox_ok:
            await self._start_with_playwright()
            self._engine = "playwright-firefox"

        await self._inject_stealth()

        # 登录
        first_page = await self.context.new_page()
        auth = AuthHandler(first_page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)
        if not self.logged_in:
            print("⚠️ 登录可能未完成")
            await first_page.close()
        else:
            print("🎉 登录成功！")
            cp = ChatPage(first_page, 0)
            await cp.ensure_clipboard_hook()
            self._pages.append(cp)
            print(f"  📄 页面#0 就绪")

        # 创建其余页面
        for i in range(1, self._page_count):
            try:
                page = await self.context.new_page()
                await page.goto(
                    "https://chat.deepseek.com/",
                    wait_until="domcontentloaded", timeout=30000,
                )
                await asyncio.sleep(2)
                cp = ChatPage(page, i)
                await cp.ensure_clipboard_hook()
                self._pages.append(cp)
                print(f"  📄 页面#{i} 就绪")
            except Exception as e:
                print(f"  ⚠️ 页面#{i} 失败: {e}")

        self._page_semaphore = asyncio.Semaphore(len(self._pages))
        self._ready = True
        self._ready_event.set()
        print(f"✅ 就绪（{self._engine}，{len(self._pages)} 并发页面）")

    async def _start_with_camoufox(self):
        from camoufox.async_api import AsyncCamoufox
        self._camoufox_ctx = AsyncCamoufox(headless=self.headless, geoip=False)
        self.browser = await self._camoufox_ctx.__aenter__()
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN", timezone_id="Asia/Shanghai",
        )
        print("  ✅ Camoufox 启动")

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
            locale="zh-CN", timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
        )
        print("  ✅ Playwright Firefox 启动")

    async def _inject_stealth(self):
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
        print(f"📨 #{req_id} ({len(message)} 字符)")

        cp = None
        try:
            cp = await asyncio.wait_for(self._acquire_page(), timeout=300)
        except (asyncio.TimeoutError, RuntimeError) as e:
            yield f"[错误] {e}"
            return

        print(f"  [#{req_id}] → 页面#{cp.page_id}")

        try:
            cp.request_count += 1

            # 检查存活
            if not await cp.is_alive():
                print(f"  [#{req_id}] 页面死亡，恢复中...")
                try:
                    new_page = await self.context.new_page()
                    await new_page.goto(
                        "https://chat.deepseek.com/",
                        wait_until="domcontentloaded", timeout=30000,
                    )
                    await asyncio.sleep(2)
                    cp.page = new_page
                    cp._hook_installed = False
                except Exception as e:
                    yield f"[错误] 页面恢复失败: {e}"
                    return

            # 新对话
            await cp.start_new_chat()
            await asyncio.sleep(0.5)

            # 安装 hook + 重置
            await cp.ensure_clipboard_hook()
            await cp.reset_clip()

            # 发送
            await cp.type_and_send(message)
            print(f"  [#{req_id}] 已发送")

            # ═══════════════════════════════════════════
            # 流式读取主循环
            # ═══════════════════════════════════════════
            max_wait = 600
            poll_interval = 0.4

            # 纯文本快照（用于审查检测，基准一致）
            best_text = ""
            # Markdown 快照（用于实际输出）
            best_md = ""
            # HTML 快照（最后的保命手段）
            best_html = ""

            yielded_md_len = 0     # 已输出的 Markdown 字符数
            gen_started = False
            no_change_count = 0
            prev_len = 0
            prev_html_len = 0      # 上次 HTML 长度（用于判断是否需要重新转换）
            start_ts = time.time()
            scroll_counter = 0

            # 等 AI 开始生成（第二个 item 出现）
            for _ in range(60):
                await asyncio.sleep(0.5)
                st = await cp.read_state()
                if st.get("itemCount", 0) >= 2:
                    break
                if time.time() - start_ts > 30:
                    break

            while True:
                elapsed = time.time() - start_ts
                if elapsed > max_wait:
                    print(f"  [#{req_id}] ⏰ 超时 {max_wait}s")
                    break

                await asyncio.sleep(poll_interval)
                scroll_counter += 1

                # 每 ~5 秒滚动一下（对抗虚拟滚动截断）
                if scroll_counter % 12 == 0:
                    await cp.scroll_to_bottom()

                # 读状态
                state = await cp.read_state()
                dom_text = state.get("domText", "")
                dom_len = state.get("domLen", 0)
                dom_html = state.get("domHtml", "")
                dom_html_len = len(dom_html)
                think_len = state.get("thinkLen", 0)
                is_complete = state.get("isComplete", False)
                has_button = state.get("hasButton", False)
                is_gen = state.get("isGenerating", False)
                btn_count = state.get("buttonCount", 0)

                # 检测生成开始
                if not gen_started and (dom_len > 0 or think_len > 0 or is_gen):
                    gen_started = True
                    no_change_count = 0
                    print(f"  [#{req_id}] 🚀 开始 "
                          f"(think={think_len} reply={dom_len} gen={is_gen})")

                # ══ 持续保存快照（生成中就一直存）══
                if dom_len > len(best_text):
                    best_text = dom_text
                if dom_html and dom_html_len > len(best_html):
                    best_html = dom_html

                # ══ HTML → Markdown（仅在 HTML 有变化时转换）══
                current_md = ""
                if dom_html and dom_html_len != prev_html_len:
                    current_md = _html_to_markdown(dom_html)
                    prev_html_len = dom_html_len
                    if len(current_md) > len(best_md):
                        best_md = current_md
                elif dom_html and dom_html_len == prev_html_len:
                    # HTML 没变，复用上次的 best_md
                    current_md = best_md

                # ── 审查检测（用纯文本，基准一致）──
                if (gen_started and len(best_text) > 80
                        and dom_text and dom_len < len(best_text) * 0.4
                        and _is_censored(dom_text)):
                    print(f"  [#{req_id}] 🛡️ 审查! "
                          f"dom={dom_len} snap={len(best_text)}")
                    remaining = best_md[yielded_md_len:]
                    if remaining:
                        yield remaining
                    break

                # ── 流式增量输出（Markdown 格式）──
                if current_md and len(current_md) > yielded_md_len:
                    new_part = current_md[yielded_md_len:]
                    if not is_complete:
                        # 生成中：直接输出
                        yield new_part
                        yielded_md_len = len(current_md)
                        no_change_count = 0
                    elif not _is_censored(dom_text):
                        # 已完成且非审查：输出
                        yield new_part
                        yielded_md_len = len(current_md)

                # ══════════════════════════════════════════
                # 完成检测
                # ══════════════════════════════════════════
                if gen_started and is_complete and has_button and btn_count >= 3:
                    # 二次确认
                    await asyncio.sleep(0.3)
                    confirm = await cp.read_state()
                    if not (confirm.get("isComplete") and confirm.get("hasButton")):
                        continue

                    # 滚到底再读一次
                    await cp.scroll_to_bottom()
                    await asyncio.sleep(0.2)
                    final = await cp.read_state()
                    final_text = final.get("domText", "")
                    final_html = final.get("domHtml", "")
                    final_len = final.get("domLen", 0)

                    # 检查此刻 DOM 是否已被审查替换
                    dom_censored = _is_censored(final_text)

                    if not dom_censored:
                        # DOM 没被替换，更新快照
                        if final_len > len(best_text):
                            best_text = final_text
                        if final_html and len(final_html) > len(best_html):
                            best_html = final_html
                        final_md = _html_to_markdown(final_html) if final_html else ""
                        if len(final_md) > len(best_md):
                            best_md = final_md

                    # 点复制按钮
                    clip_text = await cp.click_copy_and_wait(timeout=3.0)

                    # ── 决定最终输出来源 ──
                    # 优先级:
                    #   1. 剪贴板（非审查）→ 最完整的原生 Markdown
                    #   2. best_md 快照 → 生成中积累的 HTML→MD
                    #   3. best_html → HTML 快照再转一次
                    final_output = None
                    source = "none"

                    if clip_text and not _is_censored(clip_text):
                        final_output = clip_text
                        source = f"clip={len(clip_text)}"
                    elif clip_text and _is_censored(clip_text):
                        # 剪贴板被审查了，用快照
                        print(f"  [#{req_id}] 🛡️ 剪贴板被审查! "
                              f"clip={len(clip_text)} → 用快照")
                        if best_md:
                            final_output = best_md
                            source = f"md_snap={len(best_md)}"
                        elif best_html:
                            final_output = _html_to_markdown(best_html)
                            source = f"html_snap→md={len(final_output)}"
                    else:
                        # 没拿到剪贴板
                        if best_md:
                            final_output = best_md
                            source = f"md_snap={len(best_md)}"
                        elif best_html:
                            final_output = _html_to_markdown(best_html)
                            source = f"html_snap→md={len(final_output)}"

                    # 补充未输出的部分
                    if final_output and len(final_output) > yielded_md_len:
                        yield final_output[yielded_md_len:]
                        yielded_md_len = len(final_output)

                    print(f"  [#{req_id}] ✅ 完成: {yielded_md_len} 字 ({source})")
                    break

                # ── 无进展检测 ──
                if dom_len == prev_len:
                    no_change_count += 1
                else:
                    no_change_count = 0
                    prev_len = dom_len

                if no_change_count > int(90 / poll_interval):
                    if gen_started and best_md:
                        if len(best_md) > yielded_md_len:
                            yield best_md[yielded_md_len:]
                        print(f"  [#{req_id}] ⏰ 90s 无进展: {len(best_md)} 字")
                        break
                    elif not gen_started and elapsed > 120:
                        print(f"  [#{req_id}] ❌ 120s 无响应")
                        break

                # ── 日志 ──
                if scroll_counter % 37 == 0:
                    print(f"  [#{req_id}] ⏳ {elapsed:.0f}s "
                          f"dom={dom_len} md={yielded_md_len} "
                          f"snap_md={len(best_md)} snap_html={len(best_html)} "
                          f"think={think_len} comp={is_complete} btn={btn_count}")

            # ═══════════════════════════════════════════
            # 兜底（完全没输出过内容时）
            # ═══════════════════════════════════════════
            if yielded_md_len == 0:
                # 1) 点复制按钮
                clip = await cp.click_copy_and_wait(timeout=5.0)
                if clip and not _is_censored(clip):
                    yield clip
                    print(f"  [#{req_id}] 📋 兜底复制: {len(clip)} 字")
                elif best_md:
                    # 2) MD 快照
                    yield best_md
                    print(f"  [#{req_id}] 📋 兜底md快照: {len(best_md)} 字")
                elif best_html:
                    # 3) HTML 快照转 MD
                    md = _html_to_markdown(best_html)
                    if md:
                        yield md
                        print(f"  [#{req_id}] 📋 兜底html→md: {len(md)} 字")
                    else:
                        yield "抱歉，未能获取到响应。请稍后重试。"
                        print(f"  [#{req_id}] ❌ html转换失败")
                else:
                    # 4) 再读一次 DOM
                    await cp.scroll_to_bottom()
                    await asyncio.sleep(1)
                    st = await cp.read_state()
                    dt = st.get("domText", "")
                    if dt and not _is_censored(dt):
                        yield dt
                        print(f"  [#{req_id}] 📋 兜底DOM纯文本: {len(dt)} 字")
                    else:
                        yield "抱歉，未能获取到响应。请稍后重试。"
                        print(f"  [#{req_id}] ❌ 完全无响应")

        except Exception as e:
            print(f"  [#{req_id}] ❌ {e}")
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
        alive_count = sum(1 for cp in self._pages if not cp.page.is_closed())
        busy_count = sum(1 for cp in self._pages if cp.busy)
        return {
            "browser_alive": alive_count > 0,
            "logged_in": self.logged_in,
            "ready": self._ready,
            "engine": self._engine,
            "mode": "dom-poll+clipboard-v3",
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
            except Exception:
                pass

        # 定期重装 hook
        if self.heartbeat_count % 5 == 0:
            for cp in self._pages:
                if not cp.busy:
                    try:
                        await cp.ensure_clipboard_hook()
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
            if self._camoufox_ctx:
                await self._camoufox_ctx.__aexit__(None, None, None)
            elif self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 已关闭")
        except Exception as e:
            print(f"⚠️ {e}")
