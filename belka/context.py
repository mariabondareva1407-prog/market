#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
belka.context — конфиг склада (WarehouseConfig) и контекст исполнения
(PipelineContext).

WarehouseConfig — это и есть "рецепт" склада в смысле параметров: id
продавца/склада, хосты, пути к файлам. Какие stage'и из него собраны в
какие пайплайны — уже дело belka/warehouses/<склад>.py.

PipelineContext — один на запуск пайплайна (или одного stage'а через CLI).
Лениво создаёт и кеширует HTTP-сессии, чтобы все stage'и внутри одного
прогона делили один и тот же токен/AuthTokenManager, а не логинились
заново на каждом шаге.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .core.http import ThrottledSession, build_auth_manager, REQUEST_DELAY_DEFAULT, \
    MAX_CONSECUTIVE_403_DEFAULT, MAX_RETRIES_DEFAULT, RETRY_BACKOFF_BASE_DEFAULT
from .core import defaults as placeholder_defaults

BROWSER_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


@dataclass
class WarehouseConfig:
    """Всё, чем один склад отличается от другого."""

    key: str
    display_name: str
    seller_id: int
    store_id: int

    admin_api_base: str = "https://admin.belkamarket.ru"
    admin_token_env: str = "BELKA_TOKEN"
    admin_login_env: str = "BELKA_LOGIN"
    admin_password_env: str = "BELKA_PASSWORD"

    # у не-Bazar складов моста категорий может не быть вовсе — тогда просто
    # не задавайте console_api_base, stage 'bridge' им и не понадобится
    console_api_base: str | None = None
    console_token_env: str = "BELKA_CONSOLE_TOKEN"
    console_login_env: str = "BELKA_CONSOLE_LOGIN"
    console_password_env: str = "BELKA_CONSOLE_PASSWORD"

    # ПСБ Маркет (apigw-seller) — общий источник каталога для складов №6
    # (Мвидео) и №7 (b2c), см. belka/stages/psb_generate.py и
    # belka/core/psb_api.py. Не задан у складов, которым не нужен (Bazar).
    psb_api_base: str | None = None
    psb_token_env: str = "PSB_TOKEN"     # статический Bearer, без refresh — ни в одном из
                                          # присланных скриптов нет механизма его обновления
    psb_seller_id: int | None = None     # id продавца НА СТОРОНЕ ПСБ — НЕ seller_id Белки выше
                                          # (b2c=141, Мвидео=475 — см. рецепты складов)
                                          # (offer_id + "Розничная цена" + "Остатки"), см. psb_api.py

    # Префикс штрихкодов, которые пайплайн генерирует САМ (когда у товара нет
    # своего). Общий для всех складов; меняется в рецепте, когда у склада
    # появится свой диапазон. Итоговый штрихкод — ровно 13 символов, см.
    # belka/core/columns.py::build_barcode.
    barcode_prefix: str = "4700"

    # Точечные переопределения общего реестра заглушек (belka/core/defaults.py):
    # только те ключи, что отличаются у этого склада; значение None = «этому
    # складу заглушку не подставлять, оставить пусто и сообщить».
    placeholders: dict = field(default_factory=dict)

    # Обработка данных источника, включается в рецепте склада (09.2026, пока
    # только Мвидео). Логика — в core/images.py::rotate_images и
    # core/text.py::strip_html; применяет их stage psb_generate.
    #: убрать основное и последнее доп. изображение, основное <- доп. №1
    rotate_images: bool = False
    #: снимать HTML-теги в "Описании" (переносы строк сохраняются)
    strip_html_description: bool = False

    input_dir: str = "input"
    output_dir: str = "output"
    upload_dir: str | None = None       # по умолчанию = output_dir (см. resolved_upload_dir)
    templates_dir: str | None = None    # по умолчанию = output_dir (вход для import)

    category_map_file: str = "category_map.json"
    category_bridge_file: str = "console_to_admin_category_map.json"
    unmatched_bridge_report_file: str = "category_bridge_unmatched.csv"
    # None = имя выводится из ключа склада: attribute_mapping_<склад>.json
    # (см. PipelineContext.resolved_attribute_mapping_file). Ключ записи —
    # display_name колонки Белки, общий для складов, а имена атрибутов
    # источника и дефолты — разные, поэтому файл раздельный.
    attribute_mapping_file: str | None = None
    #: спрашивать про обязательный атрибут, которого нет в файле маппинга.
    #: True у Базара (имена у поставщика и у Белки систематически разные),
    #: False у ПСБ-складов (attributeName апигв уже в терминах Белки).
    attribute_prompt: bool = True
    # None = имя выводится из ключа склада: deactivated_by_certificate_<склад>.json
    # (см. PipelineContext.resolved_cert_log_file). Задавать явно нужно только
    # там, где важно сохранить исторический файл — см. bazar.py.
    cert_log_file: str | None = None
    added_values_log_file: str = "added_values.json"
    upload_log_file: str = "upload_log.json"

    request_delay: float = REQUEST_DELAY_DEFAULT
    max_consecutive_403: int = MAX_CONSECUTIVE_403_DEFAULT
    max_retries: int = MAX_RETRIES_DEFAULT
    retry_backoff_base: float = RETRY_BACKOFF_BASE_DEFAULT

    # resolved_upload_dir/resolved_templates_dir переехали на PipelineContext
    # (см. ниже) — там, где известна дата прогона (для дневной папки
    # output_dir/ГГГГ-ММ-ДД), а не только сам WarehouseConfig.


class PipelineContext:
    """Один на запуск пайплайна (или отдельного stage'а). Даёт доступ к
    .env, конфигу склада и HTTP-сессиям."""

    def __init__(self, config: WarehouseConfig, env: dict, interactive: bool = True,
                 assume_yes: bool = False, env_path: str = ".env"):
        self.config = config
        self.env = env
        self.interactive = interactive
        self.assume_yes = assume_yes
        self.env_path = env_path
        self._admin_session: ThrottledSession | None = None
        self._console_session: ThrottledSession | None = None
        self._psb_session: ThrottledSession | None = None
        # Дата фиксируется один раз на весь прогон (а не на каждое обращение
        # к output_dir), чтобы длинный прогон (например 'full_sync'),
        # случайно перешагнувший полночь, не перескочил на новую папку
        # посередине — generate и последующие import/upload остаются в одной
        # и той же папке дня.
        self.run_date: str = date.today().isoformat()

    # ------------------------------------------------------- рабочие папки --

    def resolved_input_dir(self) -> Path:
        """Входная папка склада: input_dir/<склад>, БЕЗ разбивки по дате.

        09.2026, по решению пользователя: разбивка по дням из входа убрана.
        В папке склада лежит ровно один комплект свежих файлов, который
        заменяется перед прогоном (у Базара — один xlsx-отчёт и один JSON, у
        Мвидео — один xlsx-ассортимент), поэтому папка дня создавала лишний
        шаг («создать сегодняшнюю, переложить туда») и вынуждала указывать
        путь к файлу руками. Разбивка по складам осталась — она и защищает
        от того, чтобы файл одного склада был принят за файл другого.

        Вывод (resolved_output_dir) по дате по-прежнему разложен — там
        история прогонов нужна.

        Создаётся автоматически, чтобы сразу было видно, куда класть файлы."""
        path = Path(self.config.input_dir) / self.config.key
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------ файлы во входной папке --

    def input_files(self, suffix: str) -> list[Path]:
        """Все файлы с таким расширением во входной папке склада, по алфавиту."""
        return sorted(p for p in self.resolved_input_dir().glob(f"*{suffix}") if p.is_file())

    def input_file(self, suffix: str, what: str) -> Path:
        """ЕДИНСТВЕННЫЙ входной файл склада с таким расширением.

        Заменяет прежний «укажите путь через --file / шаблон имени в рецепте
        склада»: имя файла нигде вводить не нужно — берётся то, что лежит в
        папке склада. Несколько подходящих файлов — берём первый по алфавиту
        и ГРОМКО про это говорим (тихо выбирать из нескольких нельзя: не
        видно, что взяли не то). Ни одного — понятная ошибка, а если файлы
        нашлись в подпапках, ещё и подсказка про убранную разбивку по дате."""
        files = self.input_files(suffix)
        if not files:
            folder = self.resolved_input_dir()
            hint = ""
            nested = sorted(p for p in folder.glob(f"*/*{suffix}") if p.is_file())
            if nested:
                names = ", ".join(sorted({p.parent.name for p in nested}))
                hint = (f" Похоже, файлы лежат в подпапках ({names}): разбивка входа по дате убрана — "
                        f"перенесите их на уровень выше, прямо в {folder}.")
            raise RuntimeError(f"Не найден {what}: в {folder} нет ни одного файла {suffix}.{hint}")
        if len(files) > 1:
            others = ", ".join(p.name for p in files[1:])
            print(f"  [!] В {self.resolved_input_dir()} несколько файлов {suffix} — беру первый по алфавиту: "
                  f"{files[0].name} (остальные: {others}). Лишние лучше убрать.")
        return files[0]

    def resolved_output_dir(self) -> Path:
        """Папка вывода на СЕГОДНЯ, отдельная по складу: output_dir/<склад>/
        ГГГГ-ММ-ДД — результаты разных складов и разных дней не
        перемешиваются и не перезаписывают друг друга (раньше все склады
        писали в общую output/ГГГГ-ММ-ДД — при совпадении admin_category_id
        между складами файлы одного могли переписать файлы другого).
        Создаётся автоматически, если её ещё нет."""
        path = Path(self.config.output_dir) / self.config.key / self.run_date
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolved_upload_dir(self) -> Path:
        # раньше по умолчанию было output_dir/capitalized — готовил его stage
        # 'import_capitalize'. Он убран (09.2026), upload теперь по умолчанию
        # берёт файлы прямо из (дневного) output_dir, как и import_check/
        # import_attributes. upload_dir, заданный явно в рецепте склада или
        # через .env BELKA_<СКЛАД>_UPLOAD_DIR, дату НЕ получает — берётся как есть.
        override = self.override("UPLOAD_DIR")
        if override:
            return Path(override)
        return Path(self.config.upload_dir) if self.config.upload_dir else self.resolved_output_dir()

    def resolved_templates_dir(self) -> Path:
        override = self.override("TEMPLATES_DIR")
        if override:
            return Path(override)
        return Path(self.config.templates_dir) if self.config.templates_dir else self.resolved_output_dir()

    def resolved_cert_log_file(self) -> Path:
        """Журнал деактиваций по сертификату — СВОЙ У КАЖДОГО СКЛАДА.

        09.2026 (по итогам ревью): раньше путь был общей константой
        WarehouseConfig.cert_log_file = "deactivated_by_certificate.json", ни в
        одном рецепте не переопределённой — то есть Базар и Мвидео писали и
        читали ОДИН файл. Ключ журнала — vendor_code, а у складов он из разных
        пространств нумерации, поэтому оба certificates-stage'а считали чужие
        записи за "сертификат появился" и включали публикацию чужих товаров
        (см. belka/core/cert_log.py). Теперь по умолчанию имя выводится из
        ключа склада; рецепт может задать своё (Базар так сохраняет свой
        исторический файл, см. belka/warehouses/bazar.py)."""
        if self.config.cert_log_file:
            return Path(self.config.cert_log_file)
        return Path(f"deactivated_by_certificate_{self.config.key}.json")

    # ------------------------------------------------------------ заглушки --

    def placeholder(self, key: str):
        """Значение заглушки для этого склада (None = не подставлять).
        Общий реестр — belka/core/defaults.py, переопределения — в рецепте
        склада (WarehouseConfig.placeholders)."""
        return placeholder_defaults.resolve(key, self.config.placeholders)

    def placeholder_for_column(self, column: str):
        """(ключ, значение) заглушки для колонки, или (None, None), если для
        этой колонки заглушки не предусмотрено — тогда пустая обязательная
        ячейка так и останется пустой и попадёт только в отчёт."""
        key = placeholder_defaults.COLUMN_TO_KEY.get(column)
        if key is None:
            return None, None
        return key, self.placeholder(key)

    def placeholders_report(self) -> list[str]:
        """Строки для отчёта «какие заглушки настроены» — с пометкой, что
        именно переопределено рецептом склада."""
        lines = []
        for key, ph in placeholder_defaults.PLACEHOLDERS.items():
            value = self.placeholder(key)
            source = "рецепт склада" if placeholder_defaults.is_overridden(key, self.config.placeholders) else "общий набор"
            shown = "НЕ подставлять" if value is None and not ph.always else repr(value)
            if key == "barcode" and value is None:
                shown = f"префикс {self.config.barcode_prefix!r} + Id, 13 символов"
            mark = " (константа формата)" if ph.always else ""
            lines.append(f"{', '.join(ph.columns)}: {shown} [{source}]{mark} — {ph.note}")
        return lines

    def resolved_attribute_mapping_file(self) -> Path:
        """Файл маппинга атрибутов — СВОЙ У КАЖДОГО СКЛАДА (09.2026).
        Базар остаётся на историческом attribute_mapping.json (задано явно в
        его рецепте), остальные получают attribute_mapping_<склад>.json."""
        if self.config.attribute_mapping_file:
            return Path(self.config.attribute_mapping_file)
        return Path(f"attribute_mapping_{self.config.key}.json")

    def attribute_mapper(self):
        """Готовый AttributeMapper для этого прогона."""
        from .core.attr_mapping import AttributeMapper
        return AttributeMapper(
            self.resolved_attribute_mapping_file(),
            interactive=self.interactive,
            prompt_for_missing=self.config.attribute_prompt,
        )

    # --------------------------------------------------- переопределения .env --

    def override(self, name: str) -> str | None:
        """Переопределение параметра рецепта склада из .env — ТОЛЬКО ключом с
        именем склада: BELKA_<СКЛАД>_<ПАРАМЕТР>, например BELKA_BAZAR_STORE_ID.

        09.2026 (по итогам ревью). Раньше stage'и читали БЕЗЫМЯННЫЕ ключи
        (BELKA_STORE_ID, BELKA_SELLER_ID, BELKA_UPLOAD_DIR, BELKA_TEMPLATES_DIR,
        BELKA_PSB_SELLER_ID) напрямую через ctx.env.get(...) — а .env один на
        весь проект, тогда как значения у складов РАЗНЫЕ. Оставшийся в .env
        BELKA_STORE_ID=5 (а он попадал туда естественно — с него всё
        начиналось, и .env.example его прямо предлагал) молча уводил загрузку
        Мвидео в склад Базара; BELKA_UPLOAD_DIR/BELKA_TEMPLATES_DIR так же
        заставляли один склад читать папку другого.

        Поэтому безымянный ключ теперь НЕ ПРИМЕНЯЕТСЯ — только громко
        предупреждаем, что он проигнорирован, и берём значение из рецепта
        склада. Так поведение по умолчанию всегда соответствует рецепту, а
        переопределение возможно, но обязано назвать склад явно."""
        scoped_key = f"BELKA_{self.config.key.upper()}_{name}"
        value = self.env.get(scoped_key)
        if value not in (None, ""):
            print(f"  [i] {scoped_key}={value!r} из .env переопределяет рецепт склада '{self.config.key}'")
            return value

        legacy_key = f"BELKA_{name}"
        if self.env.get(legacy_key) not in (None, ""):
            print(f"  [!] {legacy_key} задан в .env, но ИГНОРИРУЕТСЯ: этот ключ был общим на все склады "
                  f"и уводил один склад в параметры другого. Используется значение из рецепта склада "
                  f"'{self.config.key}'. Если переопределение действительно нужно — переименуйте ключ "
                  f"в {scoped_key}.")
        return None

    # ------------------------------------------------------------ токены --

    def _resolve_token(self, env_key: str, prompt: str, auth_manager: "AuthTokenManager | None" = None) -> str:
        token = None
        if auth_manager is not None:
            # BELKA_LOGIN/BELKA_PASSWORD заданы (иначе build_auth_manager
            # вернул бы None) — всегда идём через ensure_token(), а не читаем
            # .env напрямую: он сам решает, годится ли уже сохранённый токен,
            # пора ли обновиться по refresh_token (без пароля, см. http.py),
            # или нужен полный логин — включая самый первый раз, когда
            # BELKA_TOKEN ещё пуст. Так планового обновления раз в ~сутки
            # (expires_in) дожидаться не приходится — сработает само.
            try:
                token = auth_manager.ensure_token()
            except Exception as e:
                print(f"  [!] Автологин/обновление токена не удались: {e}")
                token = None
        if not token:
            token = self.env.get(env_key)
        if not token:
            if not self.interactive:
                raise SystemExit(f"{env_key} не задан в .env, а прогон неинтерактивный (--no-interactive) — "
                                  "задайте токен (или BELKA_LOGIN/BELKA_PASSWORD) и перезапустите.")
            token = input(prompt).strip()
        if not token:
            raise SystemExit("Токен не задан, выход.")
        self.env[env_key] = token
        return token

    # ------------------------------------------------------------ сессии --

    def admin_session(self) -> ThrottledSession:
        if self._admin_session is None:
            cfg = self.config
            auth_manager = build_auth_manager(
                self.env, cfg.admin_token_env, cfg.admin_login_env, cfg.admin_password_env,
                cfg.admin_api_base, env_path=self.env_path,
            )
            token = self._resolve_token(
                cfg.admin_token_env, f"{cfg.admin_token_env} не найден в .env — вставьте токен админки: ",
                auth_manager=auth_manager,
            )
            self._admin_session = ThrottledSession(
                headers={"x-access-token": token},
                delay=cfg.request_delay, max_consecutive_403=cfg.max_consecutive_403,
                max_retries=cfg.max_retries, backoff_base=cfg.retry_backoff_base,
                auth_manager=auth_manager,
            )
        return self._admin_session

    def console_session(self) -> ThrottledSession:
        if self._console_session is None:
            cfg = self.config
            if not cfg.console_api_base:
                raise SystemExit(f"У склада '{cfg.key}' не задан console_api_base — этому stage'у он не нужен?")
            token = self._resolve_token(
                cfg.console_token_env, f"{cfg.console_token_env} не найден в .env — вставьте токен консоли: "
            )
            self._console_session = ThrottledSession(
                headers={
                    "x-access-token": token,
                    "User-Agent": BROWSER_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                },
                delay=cfg.request_delay, max_consecutive_403=cfg.max_consecutive_403,
                max_retries=cfg.max_retries, backoff_base=cfg.retry_backoff_base,
            )
        return self._console_session

    def psb_session(self) -> ThrottledSession:
        """Сессия к apigw-seller (ПСБ Маркет) — статический Bearer-токен из
        .env, без auth_manager: ни в fill_templates.py, ни в update_prices.py
        нет логина/обновления токена, только его наличие в .env заранее."""
        if self._psb_session is None:
            cfg = self.config
            if not cfg.psb_api_base:
                raise SystemExit(f"У склада '{cfg.key}' не задан psb_api_base — этому stage'у он не нужен?")
            token = self._resolve_token(
                cfg.psb_token_env, f"{cfg.psb_token_env} не найден в .env — вставьте токен ПСБ (Bearer, без слова 'Bearer'): "
            )
            self._psb_session = ThrottledSession(
                headers={"Authorization": f"Bearer {token}"},
                delay=cfg.request_delay, max_consecutive_403=cfg.max_consecutive_403,
                max_retries=cfg.max_retries, backoff_base=cfg.retry_backoff_base,
            )
        return self._psb_session

    # --------------------------------------------------------- подтверждения --

    def confirm(self, question: str) -> bool:
        """Единая точка для 'да/нет?'. В неинтерактивном прогоне (--no-interactive
        или запуск из cron/CI) решает assume_yes — по умолчанию False, то есть
        ничего опасного (upload, обнуление остатков) без явного --yes не делаем."""
        if not self.interactive:
            # Печатаем и сам вопрос, и автоответ: иначе в логе крон-прогона
            # не видно, о чём вообще спрашивали и почему шаг сделан/пропущен.
            answer = "да" if self.assume_yes else "нет"
            flag = "--yes" if self.assume_yes else "без --yes"
            print(f"{question} -> {answer} (неинтерактивный прогон, {flag})")
            return self.assume_yes
        answer = input(f"{question} (да/нет): ").strip().lower()
        return answer in ("да", "y", "yes", "д")
