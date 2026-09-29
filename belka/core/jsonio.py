#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чтение/запись небольших JSON-логов и конфигов — было почти дословно
продублировано в belka_certificates/generate/import/upload, здесь один раз."""
from __future__ import annotations

import json
from pathlib import Path


def load_json_file(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"  [!] Не удалось прочитать {path}, использую значение по умолчанию")
    return default


def save_json_file(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
