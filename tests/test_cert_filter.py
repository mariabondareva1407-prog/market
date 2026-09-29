#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты фильтра шаблонов по сертификату (--green/--white/--red), 09.2026.

Что проверяется:
  1. Трактовка цвета: red/green определяются по ОТТЕНКУ, а не точным
     совпадением RGB; всё остальное — white («любой не красный и не
     зелёный»), включая серую и жёлтую заливку.
  2. Базар: фильтр на РЕАЛЬНОМ отчёте + JSON из архива, без сети
     (категорийная API — заглушка). --green --white = «все, кроме красных».
  3. Мвидео: тот же фильтр у psb_generate, источник статусов — колонка
     "Номер сертификата" файла-ассортимента, отдельный --file не нужен.
  4. Раздельные счётчики: «отсеяно по цвету» и «нет в отчёте» — это разные
     числа в summary, а не одно общее.
  5. Включение фильтра не сбрасывает параметры stage'а из рецепта склада.

Запуск:  python3 tests/test_cert_filter.py
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

import openpyxl                                                        # noqa: E402
from openpyxl.styles import PatternFill                                # noqa: E402

from belka.context import PipelineContext                              # noqa: E402
from belka.core.cert_report import (                                   # noqa: E402
    classify_rgb, apply_cert_filter, normalize_article, cert_summary_fields,
)
from belka.core.psb_api import load_cert_statuses_from_assortment      # noqa: E402
from belka.stages import generate as generate_mod                      # noqa: E402
from belka.stages.psb_generate import PsbGenerateStage                 # noqa: E402
from belka.warehouses import bazar, mvideo                             # noqa: E402

RESULTS = []

FAKE_CATEGORY = {"properties": [{"display_name": "Бренд", "is_required": False, "has_directory": True}]}


def check(name, fn):
    try:
        fn()
    except Exception:
        RESULTS.append((name, False))
        print(f"✘ {name}")
        traceback.print_exc()
    else:
        RESULTS.append((name, True))
        print(f"✔ {name}")


# --------------------------------------------------------------- 1. цвет --

def test_color_classification():
    expected = {
        "FFFF0000": "red",     # эталонный красный отчёта
        "FFC00000": "red",     # тёмно-красный — раньше молча уезжал в white
        "FFFFC7CE": "red",     # пастельный стиль Excel «Bad»
        "FF00B050": "green",   # эталонный зелёный отчёта
        "FFC6EFCE": "green",   # пастельный стиль Excel «Good»
        "FF92D050": "green",
        "FFCCCCCC": "white",   # серая — «нет отметки»
        "FFFFFFFF": "white",
        "FFFFEB9C": "white",   # жёлтая — не красная и не зелёная
        "00000000": "white",
    }
    wrong = {rgb: (classify_rgb(rgb), want) for rgb, want in expected.items() if classify_rgb(rgb) != want}
    assert not wrong, wrong


def test_white_is_everything_but_red_and_green():
    statuses = {"a": "green", "b": "white", "c": "red"}
    items = ["a", "b", "c"]
    kept, _ = apply_cert_filter(items, lambda x: x, statuses, {"green", "white"})
    assert kept == ["a", "b"], kept


def test_counters_split_color_and_missing():
    statuses = {"a": "green", "c": "red"}       # 'b' в отчёте нет вовсе
    kept, stats = apply_cert_filter(["a", "b", "c"], lambda x: x, statuses, {"green", "white"})
    assert kept == ["a"], kept
    assert stats["filtered_by_color"] == 1, stats      # красный
    assert stats["missing_from_report"] == 1, stats    # 'b'
    summary = cert_summary_fields(3, 1, stats)
    assert summary["cert_filtered_out"] == 2, summary
    assert summary["cert_filtered_by_color"] == 1 and summary["cert_missing_from_report"] == 1, summary


def test_article_normalized_from_float():
    # Excel отдаёт целое как 1140.0 — ключ должен совпасть с id '1140'
    assert normalize_article(1140.0) == "1140" == normalize_article(" 1140 ")


# ------------------------------------------------------------- 2. Базар --

def _real(pattern):
    return next(iter(sorted((PROJECT / "input" / "bazar").glob(pattern))), None)


