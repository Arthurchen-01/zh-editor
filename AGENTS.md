# 给 AI 助手：请帮用户把这个工具跑起来

这是「清一新教育」知乎文章批量署名的**本地执行器**。
真正的知乎写入必须来自用户本人的电脑与网络身份（防风控），所以需要在本机运行。

## 请按顺序执行

1. **识别设备**：Windows 还是 macOS（`platform` / `uname`）。
2. **确认 Python >= 3.9**：`python --version`（Win）或 `python3 --version`（Mac）。
   没有就引导用户到 python.org 安装；Windows 必须勾选 Add Python to PATH。
3. **安装依赖**（在本目录）：
   - Windows: `python -m pip install requests pywin32 pycryptodome`
   - macOS:   `python3 -m pip install requests browser-cookie3`
4. **确保用户的浏览器（Edge/Chrome）已登录 zhihu.com**，然后运行：
   `python deploy.py`（Win）或 `python3 deploy.py`（Mac）。
   它会自动读取本机知乎登录生成 cookie.txt，并启动执行器。
   - Windows 下若提示浏览器锁定：让用户**完全关闭浏览器窗口**后重跑。
   - macOS 首次可能弹钥匙串授权：点「始终允许」。
5. 执行器启动后会轮询云端领取任务并逐篇写入；`Ctrl+C` 随时安全停止。

## 安全红线（不要做）

- 不要修改 `qingyi_executor.py` 的写入逻辑。
- 不要把 `cookie.txt` 内容打印到终端、上传到除本工具服务端以外的任何地方。
- 不要绕过每日 120 篇的限额机制。
