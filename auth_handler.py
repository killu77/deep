"""
认证处理器：
负责在 DeepSeek 网页上完成登录流程，包括处理 Cookie 弹窗和 Cloudflare 验证。
"""

import asyncio
import base64
from typing import Optional


class AuthHandler:
    def __init__(self, page):
        self.page = page

    async def _log_screenshot(self, label: str):
        """截图并打印 Base64 到日志中。"""
        try:
            screenshot_bytes = await self.page.screenshot()
            b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
            print(f"\n--- SCREENSHOT: {label} ---")
            print(f"Base64 长度: {len(b64)} 字符")
            # 只打印前200个字符作为标识，完整截图通过 /screenshot 端点查看
            print(f"预览: {b64[:200]}...")
            print(f"--- END SCREENSHOT: {label} ---\n")
        except Exception as e:
            print(f"⚠️ 截图失败 ({label}): {e}")

    async def _handle_cookie_banner(self) -> bool:
        """处理 Cookie 同意弹窗。"""
        print("  🍪 检查 Cookie 弹窗...")

        try:
            # 根据你提供的真实 HTML，使用精确的 class 选择器
            # class="cookie_banner-accept-all-button"
            cookie_btn = self.page.locator("div.cookie_banner-accept-all-button")

            # 等待最多 8 秒
            await cookie_btn.wait_for(state="visible", timeout=8000)

            # 打印找到的按钮信息
            btn_text = await cookie_btn.text_content()
            print(f"  ✅ 找到 Cookie 按钮，文本: '{btn_text}'")
            
            # 截图：点击前
            await self._log_screenshot("Cookie弹窗_点击前")

            await cookie_btn.click()
            print("  ✅ Cookie 弹窗已关闭。")
            await asyncio.sleep(1)
            
            # 截图：点击后
            await self._log_screenshot("Cookie弹窗_点击后")
            return True

        except Exception as e:
            print(f"  ℹ️ 未发现 Cookie 弹窗或已自动关闭: {e}")
            return False

    async def _wait_for_cloudflare(self, timeout: int = 30) -> bool:
        """
        等待 Cloudflare Turnstile 验证完成。
        Camoufox 的反指纹特性应该能让 Turnstile 自动通过。
        """
        print("  🛡️ 检查 Cloudflare 验证...")

        for i in range(timeout):
            # 检查 Turnstile overlay 是否可见
            is_cf_visible = await self.page.evaluate("""
                () => {
                    const overlay = document.getElementById('cf-overlay');
                    if (!overlay) return false;
                    return overlay.style.display !== 'none';
                }
            """)

            if not is_cf_visible:
                print(f"  ✅ Cloudflare 验证已通过（或不存在）。")
                return True

            if i % 5 == 0:
                print(f"  ⏳ 等待 Cloudflare 验证... ({i}/{timeout}s)")

            # 检查是否有 iframe 需要交互
            cf_frames = self.page.frames
            for frame in cf_frames:
                if "challenges.cloudflare.com" in frame.url:
                    print(f"  🔍 发现 Cloudflare iframe: {frame.url[:80]}...")
                    # Camoufox 应该能自动处理，但如果需要可以尝试点击
                    try:
                        checkbox = frame.locator("input[type='checkbox'], .cb-i")
                        if await checkbox.is_visible(timeout=1000):
                            await checkbox.click()
                            print("  ☑️ 已点击 Cloudflare checkbox。")
                    except Exception:
                        pass

            await asyncio.sleep(1)

        print("  ⚠️ Cloudflare 验证超时，继续尝试...")
        await self._log_screenshot("Cloudflare超时")
        return False

    async def login(self, email: str, password: str) -> bool:
        """
        完整的登录流程：
        1. 访问登录页
        2. 处理 Cookie 弹窗
        3. 等待 Cloudflare 验证
        4. 输入凭据并提交
        5. 验证登录状态
        """
        if not email or not password:
            print("❌ 未设置 DEEPSEEK_EMAIL 或 DEEPSEEK_PASSWORD！")
            return False

        print("\n📋 开始登录流程...")
        print(f"  📧 邮箱: {email[:3]}***{email[email.index('@'):]}")

        try:
            # 第 1 步：访问登录页
            print("\n  [1/5] 访问登录页面...")
            await self.page.goto(
                "https://chat.deepseek.com/sign_in",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(3)  # 等待 JS 执行
            await self._log_screenshot("1_页面加载完成")

            # 打印页面标题和 URL
            title = await self.page.title()
            print(f"  📄 页面标题: {title}")
            print(f"  🔗 当前 URL: {self.page.url}")

            # 第 2 步：处理 Cookie 弹窗
            print("\n  [2/5] 处理 Cookie 弹窗...")
            await self._handle_cookie_banner()

            # 第 3 步：等待 Cloudflare
            print("\n  [3/5] 等待 Cloudflare 验证...")
            await self._wait_for_cloudflare(timeout=30)
            await self._log_screenshot("3_Cloudflare验证后")

            # 第 4 步：输入凭据
            print("\n  [4/5] 输入登录凭据...")

            # 等待邮箱输入框出现（使用多种可能的选择器）
            email_input = self.page.locator(
                "input[name='email'], "
                "input[type='email'], "
                "input[placeholder*='email'], "
                "input[placeholder*='邮箱'], "
                "input[placeholder*='Email']"
            ).first
            await email_input.wait_for(state="visible", timeout=15000)
            await email_input.click()
            await email_input.fill(email)
            print("  ✅ 邮箱已输入。")
            await asyncio.sleep(0.5)

            # 输入密码
            pwd_input = self.page.locator(
                "input[name='password'], "
                "input[type='password'], "
                "input[placeholder*='password'], "
                "input[placeholder*='密码'], "
                "input[placeholder*='Password']"
            ).first
            await pwd_input.wait_for(state="visible", timeout=5000)
            await pwd_input.click()
            await pwd_input.fill(password)
            print("  ✅ 密码已输入。")

            await self._log_screenshot("4_凭据已输入")

            # 第 5 步：点击登录按钮
            print("\n  [5/5] 点击登录按钮...")
            login_btn = self.page.locator(
                "button:has-text('Sign in'), "
                "button:has-text('Log in'), "
                "button:has-text('登录'), "
                "div[role='button']:has-text('Sign in'), "
                "div[role='button']:has-text('登录')"
            ).first
            await login_btn.wait_for(state="visible", timeout=5000)
            await login_btn.click()
            print("  ✅ 登录按钮已点击。")

            # 等待登录完成
            print("  ⏳ 等待登录完成...")
            await asyncio.sleep(5)

            # 验证登录状态
            current_url = self.page.url
            print(f"  🔗 当前 URL: {current_url}")
            await self._log_screenshot("5_登录后")

            # 判断是否成功（登录成功后通常会跳转到主页）
            if "sign_in" not in current_url and "login" not in current_url:
                print("\n  🎉🎉🎉 登录成功！")
                return True

            # 再等一会，可能有跳转延迟
            try:
                await self.page.wait_for_url(
                    "**/chat.deepseek.com/**",
                    timeout=15000,
                )
                print("\n  🎉🎉🎉 登录成功（重定向完成）！")
                await self._log_screenshot("5_登录成功_最终")
                return True
            except Exception:
                print("\n  ⚠️ 无法确认登录状态。")
                await self._log_screenshot("5_登录状态未知")
                # 即使不确定，也返回 True 让程序继续运行
                # 用户可以通过 /screenshot 端点检查实际状态
                return True

        except Exception as e:
            print(f"\n  ❌ 登录失败: {e}")
            await self._log_screenshot("99_登录失败")

            # 打印页面源码的关键部分用于调试
            try:
                page_content = await self.page.content()
                # 只打印 body 的前 2000 个字符
                body_start = page_content.find("<body")
                if body_start > -1:
                    print(f"\n--- 页面源码片段 ---")
                    print(page_content[body_start:body_start + 2000])
                    print(f"--- 源码片段结束 ---\n")
            except Exception:
                pass

            return False
