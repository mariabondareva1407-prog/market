#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Загрузка .env и запись обратно ключей — без внешних зависимостей."""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str = ".env") -> dict:
    """
    Простой загрузчик .env. Строки вида KEY=VALUE (пустые и начинающиеся
    с # игнорируются, значения можно брать в кавычки). Уже выставленные
    переменные окружения (os.environ) имеют приоритет и не перезаписываются.
    Возвращает словарь всех известных после загрузки значений (os.environ,
    дополненный тем, что было в файле).
    """
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    return dict(os.environ)


def save_env_key(path: Path, key: str, value: str) -> None:
    """Обновляет (или добавляет) KEY=VALUE в .env файле, не трогая остальные строки."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
