# 第三方组件与许可

本项目自己的代码和文档使用根目录 [MIT License](LICENSE)。下面的依赖及其原有版权/许可**不改为 MIT**。安装时仍应阅读各依赖发行包内的完整条款。

| 组件 | 当前固定版本 | 许可与上游 |
|---|---|---|
| Telethon | 1.45.0 | MIT；[源码](https://github.com/LonamiWebs/Telethon) |
| cryptg | 0.6.0 | CC0-1.0；[源码及原生组件依赖](https://github.com/cher-nov/cryptg) |
| PySide6 / Shiboken6 / Qt | 6.11.2 | LGPL-3.0 / GPL 等；[Qt for Python](https://code.qt.io/cgit/pyside/pyside-setup.git/)、[许可说明](https://doc.qt.io/qtforpython-6/licenses.html) |
| regex | 2026.9.29 | Apache-2.0 与 CNRI-Python；[源码](https://github.com/mrabarnett/mrab-regex) |
| tomli-w | 1.2.0 | MIT；[源码](https://github.com/hukkin/tomli-w) |
| pyaes | 1.6.1 | MIT；[源码](https://github.com/ricmoo/pyaes) |
| rsa | 4.9.1 | Apache-2.0；[源码](https://github.com/sybrenstuvel/python-rsa) |
| pyasn1 | 0.6.4 | BSD-2-Clause；[源码](https://github.com/pyasn1/pyasn1) |
| CPython | 构建环境决定 | PSF 及发行包所列第三方许可；[源码](https://github.com/python/cpython) |
| PyInstaller（构建工具） | 6.22.3 | GPL-2.0-or-later 加 bootloader 例外，部分文件 Apache-2.0；[官方许可](https://pyinstaller.org/en/stable/license.html) |

从实际安装包复制的许可原文在 [licenses/dependencies](licenses/dependencies)，版本及文件 SHA-256 见 [inventory.json](licenses/inventory.json)。用 `python scripts/collect_licenses.py` 可在所选构建环境重新收集；列表不能替代 Qt 自身第三方组件的条款。

## PySide6、Qt 与替换依赖

本项目使用公开发行的 PySide6/Qt 动态库，未修改这些库。开源使用分支按对应组件的 LGPL 等许可处理，不授予 Qt 商业许可。依赖包中的 `LicenseRef-Qt-Commercial.txt` 是上游附带文件，不表示本项目购买或转授商业许可。

Qt 的第三方组件信息以 [Qt Attribution](https://doc.qt.io/qt-6/licenses-used-in-qt.html) 及匹配版本的上游源码为准。Qt 与 PySide6 6.11.2 的源码可从 [Qt 归档](https://download.qt.io/archive/qt/6.11/6.11.2/) 及 [PySide 源码归档](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/) 获取；也可按相同版本标签查看官方仓库。LGPL/GPL 许可文本位于 [licenses/qt](licenses/qt)。

你可以从本项目全部 Python 源码运行，替换或重新构建 PySide6、Shiboken6 和 Qt，然后重新生成 EXE；源码未加混淆或阻止调试的限制。为调试用户对 LGPL 库的修改而进行的逆向工程不受本项目附加限制。

运行源码：新建虚拟环境、安装相容的依赖，执行 `python gui_main.py`。重新链接/构建：用所选环境的 Python 执行 `build_windows.ps1 -Python 解释器路径 -OutputDirectory 输出目录`。发行的 PyInstaller 单文件会在启动时解出动态库；希望直接替换磁盘上的库时，可按 [发布说明](docs/RELEASING.md) 生成 onedir 版本。

重新分发二进制时应一并提供本项目源码获取方式、相应依赖源码获取方式、上述许可与版权声明，并按实际打包的库重新核对其许可；加入新的 Qt 模块或其他原生组件后，需要更新组件清单和通知。
