"""
水利智审 - 云端API代理服务 v1.0
=====================================
架构：桌面版 → 云端代理 → DeepSeek/通义千问等AI服务
功能：
  - 设备注册与鉴权（设备ID + 密钥）
  - API请求代理转发（OpenAI兼容格式）
  - 管理员API配置集中管理
  - 设备管理（查看/启用/禁用）
  - 使用统计与日志
  - 更新通知推送
  - 设备心跳监控
"""

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import httpx
import json
import uuid
import secrets
import time
import os
from datetime import datetime, timedelta
from pathlib import Path

app = FastAPI(title="水利智审云端API代理", version="1.0.0")

# CORS - 允许所有来源（桌面版和网页版都可能在不同域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ 数据目录 ============
DATA_DIR = Path(os.environ.get("PROXY_DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)

DEVICES_FILE = DATA_DIR / "devices.json"
CONFIG_FILE = DATA_DIR / "admin_config.json"
USAGE_LOG_FILE = DATA_DIR / "usage_log.json"
NOTIFY_FILE = DATA_DIR / "notifications.json"
ADMIN_AUTH_FILE = DATA_DIR / "admin_auth.json"

# ============ 数据读写 ============
def load_json(filepath: Path, default=None):
    if default is None:
        default = {}
    try:
        if filepath.exists():
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    return json.loads(content)
    except Exception as e:
        print(f"[ERROR] load_json {filepath}: {e}")
    return default

def save_json(filepath: Path, data):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] save_json {filepath}: {e}")

def load_devices() -> dict:
    return load_json(DEVICES_FILE, {})

def save_devices(devices: dict):
    save_json(DEVICES_FILE, devices)

def load_admin_config() -> dict:
    default_config = {
        "text_api_base_url": "https://api.deepseek.com",
        "text_api_key": "",
        "text_model": "deepseek-chat",
        "vision_api_base_url": "https://dashscope.aliyuncs.com/compatible-mode",
        "vision_api_key": "",
        "vision_model": "qwen-vl-plus"
    }
    
    # Render部署时：环境变量只在文件不存在（首次启动）时写入
    if not CONFIG_FILE.exists():
        env_config = {
            "text_api_base_url": os.environ.get("TEXT_API_BASE_URL", "https://api.deepseek.com"),
            "text_api_key": os.environ.get("TEXT_API_KEY", ""),
            "text_model": os.environ.get("TEXT_MODEL", "deepseek-chat"),
            "vision_api_base_url": os.environ.get("VISION_API_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode"),
            "vision_api_key": os.environ.get("VISION_API_KEY", ""),
            "vision_model": os.environ.get("VISION_MODEL", "qwen-vl-plus")
        }
        # 合并到默认配置
        for k, v in env_config.items():
            if v:
                default_config[k] = v
        print(f"[初始化] 从环境变量加载API配置，文本API: {'已配置' if default_config.get('text_api_key') else '未配置'}, 视觉API: {'已配置' if default_config.get('vision_api_key') else '未配置'}")
    
    cfg = load_json(CONFIG_FILE, default_config)
    # 补齐缺失字段
    for k, v in default_config.items():
        if k not in cfg:
            cfg[k] = v
    return cfg

def save_admin_config(config: dict):
    save_json(CONFIG_FILE, config)

def load_usage_log() -> list:
    return load_json(USAGE_LOG_FILE, [])

def save_usage_log(logs: list):
    # 只保留最近10000条
    if len(logs) > 10000:
        logs = logs[-10000:]
    save_json(USAGE_LOG_FILE, logs)

def load_notifications() -> dict:
    return load_json(NOTIFY_FILE, {})

def save_notifications(notifications: dict):
    save_json(NOTIFY_FILE, notifications)

def load_admin_auth() -> dict:
    return load_json(ADMIN_AUTH_FILE, {})

def save_admin_auth(auth: dict):
    save_json(ADMIN_AUTH_FILE, auth)


# ============ 工具函数 ============
def generate_device_id() -> str:
    return f"device_{uuid.uuid4().hex[:16]}"

def generate_device_secret() -> str:
    return secrets.token_urlsafe(32)

def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log_usage(device_id: str, model: str, api_type: str, 
              prompt_tokens: int = 0, completion_tokens: int = 0,
              total_tokens: int = 0, status: str = "success", error_msg: str = ""):
    logs = load_usage_log()
    logs.append({
        "device_id": device_id,
        "timestamp": now_iso(),
        "model": model,
        "api_type": api_type,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
        "status": status,
        "error_msg": error_msg
    })
    save_usage_log(logs)


# ============ 设备认证依赖 ============
async def verify_device(request: Request) -> dict:
    """验证设备身份"""
    device_id = request.headers.get("X-Device-Id", "")
    device_secret = request.headers.get("X-Device-Secret", "")
    
    if not device_id or not device_secret:
        raise HTTPException(status_code=401, detail="缺少设备认证信息（X-Device-Id / X-Device-Secret）")
    
    devices = load_devices()
    if device_id not in devices:
        raise HTTPException(status_code=401, detail="设备未注册")
    
    device = devices[device_id]
    if device.get("secret") != device_secret:
        raise HTTPException(status_code=401, detail="设备密钥错误")
    
    if not device.get("enabled", True):
        raise HTTPException(status_code=403, detail="此设备的API权限已被管理员关闭")
    
    # 更新最后活跃时间
    device["last_heartbeat"] = now_iso()
    devices[device_id] = device
    save_devices(devices)
    
    return device


# ============ 管理员认证依赖 ============
async def verify_admin(request: Request) -> bool:
    """验证管理员身份"""
    admin_token = request.headers.get("X-Admin-Token", "")
    auth = load_admin_auth()
    expected_token = auth.get("admin_token", "")
    
    if not expected_token:
        # 未设置管理员token，允许访问（首次初始化）
        return True
    
    if admin_token != expected_token:
        raise HTTPException(status_code=403, detail="管理员认证失败")
    
    return True


# ============ 管理员初始化 ============
@app.on_event("startup")
async def startup():
    """服务启动时检查管理员初始化"""
    auth = load_admin_auth()
    
    # Render部署时：从环境变量读取 ADMIN_TOKEN（只在首次启动时生效）
    if not auth.get("admin_token"):
        env_admin_token = os.environ.get("ADMIN_TOKEN", "")
        if env_admin_token:
            auth["admin_token"] = env_admin_token
            auth["created_at"] = now_iso()
            auth["source"] = "environment"
            save_admin_auth(auth)
            print(f"[初始化] 从环境变量加载管理员Token")
        else:
            # 自动生成管理员token
            token = secrets.token_urlsafe(32)
            auth["admin_token"] = token
            auth["created_at"] = now_iso()
            save_admin_auth(auth)
            print(f"\n{'='*60}")
            print(f"  水利智审云端API代理 - 首次启动")
            print(f"  管理员Token（请妥善保存）: {token}")
            print(f"{'='*60}\n")
    
    # 打印服务状态
    devices = load_devices()
    config = load_admin_config()
    print(f"[启动] 已注册设备: {len(devices)} 台")
    print(f"[启动] 文本API: {'已配置' if config.get('text_api_key') else '未配置'}")
    print(f"[启动] 视觉API: {'已配置' if config.get('vision_api_key') else '未配置'}")


# ===================================================================
#                           设备端接口
# ===================================================================

class RegisterRequest(BaseModel):
    device_name: Optional[str] = ""
    version: Optional[str] = ""

@app.post("/api/device/register")
async def register_device(req: RegisterRequest):
    """设备首次注册"""
    device_id = generate_device_id()
    device_secret = generate_device_secret()
    
    devices = load_devices()
    devices[device_id] = {
        "name": req.device_name or f"设备-{device_id[-6:]}",
        "secret": device_secret,
        "registered_at": now_iso(),
        "last_heartbeat": now_iso(),
        "enabled": True,
        "total_calls": 0,
        "total_tokens": 0,
        "version": req.version or "",
        "ip_address": "",
        "platform": ""
    }
    save_devices(devices)
    
    print(f"[注册] 新设备: {devices[device_id]['name']} ({device_id})")
    
    return {
        "device_id": device_id,
        "device_secret": device_secret,
        "message": "设备注册成功"
    }


@app.post("/api/device/heartbeat")
async def device_heartbeat(request: Request, device: dict = Depends(verify_device)):
    """设备心跳"""
    return {"status": "ok", "time": now_iso()}


@app.get("/api/device/config")
async def get_device_config(device: dict = Depends(verify_device)):
    """获取设备AI配置（模型名称等，不暴露API Key）"""
    config = load_admin_config()
    return {
        "text_model": config.get("text_model", ""),
        "vision_model": config.get("vision_model", ""),
        "proxy_version": "1.0.0"
    }


@app.get("/api/device/check-notify")
async def check_notifications(device: dict = Depends(verify_device)):
    """桌面版检查是否有新的管理员通知"""
    device_id = device.get("device_id", "")
    notifications = load_notifications()
    
    if not notifications:
        return {"has_notification": False, "notifications": []}
    
    # 找出此设备未读的通知
    device_seen = device.get("seen_notifications", [])
    new_notifies = []
    
    for nid, notify in notifications.items():
        if nid not in device_seen:
            new_notifies.append({
                "id": nid,
                "title": notify.get("title", ""),
                "message": notify.get("message", ""),
                "type": notify.get("type", "info"),
                "timestamp": notify.get("timestamp", ""),
                "data": notify.get("data", {})
            })
    
    return {
        "has_notification": len(new_notifies) > 0,
        "notifications": new_notifies
    }


@app.post("/api/device/mark-read")
async def mark_notification_read(request: Request, device: dict = Depends(verify_device)):
    """标记通知为已读"""
    body = await request.json()
    notification_id = body.get("notification_id", "")
    
    device_id = None
    devices = load_devices()
    for did, dev in devices.items():
        if dev.get("secret") == device.get("secret"):
            device_id = did
            break
    
    if device_id and notification_id:
        seen = devices[device_id].get("seen_notifications", [])
        if notification_id not in seen:
            seen.append(notification_id)
            devices[device_id]["seen_notifications"] = seen
            save_devices(devices)
    
    return {"status": "ok"}


# ===================================================================
#                          API代理接口
# ===================================================================

@app.post("/api/proxy/chat/completions")
async def proxy_chat_completions(request: Request, device: dict = Depends(verify_device)):
    """
    代理文本AI请求
    桌面版发送OpenAI兼容格式的请求，代理转发到实际的AI服务
    """
    body = await request.json()
    model = body.get("model", "")
    device_id = None
    
    # 找到device_id
    devices = load_devices()
    for did, dev in devices.items():
        if dev.get("secret") == device.get("secret"):
            device_id = did
            break
    
    if not device_id:
        raise HTTPException(status_code=401, detail="设备认证异常")
    
    config = load_admin_config()
    
    # 确定目标API
    api_base_url = config.get("text_api_base_url", "https://api.deepseek.com").rstrip("/")
    api_key = config.get("text_api_key", "")
    target_model = model or config.get("text_model", "deepseek-chat")
    
    if not api_key:
        log_usage(device_id, target_model, "text", status="error", error_msg="管理员未配置文本API密钥")
        raise HTTPException(status_code=500, detail="管理员未配置API密钥，请联系管理员")
    
    # 构建目标URL
    if "openai" in api_base_url:
        target_url = f"{api_base_url}/v1/chat/completions"
    else:
        if not api_base_url.endswith("/v1"):
            api_base_url = api_base_url + "/v1"
        target_url = f"{api_base_url}/chat/completions"
    
    # 替换模型名称
    body["model"] = target_model
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    # 转发请求
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(target_url, headers=headers, json=body)
            
            if resp.status_code == 200:
                result = resp.json()
                
                # 记录token使用
                usage = result.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)
                
                log_usage(device_id, target_model, "text",
                         prompt_tokens=prompt_tokens,
                         completion_tokens=completion_tokens,
                         total_tokens=total_tokens)
                
                # 更新设备统计
                devices[device_id]["total_calls"] = devices[device_id].get("total_calls", 0) + 1
                devices[device_id]["total_tokens"] = devices[device_id].get("total_tokens", 0) + total_tokens
                save_devices(devices)
                
                return result
            else:
                error_msg = f"AI服务返回 {resp.status_code}: {resp.text[:200]}"
                log_usage(device_id, target_model, "text", status="error", error_msg=error_msg)
                return JSONResponse(status_code=resp.status_code, content=resp.json() if resp.headers.get("content-type","").startswith("application/json") else {"error": {"message": resp.text[:500]}})
    
    except httpx.TimeoutException:
        log_usage(device_id, target_model, "text", status="error", error_msg="请求超时")
        raise HTTPException(status_code=504, detail="AI服务请求超时，请稍后重试")
    except Exception as e:
        log_usage(device_id, target_model, "text", status="error", error_msg=str(e))
        raise HTTPException(status_code=500, detail=f"API代理错误: {str(e)}")


