# app.py
"""
主服务器：FastAPI + WebSocket 代理
外部请求通过 HTTP/WebSocket 进入，由内部浏览器在已认证的上下文中执行。
支持通过环境变量 API_SECRET_KEY 设置 API 密钥鉴权。
"""

import os
import sys
import json
import asyncio
import signal
import time
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import uvicorn

from browser_manager import BrowserManager
from keepalive import KeepaliveService

# ============================================================
# 全局实例 & 配置
# ============================================================
browser_mgr: BrowserManager = None
keepalive_svc: KeepaliveService = None

# API 密钥鉴权：从环境变量读取，默认值 zxcvbnm
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "zxcvbnm")
security = HTTPBearer(auto_error=False)


def verify_api_key(request: Request):
    """
    验证 API 密钥。支持三种传递方式：
    1. Authorization: Bearer <key>
    2. X-API-Key: <key>
    3. 查询参数 ?api_key=<key>
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token == API_SECRET_KEY:
            return True

    x_api_key = request.headers.get("x-api-key", "").strip()
    if x_api_key == API_SECRET_KEY:
        return True

    api_key_param = request.query_params.get("api_key", "").strip()
    if api_key_param == API_SECRET_KEY:
        return True

    raise HTTPException(
        status_code=401,
        detail={
            "error": {
                "message": "Invalid API key. Please provide a valid key via "
                           "'Authorization: Bearer <key>' header, "
                           "'X-API-Key: <key>' header, or '?api_key=<key>' query param.",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
    )


# ============================================================
# 等待浏览器就绪的辅助函数
# ============================================================
async def ensure_browser_ready():
    """确保浏览器已初始化完成。初始化期间阻塞等待，超时返回503。"""
    if browser_mgr is None:
        raise HTTPException(status_code=503, detail="服务正在启动中，请稍后重试")

    if not browser_mgr.is_ready:
        print("  ⏳ 请求到达，等待浏览器初始化完成...")
        ok = await browser_mgr.wait_until_ready(timeout=180)
        if not ok:
            raise HTTPException(status_code=503, detail="浏览器初始化超时，请稍后重试")

    if not await browser_mgr.is_alive():
        raise HTTPException(status_code=503, detail="浏览器会话已断开")


# ============================================================
# FastAPI 生命周期管理
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时先让服务器就绪，再在后台初始化浏览器。"""
    global browser_mgr, keepalive_svc

    print(f"\n{'='*60}")
    print(f"  应用启动 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  API_SECRET_KEY: {API_SECRET_KEY[:3]}{'*' * (len(API_SECRET_KEY) - 3)}")
    print(f"{'='*60}\n")

    browser_mgr = BrowserManager()
    keepalive_svc = KeepaliveService(browser_mgr)

    asyncio.create_task(initialize_background())

    print("🚀 服务已就绪（浏览器后台初始化中），等待请求...\n")
    yield

    print("\n⏹️  正在关闭服务...")
    if keepalive_svc:
        await keepalive_svc.stop()
    if browser_mgr:
        await browser_mgr.shutdown()
    print("✅ 服务已安全关闭。")


async def initialize_background():
    """后台初始化浏览器，完成后启动心跳服务。"""
    global browser_mgr, keepalive_svc
    print("⏳ 后台任务：开始初始化浏览器...")
    try:
        await browser_mgr.initialize()
        print("✅ 后台任务：浏览器初始化完成。")

        if keepalive_svc and not keepalive_svc.is_running:
            await keepalive_svc.start()
    except Exception as e:
        print(f"❌ 后台任务：浏览器初始化失败: {e}")


app = FastAPI(title="DeepSeek Proxy", lifespan=lifespan)


