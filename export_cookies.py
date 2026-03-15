# export_cookies.py
"""
本地 Cookie 导出工具：
1. 启动一个真实的浏览器窗口（有界面）
2. 你手动登录 DeepSeek
3. 登录成功后自动导出 Cookie 到文件 / 打印为环境变量
"""

import asyncio
import json
import sys
from pathlib import Path


async def main():
    from playwright.async_api import async_playwright

    print("=" * 60)
    print("  DeepSeek Cookie 导出工具")
    print("=" * 60)
    print()
    print("即将打开浏览器，请在浏览器中手动完成以下操作：")
    print("  1. 登录你的 DeepSeek 账号")
    print("  2. 确保看到聊天主界面")
    print("  3. 回到这个终端按 Enter 键导出 Cookie")
    print()

    async with async_playwright() as p:
        # 启动一个有界面的浏览器（非 headless）
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # 导航到 DeepSeek 登录页
        await page.goto("https://chat.deepseek.com/sign_in", wait_until="domcontentloaded")
        print("✅ 浏览器已打开，请手动登录...")
        print()

        # 等待用户手动登录
        # 方式1：等待 URL 变化（离开登录页）
        print("⏳ 正在等待你完成登录（URL 离开 sign_in 页面）...")
        print("   如果自动检测不到，登录完成后回到终端按 Enter 即可。")
        print()

        # 同时监听：URL变化 或 用户按 Enter
        logged_in = False
        for i in range(600):  # 最多等 10 分钟
            await asyncio.sleep(1)
            current_url = page.url
            if ("sign_in" not in current_url and "login" not in current_url 
                and "chat.deepseek.com" in current_url):
                print(f"🎉 检测到登录成功！当前页面: {current_url}")
                logged_in = True
                break
            if i % 30 == 0 and i > 0:
                print(f"  ⏳ 已等待 {i} 秒，请继续在浏览器中操作...")

        if not logged_in:
            input("\n请在浏览器中完成登录后，按 Enter 键继续...")

        # 等待页面稳定
        await asyncio.sleep(3)

        # 导出 Cookie
        cookies = await context.cookies()
        print(f"\n📦 共获取到 {len(cookies)} 个 Cookie")

        # 过滤出 deepseek 相关的 cookie
        ds_cookies = [c for c in cookies if "deepseek" in c.get("domain", "")]
        print(f"🎯 其中 DeepSeek 相关: {len(ds_cookies)} 个")

        if not ds_cookies:
            print("⚠️ 未找到 DeepSeek 的 Cookie，使用所有 Cookie")
            ds_cookies = cookies

        # 同时获取 localStorage 中的 token（DeepSeek 可能把 token 存在这里）
        local_storage_data = await page.evaluate("""
            () => {
                const data = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    data[key] = localStorage.getItem(key);
                }
                return data;
            }
        """)

        # 获取 sessionStorage
        session_storage_data = await page.evaluate("""
            () => {
                const data = {};
                for (let i = 0; i < sessionStorage.length; i++) {
                    const key = sessionStorage.key(i);
                    data[key] = sessionStorage.getItem(key);
                }
                return data;
            }
        """)

        # 组合完整的认证数据
        auth_data = {
            "cookies": ds_cookies,
            "local_storage": local_storage_data,
            "session_storage": session_storage_data,
            "url_after_login": page.url,
        }

        # 保存到文件
        output_file = Path("deepseek_cookies.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(auth_data, f, ensure_ascii=False, indent=2)
        print(f"\n✅ Cookie 已保存到: {output_file.absolute()}")

        # 同时生成环境变量格式（用于 HuggingFace Secrets）
        cookies_json_str = json.dumps(ds_cookies, ensure_ascii=False)
        storage_json_str = json.dumps(local_storage_data, ensure_ascii=False)

        print("\n" + "=" * 60)
        print("  以下是环境变量格式，可直接粘贴到 HuggingFace Secrets")
        print("=" * 60)
        print(f"\nDEEPSEEK_COOKIES={cookies_json_str}")
        print(f"\nDEEPSEEK_LOCAL_STORAGE={storage_json_str}")
        print("\n" + "=" * 60)

        # 显示关键 Cookie 信息
        print("\n🔍 关键 Cookie 列表：")
        for c in ds_cookies:
            expires_info = ""
            if c.get("expires", -1) > 0:
                import datetime
                try:
                    exp_time = datetime.datetime.fromtimestamp(c["expires"])
                    expires_info = f" (过期: {exp_time.strftime('%Y-%m-%d %H:%M')})"
                except Exception:
                    expires_info = f" (expires: {c['expires']})"
            print(f"  • {c['name']}: {c['value'][:40]}...{expires_info}")

        print("\n🔍 localStorage 关键项：")
        for key, value in local_storage_data.items():
            if any(kw in key.lower() for kw in ["token", "auth", "user", "session", "login"]):
                preview = str(value)[:80]
                print(f"  • {key}: {preview}...")

        await browser.close()
        print("\n✅ 浏览器已关闭。Cookie 导出完成！")


if __name__ == "__main__":
    # 先确保安装了 playwright 和 chromium
    try:
        import playwright
    except ImportError:
        print("请先安装: pip install playwright")
        print("然后运行: python -m playwright install chromium")
        sys.exit(1)

    asyncio.run(main())
