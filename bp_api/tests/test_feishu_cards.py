from __future__ import annotations

import json
import math

from bp_api import feishu_cards


def test_position_chart_groups_smaller_assets_and_preserves_total():
    holdings = [
        {"name": f"资产{i}", "weight": (10 - i) / 100}
        for i in range(10)
    ]

    chart = feishu_cards.build_position_chart(holdings)

    assert chart is not None
    values = chart["chart_spec"]["data"][0]["values"]
    assert len(values) == feishu_cards.POSITION_CHART_MAX_ASSETS
    assert [item["asset"] for item in values[:2]] == ["资产0", "资产1"]
    assert values[-1]["asset"] == "其他"
    assert sum(item["weight"] for item in values) == 55.0
    json.dumps(chart, allow_nan=False)


def test_position_chart_ignores_non_positive_and_non_finite_weights():
    chart = feishu_cards.build_position_chart(
        [
            {"name": "有效", "weight": 0.25},
            {"name": "零", "weight": 0},
            {"name": "负数", "weight": -0.1},
            {"name": "非法", "weight": math.nan},
        ]
    )

    assert chart is not None
    assert chart["chart_spec"]["data"][0]["values"] == [
        {"asset": "有效", "weight": 25.0}
    ]


def test_rebalance_chart_sorts_by_absolute_change_caps_and_skips_unchanged():
    assets = [
        {
            "name": f"资产{i}",
            "previous": 0.1,
            "target": 0.1 + i / 100,
            "delta": i / 100,
        }
        for i in range(12)
    ]

    chart = feishu_cards.build_rebalance_chart(assets)

    assert chart is not None
    spec = chart["chart_spec"]
    values = spec["data"][0]["values"]
    assert spec["type"] == "bar" and spec["direction"] == "horizontal"
    assert len(values) == feishu_cards.REBALANCE_CHART_MAX_ASSETS * 2
    assert values[0]["asset"] == "资产11"
    assert values[0]["stage"] == "调仓前"
    assert values[1]["stage"] == "目标"
    assert not any(item["asset"] == "资产0" for item in values)


def test_rebalance_chart_is_omitted_without_valid_changes():
    assert (
        feishu_cards.build_rebalance_chart(
            [
                {"name": "不变", "previous": 0.2, "target": 0.2, "delta": 0},
                {"name": "非法", "previous": math.inf, "target": 0.2, "delta": -0.1},
            ]
        )
        is None
    )