def test_bazar_generate_green_white_on_real_files():
    report, real_json = _real("*.xlsx"), _real("*.json")
    if not report or not real_json:
        print("  [i] реальных файлов Базара в архиве нет — тест пропущен")
        return

    workdir = Path(tempfile.mkdtemp())
    cwd = os.getcwd()
    try:
        for name in ("category_map.json", "console_to_admin_category_map.json", "attribute_mapping.json"):
            shutil.copy(PROJECT / name, workdir / name)
        os.chdir(workdir)
        ctx = PipelineContext(config=bazar.CONFIG, env={}, interactive=False, assume_yes=True)
        shutil.copy(real_json, ctx.resolved_input_dir() / real_json.name)
        ctx.admin_session = lambda: None
        generate_mod.fetch_admin_category = lambda session, api_base, category_id: FAKE_CATEGORY

        total = len(json.loads(real_json.read_text(encoding="utf-8"))["products"])

        # фильтр включается ТАК ЖЕ, как это делает CLI — на готовом объекте
        stage = bazar.build_pipelines()["generate"].stages[0]
        stage.set_cert_filter(file=report, green=True, white=True)
        res = stage.run(ctx)
        assert res.ok, res

        s = res.summary
        by = s["cert_by_status"]
        assert s["cert_filtered_by_color"] == by["red"], s          # отсеяны ровно красные
        assert s["cert_missing_from_report"] == 0, s                # отчёт покрывает весь JSON
        assert s["products_placed"] == by["green"] + by["white"], s
        assert s["products_placed"] + by["red"] == total, (s, total)
        print(f"  [i] всего {total}: зелёных {by['green']}, белых {by['white']}, красных {by['red']} "
              f"-> в шаблоны {s['products_placed']}")

        # --green без --white даёт строго меньше
        stage_green = bazar.build_pipelines()["generate"].stages[0]
        stage_green.set_cert_filter(file=report, green=True)
        res_green = stage_green.run(ctx)
        assert res_green.summary["products_placed"] == by["green"], res_green.summary
    finally:
        os.chdir(cwd)
        shutil.rmtree(workdir, ignore_errors=True)


def test_recipe_params_survive_filter():
    stage = generate_mod.GenerateStage(vat_default="10%", price_markup_const=15.01)
    stage.set_cert_filter(file="report.xlsx", green=True)
    assert stage.vat_default == "10%" and stage.price_markup_const == 15.01
    assert stage.cert_colors == {"green"}


def test_filter_takes_report_from_input_folder():
    """--file не нужен: отчёт берётся из входной папки склада."""
    workdir = Path(tempfile.mkdtemp())
    cwd = os.getcwd()
    try:
        os.chdir(workdir)
        ctx = PipelineContext(config=bazar.CONFIG, env={}, interactive=False, assume_yes=True)
        report = ctx.resolved_input_dir() / "отчёт.xlsx"
        _make_bazar_report(report, [("100", "FF00B050"), ("200", "FFFF0000")])
        stage = generate_mod.GenerateStage()
        stage.set_cert_filter(green=True)            # без file
        kept, stats = stage._filter_by_cert([{"id": 100}, {"id": 200}],
                                            lambda p: normalize_article(p["id"]), ctx)
        assert kept == [{"id": 100}], kept
        assert stats["source"] == "отчёт.xlsx", stats
    finally:
        os.chdir(cwd)
        shutil.rmtree(workdir, ignore_errors=True)


# ------------------------------------------------------------ 3. Мвидео --

def _make_bazar_report(path: Path, rows):
    """rows: [(id товара, rgb заливки S_CERT|None)] — формат отчёта Базара:
    шапка на 2-й строке, данные с 3-й."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Категория"
    ws.append([])
    ws.append(["N:Номер товара", "S_CERT:Сертификат"])
    for article, rgb in rows:
        ws.append([article, "текст"])
        if rgb:
            ws.cell(row=ws.max_row, column=2).fill = PatternFill("solid", fgColor=rgb)
    wb.save(path)


def _make_assortment(path: Path, rows):
    """rows: [(offer_id, текст сертификата, rgb заливки|None)]"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Материал", "Розничная цена", "Остатки", "Номер сертификата"])
    for offer_id, text, rgb in rows:
        ws.append([offer_id, 100, 5, text])
        if rgb:
            ws.cell(row=ws.max_row, column=4).fill = PatternFill("solid", fgColor=rgb)
    wb.save(path)


