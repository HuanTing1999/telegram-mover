# 参与贡献

欢迎提交问题、改进文档和 Pull Request。提交前先阅读 [软件说明书](docs/USER_GUIDE.md) 和 [技术说明](docs/TECHNICAL.md)，确认实际功能与接口边界。

## 报告问题

在 [Issues](https://github.com/HuanTing1999/telegram-mover/issues) 中说明系统版本、软件版本、使用模式、复现步骤、预期结果和实际错误。截图请遮住手机号、聊天名称、私有链接及消息正文。

**不要上传** `config.toml`、`app_credentials.toml`、`.env`、session、数据库、账号索引、验证码、密码或下载文件。session 本身可以授权操作 Telegram 账号。涉及凭证泄露或会话安全的问题，按 [安全政策](SECURITY.md) 处理。

## 开发与检查

Windows、64 位 Python 3.11+；当前验证环境为 Python 3.14.5。所有命令在仓库根目录执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts/check_public_tree.py
```

图形界面离线自检（不会登录或发送 Telegram 内容）：

```powershell
.\.venv\Scripts\python.exe gui_main.py --self-test --output .build/gui-qa
```

网络功能的测试必须使用模拟传输。请勿在 CI 中加入真实 Telegram 凭证、自动获取验证码或对真实频道发帖。

## 修改要求

- 保留 session、数据库、旧配置和增量进度的兼容性。
- 相册整体发送并提交映射，不能因优化下载破坏顺序。
- 遵守服务端 FloodWait、慢速模式、内容保护及账号停止状态。
- 发送结果不明时保留 pending，不自动重放；任何恢复功能都应说明重复风险。
- 过滤规则先给出可解释结果；清理功能只处理已登记且已确认可清理的文件。
- 对行为变化补充有意义的离线检查，更新用户说明；只改排版不必新增测试。

本项目原创代码与文档以 [MIT](LICENSE) 发布。提交贡献即表示你有权提供该内容，并同意以相同许可证发布；第三方代码保留原许可和署名。
