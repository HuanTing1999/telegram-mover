"""Console EXE entry point with config paths independent of its working folder."""

from __future__ import annotations

import sys
from pathlib import Path

import main


def _configure_redirected_output() -> None:
    """Use UTF-8 in pipes/files while preserving native console and input I/O."""
    for stream in (sys.stdout, sys.stderr):
        try:
            reconfigure = getattr(stream, "reconfigure", None)
            if stream is not None and not stream.isatty() and callable(reconfigure):
                reconfigure(encoding="utf-8")
        except (AttributeError, OSError, TypeError, ValueError):
            # Some embedders and test streams do not permit reconfiguration.
            pass


def _config_arguments(arguments: list[str], base: Path) -> tuple[list[str], Path]:
    """Preserve explicit --config semantics, making the selected path absolute."""
    arguments = list(arguments)
    selected = None
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--config":
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                raise ValueError("Missing configuration path")
            selected = (index + 1, arguments[index + 1], False)
            index += 2
            continue
        if value.startswith("--config="):
            if not value[len("--config="):]:
                raise ValueError("Missing configuration path")
            selected = (index, value[len("--config="):], True)
        index += 1
    if selected is None:
        config = (base / "config.toml").resolve()
        return ["--config", str(config), *arguments], config
    position, value, inline = selected
    config = Path(value).expanduser().resolve()
    arguments[position] = f"--config={config}" if inline else str(config)
    return arguments, config


def _create_missing_config(config: Path, template: Path) -> bool:
    if config.exists():
        return False
    data = template.read_bytes()
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Exclusive creation also protects an existing file from a launch race.
        with config.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        return False
    return True


def entrypoint() -> int:
    """Run main.main; a no-argument frozen launch keeps its console visible."""
    original_argv = sys.argv
    user_arguments = list(original_argv[1:])
    frozen = bool(getattr(sys, "frozen", False))
    pause_on_exit = frozen and not user_arguments
    result = 2
    try:
        _configure_redirected_output()
        source_directory = Path(__file__).resolve().parent
        base = Path(sys.executable).resolve().parent if frozen else source_directory
        bundle = Path(getattr(sys, "_MEIPASS", source_directory)) if frozen else source_directory
        arguments, config = _config_arguments(user_arguments, base)
        if pause_on_exit:
            print("Telegram 内容搬运工具")
            print(f"配置文件：{config}")
            print("首次使用请填写 API_ID / API_HASH、源 source 与目标 target；应用凭证需自行到 my.telegram.org/apps 申请。")
            print("登录仅使用手机号和验证码；成功后的用户 session 会保存在配置指定的位置。")
        created = _create_missing_config(config, bundle / "config.example.toml")
        if created:
            print(f"已创建首次配置：{config}")
            print("请先编辑配置并填写自己的应用凭证、源和目标，再重新运行。")
        sys.argv = [original_argv[0], *arguments]
        returned = main.main()
        result = 0 if returned is None else int(returned)
    except SystemExit as exc:
        result = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    except (KeyboardInterrupt, EOFError):
        print("已停止。已完成的进度保留；重新运行可继续。")
        result = 2
    except Exception as exc:
        # Do not print exception text, arguments, config contents or traceback:
        # any of them may contain account credentials or a verification code.
        print(f"程序未能完成（{type(exc).__name__}）。请检查配置文件、文件权限和依赖后重试。")
        result = 2
    finally:
        sys.argv = original_argv
        if pause_on_exit:
            try:
                input("按回车关闭…")
            except (KeyboardInterrupt, EOFError):
                pass
    return result


if __name__ == "__main__":
    raise SystemExit(entrypoint())
