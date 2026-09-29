#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
belka.pipeline — общий "движок" прогона.

Stage      — единица работы: ровно то, чем раньше был один belka_*.py файл
             (bridge / generate / import / upload / certificates /
             zero_stock), теперь как класс с run(ctx).
StageResult — что stage сделал (для итогового отчёта).
Pipeline    — именованная упорядоченная последовательность stage'ов,
             которую можно "собрать" под конкретный склад (см.
             belka/warehouses/*.py) и "разобрать" — прогнать через CLI
             целиком или частично (--only).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .core.http import SessionBlockedError
from .context import PipelineContext


@dataclass
class StageResult:
    stage: str
    ok: bool
    blocked: bool = False
    error: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)


class Stage:
    """Базовый класс одного шага пайплайна. Наследники переопределяют
    `name` и `run(ctx)`."""

    name: str = "stage"
    #: упала стадия (исключение или result.ok=False) — прервать весь
    #: пайплайн (True, по умолчанию) или пробовать следующие stage'и (False)?
    stop_pipeline_on_failure: bool = True

    def run(self, ctx: PipelineContext) -> StageResult:  # pragma: no cover - abstract
        raise NotImplementedError


class Pipeline:
    """Собирается один раз в belka/warehouses/<склад>.py; CLI умеет
    прогнать её целиком или подмножество через --only."""

    def __init__(self, name: str, stages: list[Stage]):
        self.name = name
        self.stages = stages

    def stage_names(self) -> list[str]:
        return [s.name for s in self.stages]

    def run(self, ctx: PipelineContext, only: list[str] | None = None) -> list[StageResult]:
        stages = self.stages if not only else [s for s in self.stages if s.name in only]
        if only:
            missing = set(only) - {s.name for s in stages}
            if missing:
                raise SystemExit(f"Неизвестные stage'и для --only: {sorted(missing)}. "
                                  f"Доступные в '{self.name}': {self.stage_names()}")

        results: list[StageResult] = []
        print(f"\n{'=' * 70}\nПАЙПЛАЙН '{self.name}': {[s.name for s in stages]}\n{'=' * 70}")

        for stage in stages:
            print(f"\n--- stage: {stage.name} ---")
            try:
                result = stage.run(ctx)
            except SessionBlockedError as e:
                print(f"\n⛔ ОСТАНОВКА на '{stage.name}': {e}")
                print("Обновите токен и перезапустите — уже сделанное этим прогоном (файлы, логи) не потеряно.")
                results.append(StageResult(stage.name, ok=False, blocked=True, error=str(e)))
                break
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001 — сознательно широкий catch на границе stage'а
                print(f"\n✘ Ошибка на '{stage.name}': {e}")
                results.append(StageResult(stage.name, ok=False, error=str(e)))
                if stage.stop_pipeline_on_failure:
                    break
                continue
            else:
                results.append(result)
                if not result.ok and stage.stop_pipeline_on_failure:
                    break

        print(f"\n{'=' * 70}\nИТОГ ПАЙПЛАЙНА '{self.name}'\n{'=' * 70}")
        for r in results:
            mark = "✔" if r.ok else ("⛔" if r.blocked else "✘")
            print(f"  {mark} {r.stage}: {r.summary if r.ok else (r.error or 'см. вывод выше')}")
        return results
