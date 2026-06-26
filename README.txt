===============================================
水利智审云端API代理 - Render 免费部署指南
===============================================

【第一步】注册 Render 账号
-------------------------
1. 访问 https://render.com
2. 使用 GitHub 账号登录（或邮箱注册）
3. 完成邮箱验证

【第二步】创建 Web Service
-------------------------
方式A：通过 GitHub 仓库（推荐用于持续部署）
1. 在 GitHub 创建新仓库，将本目录代码上传
2. Render Dashboard → New → Static Site（或 Blueprint）
3. 选择你的 GitHub 仓库
4. Render 会自动识别 render.yaml 配置

方式B：直接上传代码
1. Render Dashboard → New → Web Service
2. 选择 "Upload files" 上传本目录所有文件
3. 设置以下配置：
   - Name: shuili-proxy（可自定义）
   - Region: Singapore（延迟最低）
   - Branch: main
   - Runtime: Python 3.11
   - Build Command: pip install -r requirements.txt
   - Start Command: uvicorn proxy_server:app --host 0.0.0.0 --port $PORT

【第三步】设置环境变量
-------------------------
在 Render Dashboard → 你的服务 → Environment 中添加：

必需（至少配置一个）：
- TEXT_API_KEY: 你的 DeepSeek API 密钥
- VISION_API_KEY: 你的阿里云通义千问 API 密钥（用于图纸校审）

可选：
- TEXT_API_BASE_URL: https://api.deepseek.com（默认）
- TEXT_MODEL: deepseek-chat（默认）
- VISION_API_BASE_URL: https://dashscope.aliyuncs.com/compatible-mode（默认）
- VISION_MODEL: qwen-vl-plus（默认）
- ADMIN_TOKEN: 你的管理员Token（自定义）

【第四步】部署
-------------------------
1. 点击 "Create Web Service"
2. 等待构建完成（1-3分钟）
3. 部署成功后，复制显示的 URL，如：
   https://shuili-proxy.onrender.com

【第五步】获取管理员Token
-------------------------
1. 点击服务日志，查找 "管理员Token（请妥善保存）:"
2. 记录显示的 Token

【第六步】配置桌面版/网页版
-------------------------
1. 桌面版：打开软件 → 设置 → 代理地址填入：
   https://shuili-proxy.onrender.com
2. 网页版：在部署代理服务后端管理页面配置

【注意事项】
-------------------------
- Render 免费版每月有750小时额度，闲置会自动休眠（首次访问会冷启动，需等待10-30秒）
- 如需保持24小时在线，可绑定信用卡升级，或使用 Paid Plan
- 免费版不支持自定义域名 HTTPS（但 onrender.com 域名自带 HTTPS）
- 数据存储在容器内，重启后会丢失。建议通过 Render Dashboard 的 Persistent Disk 或外部数据库存储
- 首次部署后，API配置会写入文件，后续修改可通过环境变量或网页管理界面

【管理界面】
-------------------------
部署成功后访问：
https://shuili-proxy.onrender.com/admin
（使用管理员Token登录）

【故障排除】
-------------------------
1. 部署失败：检查 Build Logs
2. 启动失败：检查 Service Logs
3. API调用超时：Render 免费版请求超时限制30秒，大文件可能超时
4. 冷启动慢：正常现象，免费版休眠后首次访问需要重新启动服务

===============================================