@app.post("/api/proxy/vision")
async def proxy_vision(request: Request, device: dict = Depends(verify_device)):
    """
    代理视觉AI请求
    支持多模态输入（图片+文字）
    """
    body = await request.json()
    device_id = None
    
    devices = load_devices()
    for did, dev in devices.items():
        if dev.get("secret") == device.get("secret"):
            device_id = did
            break
    
    if not device_id:
        raise HTTPException(status_code=401, detail="设备认证异常")
    
    config = load_admin_config()
    
    # 视觉API配置
    api_base_url = config.get("vision_api_base_url", "https://dashscope.aliyuncs.com/compatible-mode").rstrip("/")
    api_key = config.get("vision_api_key", "")
    target_model = body.get("model", "") or config.get("vision_model", "qwen-vl-plus")
    
    if not api_key:
        log_usage(device_id, target_model, "vision", status="error", error_msg="管理员未配置视觉API密钥")
        raise HTTPException(status_code=500, detail="管理员未配置视觉API密钥，请联系管理员")
    
    # 构建目标URL
    if not api_base_url.endswith("/v1"):
        api_base_url = api_base_url + "/v1"
    target_url = f"{api_base_url}/chat/completions"
    
    body["model"] = target_model
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(target_url, headers=headers, json=body)
            
            if resp.status_code == 200:
                result = resp.json()
                usage = result.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)
                
                log_usage(device_id, target_model, "vision",
                         prompt_tokens=prompt_tokens,
                         completion_tokens=completion_tokens,
                         total_tokens=total_tokens)
                
                devices[device_id]["total_calls"] = devices[device_id].get("total_calls", 0) + 1
                devices[device_id]["total_tokens"] = devices[device_id].get("total_tokens", 0) + total_tokens
                save_devices(devices)
                
                return result
            else:
                error_msg = f"视觉AI返回 {resp.status_code}: {resp.text[:200]}"
                log_usage(device_id, target_model, "vision", status="error", error_msg=error_msg)
                return JSONResponse(status_code=resp.status_code, content=resp.json() if resp.headers.get("content-type","").startswith("application/json") else {"error": {"message": resp.text[:500]}})
    
    except httpx.TimeoutException:
        log_usage(device_id, target_model, "vision", status="error", error_msg="视觉请求超时")
        raise HTTPException(status_code=504, detail="视觉AI请求超时，请稍后重试")
    except Exception as e:
        log_usage(device_id, target_model, "vision", status="error", error_msg=str(e))
        raise HTTPException(status_code=500, detail=f"视觉API代理错误: {str(e)}")


