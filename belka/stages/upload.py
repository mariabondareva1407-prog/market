#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 'upload' — грузит xlsx-шаблоны на сайт через API импорта товаров:
  1. POST .../product-imports:preload-file (бинарник файла) -> preload_file_id
  2. POST .../product-imports (type, preload_file_id, seller_id, store_id) -> import_id
  3. POST .../product-imports:search — опрашиваем, пока статус не станет
     терминальным (3 успех / 4 ошибка -> retry до MAX_IMPORT_RETRIES раз).

Идемпотентно: ведёт upload_log.json в папке с шаблонами — уже успешно
загруженные файлы при повторном запуске пропускаются.

09.2026: при ошибке импорта (статус 4) теперь не просто печатается "ошибка
(статус 4)", а сохраняется и печатается ПОЛНЫЙ сырой ответ
product-imports:search (`raw_response` в upload_log.json) плюс попытка
угадать человекочитаемую причину по типовым именам полей (`_extract_error_hint`
— эвристика). Отдельно от этого, пользователь прислал HAR-дамп сессии в
админке (2026-09-12) — так нашлась настоящая "ручка" журнала ошибок,
которую страница `/products/import/errors` дёргает под капотом:

  4. POST .../product-import-warnings:search — журнал предупреждений/ошибок
     ИМПОРТА (в интерфейсе называется "Ошибки", хотя ручка "warnings"),
     поля: id, import_id, vendor_code (может быть null — тогда проблема не
     привязана к конкретной строке/товару, а к файлу/колонке целиком),
     import_type, message, created_at.

Важное открытие из HAR: в этот журнал попадают записи и для ИМПОРТОВ СО
СТАТУСОМ 3 (успех) — например неоднозначное сопоставление колонки
("Column X matched both system field and category property") или
несбойка одной картинки при в целом успешном импорте. То есть раньше
`status: 3` молча трактовался как "всё ок", хотя по факту при этом статусе
отдельные строки/поля/картинки могли не примениться — теперь `upload`
запрашивает этот журнал по `import_id` ПОСЛЕ ЛЮБОГО терминального статуса,
не только после ошибки.

Фильтрация журнала по `import_id` в `:search` не подтверждена документально
(в `product-import-warnings:meta` этого поля нет среди `fields`, только
`id`/`import_type`/`vendor_code`/`message`/`created_at` — но и у самого
`product-imports:search` `:meta` для `id` тоже стоит `"filter": null`,
хотя рабочий фильтр `{"id": import_id}` там есть, см.
learnings-and-conventions.md — значит `:meta` тут вообще не описывает,
что реально фильтруется) — пробуем `filter: {"import_id": ...}` первой
попыткой (см. `_api_fetch_import_warnings`); если вернувшиеся записи
имеют ДРУГОЙ import_id, значит фильтр проигнорирован.

