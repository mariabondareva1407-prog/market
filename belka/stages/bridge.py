#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 'bridge' — строит мост между id категорий консоли и id категорий
админки (разные системы с разными числовыми id для одних и тех же
категорий), по ТОЧНОМУ совпадению полного пути листовой категории.

Разовый/редкий шаг (категории меняются нечасто) — не входит в 'listing',
гоняется отдельно: `python -m belka <склад> bridge`.
"""
from __future__ import annotations

import re
from pathlib import Path
from collections import defaultdict

from ..core.categories import fetch_category_tree, extract_leaf_categories
from ..core.csvio import write_csv_report
from ..core.jsonio import save_json_file
from ..pipeline import Stage, StageResult

ADMIN_ROOT_CATEGORY_NAME = "Все товары"
TREE_URL_PART = "/api/v1/catalog/categories:tree"


def normalize_path(path: str) -> str:
    return re.sub(r"\s+", " ", (path or "").strip())


def strip_root_segment(path: str, root_name: str) -> str:
    """Убирает самый первый сегмент пути, если он равен root_name (служебный
    корень 'Все товары' в дереве админки, которого нет в консоли)."""
    prefix = f"{root_name} / "
    if path.startswith(prefix):
        return path[len(prefix):]
    if path == root_name:
        return ""
    return path


class BuildCategoryBridgeStage(Stage):
    name = "bridge"

    def __init__(self, admin_root_category_name: str = ADMIN_ROOT_CATEGORY_NAME,
                 tree_url_part: str = TREE_URL_PART):
        self.admin_root_category_name = admin_root_category_name
        self.tree_url_part = tree_url_part

    def run(self, ctx) -> StageResult:
        cfg = ctx.config
        admin_session = ctx.admin_session()
        console_session = ctx.console_session()

        print("Скачиваю дерево категорий админки...")
        admin_tree = fetch_category_tree(admin_session, cfg.admin_api_base, self.tree_url_part)
        admin_leaves = extract_leaf_categories(admin_tree)
        print(f"  листовых категорий: {len(admin_leaves)}")

        print("Скачиваю дерево категорий консоли...")
        console_tree = fetch_category_tree(console_session, cfg.console_api_base, self.tree_url_part)
        console_leaves = extract_leaf_categories(console_tree)
        print(f"  листовых категорий: {len(console_leaves)}")

        admin_by_path = {}
        admin_path_duplicates = defaultdict(list)
        for leaf in admin_leaves:
            stripped_path = strip_root_segment(leaf["path"], self.admin_root_category_name)
            key = normalize_path(stripped_path)
            admin_path_duplicates[key].append(leaf["id"])
            if key not in admin_by_path:
                admin_by_path[key] = leaf["id"]

        if any(len(ids) > 1 for ids in admin_path_duplicates.values()):
            print("\n⚠️ В админке есть несколько категорий с ОДИНАКОВЫМ полным путём "
                  "(беру первую встреченную, остальные — потенциально спорные):")
            for path, ids in admin_path_duplicates.items():
                if len(ids) > 1:
                    print(f"  '{path}' -> id: {ids}")

        bridge = {}
        matched_console_paths = set()
        unmatched_console = []
        for leaf in console_leaves:
            key = normalize_path(leaf["path"])
            admin_id = admin_by_path.get(key)
            if admin_id is not None:
                bridge[str(leaf["id"])] = admin_id
                matched_console_paths.add(key)
            else:
                unmatched_console.append(leaf)

        unmatched_admin = [
            {"id": admin_id, "path": path}
            for path, admin_id in admin_by_path.items()
            if path not in matched_console_paths
        ]

        bridge_path = Path(cfg.category_bridge_file)
        save_json_file(bridge_path, bridge)

        if unmatched_console or unmatched_admin:
            report_path = Path(cfg.unmatched_bridge_report_file)
            rows = [["консоль (нет в админке)", leaf["id"], leaf["path"]] for leaf in unmatched_console]
            rows += [["админка (нет в консоли)", leaf["id"], leaf["path"]] for leaf in unmatched_admin]
            write_csv_report(report_path, ["Источник", "id", "Путь"], rows)
            print(f"\nНесопоставленные категории — в {report_path} ({len(rows)} шт.)")

        summary = {
            "console_leaves": len(console_leaves),
            "admin_leaves": len(admin_leaves),
            "matched": len(bridge),
            "unmatched_console": len(unmatched_console),
            "unmatched_admin": len(unmatched_admin),
        }
        print("\n=== ИТОГ bridge ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        print(f"Мост сохранён в {bridge_path}")
        return StageResult(self.name, ok=True, summary=summary)