# ===================================================================
#                          管理员接口
# ===================================================================

class AdminConfigRequest(BaseModel):
    text_api_base_url: Optional[str] = ""
    text_api_key: Optional[str] = ""
    text_model: Optional[str] = ""
    vision_api_base_url: Optional[str] = ""
    vision_api_key: Optional[str] = ""
    vision_model: Optional[str] = ""

@app.get("/api/admin/status")
async def admin_status(admin: bool = Depends(verify_admin)):
    """管理员状态检查"""
    devices = load_devices()
    config = load_admin_config()
    logs = load_usage_log()
    
    return {
        "total_devices": len(devices),
        "active_devices": sum(1 for d in devices.values() if d.get("enabled")),
        "text_api_configured": bool(config.get("text_api_key")),
        "vision_api_configured": bool(config.get("vision_api_key")),
        "total_api_calls": len(logs),
        "proxy_version": "1.0.0"
    }


@app.get("/api/admin/devices")
async def admin_list_devices(admin: bool = Depends(verify_admin)):
    """管理员查看所有设备"""
    devices = load_devices()
    logs = load_usage_log()
    
    device_list = []
    for did, dev in devices.items():
        # 统计今日调用
        today = datetime.now().strftime("%Y-%m-%d")
        today_calls = sum(1 for l in logs if l.get("device_id") == did and l.get("timestamp", "").startswith(today))
        
        device_list.append({
            "device_id": did,
            "name": dev.get("name", ""),
            "registered_at": dev.get("registered_at", ""),
            "last_heartbeat": dev.get("last_heartbeat", ""),
            "enabled": dev.get("enabled", True),
            "total_calls": dev.get("total_calls", 0),
            "total_tokens": dev.get("total_tokens", 0),
            "today_calls": today_calls,
            "version": dev.get("version", ""),
            "is_online": _is_device_online(dev)
        })
    
    # 按注册时间倒序
    device_list.sort(key=lambda x: x.get("registered_at", ""), reverse=True)
    
    return {"devices": device_list, "total": len(device_list)}