def test_mvideo_filter_reads_assortment_without_file_flag():
    workdir = Path(tempfile.mkdtemp())
    try:
        path = workdir / "Ассортимент.xlsx"
        _make_assortment(path, [
            ("100", "ЕАЭС RU С-KR", "FF00B050"),   # зелёный
            ("200", "#N/A", None),                  # без заливки -> white
            ("300", "#N/A", "FFCCCCCC"),            # серый -> white
            ("400", "отзыв", "FFFF0000"),           # красный
        ])
        statuses = load_cert_statuses_from_assortment(path)
        assert statuses == {"100": "green", "200": "white", "300": "white", "400": "red"}, statuses

        stage = mvideo.build_pipelines()["psb_generate"].stages[0]
        assert hasattr(stage, "set_cert_filter"), "psb_generate должен поддерживать фильтр"
        stage.set_cert_filter(file=path, green=True, white=True)
        kept, stats = stage._filter_by_cert(["100", "200", "300", "400"], normalize_article, ctx=None)
        assert kept == ["100", "200", "300"], kept
        assert stats["filtered_by_color"] == 1 and stats["missing_from_report"] == 0, stats
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_mvideo_worst_status_wins_on_duplicate_rows():
    # один offer_id много раз в файле (999 строк на ~70 товаров): при
    # расхождении побеждает «худший», а не последняя строка
    workdir = Path(tempfile.mkdtemp())
    try:
        path = workdir / "Ассортимент.xlsx"
        _make_assortment(path, [
            ("100", "есть", "FF00B050"),
            ("100", "#N/A", None),          # последняя строка — белая
            ("200", "отзыв", "FFFF0000"),
            ("200", "есть", "FF00B050"),    # последняя строка — зелёная
        ])
        conflicts = []
        statuses = load_cert_statuses_from_assortment(path, conflicts=conflicts)
        # приоритет проекта: red > green > white, порядок строк не решает
        assert statuses == {"100": "green", "200": "red"}, statuses
        assert len(conflicts) == 2, conflicts   # расхождение не проглатывается молча
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_mvideo_certificates_stage_uses_same_reader():
    from belka.stages.certificates_assortment import _scan_assortment
    workdir = Path(tempfile.mkdtemp())
    try:
        path = workdir / "Ассортимент.xlsx"
        _make_assortment(path, [("100", "есть", "FFC6EFCE"),   # пастельный зелёный
                                 ("200", "#N/A", "FFCCCCCC")])
        # светло-зелёный «Good» — это сертификат есть, а не «нет»
        assert _scan_assortment(path) == {"100": True, "200": False}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_mvideo_filter_without_cert_column_says_so():
    workdir = Path(tempfile.mkdtemp())
    try:
        path = workdir / "Ассортимент.xlsx"
        wb = openpyxl.Workbook()
        wb.active.append(["Материал", "Розничная цена", "Остатки"])   # без колонки сертификата
        wb.save(path)
        stage = PsbGenerateStage()
        stage.set_cert_filter(file=path, green=True)

        class Ctx:
            config = mvideo.CONFIG
        try:
            stage._filter_by_cert(["1"], normalize_article, ctx=Ctx())
        except RuntimeError as e:
            assert "Номер сертификата" in str(e), e
            return
        raise AssertionError("без колонки сертификата фильтр обязан сказать об этом, а не отсеять всё молча")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_filter_off_by_default():
    for stage in (generate_mod.GenerateStage(), PsbGenerateStage()):
        assert not stage.cert_filter_enabled
        kept, stats = stage._filter_by_cert([1, 2, 3], str, ctx=None)
        assert kept == [1, 2, 3] and stats == {} and cert_summary_fields(3, 3, stats) == {}


if __name__ == "__main__":
    print("=== трактовка цвета ===")
    check("red/green по оттенку, остальное white", test_color_classification)
    check("--green --white = всё, кроме красного", test_white_is_everything_but_red_and_green)
    check("счётчики: по цвету и 'нет в отчёте' раздельно", test_counters_split_color_and_missing)
    check("id из Excel-float нормализуется", test_article_normalized_from_float)

    print("\n=== Базар (реальные файлы, без сети) ===")
    check("generate --green --white отсеивает ровно красные", test_bazar_generate_green_white_on_real_files)
    check("параметры рецепта переживают включение фильтра", test_recipe_params_survive_filter)
    check("отчёт берётся из папки склада, без --file", test_filter_takes_report_from_input_folder)

    print("\n=== Мвидео ===")
    check("psb_generate фильтрует по файлу-ассортименту без --file", test_mvideo_filter_reads_assortment_without_file_flag)
    check("дубли строк: побеждает худший статус", test_mvideo_worst_status_wins_on_duplicate_rows)
    check("stage 'certificates' читает тем же кодом", test_mvideo_certificates_stage_uses_same_reader)
    check("нет колонки сертификата — понятная ошибка", test_mvideo_filter_without_cert_column_says_so)
    check("без флагов фильтр выключен", test_filter_off_by_default)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n=== ИТОГ: {len(RESULTS) - len(failed)}/{len(RESULTS)} ===")
    sys.exit(1 if failed else 0)
