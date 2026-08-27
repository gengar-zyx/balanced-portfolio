"""Shared Feishu Card JSON 2.0 and VChart builders."""

from __future__ import annotations

import math
from typing import Any, Iterable


POSITION_CHART_MAX_ASSETS = 8
REBALANCE_CHART_MAX_ASSETS = 10


def escape_lark_md(value: Any) -> str:
    text = str(value)
    for char in ("\\", "*", "_", "~", "`", "[", "]"):
        text = text.replace(char, f"\\{char}")
    return text


def build_card(*, title: str, template: str, elements: list[dict]) -> dict:
    """Wrap elements in the Card JSON 2.0 envelope required by charts."""
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"elements": elements},
    }


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _percent(value: Any) -> float | None:
    number = _finite_float(value)
    if number is None:
        return None
    return round(number * 100, 4)


def build_position_chart(holdings: Iterable[dict]) -> dict | None:
    """Build a donut chart, grouping smaller holdings into an ``other`` slice."""
    values: list[dict[str, Any]] = []
    for item in holdings:
        weight = _percent(item.get("weight"))
        if weight is None or weight <= 0:
            continue
        values.append(
            {
                "asset": str(item.get("name") or item.get("key") or "未命名资产"),
                "weight": weight,
            }
        )
    values.sort(key=lambda item: (-item["weight"], item["asset"]))
    if not values:
        return None

    if len(values) > POSITION_CHART_MAX_ASSETS:
        visible_count = POSITION_CHART_MAX_ASSETS - 1
        other_weight = round(sum(item["weight"] for item in values[visible_count:]), 4)
        values = [*values[:visible_count], {"asset": "其他", "weight": other_weight}]

    return {
        "tag": "chart",
        "aspect_ratio": "16:9",
        "color_theme": "brand",
        "preview": True,
        "chart_spec": {
            "type": "pie",
            "data": [{"id": "holdings", "values": values}],
            "categoryField": "asset",
            "valueField": "weight",
            "seriesField": "asset",
            "outerRadius": 0.82,
            "innerRadius": 0.52,
            "label": {"visible": True},
            "legends": {"visible": True, "orient": "bottom"},
        },
    }


def build_rebalance_chart(assets: Iterable[dict]) -> dict | None:
    """Build a grouped horizontal bar chart for before/target weights."""
    ranked: list[tuple[float, str, float, float]] = []
    for item in assets:
        previous = _percent(item.get("previous"))
        target = _percent(item.get("target"))
        if previous is None or target is None or previous < 0 or target < 0:
            continue
        delta = _percent(item.get("delta"))
        change = target - previous if delta is None else delta
        if math.isclose(change, 0.0, abs_tol=1e-9):
            continue
        name = str(item.get("name") or item.get("key") or "未命名资产")
        ranked.append((abs(change), name, previous, target))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        return None

    values: list[dict[str, Any]] = []
    for _, name, previous, target in ranked[:REBALANCE_CHART_MAX_ASSETS]:
        values.extend(
            (
                {"asset": name, "stage": "调仓前", "weight": previous},
                {"asset": name, "stage": "目标", "weight": target},
            )
        )

    return {
        "tag": "chart",
        "aspect_ratio": "16:9",
        "color_theme": "complementary",
        "preview": True,
        "chart_spec": {
            "type": "bar",
            "direction": "horizontal",
            "data": [{"id": "rebalance", "values": values}],
            "xField": "weight",
            "yField": "asset",
            "seriesField": "stage",
            "stack": False,
            "legends": {"visible": True, "orient": "bottom"},
            "label": {"visible": True, "position": "outside"},
        },
    }
