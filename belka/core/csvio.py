#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Единый формат CSV-отчётов проекта: ';'-разделитель, utf-8-sig (открывается
в Excel без танцев с кодировкой), одна шапка."""
from __future__ import annotations

import csv
from pathlib import Path


def write_csv_report(path: Path, header: list, rows) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
