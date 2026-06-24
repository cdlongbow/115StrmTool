# 115网盘STRM生成与302工具

115 网盘 STRM 文件生成 + 302 跳转 + Emby 302 反向代理 Windows 桌面工具。

## 功能

- **115 网盘 STRM 生成**：扫描 115 网盘目录，自动生成 STRM 文件
- **302 跳转服务**：播放器访问 STRM 文件时 302 重定向到 115 真实下载地址
- **Emby 302 反向代理**：代理 Emby 媒体链接，跳转最终播放地址，支持外部播放器调用
- **开机自动启动**：支持后台托盘运行，开机自启

## 使用

直接运行 `115网盘STRM生成与302工具.exe`，默认弹出原生管理窗口。
关闭窗口缩小到系统托盘，右键托盘图标可打开窗口或退出。

管理界面访问 http://localhost:8100

## 开发

```bash
cd combined
pip install -r requirements.txt
python main.py        # 控制台模式
python main.py --no-tray  # Windows 强制控制台模式
```

## 打包

```bash
python build_exe.py
```

生成 `dist/115网盘STRM生成与302工具.exe`。

## 技术栈

- Python + FastAPI + Uvicorn
- HTML + JavaScript 单页 Web UI
- pystray（系统托盘）
- pywebview（原生窗口，可选）
- PyInstaller（打包）
- p115client（115 网盘 SDK）