09.2026 (правка после вопроса пользователя "зачем 1000, там можно
посмотреть по id товара и времени записи"): раньше fallback был "слепым"
постраничным просмотром общего журнала с фиксированной отсечкой в 1000
записей (10 страниц) — независимо от того, нашёлся наш импорт на первой
же странице или нет. Проверка по присланному HAR показала: у записи
журнала предупреждений `created_at` всегда попадает МЕЖДУ `created_at` и
`updated_at` самого импорта (пример: импорт id=1344 создан в 20:23:02,
завершён в 20:23:08, его предупреждения — все с created_at 20:23:04).
Раз общий журнал отсортирован по убыванию `id` (= по убыванию времени,
т.к. id автоинкрементный), можно останавливать постраничный просмотр
СРАЗУ, как только на очередной странице все записи стали старше момента
создания нашего импорта (`item["created_at"]` из ответа
`product-imports:search`, с запасом в WARNINGS_TIME_BUFFER_MINUTES на
рассинхрон часов) — дальше в списке будут только более старые записи, из
других, более ранних импортов. На практике (наш импорт почти всегда
самый свежий в общем журнале) это 1 страница вместо 10.
`WARNINGS_FALLBACK_MAX_PAGES` остаётся как абсолютный предохранитель на
случай, если часы разъехались или `created_at` не пришёл вовсе.

09.2026 (ещё одна правка по тому же вопросу пользователя, вторая
половина — "надо дописать из какого это магазина и шаблона" +
"будет ли сохраняться история загрузки"): запись из product-import-warnings
сама по себе не говорит, с какого склада (мвидео/б2с/базар — journal один
на все склады/продавцов) и из какого файла-шаблона она взялась. `run()`
теперь дописывает `warehouse` (`cfg.key`) и `template_file` (имя xlsx) —
и в результат файла целиком, и в каждую запись внутри `warnings`. Плюс:
раньше повторный запуск stage на тот же файл ЗАТИРАЛ предыдущую запись в
upload_log.json целиком — если файл сначала падал, а потом (после правки)
проходил успешно, детали неудачной попытки терялись. Теперь `run()`
копит `history` — список всех попыток по файлу за сегодня; верхний
уровень записи по-прежнему отражает только последнюю попытку, так что
идемпотентность (`status == "success"` -> не грузим повторно) не
изменилась. Между днями история и так сохраняется естественно — у
каждого дня свой upload_log.json в output/<склад>/<дата>/.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

from ..core.jsonio import load_json_file, save_json_file
from ..core.http import SessionBlockedError
from ..pipeline import Stage, StageResult

PRELOAD_URL_PART = "/api/v1/catalog/product-imports:preload-file"
IMPORT_CREATE_URL_PART = "/api/v1/catalog/product-imports"
IMPORT_SEARCH_URL_PART = "/api/v1/catalog/product-imports:search"
IMPORT_WARNINGS_URL_PART = "/api/v1/catalog/product-import-warnings:search"

IMPORT_TYPE = 1
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

DELAY_BETWEEN_IMPORT_CREATIONS = 2.0   # секунд, между шагами 2 (создание импорта) для разных файлов
POLL_INTERVAL_SECONDS = 5.0            # пауза между проверками статуса
POLL_TIMEOUT_SECONDS = 300             # сколько максимум ждать терминального статуса на файл
MAX_IMPORT_RETRIES = 3                 # всего попыток на файл при статусе 4 (ошибка)

WARNINGS_PAGE_LIMIT = 100
WARNINGS_FALLBACK_MAX_PAGES = 10       # абсолютный предохранитель (см. docstring _api_fetch_import_warnings) —
                                        # реально просмотр останавливается раньше, по времени (WARNINGS_TIME_BUFFER_MINUTES)
WARNINGS_TIME_BUFFER_MINUTES = 5       # запас перед created_at импорта на случай рассинхрона часов
WARNINGS_PRINT_LIMIT = 20              # сколько строк печатать в консоль (остальное — только в upload_log.json)

STATUS_QUEUED = 1
STATUS_PROCESSING = 2
STATUS_SUCCESS = 3
STATUS_ERROR = 4
IN_PROGRESS_STATUSES = {STATUS_QUEUED, STATUS_PROCESSING}


class UploadStage(Stage):
    name = "upload"

    def __init__(self, folder=None):
        self.folder_override = Path(folder) if folder else None

    @staticmethod
    def _api_preload_file(session, api_base, file_bytes, filename):
        url = f"{api_base}{PRELOAD_URL_PART}"
        files = {"file": (filename, file_bytes, XLSX_CONTENT_TYPE)}
        resp = session.post(url, files=files, timeout=120)
        return resp.json()["data"]["preload_file_id"]

    @staticmethod
    def _api_create_import(session, api_base, preload_file_id, seller_id, store_id):
        url = f"{api_base}{IMPORT_CREATE_URL_PART}"
        payload = {"type": IMPORT_TYPE, "preload_file_id": preload_file_id, "seller_id": seller_id, "store_id": store_id}
        resp = session.post(url, json=payload, timeout=30)
        return resp.json()["data"]

    @staticmethod
    def _api_check_import_status(session, api_base, import_id):
        url = f"{api_base}{IMPORT_SEARCH_URL_PART}"
        payload = {"sort": [], "filter": {"id": import_id}, "pagination": {"type": "offset", "offset": 0, "limit": 1}}
        resp = session.post(url, json=payload, timeout=30)
        data = resp.json().get("data", [])
        return data[0] if data else None

    # Ключи, под которыми в подобных API обычно приходит человекочитаемая
    # причина ошибки — ни один не подтверждён на реальном ответе (в
    # api-reference.md задокументированы только сами статусы 1-4, без
    # структуры ошибки), поэтому это ТОЛЬКО эвристика для короткой строки в
    # консоли. Главное — не она, а то, что ПОЛНЫЙ сырой `item` в любом случае
    # сохраняется в upload_log.json (см. _upload_one_file) — так реальная
    # структура ошибки станет видна при первой же настоящей ошибке импорта,
    # даже если ни один из этих ключей не угадан.
    _ERROR_HINT_KEYS = ("message", "error", "errors", "comment", "description",
                        "reason", "details", "error_message", "log", "logs")

    @classmethod
    def _extract_error_hint(cls, item: dict) -> str | None:
        found = {k: item[k] for k in cls._ERROR_HINT_KEYS if item.get(k) not in (None, "", [], {})}
        if not found:
            return None
        return "; ".join(f"{k}={v!r}" for k, v in found.items())

    # ------------------------------------------------- журнал предупреждений/ошибок импорта --

    @staticmethod
    def _parse_api_datetime(value: str | None):
        """Парсит `created_at`/`updated_at` вида '2026-09-11T20:23:02.000000Z'
        в aware datetime (UTC). Формат фиксированный и с ведущими нулями,
        поэтому для простого "новее/старее" сравнения хватило бы и строкового
        сравнения, но парсинг нужен, чтобы вычесть буфер (WARNINGS_TIME_BUFFER_MINUTES)."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _api_fetch_import_warnings(session, api_base, import_id, not_before: str | None = None) -> tuple[list, bool]:
        """Возвращает (записи, truncated) из product-import-warnings:search
        ИМЕННО для этого import_id — в интерфейсе админки это страница
        "Ошибки" (`/products/import/errors`), хотя ручка называется
        "warnings": сюда попадают и мягкие предупреждения на УСПЕШНОМ
        импорте (например, "Column X matched both system field and category
        property"), и жёсткие ошибки по конкретной строке/полю (например
        "image ... was not imported: Client error"), независимо от
        итогового статуса самого импорта — см. подтверждённый пример в HAR,
        присланном пользователем 2026-09-12 (import_id с status=3, но с
        предупреждениями).

        Сначала пробуем серверный filter по import_id (не подтверждено
        документацией — product-import-warnings:meta не перечисляет
        import_id среди filterable-полей, но и другие ручки этого API часто
        фильтруют по полям, которых нет в :meta, см. products/drafts:search
        в certificates.py). Если пришедшие записи имеют ДРУГОЙ import_id —
        значит filter проигнорирован, переключаемся на постраничный обзор
        ОБЩЕГО журнала (сортировка по убыванию id — сначала новые).

        `not_before` — `created_at` САМОГО импорта (из product-imports:search),
        не строка-фильтр для сервера (сервер её не поддерживает), а ОРИЕНТИР
        для клиентской остановки: по HAR подтверждено, что created_at записи
        в журнале предупреждений всегда лежит МЕЖДУ created_at и updated_at
        своего импорта (пример: импорт id=1344 создан в 20:23:02, завершён в
        20:23:08, предупреждения — все с created_at 20:23:04). Раз общий
        список отсортирован по убыванию id (= по убыванию времени), как
        только на очередной странице ВСЕ записи старше `not_before` минус
        WARNINGS_TIME_BUFFER_MINUTES — дальше нет смысла смотреть, там будут
        только более старые чужие импорты. Без `not_before` (не пришёл
        created_at) используется старое поведение — фиксированная отсечка
        в WARNINGS_FALLBACK_MAX_PAGES страниц.

        truncated=True означает "мог найтись не весь список" — только для
        fallback-ветки, когда бюджет страниц исчерпан (по времени или по
        абсолютному пределу страниц), а конца общего списка мы не увидели."""
        url = f"{api_base}{IMPORT_WARNINGS_URL_PART}"

        def _fetch_page(filter_body, offset):
            payload = {"sort": ["-id"], "filter": filter_body,
                       "pagination": {"type": "offset", "offset": offset, "limit": WARNINGS_PAGE_LIMIT}}
            resp = session.post(url, json=payload, timeout=30)
            return resp.json().get("data", []) or []

        first_page = _fetch_page({"import_id": import_id}, 0)
        if not first_page:
            # либо предупреждений действительно нет (частый случай — не
            # платим за дорогой fallback-скан ради редкого сценария
            # "фильтр сломан, а на первой странице случайно пусто"),
            # либо filter не поддерживается, но тогда узнаем об этом в
            # СЛЕДУЮЩИЙ раз, когда предупреждения реально будут — см. ниже
            return [], False
        if all(item.get("import_id") == import_id for item in first_page):
            warnings = list(first_page)
            offset = len(first_page)
            while len(first_page) == WARNINGS_PAGE_LIMIT:
                first_page = _fetch_page({"import_id": import_id}, offset)
                warnings.extend(first_page)
                offset += len(first_page)
            return warnings, False

        # filter по import_id проигнорирован (вернулись чужие записи) —
        # fallback: смотрим общий список постранично, сами отбираем "наши",
        # останавливаясь по времени (см. docstring), а не по фиксированному
        # числу записей.
        cutoff = UploadStage._parse_api_datetime(not_before)
        if cutoff is not None:
            cutoff = cutoff - timedelta(minutes=WARNINGS_TIME_BUFFER_MINUTES)

        warnings = []
        truncated = True
        for page in range(WARNINGS_FALLBACK_MAX_PAGES):
            page_items = _fetch_page({}, page * WARNINGS_PAGE_LIMIT)
            if not page_items:
                truncated = False
                break
            warnings.extend(item for item in page_items if item.get("import_id") == import_id)
            if len(page_items) < WARNINGS_PAGE_LIMIT:
                truncated = False
                break
            if cutoff is not None:
                oldest_on_page = UploadStage._parse_api_datetime(page_items[-1].get("created_at"))
                if oldest_on_page is not None and oldest_on_page < cutoff:
                    # дальше в общем журнале — только записи старше момента
                    # создания нашего импорта (с запасом), можно остановиться
                    truncated = False
                    break
        return warnings, truncated

    @staticmethod
    def _print_warnings(warnings: list, truncated: bool) -> None:
        if not warnings:
            print("    [i] В журнале product-import-warnings записей по этому импорту не найдено.")
            return
        print(f"    [i] Записей в журнале импорта (product-import-warnings): {len(warnings)}")
        for w in warnings[:WARNINGS_PRINT_LIMIT]:
            vc = w.get("vendor_code") or "(без артикула — относится к файлу/колонке целиком)"
            print(f"      - [{vc}] {w.get('message')}")
        if len(warnings) > WARNINGS_PRINT_LIMIT:
            print(f"      ... и ещё {len(warnings) - WARNINGS_PRINT_LIMIT}, полный список в upload_log.json")
        if truncated:
            print(f"      [!] Фильтр по import_id в product-import-warnings:search не сработал, а по времени "
                  f"остановиться не получилось (нет created_at импорта или лимит в "
                  f"{WARNINGS_FALLBACK_MAX_PAGES * WARNINGS_PAGE_LIMIT} записей исчерпан раньше) — список для "
                  f"этого импорта может быть неполным.")

    def _upload_one_file(self, session, api_base, path: Path, seller_id: int, store_id: int) -> dict:
        file_bytes = path.read_bytes()
        last_import_id = None
        last_item = None
        last_warnings: list = []
        last_warnings_truncated = False

        for attempt in range(1, MAX_IMPORT_RETRIES + 1):
            print(f"  Попытка {attempt}/{MAX_IMPORT_RETRIES}: preload...")
            preload_file_id = self._api_preload_file(session, api_base, file_bytes, path.name)
            print(f"  preload_file_id={preload_file_id} -> создаю импорт...")
            import_data = self._api_create_import(session, api_base, preload_file_id, seller_id, store_id)
            import_id = import_data["id"]
            last_import_id = import_id
            print(f"  import_id={import_id}, статус сразу после создания: {import_data.get('status')}")
            time.sleep(DELAY_BETWEEN_IMPORT_CREATIONS)

            started = time.monotonic()
            final_status = None
            while True:
                item = self._api_check_import_status(session, api_base, import_id)
                if item is None:
                    print(f"  [!] Импорт id={import_id} не найден в product-imports:search")
                    break
                last_item = item
                status = item.get("status")
                if status == STATUS_SUCCESS:
                    print(f"  ✔ Импорт id={import_id}: успех (статус 3)")
                    warnings, w_trunc = self._api_fetch_import_warnings(session, api_base, import_id, not_before=item.get("created_at"))
                    self._print_warnings(warnings, w_trunc)
                    return {"status": "success", "import_id": import_id, "attempts": attempt, "detail": None,
                            "warnings": warnings, "warnings_truncated": w_trunc}
                if status == STATUS_ERROR:
                    hint = self._extract_error_hint(item)
                    print(f"  ✘ Импорт id={import_id}: ошибка (статус 4)")
                    if hint:
                        print(f"    Похоже на причину ошибки: {hint}")
                    print(f"    Полный ответ product-imports:search: {item!r}")
                    last_warnings, last_warnings_truncated = self._api_fetch_import_warnings(session, api_base, import_id, not_before=item.get("created_at"))
                    self._print_warnings(last_warnings, last_warnings_truncated)
                    final_status = STATUS_ERROR
                    break
                if status in IN_PROGRESS_STATUSES:
                    if time.monotonic() - started > POLL_TIMEOUT_SECONDS:
                        print(f"  [!] Импорт id={import_id}: таймаут ожидания (>{POLL_TIMEOUT_SECONDS}с), последний статус {status}")
                        warnings, w_trunc = self._api_fetch_import_warnings(session, api_base, import_id, not_before=item.get("created_at"))
                        self._print_warnings(warnings, w_trunc)
                        return {"status": "timeout", "import_id": import_id, "attempts": attempt,
                                "detail": f"последний статус {status}", "raw_response": item,
                                "warnings": warnings, "warnings_truncated": w_trunc}
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue
                print(f"  [!] Импорт id={import_id}: неожиданный статус {status} — останавливаюсь по этому файлу")
                warnings, w_trunc = self._api_fetch_import_warnings(session, api_base, import_id, not_before=item.get("created_at"))
                self._print_warnings(warnings, w_trunc)
                return {"status": "unknown", "import_id": import_id, "attempts": attempt,
                        "detail": f"неожиданный статус {status}", "raw_response": item,
                        "warnings": warnings, "warnings_truncated": w_trunc}

            if final_status == STATUS_ERROR and attempt < MAX_IMPORT_RETRIES:
                print(f"  Повторяю загрузку файла (попытка {attempt + 1}/{MAX_IMPORT_RETRIES})...")
                continue

        return {"status": "failed", "import_id": last_import_id, "attempts": MAX_IMPORT_RETRIES,
                "detail": f"статус 4 после {MAX_IMPORT_RETRIES} попыток",
                "error_hint": self._extract_error_hint(last_item) if last_item else None,
                "raw_response": last_item,
                "warnings": last_warnings, "warnings_truncated": last_warnings_truncated}

    def run(self, ctx) -> StageResult:
        cfg = ctx.config
        session = ctx.admin_session()

        # Переопределения — только ключами с именем склада
        # (BELKA_<СКЛАД>_SELLER_ID / _STORE_ID / _UPLOAD_DIR, см.
        # PipelineContext.override). Безымянные BELKA_SELLER_ID/BELKA_STORE_ID/
        # BELKA_UPLOAD_DIR были общими на все склады и молча уводили загрузку
        # одного склада в параметры другого — теперь они игнорируются с
        # предупреждением, а значения берутся из рецепта склада.
        seller_id = int(ctx.override("SELLER_ID") or cfg.seller_id)
        store_id = int(ctx.override("STORE_ID") or cfg.store_id)

        folder = self.folder_override or ctx.resolved_upload_dir()
        if not folder.is_dir():
            raise RuntimeError(f"Папка не найдена: {folder}")
        files = sorted(folder.glob("*.xlsx"))
        if not files:
            raise RuntimeError(f"В папке {folder} нет .xlsx файлов.")

        log_path = folder / cfg.upload_log_file
        upload_log = load_json_file(log_path, {})
        already_done = [f.name for f in files if upload_log.get(f.name, {}).get("status") == "success"]
        to_upload = [f for f in files if f.name not in already_done]

        print(f"Всего файлов: {len(files)}. Уже успешно загружены ранее (пропускаю): {len(already_done)}.")
        print(f"К загрузке сейчас: {len(to_upload)}")
        if not to_upload:
            print("Нечего загружать.")
            return StageResult(self.name, ok=True, summary={"uploaded": 0, "already_done": len(already_done)})

        print("\nБудут загружены:")
        for f in to_upload:
            print(f"  - {f.name}")
        print(f"\nseller_id={seller_id}, store_id={store_id}, api_base={cfg.admin_api_base}")
        if not ctx.confirm("\nПродолжить загрузку?"):
            print("Отменено.")
            return StageResult(self.name, ok=True, summary={"uploaded": 0, "cancelled": True})

        blocked = False
        for path in to_upload:
            print(f"\n=== {path.name} ===")
            try:
                result = self._upload_one_file(session, cfg.admin_api_base, path, seller_id, store_id)
            except SessionBlockedError as e:
                print(f"\n⛔ ОСТАНОВКА: {e}")
                print("Загрузка прервана — обновите токен и перезапустите (уже успешные файлы не задублируются).")
                blocked = True
                break

            # 09.2026: каждая запись из product-import-warnings содержит
            # только vendor_code ("id товара") — сам по себе он не говорит,
            # с какого склада (мвидео/б2с/базар) и из какого шаблона взялась
            # проблема. Т.к. warnings приходят "как есть" от API и общего
            # для всех складов journal'а, размечаем и сам результат файла, и
            # каждую запись warnings своими store/template — так строка
            # остаётся понятной сама по себе, даже если её потом выдернуть
            # из upload_log.json без остального контекста (папки).
            for w in (result.get("warnings") or []):
                w["warehouse"] = cfg.key
                w["template_file"] = path.name

            attempt = {**result, "warehouse": cfg.key, "template_file": path.name,
                       "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            # История загрузок: раньше запись в upload_log.json на файл
            # просто ЗАТИРАЛАСЬ при повторном запуске — если файл сначала
            # упал, а потом (после правки) прошёл успешно, детали неудачной
            # попытки терялись безвозвратно. Теперь прошлые попытки (в т.ч.
            # за сегодня, если stage перезапускали) копятся в "history" —
            # верхний уровень (`status`/`warnings`/... прямо в
            # upload_log[filename]) по-прежнему отражает ТОЛЬКО последнюю
            # попытку (существующая проверка идемпотентности
            # `.get("status") == "success"` продолжает работать как раньше).
            # Между днями история и так сохраняется естественно — у каждого
            # дня свой upload_log.json в output/<склад>/<дата>/.
            history = list(upload_log.get(path.name, {}).get("history", []))
            history.append(attempt)
            upload_log[path.name] = {**attempt, "history": history}
            save_json_file(log_path, upload_log)

        counts = {}
        for f in files:
            st = upload_log.get(f.name, {}).get("status", "не загружался")
            counts[st] = counts.get(st, 0) + 1
        summary = {"by_status": counts, "log": str(log_path), "blocked": blocked}
        print("\n=== ИТОГ upload ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        return StageResult(self.name, ok=not blocked, summary=summary)
