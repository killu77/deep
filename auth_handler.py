# auth_handler.py
"""
认证处理器（Cookie 注入版）：
不再模拟输入账号密码，而是直接注入预先获取的 Cookie 和 localStorage。
支持三种 Cookie 来源：
1. 环境变量 DEEPSEEK_COOKIES / DEEPSEEK_LOCAL_STORAGE（适合 HF 部署）
2. 本地文件 deepseek_cookies.json（适合本地开发）
3. 环境变量 DEEPSEEK_TOKEN（直接注入 API token）
"""

import os
import json
import asyncio
import base64
from pathlib import Path
from typing import Optional


class AuthHandler:
    def __init__(self, page, context=None):
        """
        Args:
            page: Playwright page 实例
            context: Playwright browser context 实例（用于注入 cookie）
        """
        self.page = page
        self.context = context

    async def _log_screenshot(self, label: str):
        try:
            screenshot_bytes = await self.page.screenshot()
            b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
            print(f"\n--- SCREENSHOT: {label} ---")
            print(f"Base64 长度: {len(b64)} 字符")
            print(f"预览: {b64[:200]}...")
            print(f"--- END SCREENSHOT: {label} ---\n")
        except Exception as e:
            print(f"⚠️ 截图失败 ({label}): {e}")

    def _load_auth_data(self) -> dict:
        """
        从多种来源加载认证数据，优先级：
        1. 环境变量 DEEPSEEK_TOKEN（最简单）
        2. 环境变量 DEEPSEEK_COOKIES + DEEPSEEK_LOCAL_STORAGE
        3. 本地文件 deepseek_cookies.json
        """
        auth_data = {
            "cookies": [],
            "local_storage": {},
            "session_storage": {},
            "token": None,
        }

        # === 来源1：直接 Token ===
        token = os.getenv("DEEPSEEK_TOKEN", "").strip()
        if token:
            print("  📋 认证来源: 环境变量 DEEPSEEK_TOKEN")
            auth_data["token"] = token
            return auth_data

        # === 来源2：环境变量 Cookie JSON ===
        cookies_env = os.getenv("DEEPSEEK_COOKIES", "").strip()
        if cookies_env:
            print("  📋 认证来源: 环境变量 DEEPSEEK_COOKIES")
            try:
                auth_data["cookies"] = json.loads(cookies_env)
            except json.JSONDecodeError as e:
                print(f"  ⚠️ DEEPSEEK_COOKIES JSON 解析失败: {e}")

            storage_env = os.getenv("DEEPSEEK_LOCAL_STORAGE", "").strip()
            if storage_env:
                try:
                    auth_data["local_storage"] = json.loads(storage_env)
                except json.JSONDecodeError as e:
                    print(f"  ⚠️ DEEPSEEK_LOCAL_STORAGE JSON 解析失败: {e}")
            return auth_data

        # === 来源3：本地文件 ===
        cookie_file = Path("deepseek_cookies.json")
        if cookie_file.exists():
            print(f"  📋 认证来源: 本地文件 {cookie_file}")
            try:
                with open(cookie_file, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
                auth_data["cookies"] = file_data.get("cookies", [])
                auth_data["local_storage"] = file_data.get("local_storage", {})
                auth_data["session_storage"] = file_data.get("session_storage", {})
                return auth_data
            except Exception as e:
                print(f"  ⚠️ 读取 Cookie 文件失败: {e}")

        print("  ❌ 未找到任何认证数据！")
        print("     请先运行 export_cookies.py 导出 Cookie，")
        print("     或设置环境变量 DEEPSEEK_TOKEN / DEEPSEEK_COOKIES")
        return auth_data

    async def login(self, email: str = "", password: str = "") -> bool:
        """
        通过注入 Cookie 完成登录。
        email 和 password 参数保留是为了兼容接口，但不再使用。
        """
        print("\n📋 开始 Cookie 注入登录流程...")

        auth_data = self._load_auth_data()

        has_cookies = bool(auth_data.get("cookies"))
        has_token = bool(auth_data.get("token"))
        has_storage = bool(auth_data.get("local_storage"))

        if not has_cookies and not has_token and not has_storage:
            print("❌ 没有可用的认证数据，无法登录！")
            return False

        try:
            # ==== 步骤 1：先导航到 DeepSeek 域名下（Cookie 必须在对应域名下才能注入）====
            print("\n  [1/4] 导航到 DeepSeek 域名...")
            await self.page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(2)
            print(f"  ✅ 当前 URL: {self.page.url}")

            # ==== 步骤 2：注入 Cookie ====
            if has_cookies and self.context:
                print(f"\n  [2/4] 注入 {len(auth_data['cookies'])} 个 Cookie...")
                await self._inject_cookies(auth_data["cookies"])
                print("  ✅ Cookie 注入完成。")
            else:
                print("\n  [2/4] 跳过 Cookie 注入（无 Cookie 数据或无 context）")

            # ==== 步骤 3：注入 localStorage / Token ====
            if has_token:
                print(f"\n  [3/4] 注入 Token 到 localStorage...")
                await self._inject_token(auth_data["token"])
                print("  ✅ Token 注入完成。")
            elif has_storage:
                print(f"\n  [3/4] 注入 {len(auth_data['local_storage'])} 项 localStorage...")
                await self._inject_local_storage(auth_data["local_storage"])
                # 注入 sessionStorage
                if auth_data.get("session_storage"):
                    await self._inject_session_storage(auth_data["session_storage"])
                print("  ✅ localStorage 注入完成。")
            else:
                print("\n  [3/4] 跳过 localStorage 注入")

            # ==== 步骤 4：刷新页面并验证登录状态 ====
            print(f"\n  [4/4] 刷新页面，验证登录状态...")
            await self.page.reload(wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # 检查是否成功进入聊天页面
            success = await self._verify_login(max_wait=30)

            if success:
                print("\n  🎉🎉🎉 Cookie 注入登录成功！")
                await self._log_screenshot("login_success")
                return True
            else:
                print("\n  ⚠️ Cookie 注入后未能成功登录，Cookie 可能已过期。")
                print("  请重新运行 export_cookies.py 获取新的 Cookie。")
                await self._log_screenshot("login_failed")
                return False

        except Exception as e:
            print(f"\n  ❌ Cookie 注入登录异常: {e}")
            await self._log_screenshot("login_exception")
            return False

    async def _inject_cookies(self, cookies: list):
        """将 Cookie 列表注入到浏览器 context 中。"""
        if not self.context:
            print("  ⚠️ 无法注入 Cookie：context 未提供")
            return

        # Playwright 的 add_cookies 需要特定格式
        formatted_cookies = []
        for cookie in cookies:
            c = {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": cookie.get("domain", ".deepseek.com"),
                "path": cookie.get("path", "/"),
            }
            # 可选字段
            if cookie.get("expires") and cookie["expires"] > 0:
                c["expires"] = cookie["expires"]
            if cookie.get("httpOnly") is not None:
                c["httpOnly"] = cookie["httpOnly"]
            if cookie.get("secure") is not None:
                c["secure"] = cookie["secure"]
            if cookie.get("sameSite"):
                # Playwright 要求 sameSite 首字母大写
                ss = cookie["sameSite"]
                if ss.lower() in ("strict", "lax", "none"):
                    c["sameSite"] = ss.capitalize()
                    if ss.lower() == "none":
                        c["sameSite"] = "None"

            formatted_cookies.append(c)

        try:
            await self.context.add_cookies(formatted_cookies)
            print(f"  ✅ 成功注入 {len(formatted_cookies)} 个 Cookie")

            # 打印关键 Cookie 用于调试
            for c in formatted_cookies:
                if any(kw in c["name"].lower() for kw in ["token", "session", "auth", "user"]):
                    print(f"     🔑 {c['name']}: {c['value'][:30]}...")
        except Exception as e:
            print(f"  ⚠️ Cookie 注入出错: {e}")
            # 尝试逐个注入，跳过有问题的
            success_count = 0
            for c in formatted_cookies:
                try:
                    await self.context.add_cookies([c])
                    success_count += 1
                except Exception:
                    print(f"     ⚠️ 跳过无效 Cookie: {c['name']}")
            print(f"  ✅ 成功注入 {success_count}/{len(formatted_cookies)} 个 Cookie")

    async def _inject_token(self, token: str):
        """将 Token 直接注入到 localStorage（DeepSeek 常用的存储方式）。"""
        await self.page.evaluate(f"""
            () => {{
                // DeepSeek 常见的 token 存储 key
                const token = {json.dumps(token)};
                
                // 尝试多种常见的 key 名
                localStorage.setItem('token', token);
                localStorage.setItem('ds_token', token);
                localStorage.setItem('userToken', token);
                
                // DeepSeek 可能使用的用户信息格式
                try {{
                    const userInfo = JSON.parse(localStorage.getItem('ds_chat_user_info') || '{{}}');
                    userInfo.token = token;
                    localStorage.setItem('ds_chat_user_info', JSON.stringify(userInfo));
                }} catch(e) {{}}
                
                // 有些版本会把整个 auth 状态存在一个 key 里
                try {{
                    const authState = {{
                        token: token,
                        isLoggedIn: true
                    }};
                    localStorage.setItem('auth', JSON.stringify(authState));
                }} catch(e) {{}}
                
                console.log('Token injected successfully');
            }}
        """)

    async def _inject_local_storage(self, storage_data: dict):
        """将 localStorage 数据注入到页面中。"""
        for key, value in storage_data.items():
            try:
                await self.page.evaluate(
                    """
                    ([key, value]) => {
                        localStorage.setItem(key, value);
                    }
                    """,
                    [key, value],
                )
            except Exception as e:
                print(f"  ⚠️ 注入 localStorage[{key}] 失败: {e}")

        # 打印关键项
        for key, value in storage_data.items():
            if any(kw in key.lower() for kw in ["token", "auth", "user", "session"]):
                preview = str(value)[:50]
                print(f"     🔑 localStorage[{key}]: {preview}...")

    async def _inject_session_storage(self, storage_data: dict):
        """将 sessionStorage 数据注入到页面中。"""
        for key, value in storage_data.items():
            try:
                await self.page.evaluate(
                    """
                    ([key, value]) => {
                        sessionStorage.setItem(key, value);
                    }
                    """,
                    [key, value],
                )
            except Exception as e:
                print(f"  ⚠️ 注入 sessionStorage[{key}] 失败: {e}")

    async def _verify_login(self, max_wait: int = 30) -> bool:
        """验证是否成功登录。"""
        for i in range(max_wait):
            await asyncio.sleep(1)
            current_url = self.page.url

            # 如果还在登录页，说明还没成功
            if "sign_in" in current_url or "login" in current_url:
                if i % 10 == 0 and i > 0:
                    print(f"  ⏳ 验证登录状态中... ({i}s) URL: {current_url}")
                continue

            # 检查页面上是否有聊天界面的特征元素
            is_chat_page = await self.page.evaluate("""
                () => {
                    // 检查多种聊天页面的特征
                    const indicators = [
                        'textarea',                          // 输入框
                        '[contenteditable="true"]',          // 可编辑区域
                        '[class*="chat"]',                   // 聊天相关类
                        '[class*="sidebar"]',                // 侧边栏
                        '[class*="conversation"]',           // 对话列表
                        '#chat-input',                       // 聊天输入
                    ];
                    
                    for (const sel of indicators) {
                        const el = document.querySelector(sel);
                        if (el) return true;
                    }
                    return false;
                }
            """)

            if is_chat_page:
                print(f"  ✅ 已进入聊天页面（等待了 {i + 1} 秒）")
                return True

            # URL 已离开登录页，但还在加载
            if "chat.deepseek.com" in current_url:
                if i > 10:
                    print(f"  ✅ URL 已是 DeepSeek 主域名: {current_url}")
                    return True

        # 最后检查一次 URL
        final_url = self.page.url
        if "sign_in" not in final_url and "login" not in final_url:
            return True

        return False
