# MediaServiceHub

Emby 302 反向代理 + P115 STRM Helper 桌面整合版。

## 功能

- **Emby 302 反向代理**：自动代理 Emby 媒体链接，跳转最终播放地址，支持外部播放器调用
- **P115 STRM 助手**：115 网盘 STRM 文件生成、302 跳转、同步管理

## 使用方法

```bash
cd combined

# 浏览器模式（默认）
python main.py

# 系统托盘模式（Windows）
python main.py --tray
```

访问 http://localhost:8100 打开管理界面。

## 打包

```bash
python build_exe.py
```

生成 `dist/MediaServiceHub.exe`。

## 技术栈

- Python + FastAPI + Uvicorn（后端）
- HTML + JavaScript 单页应用（前端）
- pystray（系统托盘）
- PyInstaller（打包）
