# 构建与发布

本仓库不包含真实应用凭证、账号会话、任务数据库、下载内容或预编译 EXE。用户发行包通过 GitHub Releases 提供，源码始终可独立构建。

## 本地检查

在 Windows x64、Python 3.11+ 环境中，仓库根目录执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts/check_public_tree.py
.\.venv\Scripts\python.exe gui_main.py --self-test --output .build/gui-qa
```

上述测试都不连接 Telegram。离线检查不能证明真实账号网络连通、服务器限额或实际下载速度。

## 生成 Windows 程序

```powershell
.\build_windows.ps1 -Python .\.venv\Scripts\python.exe -OutputDirectory .\dist
```

输出 `dist/TelegramMover.exe`、配置模板和简要说明。已有 `config.toml` 不覆盖。构建会确认 cryptg 可用、隔离 DLL 搜索路径，只内嵌无凭证模板。不要为公开版传入 `-AppCredentialsPath`。

若需要可直接替换动态库的 onedir 形式，在安装相应依赖的环境中执行：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onedir --windowed --noupx --name TelegramMover --collect-all telethon --collect-all cryptg --add-data "config.example.toml;." --distpath dist --workpath .build/onedir --specpath .build gui_main.py
```

这会得到 `dist/TelegramMover/`。保留其中 `_internal` 下的动态库结构，用自行构建的兼容 Qt/PySide6/Shiboken6 库替换后再启动；或直接用替代依赖运行 Python 源码。匹配版本与许可见 [第三方声明](../THIRD_PARTY_NOTICES.md)。

## 打发行包

```powershell
.\.venv\Scripts\python.exe scripts/collect_licenses.py
.\.venv\Scripts\python.exe scripts/package_release.py --exe dist/TelegramMover.exe --output dist/release
```

打包脚本仅用明确的源文件清单和无凭证模板，不遍历复制用户目录。会生成 Windows ZIP、源码 ZIP 和 SHA-256；Windows ZIP 包含软件说明书、MIT 及第三方许可文本。

发布前运行该次生成的 EXE（离线交互检查），确认 `gui-smoke-result.json` 中 `passed=true`、`offline=true`、`network_used=false`，并确认 cryptg 字节往返正确。检查截图里只包含演示数据。

```powershell
dist\TelegramMover.exe --self-test --output .build/frozen-qa
```

## GitHub 发布

1. 检查 `git diff --cached` 和 `git ls-files`，只提交源码、测试、文档、无凭证模板、许可和离线演示图片。
2. 在 `main` 提交通过检查的版本，版本号与 `CHANGELOG.md` 一致。
3. 创建对应版本标签，例如 `v0.1.0`，再建立 GitHub Release。
4. 上传 Windows ZIP、源码 ZIP 和 SHA-256 文件；说明验证范围、已知边界及实际是否做过 Telegram 网络验收。
5. 从未登录 GitHub 的只读环境确认仓库公开、README/说明书可读、Release 附件可下载。

不要把本机 `config.toml` 当作模板，也不要上传 `.build`、日志、数据库、账号索引、session、截图中的真实手机号/私有消息。发现泄露后按 [安全政策](../SECURITY.md) 处理。