# ============================================================
# 健康检查 & 状态页（不需要鉴权）
# ============================================================
@app.get("/")
async def index():
    """状态仪表盘页面。"""
    status = await browser_mgr.get_status() if browser_mgr else {"status": "initializing"}
    uptime = status.get("uptime_seconds", 0)
    hours, remainder = divmod(int(uptime), 3600)
    minutes, seconds = divmod(remainder, 60)

    ready = status.get("ready", False)
    alive = status.get("browser_alive", False)

    if ready and alive:
        status_text = "运行中"
        dot_color = "#3fb950"
    elif not ready:
        status_text = "初始化中..."
        dot_color = "#f0ad4e"
    else:
        status_text = "离线"
        dot_color = "#f85149"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>DeepSeek Proxy</title>
        <meta http-equiv="refresh" content="10">
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; 
                   display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
            .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; 
                     padding: 40px; max-width: 500px; width: 90%; }}
            h1 {{ color: #58a6ff; margin-top: 0; }}
            .status {{ display: flex; align-items: center; gap: 10px; margin: 20px 0; }}
            .dot {{ width: 12px; height: 12px; border-radius: 50%; background: {dot_color}; }}
            .info {{ background: #21262d; padding: 15px; border-radius: 8px; margin: 10px 0; 
                     font-family: monospace; font-size: 14px; }}
            .label {{ color: #8b949e; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🤖 DeepSeek Proxy</h1>
            <div class="status">
                <div class="dot"></div>
                <span>{status_text}</span>
            </div>
            <div class="info">
                <div><span class="label">运行时间：</span>{hours}h {minutes}m {seconds}s</div>
                <div><span class="label">就绪状态：</span>{"✅ 就绪" if ready else "⏳ 初始化中"}</div>
                <div><span class="label">登录状态：</span>{"✅ 已登录" if status.get("logged_in") else "❌ 未登录"}</div>
                <div><span class="label">引擎：</span>{status.get("engine", "N/A")}</div>
                <div><span class="label">Token：</span>{"✅ 有" if status.get("has_token") else "❌ 无"}</div>
                <div><span class="label">心跳次数：</span>{status.get("heartbeat_count", 0)}</div>
                <div><span class="label">处理请求：</span>{status.get("requests_handled", 0)}</div>
                <div><span class="label">API鉴权：</span>✅ 已启用</div>
            </div>
            <p style="color: #8b949e; font-size: 12px;">
                POST /v1/chat/completions 发送聊天请求（需要 API Key）<br>
                WS /ws 建立 WebSocket 连接（需要 API Key）<br>
                <br>
                鉴权方式：Authorization: Bearer &lt;your-api-key&gt;
            </p>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/health")
async def health():
    """健康检查端点。"""
    if browser_mgr and browser_mgr.is_ready:
        return {"status": "ok", "ready": True}
    return {"status": "initializing", "ready": False}


@app.get("/status")
async def status():
    """详细状态端点。"""
    if browser_mgr:
        return await browser_mgr.get_status()
    return {"status": "initializing"}


@app.get("/screenshot")
async def screenshot():
    """获取当前浏览器截图（Base64），用于调试。"""
    if not browser_mgr:
        raise HTTPException(status_code=503, detail="浏览器未就绪")
    img_base64 = await browser_mgr.take_screenshot_base64()
    if img_base64:
        html = f"""
        <html><body style="background:#000;display:flex;justify-content:center;padding:20px;">
        <img src="data:image/png;base64,{img_base64}" style="max-width:100%;border:1px solid #333;"/>
        </body></html>
        """
        return HTMLResponse(content=html)
    raise HTTPException(status_code=500, detail="截图失败")


# ============================================================
# OpenAI 兼容端点：列出模型（需要鉴权）
# ============================================================
@app.get("/v1/models")
async def list_models(request: Request):
    """列出可用模型，兼容 OpenAI API 格式。"""
    verify_api_key(request)
    return {
        "object": "list",
        "data": [
            {
                "id": "deepseek-chat",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "deepseek-proxy",
            }
        ],
    }


# ============================================================
# 辅助函数：将 messages 数组拼接为完整的上下文字符串
# ============================================================
def build_prompt_from_messages(messages: list) -> str:
    """将 OpenAI 格式的 messages 数组拼接为单个 prompt 字符串。"""
    prompt_parts = []
    prompt_parts.append("请根据以下对话历史和最后一个用户对话，生成对应的回复。")

    for message in messages:
        role = message.get("role", "")
        content = message.get("content", "")

        processed_content = ""
        if isinstance(content, str):
            processed_content = content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    processed_content += part.get("text", "")

        if role and processed_content:
            prompt_parts.append(f"角色: {role}\n内容: {processed_content}")

    return "\n\n---\n\n".join(prompt_parts)


# ============================================================
# 核心 API：兼容 OpenAI 格式的聊天接口（需要鉴权）
# ============================================================
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """兼容 OpenAI API 格式的聊天端点。需要 API Key 鉴权。"""
    # 鉴权
    verify_api_key(request)

    # ★ 关键改动：等待浏览器就绪，不再直接 503
    await ensure_browser_ready()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    stream = body.get("stream", False)

    user_prompt = build_prompt_from_messages(messages)

    if not user_prompt:
        raise HTTPException(status_code=400, detail="未找到有效的用户输入")

    print(f"📝 构建的 prompt 长度: {len(user_prompt)} 字符")

    if stream:
        async def generate():
            async for chunk in browser_mgr.send_message_stream(user_prompt):
                data = {
                    "id": f"chatcmpl-{int(time.time()*1000)}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "deepseek-chat",
                    "choices": [{
                        "index": 0,
                        "delta": {"content": chunk},
                        "finish_reason": None
                    }]
                }
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

            end_data = {
                "id": f"chatcmpl-{int(time.time()*1000)}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "deepseek-chat",
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")
    else:
        response_text = await browser_mgr.send_message(user_prompt)
        return {
            "id": f"chatcmpl-{int(time.time()*1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "deepseek-chat",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": len(user_prompt),
                "completion_tokens": len(response_text),
                "total_tokens": len(user_prompt) + len(response_text)
            }
        }


# ============================================================
# WebSocket 端点（需要鉴权）
# ============================================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 连接。"""
    await websocket.accept()
    print(f"📡 WebSocket 客户端已连接: {websocket.client}")

    api_key_param = websocket.query_params.get("api_key", "").strip()
    authenticated = (api_key_param == API_SECRET_KEY)

    if not authenticated:
        try:
            first_msg = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            try:
                payload = json.loads(first_msg)
                if payload.get("type") == "auth" and payload.get("api_key") == API_SECRET_KEY:
                    authenticated = True
                    await websocket.send_json({"type": "auth", "status": "ok"})
                elif payload.get("api_key") == API_SECRET_KEY:
                    authenticated = True
                    await websocket.send_json({"type": "auth", "status": "ok"})
            except json.JSONDecodeError:
                pass
        except asyncio.TimeoutError:
            pass

    if not authenticated:
        await websocket.send_json({
            "type": "error",
            "error": "Authentication required. Send {\"type\":\"auth\",\"api_key\":\"<key>\"} "
                     "or connect with ?api_key=<key>"
        })
        await websocket.close(code=4001, reason="Unauthorized")
        return

    try:
        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
                message = payload.get("message", "")
            except json.JSONDecodeError:
                message = data

            if not message:
                await websocket.send_json({"error": "消息不能为空"})
                continue

            # WebSocket 也等待就绪
            if not browser_mgr or not browser_mgr.is_ready:
                await websocket.send_json({"type": "info", "message": "浏览器初始化中，请稍候..."})
                ok = await browser_mgr.wait_until_ready(timeout=180) if browser_mgr else False
                if not ok:
                    await websocket.send_json({"error": "浏览器初始化超时"})
                    continue

            if not await browser_mgr.is_alive():
                await websocket.send_json({"error": "浏览器会话已断开"})
                continue

            await websocket.send_json({"type": "start"})
            full_response = ""
            async for chunk in browser_mgr.send_message_stream(message):
                full_response += chunk
                await websocket.send_json({"type": "chunk", "content": chunk})
            await websocket.send_json({"type": "end", "full_content": full_response})

    except WebSocketDisconnect:
        print(f"📡 WebSocket 客户端已断开: {websocket.client}")
    except Exception as e:
        print(f"❌ WebSocket 错误: {e}")
        try:
            await websocket.send_json({"error": str(e)})
        except Exception:
            pass


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