@app.put("/api/admin/devices/{device_id}/toggle")
async def admin_toggle_device(device_id: str, admin: bool = Depends(verify_admin)):
    """管理员启用/禁用设备"""
    devices = load_devices()
    if device_id not in devices:
        raise HTTPException(status_code=404, detail="设备不存在")
    
    devices[device_id]["enabled"] = not devices[device_id].get("enabled", True)
    save_devices(devices)
    
    status = "已启用" if devices[device_id]["enabled"] else "已禁用"
    print(f"[管理] 设备 {devices[device_id]['name']} {status}")
    
    return {
        "device_id": device_id,
        "enabled": devices[device_id]["enabled"],
        "message": f"设备{status}"
    }


@app.delete("/api/admin/devices/{device_id}")
async def admin_delete_device(device_id: str, admin: bool = Depends(verify_admin)):
    """管理员删除设备"""
    devices = load_devices()
    if device_id not in devices:
        raise HTTPException(status_code=404, detail="设备不存在")
    
    name = devices[device_id].get("name", device_id)
    del devices[device_id]
    save_devices(devices)
    
    print(f"[管理] 删除设备: {name} ({device_id})")
    
    return {"message": f"设备 {name} 已删除"}


@app.post("/api/admin/config")
async def admin_update_config(req: AdminConfigRequest, admin: bool = Depends(verify_admin)):
    """管理员更新API配置"""
    config = load_admin_config()
    
    if req.text_api_base_url:
        config["text_api_base_url"] = req.text_api_base_url
    if req.text_api_key:
        config["text_api_key"] = req.text_api_key
    if req.text_model:
        config["text_model"] = req.text_model
    if req.vision_api_base_url:
        config["vision_api_base_url"] = req.vision_api_base_url
    if req.vision_api_key:
        config["vision_api_key"] = req.vision_api_key
    if req.vision_model:
        config["vision_model"] = req.vision_model
    
    save_admin_config(config)
    print(f"[管理] API配置已更新")
    
    return {"message": "配置已保存"}


