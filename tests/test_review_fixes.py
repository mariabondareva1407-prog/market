#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты на три правки по итогам ревью 2026-09-13 (см.
claude/code-review-belka-2026-09-13.md, пункты 1-3).

Сети тут нет: всё, что ходит в API, подменяется заглушками — проверяется
ровно поведение пайплайна, а не сервер.

Запуск:  python3 tests/test_review_fixes.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from belka.context import PipelineContext                              # noqa: E402
from belka.core.cert_log import load_cert_log, save_cert_log, CertLogOwnerMismatch  # noqa: E402
from belka.core.categories import clean_stale_catalog_files, default_catalog_filename  # noqa: E402
from belka.warehouses import bazar, mvideo, b2c                        # noqa: E402

RESULTS = []


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


def ctx_for(module, env=None, tmp=None):
    ctx = PipelineContext(config=module.CONFIG, env=env or {}, interactive=False, assume_yes=False)
    if tmp:
        ctx.run_date = "2026-09-13"
    return ctx


# ---------------------------------------------------------------- фикс 1 --

def test_cert_log_paths_differ_per_warehouse():
    """Журнал сертификатов у каждого склада свой; Базар сохраняет исторический файл."""
    assert ctx_for(bazar).resolved_cert_log_file() == Path("deactivated_by_certificate.json")
    assert ctx_for(mvideo).resolved_cert_log_file() == Path("deactivated_by_certificate_mvideo.json")
    assert ctx_for(b2c).resolved_cert_log_file() == Path("deactivated_by_certificate_b2c.json")
    paths = {str(ctx_for(m).resolved_cert_log_file()) for m in (bazar, mvideo, b2c)}
    assert len(paths) == 3, paths


def test_cert_log_rejects_foreign_warehouse():
    """Журнал, помеченный чужим складом, не читается — вместо массовой активации ошибка."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "log.json"
        save_cert_log(p, "bazar", {"125605": {"id": 1, "deactivated_at": "2026-09-11T00:00:00"}})

        assert load_cert_log(p, "bazar") == {"125605": {"id": 1, "deactivated_at": "2026-09-11T00:00:00"}}
        assert "__meta__" not in load_cert_log(p, "bazar"), "служебная запись не должна попадать в артикулы"

        try:
            load_cert_log(p, "mvideo")
        except CertLogOwnerMismatch as e:
            assert "bazar" in str(e) and "mvideo" in str(e)
        else:
            raise AssertionError("чужой склад должен получать CertLogOwnerMismatch")


def test_cert_log_reads_legacy_file_without_meta():
    """Старый журнал (без маркера) читается как есть и получает маркер при записи."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "legacy.json"
        p.write_text(json.dumps({"125605": {"id": 1}}), encoding="utf-8")

        entries = load_cert_log(p, "bazar")
        assert entries == {"125605": {"id": 1}}

        save_cert_log(p, "bazar", entries)
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert raw["__meta__"]["warehouse"] == "bazar"
        assert list(raw)[0] == "__meta__", "маркер владельца должен быть первым ключом"


def test_real_bazar_log_is_not_readable_as_mvideo():
    """Тот самый сценарий из ревью, на реальном файле из архива."""
    real = Path(__file__).resolve().parent.parent / "deactivated_by_certificate.json"
    if not real.is_file():
        print("    (пропущено: deactivated_by_certificate.json нет в рабочей папке)")
        return

    entries = load_cert_log(real, "bazar")
    assert len(entries) >= 190, f"ожидались записи Базара, найдено {len(entries)}"

    # до правки этот же файл открывался бы складом mvideo и все его ключи
    # ушли бы в to_activate; теперь путь у mvideo другой и файла там нет
    assert ctx_for(mvideo).resolved_cert_log_file() != real
    assert load_cert_log(ctx_for(mvideo).resolved_cert_log_file(), "mvideo") == {}


# ---------------------------------------------------------------- фикс 2 --

def test_generate_cleans_stale_catalogs():
    """GenerateStage чистит catalog_*.xlsx прошлого прогона и не трогает состояние."""
    import inspect
    from belka.stages import generate

    src = inspect.getsource(generate.GenerateStage.run)
    assert "clean_stale_catalog_files(output_dir)" in src, "вызов очистки отсутствует в generate.run"
    assert "default_catalog_filename" in inspect.getsource(generate.GenerateStage._process_category)

    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "catalog_26362_26-09-13-11.xlsx").write_text("старый прогон")
        (out / "catalog_26385_26-09-13-11.xlsx").write_text("старый прогон")
        (out / "upload_log.json").write_text("{}")
        (out / "added_values.json").write_text("{}")

        removed = clean_stale_catalog_files(out)
        assert removed == 2, removed
        assert not list(out.glob("catalog_*.xlsx"))
        assert (out / "upload_log.json").is_file(), "персистентное состояние трогать нельзя"
        assert (out / "added_values.json").is_file()


