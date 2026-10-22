"""Offline detection and integrity benchmarks for Telethon's actual AES backend."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
import platform
import struct
import sys
import time

from telethon.crypto import aes


def backend_info() -> dict:
    """Inspect Telethon's selected implementation, not just installed packages."""
    native = getattr(aes, "cryptg", None)
    version = None
    if native is not None and all(callable(getattr(native, name, None)) for name in ("encrypt_ige", "decrypt_ige")):
        backend = "cryptg"
        try:
            version = metadata.version("cryptg")
        except metadata.PackageNotFoundError:
            version = getattr(native, "__version__", None)
        message = "已启用 cryptg 本地加密加速；实际传输速度仍取决于网络和 Telegram 服务端。"
    elif all(callable(getattr(aes.libssl, name, None)) for name in ("encrypt_ige", "decrypt_ige")):
        backend = "libssl"
        message = "已启用本地 OpenSSL 加密加速；实际传输速度仍取决于网络和 Telegram 服务端。"
    else:
        backend = "pyaes"
        message = "当前使用纯 Python 加密；大文件传输可能受本地处理速度影响。"
    return {"backend": backend, "accelerated": backend != "pyaes", "version": version,
            "message": message}


def benchmark_current_backend(size_bytes: int = 262144, repeats: int = 3) -> dict:
    """Measure synthetic AES work only; never connect, read sessions, or send.

    This measures local CPU processing and validates byte-for-byte round trips.
    It cannot predict or promise Telegram/network download throughput.
    """
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0 or size_bytes % 16:
        raise ValueError("Benchmark size must be a positive multiple of 16 bytes.")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 20:
        raise ValueError("Benchmark repeats must be between 1 and 20.")
    payload = (bytes(range(256)) * ((size_bytes + 255) // 256))[:size_bytes]
    # Synthetic public test inputs, unrelated to application/account credentials.
    key, iv = bytes(range(32)), bytes(range(32, 64))
    encrypt_times, decrypt_times = [], []
    for _ in range(repeats):
        started = time.perf_counter()
        encrypted = aes.AES.encrypt_ige(payload, key, iv)
        encrypt_times.append(time.perf_counter() - started)
        started = time.perf_counter()
        decoded = aes.AES.decrypt_ige(encrypted, key, iv)
        decrypt_times.append(time.perf_counter() - started)
        if decoded != payload:
            raise RuntimeError("AES integrity check failed; accelerated backend must not be used.")
    encryption = sum(encrypt_times) / repeats
    decryption = sum(decrypt_times) / repeats
    size_mib = size_bytes / 1048576
    return {
        "local_only": True, "roundtrip_correct": True,
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(), "platform": sys.platform,
        "interpreter_bits": struct.calcsize("P") * 8, "frozen": bool(getattr(sys, "frozen", False)),
        "backend": backend_info(), "size_bytes": size_bytes, "repeats": repeats,
        "encrypt_seconds_mean": encryption, "decrypt_seconds_mean": decryption,
        "encrypt_mib_per_second": size_mib / encryption,
        "decrypt_mib_per_second": size_mib / decryption,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline Telethon AES backend diagnostics; no Telegram connection.")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--size-bytes", type=int, default=262144)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--require-native", action="store_true")
    parser.add_argument("--require-cryptg", action="store_true")
    args = parser.parse_args(argv)
    info = backend_info()
    if (args.require_native and not info["accelerated"]) or (args.require_cryptg and info["backend"] != "cryptg"):
        print(json.dumps(info, ensure_ascii=True))
        return 2
    result = benchmark_current_backend(args.size_bytes, args.repeats) if args.benchmark else info
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
