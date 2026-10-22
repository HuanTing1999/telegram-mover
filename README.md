# Telegram 搬运工作台 · Telegram Mover

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

一个基于 **Python + Telethon（MTProto 用户账号）** 的 Telegram 频道、群组内容备份与搬运工具，提供 Windows 图形界面和命令行入口。

支持国际手机号验证码登录、按需两步验证和本地多账号会话；**不使用 Bot API、Bot Token、扫码或第三方登录**。

**[下载 Windows 软件](https://github.com/HuanTing1999/telegram-mover/releases) · [完整软件说明书](docs/USER_GUIDE.md) · [报告问题](https://github.com/HuanTing1999/telegram-mover/issues)**

![Telegram 搬运工作台](GUI-PREVIEW.png)

> 界面截图均为离线演示，不包含真实 Telegram 账号或私有消息。

## 可以做什么

- **四种正文模式**：纯下载、直接转发、下载后重传、下载留档后转发。
- **频道评论增量**：解析关联讨论组和真实评论根，支持评论媒体重传或纯备份。
- **群组话题**：分别选择源/目标话题，各范围独立保存同步进度。
- **相册与断点进度**：整组有序发送，SQLite 保存帖子/评论映射、游标和下载审计。
- **广告过滤**：关键词、正则、域名、隐藏链接和推广按钮，支持预览、白名单及整组手动屏蔽。
- **下载管理**：自选文件夹/硬盘、1–8 路受限并行、原生加解密加速、已确认转发文件清理。
- **发送控制**：账号共享配额、服务端冷却持久化、安全停止和发送结果待核对保护。

## Windows 快速开始

1. 从 [Releases](https://github.com/HuanTing1999/telegram-mover/releases) 下载 Windows x64 包，完整解压，双击 `TelegramMover.exe`；无需安装 Python。
2. 到 [my.telegram.org/apps](https://my.telegram.org/apps) 申请 API_ID / API_HASH，在软件“应用设置”填写一次。开源版不含共享密钥；切换其他手机号无需重新申请。
3. 输入含国际区号的手机号，接收 Telegram 验证码；若账号要求，再输入两步验证密码。
4. 选择源、目标、话题及搬运模式，先检查连接和预览，再开始运行。

默认设置和授权数据保存在本机。配置、session、数据库和下载文件不应上传 GitHub。更新程序时保留原配置和数据目录。

## 运行源码

推荐 Windows x64 + Python 3.11+；当前本地验证环境为 Python 3.14.5。以下命令在仓库根目录运行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe gui_main.py
```

命令行使用无凭证模板建立本地配置，并通过环境变量或本地配置设置自己的应用凭证：

```powershell
Copy-Item config.example.toml config.toml
# 编辑 config.toml 后：只检查连接 / 运行一轮
.\.venv\Scripts\python.exe main.py --check
.\.venv\Scripts\python.exe main.py --once
```

Linux/macOS 可使用 Python 命令行，尚未进行该两平台的完整打包和窗口验收。安装 Qt 依赖及网络连通由运行环境负责。

## 使用前了解的边界

- 只处理账号已有权限且你有权备份、再发布的内容；不自动入群或绕过内容保护。源禁止转发时会明确停止。
- **普通频道评论不支持原生直接转发到指定评论线程**，可选择正文转发、评论重传，或关闭评论同步。
- 下载重传上传本地文件，不保证 Telegram 服务端分配独一媒体 ID，也不保证搜索归属或排名。
- 只同步新增消息和评论，不镜像编辑、撤回、成员或多层评论树；整个话题群不会自动复制成相同话题结构。
- 广告识别限文字、链接、按钮和文件名，不含图片/视频 OCR。
- 网络中断可能留下“待核对发送”，需先核对目标，不能直接重试；不承诺跨网络严格零重复。
- 默认发送间隔与配额是软件策略，**不是 Telegram 官方安全上限**。实际下载速度、账号限制和发帖效果取决于网络、权限和服务端。

## 文档

| 文档 | 内容 |
|---|---|
| [软件说明书](docs/USER_GUIDE.md) | 安装、登录、搬运、过滤、限速、清理、更新与故障处理 |
| [技术说明](docs/TECHNICAL.md) | 配置字段、MTProto 接口、评论映射、相册和恢复命令 |
| [多账号与凭证](ACCOUNTS.md) | 凭证优先级、换号、验证码重发与两步验证 |
| [广告过滤](AD_FILTERS.md) | 规则优先级、域名与正则示例、预览和审计 |
| [限速与恢复](LIMITS.md) | 配额、FloodWait、慢速模式和账号停止 |
| [开发与贡献](CONTRIBUTING.md) | 本地检查、脱敏报错与贡献要求 |
| [第三方许可](THIRD_PARTY_NOTICES.md) | 依赖许可、Qt 源码及重新构建方式 |
| [验证记录](VALIDATION.md) | 本地检查范围和未验证事项 |

## 检查与打包

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts/check_public_tree.py
.\.venv\Scripts\python.exe gui_main.py --self-test --output .build/gui-qa
.\build_windows.ps1 -Python .\.venv\Scripts\python.exe
```

测试模拟 Telegram 网络，不需要真实凭证。现有离线验证覆盖登录状态、SQLite 事务、相册/评论、下载完整性、限速和 Qt 窗口；未完成使用真实账号的端到端验收。完整构建、发布包和依赖替换步骤见 [发布说明](docs/RELEASING.md)。

## 许可

本项目原创代码及文档使用 [MIT License](LICENSE)。第三方依赖保留各自许可，尤其 PySide6/Qt 的 LGPL/GPL 及组件许可，不由本项目的 MIT 覆盖；见 [第三方声明](THIRD_PARTY_NOTICES.md)。项目与 Telegram 官方无隶属关系。

## English overview

Telegram Mover is a Python/Telethon desktop tool for authorized Telegram backups and incremental reposting. It supports phone-code user authentication with optional 2FA, albums, forum topics, linked discussion comments, text/link/button filtering, bounded parallel downloads and persistent rate limits. Windows GUI and Python sources are provided. No bot token or bundled API credentials. Native forwarding into ordinary linked comment threads is unsupported; reupload or backup comments instead. Original code is MIT licensed; dependencies retain their own licenses.
