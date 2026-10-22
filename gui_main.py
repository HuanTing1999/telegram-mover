"""Windowed executable entry point: no terminal and no console login prompts."""
from __future__ import annotations

import argparse
import multiprocessing


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Telegram 搬运工作台")
    parser.add_argument("--config", help="可选 TOML 配置文件")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test:
        from tgmover.gui_smoke import run_self_test
        return run_self_test(args.output or "gui-validation")
    from tgmover.gui import run_gui
    return run_gui(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
