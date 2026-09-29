#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
belka.core.http — троттлинг HTTP-запросов и авто-обновление токена.

ThrottledSession: обёртка над requests.Session с паузой между КАЖДЫМ
запросом, retry с нарастающей паузой на сетевые ошибки и 5xx, и остановкой
всего прогона (SessionBlockedError) при каскаде из max_consecutive_403
подряд 403 Forbidden — это не транзиентная ошибка, retry не поможет,
нужно остановиться и попросить у пользователя свежий токен.

AuthTokenManager: логин + автообновление токена по образцу auth.py —
потокобезопасный логин на POST {base_url}{login_path} с {"login",
"password"}, новый access_token сразу персистится в .env (переживает
перезапуск). Один менеджер на хост — у admin.belkamarket.ru и у консоли
разные логин/пароль/токены, для каждого свой AuthTokenManager.

Подтверждено на живом ответе admin.belkamarket.ru (2026-09-11):
    POST {base_url}/api/v1/auth/login  {"login","password"}
    POST {base_url}/api/v1/auth/refresh {"refresh_token"}
    -> оба отвечают {"data": {"access_token", "refresh_token", "expires_in"}}
(expires_in — секунды жизни access_token, на проде наблюдалось 86400 = 24ч).
Обновление токена сначала пробует refresh_token (без пароля), и только если
это не удалось (нет refresh_token, он тоже протух, или сервер его не принял)
— падает на полный логин по паролю.
"""
from __future__ import annotations

import os
import time
import threading
from pathlib import Path

import requests

from .env import save_env_key

REQUEST_DELAY_DEFAULT = 0.3
MAX_CONSECUTIVE_403_DEFAULT = 3
MAX_RETRIES_DEFAULT = 3
RETRY_BACKOFF_BASE_DEFAULT = 1.5  # секунд, растёт как BASE * (2 ** попытка)


class SessionBlockedError(Exception):
    """Сервер стабильно отвечает 403 — токен истёк / нет прав / сработал лимит запросов."""


class AuthTokenManager:
    """
    Использование:
        auth = AuthTokenManager(base_url=..., login=..., password=...,
                                 token_env_key="BELKA_TOKEN")
        session = ThrottledSession(headers={"x-access-token": auth.get_token()},
                                    auth_manager=auth)
    """

    def __init__(
        self,
        base_url: str,
        login: str,
        password: str,
        token_env_key: str,
        env_path: str = ".env",
        login_path: str = "/api/v1/auth/login",
        refresh_path: str = "/api/v1/auth/refresh",
        refresh_token_env_key: str | None = None,
        expires_at_env_key: str | None = None,
        expiry_margin_seconds: int = 3600,
        verify_ssl: bool = True,
        timeout: int = 30,
    ):
        self._lock = threading.Lock()
        self.base_url = base_url
        self.login = login
        self.password = password
        self.token_env_key = token_env_key
        self.env_path = Path(env_path)
        self.login_path = login_path
        self.refresh_path = refresh_path
        # По умолчанию — рядом с BELKA_TOKEN: BELKA_TOKEN_REFRESH / BELKA_TOKEN_EXPIRES_AT
        self.refresh_token_env_key = refresh_token_env_key or f"{token_env_key}_REFRESH"
        self.expires_at_env_key = expires_at_env_key or f"{token_env_key}_EXPIRES_AT"
        self.expiry_margin_seconds = expiry_margin_seconds
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._token = os.environ.get(token_env_key)
        self._refresh_token = os.environ.get(self.refresh_token_env_key)
        expires_at_raw = os.environ.get(self.expires_at_env_key)
        self._expires_at = float(expires_at_raw) if expires_at_raw else None

    def get_token(self) -> str | None:
        """Текущий токен без сетевого запроса (может быть None, если ещё не логинились)."""
        return self._token

    def _is_expiring_soon(self) -> bool:
        if self._expires_at is None:
            return False
        return time.time() >= (self._expires_at - self.expiry_margin_seconds)

    def ensure_token(self) -> str:
        """Актуальный токен: если текущий есть и не близок к истечению — вернёт
        его без сетевых запросов. Если протух/скоро протухнет/его ещё нет
        вовсе — сначала пробует обновиться через refresh_token (не требует
        пароля), и только если это не удалось — логинится заново по
        BELKA_LOGIN/BELKA_PASSWORD. Так первый прогон (BELKA_TOKEN пуст) и
        плановое обновление раз в ~сутки (expires_in) идут одним и тем же
        путём, не требуя от пользователя ничего вставлять руками."""
        with self._lock:
            if self._token and not self._is_expiring_soon():
                return self._token
            if not self._refresh():
                self._login()
            return self._token

    def refresh_if_needed(self, stale_token: str | None = None) -> str:
        """
        Обновить токен ПОСЛЕ 401 от уже идущей сессии. Если stale_token
        передан и уже не совпадает с текущим — значит токен уже обновили
        (например, другой поток), повторно обновляться не нужно. Иначе —
        сперва refresh_token, затем (если не вышло) полный логин.
        """
        with self._lock:
            if stale_token is not None and self._token != stale_token:
                return self._token
            if not self._refresh():
                self._login()
            return self._token

    # ------------------------------------------------------- сетевые вызовы --

    def _parse_auth_response(self, resp, what: str) -> dict:
        """Общий разбор ответа и /login, и /refresh — оба отвечают одинаково
        (подтверждено на живом ответе): {"data": {"access_token",
        "refresh_token", "expires_in"}}. На всякий случай понимает и плоский
        вид ({"access_token": ...} без обёртки "data")."""
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError:
            raise RuntimeError(
                f"{what} ответил статусом {resp.status_code}, но тело не JSON: {resp.text[:300]!r}"
            )
        data = payload.get("data") if isinstance(payload, dict) else None
        src = data if isinstance(data, dict) else payload
        access_token = src.get("access_token") if isinstance(src, dict) else None
        if not access_token:
            raise RuntimeError(
                f"{what} ответил статусом {resp.status_code}, но без access_token в теле: "
                f"{resp.text[:500]!r}"
            )
        return {
            "access_token": access_token,
            "refresh_token": src.get("refresh_token"),
            "expires_in": src.get("expires_in"),
        }

    def _apply_auth_result(self, result: dict) -> None:
        self._token = result["access_token"]
        os.environ[self.token_env_key] = self._token
        save_env_key(self.env_path, self.token_env_key, self._token)

        if result.get("refresh_token"):
            self._refresh_token = result["refresh_token"]
            os.environ[self.refresh_token_env_key] = self._refresh_token
            save_env_key(self.env_path, self.refresh_token_env_key, self._refresh_token)

        if result.get("expires_in"):
            try:
                self._expires_at = time.time() + float(result["expires_in"])
                os.environ[self.expires_at_env_key] = str(self._expires_at)
                save_env_key(self.env_path, self.expires_at_env_key, str(self._expires_at))
            except (TypeError, ValueError):
                pass

    def _refresh(self) -> bool:
        """Пробует обменять refresh_token на новый access_token, без пароля.
        True при успехе. False, если рефреш-токена нет или сервер его не
        принял (протух/невалиден) — тогда вызывающий код падает на _login()."""
        if not self._refresh_token:
            return False
        url = f"{self.base_url}{self.refresh_path}"
        try:
            resp = requests.post(
                url, json={"refresh_token": self._refresh_token},
                verify=self.verify_ssl, timeout=self.timeout,
            )
            result = self._parse_auth_response(resp, f"Обновление токена {url}")
        except Exception as e:
            print(f"  [!] Обновление по refresh_token не удалось ({e}) — логинюсь заново по логину/паролю")
            return False
        self._apply_auth_result(result)
        print(f"  [i] {self.token_env_key} обновлён через {self.refresh_path} (без повторного логина)")
        return True

    def _login(self) -> None:
        url = f"{self.base_url}{self.login_path}"
        resp = requests.post(
            url,
            json={"login": self.login, "password": self.password},
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        result = self._parse_auth_response(resp, f"Логин {url}")
        self._apply_auth_result(result)
        print(f"  [i] {self.token_env_key} обновлён через {self.login_path}")


def build_auth_manager(env: dict, token_env_key: str, login_env_key: str, password_env_key: str,
                        base_url: str, env_path: str = ".env") -> "AuthTokenManager | None":
    """Собирает AuthTokenManager, если в .env заданы логин и пароль под указанными
    ключами, иначе None (тогда автологин на 401 просто не подключается)."""
    login = env.get(login_env_key)
    password = env.get(password_env_key)
    if not login or not password:
        return None
    return AuthTokenManager(base_url=base_url, login=login, password=password,
                             token_env_key=token_env_key, env_path=env_path)


class ThrottledSession:
    """
    Использование:
        session = ThrottledSession(headers={"x-access-token": token})
        resp = session.post(url, json=payload, timeout=30)
        resp = session.get(url, params={...}, timeout=30)
    """

    def __init__(
        self,
        headers: dict,
        delay: float = REQUEST_DELAY_DEFAULT,
        max_consecutive_403: int = MAX_CONSECUTIVE_403_DEFAULT,
        max_retries: int = MAX_RETRIES_DEFAULT,
        backoff_base: float = RETRY_BACKOFF_BASE_DEFAULT,
        auth_manager: "AuthTokenManager | None" = None,
        token_header: str = "x-access-token",
    ):
        self._session = requests.Session()
        self._session.headers.update(headers)
        self.delay = delay
        self.max_consecutive_403 = max_consecutive_403
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._consecutive_403 = 0
        self.auth_manager = auth_manager
        self.token_header = token_header

    def request(self, method: str, url: str, **kwargs):
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            time.sleep(self.delay)
            try:
                resp = self._session.request(method, url, **kwargs)
            except requests.RequestException as e:
                last_exc = e
                wait = self.backoff_base * (2 ** (attempt - 1))
                print(f"  [!] Сетевая ошибка (попытка {attempt}/{self.max_retries}): {e}. Повтор через {wait:.1f}с")
                time.sleep(wait)
                continue

            if resp.status_code == 401 and self.auth_manager is not None:
                stale_token = self._session.headers.get(self.token_header)
                print(f"  [!] 401 Unauthorized — обновляю токен и повторяю запрос")
                new_token = self.auth_manager.refresh_if_needed(stale_token=stale_token)
                self._session.headers[self.token_header] = new_token
                continue

            if resp.status_code == 403:
                self._consecutive_403 += 1
                body_preview = resp.text[:300].replace("\n", " ")
                print(f"  [!] 403 Forbidden (подряд: {self._consecutive_403}). Тело ответа: {body_preview}")
                if self._consecutive_403 >= self.max_consecutive_403:
                    raise SessionBlockedError(
                        f"Получено {self._consecutive_403} подряд ответов 403 Forbidden. "
                        "Похоже, токен истёк / нет прав на этот эндпоинт / сработал лимит "
                        f"запросов. Последнее тело ответа: {body_preview}"
                    )
                time.sleep(self.backoff_base)
                continue

            self._consecutive_403 = 0

            if resp.status_code >= 500:
                wait = self.backoff_base * (2 ** (attempt - 1))
                print(f"  [!] Ошибка сервера {resp.status_code} (попытка {attempt}/{self.max_retries}). Повтор через {wait:.1f}с")
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                body_preview = resp.text[:300].replace("\n", " ")
                print(f"  [!] Ошибка {resp.status_code}. Тело ответа: {body_preview}")

            resp.raise_for_status()
            return resp

        if last_exc:
            raise last_exc
        raise RuntimeError("Не удалось выполнить запрос после нескольких попыток.")

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs):
        return self.request("PATCH", url, **kwargs)
