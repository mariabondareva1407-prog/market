#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Интеграционная проверка GenerateStage на РЕАЛЬНОМ JSON-экспорте Bazar из
архива, без сети: категорийная API подменяется заглушкой.

Проверяет главное по фиксу 2: второй прогон за день (в другой час) не
оставляет файлов первого прогона, а import_check/upload, сканирующие папку
целиком, видят ровно текущий результат.

Запуск:  python3 tests/test_generate_integration.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROJECT = Path(__file__).resolve().parent.parent

from belka.context import PipelineContext                    # noqa: E402
from belka.stages import generate as generate_mod            # noqa: E402
from belka.warehouses import bazar                           # noqa: E402

FAKE_CATEGORY = {
    "properties": [
        {"display_name": "Бренд", "is_required": False, "has_directory": True},
        {"display_name": "Цвет", "is_required": False, "has_directory": True},
    ]
}


def _find_real_bazar_json() -> Path | None:
    return next(iter(sorted((PROJECT / "input" / "bazar").glob("*.json"))), None)


def main() -> int:
    real_json = _find_real_bazar_json()
    if real_json is None:
        print("✘ не найден реальный JSON-экспорт Bazar в input/bazar/*/ — тест пропущен")
        return 0

    products = json.loads(real_json.read_text(encoding="utf-8")).get("products", [])
    print(f"[i] реальный экспорт: {real_json.name}, товаров: {len(products)}")

    workdir = Path(tempfile.mkdtemp())
    cwd = os.getcwd()
    try:
        # рабочая копия проекта: только то, что нужно generate
        for name in ("category_map.json", "console_to_admin_category_map.json", "attribute_mapping.json"):
            shutil.copy(PROJECT / name, workdir / name)
        os.chdir(workdir)

        ctx = PipelineContext(config=bazar.CONFIG, env={}, interactive=False, assume_yes=True)
        shutil.copy(real_json, ctx.resolved_input_dir() / real_json.name)

        # ни одного сетевого вызова: и сессия, и карточка категории — заглушки
        ctx.admin_session = lambda: None
        generate_mod.fetch_admin_category = lambda session, api_base, category_id: FAKE_CATEGORY

        out_dir = ctx.resolved_output_dir()

        # --- прогон 1
        result1 = generate_mod.GenerateStage().run(ctx)
        first_files = sorted(p.name for p in out_dir.glob("catalog_*.xlsx"))
        assert result1.ok and first_files, (result1, first_files)
        print(f"✔ прогон 1: файлов {len(first_files)}, товаров размещено {result1.summary['products_placed']}")

        # имитируем "прошлый прогон в другой час" — ровно тот случай, из-за
        # которого import_check/upload видели старые файлы вперемешку с новыми
        stale = out_dir / "catalog_99999_26-09-13-01.xlsx"
        shutil.copy(out_dir / first_files[0], stale)
        (out_dir / "upload_log.json").write_text("{}", encoding="utf-8")
        assert stale.is_file()

        # --- прогон 2
        result2 = generate_mod.GenerateStage().run(ctx)
        second_files = sorted(p.name for p in out_dir.glob("catalog_*.xlsx"))
        assert result2.ok, result2

        assert not stale.is_file(), "файл прошлого прогона остался — очистка не сработала"
        assert second_files == first_files, (second_files, first_files)
        assert (out_dir / "upload_log.json").is_file(), "upload_log.json удалять нельзя"
        print(f"✔ прогон 2: старый catalog_*.xlsx удалён, в папке ровно {len(second_files)} актуальных файлов")

        # LockedInfo — id ПСБ/консоли, а не Белки (инвариант проекта)
        import openpyxl
        wb = openpyxl.load_workbook(out_dir / first_files[0])
        console_id_from_name = int(first_files[0].split("_")[1])
        assert wb["LockedInfo"]["A1"].value == console_id_from_name
        headers = [c.value for c in wb["Products"][1]]
        assert "Id Товара *" in headers and "Цена, руб *" in headers
        print(f"✔ структура файла: LockedInfo!A1={console_id_from_name}, колонок {len(headers)}")
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        os.chdir(cwd)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