def test_catalog_filename_shared_between_generators():
    """Имя файла у Bazar и ПСБ строится одной функцией — значит glob очистки совпадает."""
    name = default_catalog_filename(26362)
    assert name.startswith("catalog_26362_") and name.endswith(".xlsx")
    assert Path(name).match("catalog_*.xlsx")


# ---------------------------------------------------------------- фикс 3 --

def test_unscoped_env_override_is_ignored():
    """Безымянный BELKA_STORE_ID не применяется — берётся значение рецепта."""
    ctx = ctx_for(mvideo, env={"BELKA_STORE_ID": "5", "BELKA_SELLER_ID": "6"})
    assert ctx.override("STORE_ID") is None
    store_id = int(ctx.override("STORE_ID") or ctx.config.store_id)
    assert store_id == 6, f"Мвидео должен грузиться в склад 6, а не {store_id}"


def test_scoped_env_override_applies():
    """Ключ с именем склада работает и не задевает соседний склад."""
    env = {"BELKA_MVIDEO_STORE_ID": "61", "BELKA_BAZAR_STORE_ID": "5"}
    assert ctx_for(mvideo, env=env).override("STORE_ID") == "61"
    assert ctx_for(bazar, env=env).override("STORE_ID") == "5"
    assert ctx_for(b2c, env=env).override("STORE_ID") is None


def test_dirs_are_per_warehouse_despite_unscoped_env():
    """BELKA_UPLOAD_DIR/BELKA_TEMPLATES_DIR больше не уводят склад в чужую папку."""
    env = {"BELKA_UPLOAD_DIR": "output/bazar/2026-09-11", "BELKA_TEMPLATES_DIR": "output/bazar/2026-09-11"}
    with tempfile.TemporaryDirectory() as d:
        cwd = os.getcwd()
        os.chdir(d)
        try:
            ctx = ctx_for(mvideo, env=env)
            assert ctx.resolved_upload_dir() == Path("output/mvideo") / ctx.run_date
            assert ctx.resolved_templates_dir() == Path("output/mvideo") / ctx.run_date

            scoped = ctx_for(mvideo, env={"BELKA_MVIDEO_UPLOAD_DIR": "output/mvideo/2026-09-01"})
            assert scoped.resolved_upload_dir() == Path("output/mvideo/2026-09-01")
        finally:
            os.chdir(cwd)


def test_psb_seller_id_override_scoped():
    env = {"BELKA_PSB_SELLER_ID": "141"}   # безымянный — раньше увёл бы Мвидео на продавца b2c
    ctx = ctx_for(mvideo, env=env)
    assert int(ctx.override("PSB_SELLER_ID") or ctx.config.psb_seller_id) == 475


if __name__ == "__main__":
    print("=== фикс 1: журнал сертификатов по складам ===")
    check("пути журналов различаются по складам", test_cert_log_paths_differ_per_warehouse)
    check("чужой склад не может открыть журнал", test_cert_log_rejects_foreign_warehouse)
    check("старый журнал без маркера читается", test_cert_log_reads_legacy_file_without_meta)
    check("реальный журнал Базара недоступен Мвидео", test_real_bazar_log_is_not_readable_as_mvideo)

    print("\n=== фикс 2: очистка старых каталогов у Базара ===")
    check("generate чистит catalog_*.xlsx", test_generate_cleans_stale_catalogs)
    check("имя каталога общее у обоих генераторов", test_catalog_filename_shared_between_generators)

    print("\n=== фикс 3: переопределения .env по складу ===")
    check("безымянный BELKA_STORE_ID игнорируется", test_unscoped_env_override_is_ignored)
    check("BELKA_<СКЛАД>_STORE_ID применяется", test_scoped_env_override_applies)
    check("папки не уводятся безымянными ключами", test_dirs_are_per_warehouse_despite_unscoped_env)
    check("psb_seller_id не подменяется чужим", test_psb_seller_id_override_scoped)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n=== ИТОГ: {len(RESULTS) - len(failed)}/{len(RESULTS)} ===")
    sys.exit(1 if failed else 0)