@app.get("/api/admin/config")
async def admin_get_config(admin: bool = Depends(verify_admin)):
    """管理员查看API配置（密钥脱敏）"""
    config = load_admin_config()
    
    def mask_key(key: str) -> str:
        if not key or len(key) < 10:
            return key
        return key[:6] + "***" + key[-4:]
    
    return {
        "text_api_base_url": config.get("text_api_base_url", ""),
        "text_api_key_masked": mask_key(config.get("text_api_key", "")),
        "text_model": config.get("text_model", ""),
        "vision_api_base_url": config.get("vision_api_base_url", ""),
        "vision_api_key_masked": mask_key(config.get("vision_api_key", "")),
        "vision_model": config.get("vision_model", ""),
        "text_api_configured": bool(config.get("text_api_key")),
        "vision_api_configured": bool(config.get("vision_api_key"))
    }


@app.get("/api/admin/stats")
async def admin_stats(admin: bool = Depends(verify_admin)):
    """管理员使用统计"""
    devices = load_devices()
    logs = load_usage_log()
    
    # 按天统计最近7天
    daily_stats = {}
    for i in range(7):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        daily_stats[day] = {"calls": 0, "tokens": 0}
    
    for log in logs:
        day = log.get("timestamp", "")[:10]
        if day in daily_stats:
            daily_stats[day]["calls"] += 1
            daily_stats[day]["tokens"] += log.get("total_tokens", 0)
    
    # 按模型统计
    model_stats = {}
    for log in logs:
        model = log.get("model", "unknown")
        if model not in model_stats:
            model_stats[model] = {"calls": 0, "tokens": 0}
        model_stats[model]["calls"] += 1
        model_stats[model]["tokens"] += log.get("total_tokens", 0)
    
    # 按设备统计
    device_stats = {}
    for did, dev in devices.items():
        device_stats[dev.get("name", did)] = {
            "total_calls": dev.get("total_calls", 0),
            "total_tokens": dev.get("total_tokens", 0)
        }
    
    # 总体统计
    total_calls = len(logs)
    total_tokens = sum(l.get("total_tokens", 0) for l in logs)
    total_errors = sum(1 for l in logs if l.get("status") == "error")
    
    return {
        "total_devices": len(devices),
        "active_devices": sum(1 for d in devices.values() if d.get("enabled")),
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "total_errors": total_errors,
        "daily_stats": daily_stats,
        "model_stats": model_stats,
        "device_stats": device_stats
    }


@app.get("/api/admin/logs")
async def admin_logs(limit: int = 100, device_id: str = "", admin: bool = Depends(verify_admin)):
    """管理员查看使用日志"""
    logs = load_usage_log()
    
    # 筛选
    if device_id:
        logs = [l for l in logs if l.get("device_id") == device_id]
    
    # 最近N条
    logs = logs[-limit:]
    logs.reverse()  # 最新的在前
    
    # 补充设备名称
    devices = load_devices()
    for log in logs:
        did = log.get("device_id", "")
        log["device_name"] = devices.get(did, {}).get("name", "未知设备")
    
    return {"logs": logs, "total": len(logs)}


class NotifyRequest(BaseModel):
    title: str = ""
    message: str = ""
    type: str = "info"  # info, update, warning
    data: Optional[dict] = {}

@app.post("/api/admin/notify")
async def admin_send_notification(req: NotifyRequest, admin: bool = Depends(verify_admin)):
    """管理员向所有设备发送通知"""
    notifications = load_notifications()
    notify_id = f"notify_{int(time.time())}"
    
    notifications[notify_id] = {
        "title": req.title or "系统通知",
        "message": req.message,
        "type": req.type,
        "data": req.data or {},
        "timestamp": now_iso(),
        "read_by": []
    }
    save_notifications(notifications)
    
    device_count = len(load_devices())
    print(f"[通知] 发送通知: {req.title} → {device_count} 台设备")
    
    return {"message": f"通知已发送", "notify_id": notify_id, "device_count": device_count}


@app.post("/api/admin/notify-specific")
async def admin_send_notification_to_device(request: Request, admin: bool = Depends(verify_admin)):
    """管理员向指定设备发送通知"""
    body = await request.json()
    device_id = body.get("device_id", "")
    title = body.get("title", "系统通知")
    message = body.get("message", "")
    notify_type = body.get("type", "info")
    data = body.get("data", {})
    
    devices = load_devices()
    if device_id not in devices:
        raise HTTPException(status_code=404, detail="设备不存在")
    
    notifications = load_notifications()
    notify_id = f"notify_{int(time.time())}"
    
    notifications[notify_id] = {
        "title": title,
        "message": message,
        "type": notify_type,
        "data": data,
        "timestamp": now_iso(),
        "read_by": [],
        "target_device": device_id  # 指定设备
    }
    save_notifications(notifications)
    
    return {"message": f"通知已发送给 {devices[device_id]['name']}", "notify_id": notify_id}


@app.get("/api/admin/token")
async def admin_get_token_info(admin: bool = Depends(verify_admin)):
    """查看管理员token信息"""
    auth = load_admin_auth()
    token = auth.get("admin_token", "")
    if not token:
        return {"configured": False}
    return {
        "configured": True,
        "token_masked": token[:8] + "***" + token[-4:],
        "created_at": auth.get("created_at", "")
    }


# ============ 辅助函数 ============
def _is_device_online(device: dict) -> bool:
    """判断设备是否在线（5分钟内有活动）"""
    last_hb = device.get("last_heartbeat", "")
    if not last_hb:
        return False
    try:
        last_time = datetime.strptime(last_hb, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - last_time).total_seconds() < 300  # 5分钟内
    except:
        return False


# ============ 健康检查 ============
@app.get("/")
async def root():
    return {
        "service": "水利智审云端API代理",
        "version": "1.0.0",
        "status": "running",
        "time": now_iso()
    }

@app.get("/health")
async def health_check():
    devices = load_devices()
    config = load_admin_config()
    return {
        "status": "ok",
        "devices_registered": len(devices),
        "text_api_ready": bool(config.get("text_api_key")),
        "vision_api_ready": bool(config.get("vision_api_key"))
    }


# ============ 启动入口 ============
if __name__ == "__main__":
    import uvicorn
    # Render 平台通过 PORT 环境变量指定端口
    port = int(os.environ.get("PORT", os.environ.get("PROXY_PORT", "8800")))
    host = os.environ.get("PROXY_HOST", "0.0.0.0")
    print(f"\n水利智审云端API代理 v1.0.0")
    print(f"监听: {host}:{port}")
    print(f"数据目录: {DATA_DIR.absolute()}\n")
    uvicorn.run(app, host=host, port=port)
