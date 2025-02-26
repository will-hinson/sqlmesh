from __future__ import annotations

import typing as t
from collections import Counter
from datetime import timedelta
from unittest import mock

import numpy as np
import pandas as pd
import pytest
from pathlib import Path
from freezegun import freeze_time
from pytest_mock.plugin import MockerFixture
from sqlglot import exp
from sqlglot.expressions import DataType

from sqlmesh import CustomMaterialization
from sqlmesh.cli.example_project import init_example_project
from sqlmesh.core import constants as c
from sqlmesh.core import dialect as d
from sqlmesh.core.config import (
    AutoCategorizationMode,
    Config,
    GatewayConfig,
    ModelDefaultsConfig,
    DuckDBConnectionConfig,
)
from sqlmesh.core.console import Console
from sqlmesh.core.context import Context
from sqlmesh.core.config.categorizer import CategorizerConfig
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.environment import EnvironmentNamingInfo
from sqlmesh.core.model import (
    IncrementalByTimeRangeKind,
    IncrementalByUniqueKeyKind,
    Model,
    ModelKind,
    ModelKindName,
    SqlModel,
    PythonModel,
    ViewKind,
    TimeColumn,
    load_sql_based_model,
)
from sqlmesh.core.model.kind import model_kind_type_from_name
from sqlmesh.core.plan import Plan, PlanBuilder, SnapshotIntervals
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    Snapshot,
    SnapshotChangeCategory,
    SnapshotId,
    SnapshotInfoLike,
    SnapshotTableInfo,
)
from sqlmesh.utils.date import TimeLike, now, to_date, to_datetime, to_timestamp
from sqlmesh.utils.errors import NoChangesPlanError
from tests.conftest import DuckDBMetadata, SushiDataValidator


if t.TYPE_CHECKING:
    from sqlmesh import QueryOrDF

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def mock_choices(mocker: MockerFixture):
    mocker.patch("sqlmesh.core.console.TerminalConsole._get_snapshot_change_category")
    mocker.patch("sqlmesh.core.console.TerminalConsole._prompt_backfill")


def plan_choice(plan_builder: PlanBuilder, choice: SnapshotChangeCategory) -> None:
    for snapshot in plan_builder.build().snapshots.values():
        if not snapshot.version:
            plan_builder.set_choice(snapshot, choice)


@freeze_time("2023-01-08 15:00:00")
@pytest.mark.parametrize(
    "context_fixture",
    ["sushi_context", "sushi_no_default_catalog"],
)
def test_forward_only_plan_with_effective_date(context_fixture: Context, request):
    context = request.getfixturevalue(context_fixture)
    model_name = "sushi.waiter_revenue_by_day"
    model = context.get_model(model_name)
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)), start="2023-01-01")
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan_builder = context.plan_builder("dev", skip_tests=True, forward_only=True)
    plan = plan_builder.build()
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert plan.start == to_date("2023-01-07")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[(to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[(to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))],
        ),
    ]

    plan = plan_builder.set_effective_from("2023-01-05").build()
    # Default start should be set to effective_from
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    plan = plan_builder.set_start("2023-01-06").build()
    # Start override should take precedence
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    plan = plan_builder.set_effective_from("2023-01-04").build()
    # Start should remain unchanged
    assert plan.start == "2023-01-06"
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert dev_df["event_date"].tolist() == [
        pd.to_datetime("2023-01-06"),
        pd.to_datetime("2023-01-07"),
    ]

    prod_plan = context.plan(no_prompts=True, skip_tests=True)
    # Make sure that the previously set effective_from is respected
    assert prod_plan.start == to_timestamp("2023-01-04")
    assert prod_plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(prod_plan)

    prod_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi.waiter_revenue_by_day WHERE one IS NOT NULL ORDER BY event_date"
    )
    assert prod_df["event_date"].tolist() == [
        pd.to_datetime(x) for x in ["2023-01-04", "2023-01-05", "2023-01-06", "2023-01-07"]
    ]


@freeze_time("2023-01-08 15:00:00")
def test_forward_only_model_regular_plan(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model_name = "sushi.waiter_revenue_by_day"

    model = context.get_model(model_name)
    model = add_projection_to_model(t.cast(SqlModel, model))
    forward_only_kind = model.kind.copy(update={"forward_only": True})
    model = model.copy(update={"kind": forward_only_kind})

    context.upsert_model(model)
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan("dev", no_prompts=True, skip_tests=True)
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert plan.start == to_datetime("2023-01-01")
    assert not plan.missing_intervals

    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert not dev_df["event_date"].tolist()

    # Run a restatement plan to preview changes
    plan_builder = context.plan_builder("dev", skip_tests=True, restate_models=[model_name])
    plan_builder.set_start("2023-01-06")
    assert plan_builder.build().missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    # Make sure that changed start is reflected in missing intervals
    plan_builder.set_start("2023-01-07")
    assert plan_builder.build().missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan_builder.build())

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert dev_df["event_date"].tolist() == [pd.to_datetime("2023-01-07")]

    # Promote changes to prod
    prod_plan = context.plan(no_prompts=True, skip_tests=True)
    assert not prod_plan.missing_intervals

    context.apply(prod_plan)

    # The change was applied in a forward-only manner so no values in the new column should be populated
    prod_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi.waiter_revenue_by_day WHERE one IS NOT NULL ORDER BY event_date"
    )
    assert not prod_df["event_date"].tolist()


@freeze_time("2023-01-08 15:00:00")
def test_forward_only_model_regular_plan_preview_enabled(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model_name = "sushi.waiter_revenue_by_day"

    model = context.get_model(model_name)
    model = add_projection_to_model(t.cast(SqlModel, model))
    forward_only_kind = model.kind.copy(update={"forward_only": True})
    model = model.copy(update={"kind": forward_only_kind})

    context.upsert_model(model)
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan("dev", no_prompts=True, skip_tests=True, enable_preview=True)
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert plan.start == to_date("2023-01-07")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert dev_df["event_date"].tolist() == [pd.to_datetime("2023-01-07")]


@freeze_time("2023-01-08 15:00:00")
def test_full_history_restatement_model_regular_plan_preview_enabled(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model_name = "sushi.marketing"  # SCD2 model

    model = context.get_model(model_name)
    model = add_projection_to_model(t.cast(SqlModel, model))

    context.upsert_model(model)
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    customers_snapshot = context.get_snapshot("sushi.customers", raise_if_missing=True)
    active_customers_snapshot = context.get_snapshot(
        "sushi.active_customers", raise_if_missing=True
    )
    waiter_as_customer_snapshot = context.get_snapshot(
        "sushi.waiter_as_customer_by_day", raise_if_missing=True
    )

    plan = context.plan("dev", no_prompts=True, skip_tests=True, enable_preview=True)

    assert len(plan.new_snapshots) == 4
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert (
        plan.context_diff.snapshots[customers_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert (
        plan.context_diff.snapshots[active_customers_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert (
        plan.context_diff.snapshots[waiter_as_customer_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )

    assert plan.start == to_date("2023-01-07")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=active_customers_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=customers_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=waiter_as_customer_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan)


@freeze_time("2023-01-08 15:00:00")
def test_metadata_changed_regular_plan_preview_enabled(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model_name = "sushi.waiter_revenue_by_day"

    model = context.get_model(model_name)
    model = model.copy(update={"owner": "new_owner"})

    context.upsert_model(model)
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan("dev", no_prompts=True, skip_tests=True, enable_preview=True)
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.METADATA
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.METADATA
    )
    assert not plan.missing_intervals
    assert not plan.restatements


@freeze_time("2023-01-08 15:00:00")
def test_hourly_model_with_lookback_no_backfill_in_dev(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")

    model_name = "sushi.waiter_revenue_by_day"

    model = context.get_model(model_name)
    model = SqlModel.parse_obj(
        {
            **model.dict(),
            "kind": model.kind.copy(update={"lookback": 1}),
            "cron": "@hourly",
            "audits": [],
        }
    )
    context.upsert_model(model)

    plan = context.plan("prod", no_prompts=True, skip_tests=True)
    context.apply(plan)

    top_waiters_model = context.get_model("sushi.top_waiters")
    top_waiters_model = add_projection_to_model(t.cast(SqlModel, top_waiters_model), literal=True)
    context.upsert_model(top_waiters_model)

    context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    with freeze_time(now() + timedelta(hours=2)):
        plan = context.plan("dev", no_prompts=True, skip_tests=True)
        # Make sure the waiter_revenue_by_day model is not backfilled.
        assert plan.missing_intervals == [
            SnapshotIntervals(
                snapshot_id=top_waiters_snapshot.snapshot_id,
                intervals=[
                    (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                    (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                    (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                    (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                    (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                    (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                    (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
                ],
            ),
        ]


@freeze_time("2023-01-08 00:00:00")
def test_parent_cron_after_child(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")

    model = context.get_model("sushi.waiter_revenue_by_day")
    model = SqlModel.parse_obj(
        {
            **model.dict(),
            "cron": "50 23 * * *",
        }
    )
    context.upsert_model(model)

    plan = context.plan("prod", no_prompts=True, skip_tests=True)
    context.apply(plan)

    waiter_revenue_by_day_snapshot = context.get_snapshot(model.name, raise_if_missing=True)
    assert waiter_revenue_by_day_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-07"))
    ]

    top_waiters_model = context.get_model("sushi.top_waiters")
    top_waiters_model = add_projection_to_model(t.cast(SqlModel, top_waiters_model), literal=True)
    context.upsert_model(top_waiters_model)

    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    with freeze_time("2023-01-08 23:55:00"):  # Past parent's cron, but before child's
        plan = context.plan("dev", no_prompts=True, skip_tests=True)
        # Make sure the waiter_revenue_by_day model is not backfilled.
        assert plan.missing_intervals == [
            SnapshotIntervals(
                snapshot_id=top_waiters_snapshot.snapshot_id,
                intervals=[
                    (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                    (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                    (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                    (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                    (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                    (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                    (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
                ],
            ),
        ]


@freeze_time("2023-01-08 00:00:00")
@pytest.mark.parametrize(
    "forward_only, expected_intervals",
    [
        (
            False,
            [
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
            ],
        ),
        (
            True,
            [
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
            ],
        ),
    ],
)
def test_cron_not_aligned_with_day_boundary(
    init_and_plan_context: t.Callable,
    forward_only: bool,
    expected_intervals: t.List[t.Tuple[int, int]],
):
    context, plan = init_and_plan_context("examples/sushi")

    model = context.get_model("sushi.waiter_revenue_by_day")
    model = SqlModel.parse_obj(
        {
            **model.dict(),
            "kind": model.kind.copy(update={"forward_only": forward_only}),
            "cron": "0 12 * * *",
        }
    )
    context.upsert_model(model)

    plan = context.plan("prod", no_prompts=True, skip_tests=True)
    context.apply(plan)

    waiter_revenue_by_day_snapshot = context.get_snapshot(model.name, raise_if_missing=True)
    assert waiter_revenue_by_day_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-07"))
    ]

    model = add_projection_to_model(t.cast(SqlModel, model), literal=True)
    context.upsert_model(model)

    waiter_revenue_by_day_snapshot = context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    )

    with freeze_time("2023-01-08 00:10:00"):  # Past model's cron.
        plan = context.plan(
            "dev", select_models=[model.name], no_prompts=True, skip_tests=True, enable_preview=True
        )
        assert plan.missing_intervals == [
            SnapshotIntervals(
                snapshot_id=waiter_revenue_by_day_snapshot.snapshot_id,
                intervals=expected_intervals,
            ),
        ]


@freeze_time("2023-01-08 15:00:00")
def test_forward_only_parent_created_in_dev_child_created_in_prod(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    waiter_revenue_by_day_model = context.get_model("sushi.waiter_revenue_by_day")
    waiter_revenue_by_day_model = add_projection_to_model(
        t.cast(SqlModel, waiter_revenue_by_day_model)
    )
    forward_only_kind = waiter_revenue_by_day_model.kind.copy(update={"forward_only": True})
    waiter_revenue_by_day_model = waiter_revenue_by_day_model.copy(
        update={"kind": forward_only_kind}
    )
    context.upsert_model(waiter_revenue_by_day_model)

    waiter_revenue_by_day_snapshot = context.get_snapshot(
        waiter_revenue_by_day_model, raise_if_missing=True
    )
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan("dev", no_prompts=True, skip_tests=True)
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[waiter_revenue_by_day_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert plan.start == to_datetime("2023-01-01")
    assert not plan.missing_intervals

    context.apply(plan)

    # Update the child to refer to a newly added column.
    top_waiters_model = context.get_model("sushi.top_waiters")
    top_waiters_model = add_projection_to_model(t.cast(SqlModel, top_waiters_model), literal=False)
    context.upsert_model(top_waiters_model)

    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan("prod", no_prompts=True, skip_tests=True)
    assert len(plan.new_snapshots) == 1
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )

    context.apply(plan)


@freeze_time("2023-01-08 15:00:00")
def test_plan_set_choice_is_reflected_in_missing_intervals(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model_name = "sushi.waiter_revenue_by_day"

    model = context.get_model(model_name)
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan_builder = context.plan_builder("dev", skip_tests=True)
    plan = plan_builder.build()
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    # Change the category to BREAKING
    plan = plan_builder.set_choice(
        plan.context_diff.snapshots[snapshot.snapshot_id], SnapshotChangeCategory.BREAKING
    ).build()
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_BREAKING
    )
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    # Change the category back to NON_BREAKING
    plan = plan_builder.set_choice(
        plan.context_diff.snapshots[snapshot.snapshot_id], SnapshotChangeCategory.NON_BREAKING
    ).build()
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert dev_df["event_date"].tolist() == [
        pd.to_datetime(x)
        for x in [
            "2023-01-01",
            "2023-01-02",
            "2023-01-03",
            "2023-01-04",
            "2023-01-05",
            "2023-01-06",
            "2023-01-07",
        ]
    ]

    # Promote changes to prod
    prod_plan = context.plan(no_prompts=True, skip_tests=True)
    assert not prod_plan.missing_intervals

    context.apply(prod_plan)
    prod_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi.waiter_revenue_by_day WHERE one IS NOT NULL ORDER BY event_date"
    )
    assert prod_df["event_date"].tolist() == [
        pd.to_datetime(x)
        for x in [
            "2023-01-01",
            "2023-01-02",
            "2023-01-03",
            "2023-01-04",
            "2023-01-05",
            "2023-01-06",
            "2023-01-07",
        ]
    ]


@freeze_time("2023-01-08 15:00:00", tick=True)
@pytest.mark.parametrize("has_view_binding", [False, True])
def test_non_breaking_change_after_forward_only_in_dev(
    init_and_plan_context: t.Callable, has_view_binding: bool
):
    context, plan = init_and_plan_context("examples/sushi")
    context.snapshot_evaluator.adapter.HAS_VIEW_BINDING = has_view_binding
    context.apply(plan)

    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))
    waiter_revenue_by_day_snapshot = context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    )
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan("dev", no_prompts=True, skip_tests=True, forward_only=True)
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[waiter_revenue_by_day_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert plan.start == pd.to_datetime("2023-01-07")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[(to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))],
        ),
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot.snapshot_id,
            intervals=[(to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))],
        ),
    ]

    # Apply the forward-only changes first.
    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert dev_df["event_date"].tolist() == [pd.to_datetime("2023-01-07")]

    # Make a non-breaking change to a model downstream.
    model = context.get_model("sushi.top_waiters")
    # Select 'one' column from the updated upstream model.
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model), literal=False))
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan("dev", no_prompts=True, skip_tests=True)
    assert len(plan.new_snapshots) == 1
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    # Apply the non-breaking changes.
    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT waiter_id FROM sushi__dev.top_waiters WHERE one IS NOT NULL"
    )
    assert not dev_df.empty

    prod_df = context.engine_adapter.fetchdf("DESCRIBE sushi.top_waiters")
    assert "one" not in prod_df["column_name"].tolist()

    # Deploy both changes to prod.
    plan = context.plan("prod", no_prompts=True, skip_tests=True)
    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan)

    prod_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi.waiter_revenue_by_day WHERE one IS NOT NULL ORDER BY event_date"
    )
    assert prod_df.empty

    prod_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT waiter_id FROM sushi.top_waiters WHERE one IS NOT NULL"
    )
    assert prod_df.empty


@freeze_time("2023-01-08 15:00:00")
def test_indirect_non_breaking_change_after_forward_only_in_dev(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Make sushi.orders a forward-only model.
    model = context.get_model("sushi.orders")
    updated_model_kind = model.kind.copy(update={"forward_only": True})
    model = model.copy(update={"stamp": "force new version", "kind": updated_model_kind})
    context.upsert_model(model)
    snapshot = context.get_snapshot(model, raise_if_missing=True)

    plan = context.plan("dev", no_prompts=True, skip_tests=True)
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert not plan.requires_backfill
    context.apply(plan)

    # Make a non-breaking change to a model.
    model = context.get_model("sushi.top_waiters")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan("dev", no_prompts=True, skip_tests=True)
    assert len(plan.new_snapshots) == 1
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    # Apply the non-breaking changes.
    context.apply(plan)

    # Make a non-breaking change upstream from the previously modified model.
    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))
    waiter_revenue_by_day_snapshot = context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    )
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan("dev", no_prompts=True, skip_tests=True)
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[waiter_revenue_by_day_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    # Apply the upstream non-breaking changes.
    context.apply(plan)
    assert not context.plan("dev", no_prompts=True, skip_tests=True).requires_backfill

    # Deploy everything to prod.
    plan = context.plan("prod", no_prompts=True, skip_tests=True)
    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan)
    assert not context.plan("prod", no_prompts=True, skip_tests=True).requires_backfill


@freeze_time("2023-01-08 15:00:00")
def test_forward_only_precedence_over_indirect_non_breaking(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Make sushi.orders a forward-only model.
    forward_only_model = context.get_model("sushi.orders")
    updated_model_kind = forward_only_model.kind.copy(update={"forward_only": True})
    forward_only_model = forward_only_model.copy(
        update={"stamp": "force new version", "kind": updated_model_kind}
    )
    context.upsert_model(forward_only_model)
    forward_only_snapshot = context.get_snapshot(forward_only_model, raise_if_missing=True)

    non_breaking_model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, non_breaking_model)))
    non_breaking_snapshot = context.get_snapshot(non_breaking_model, raise_if_missing=True)
    top_waiter_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan("dev", no_prompts=True, skip_tests=True)
    assert (
        plan.context_diff.snapshots[forward_only_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert (
        plan.context_diff.snapshots[non_breaking_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiter_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.FORWARD_ONLY
    )
    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=non_breaking_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan)
    assert not context.plan("dev", no_prompts=True, skip_tests=True).requires_backfill

    # Deploy everything to prod.
    plan = context.plan("prod", no_prompts=True, skip_tests=True)
    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=non_breaking_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan)
    assert not context.plan("prod", no_prompts=True, skip_tests=True).requires_backfill


@freeze_time("2023-01-08 15:00:00")
def test_run_with_select_models(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    with freeze_time("2023-01-09 00:00:00"):
        assert context.run(select_models=["*waiter_revenue_by_day"])

        snapshots = context.state_sync.state_sync.get_snapshots(context.snapshots.values())
        # Only waiter_revenue_by_day and its parents should be backfilled up to 2023-01-09.
        assert {s.name: s.intervals[0][1] for s in snapshots.values() if s.intervals} == {
            '"memory"."sushi"."waiter_revenue_by_day"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."order_items"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."orders"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."items"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."customer_revenue_lifetime"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."customer_revenue_by_day"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."waiter_names"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."raw_marketing"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."marketing"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."waiter_as_customer_by_day"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."top_waiters"': to_timestamp("2023-01-08"),
            '"memory"."raw"."demographics"': to_timestamp("2023-01-08"),
            "assert_item_price_above_zero": to_timestamp("2023-01-08"),
            '"memory"."sushi"."active_customers"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."customers"': to_timestamp("2023-01-08"),
        }


@freeze_time("2023-01-08 15:00:00")
def test_run_with_select_models_no_auto_upstream(
    init_and_plan_context: t.Callable,
):
    context, _ = init_and_plan_context("examples/sushi")

    model = context.get_model("sushi.waiter_revenue_by_day")
    model = SqlModel.parse_obj({**model.dict(), "audits": []})
    context.upsert_model(model)

    context.plan("prod", no_prompts=True, skip_tests=True, auto_apply=True)

    with freeze_time("2023-01-09 00:00:00"):
        assert context.run(select_models=["*waiter_revenue_by_day"], no_auto_upstream=True)

        snapshots = context.state_sync.state_sync.get_snapshots(context.snapshots.values())
        # Only waiter_revenue_by_day should be backfilled up to 2023-01-09.
        assert {s.name: s.intervals[0][1] for s in snapshots.values() if s.intervals} == {
            '"memory"."sushi"."waiter_revenue_by_day"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."order_items"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."orders"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."items"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."customer_revenue_lifetime"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."customer_revenue_by_day"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."waiter_names"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."raw_marketing"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."marketing"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."waiter_as_customer_by_day"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."top_waiters"': to_timestamp("2023-01-08"),
            '"memory"."raw"."demographics"': to_timestamp("2023-01-08"),
            "assert_item_price_above_zero": to_timestamp("2023-01-08"),
            '"memory"."sushi"."active_customers"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."customers"': to_timestamp("2023-01-08"),
        }


@freeze_time("2023-01-08 15:00:00")
def test_select_models(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Modify 2 models.
    model = context.get_model("sushi.waiter_revenue_by_day")
    kwargs = {
        **model.dict(),
        # Make a breaking change.
        "query": model.query.order_by("waiter_id"),  # type: ignore
    }
    context.upsert_model(SqlModel.parse_obj(kwargs))

    model = context.get_model("sushi.customer_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    expected_intervals = [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
        (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
        (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
        (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
    ]

    waiter_revenue_by_day_snapshot_id = context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    ).snapshot_id

    # Select one of the modified models.
    plan_builder = context.plan_builder(
        "dev", select_models=["*waiter_revenue_by_day"], skip_tests=True
    )
    snapshot = plan_builder._context_diff.snapshots[waiter_revenue_by_day_snapshot_id]
    plan_builder.set_choice(snapshot, SnapshotChangeCategory.BREAKING)
    plan = plan_builder.build()

    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot_id,
            intervals=expected_intervals,
        ),
    ]

    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert len(dev_df) == 7

    # Make sure that we only create a view for the selected model.
    schema_objects = context.engine_adapter.get_data_objects("sushi__dev")
    assert len(schema_objects) == 1
    assert schema_objects[0].name == "waiter_revenue_by_day"

    # Validate the other modified model.
    assert not context.get_snapshot("sushi.customer_revenue_by_day").change_category
    assert not context.get_snapshot("sushi.customer_revenue_by_day").version

    # Validate the downstream model.
    assert not context.engine_adapter.table_exists(
        context.get_snapshot("sushi.top_waiters").table_name()
    )
    assert not context.engine_adapter.table_exists(
        context.get_snapshot("sushi.top_waiters").table_name(False)
    )

    # Make sure that tables are created when deploying to prod.
    plan = context.plan("prod", skip_tests=True)
    context.apply(plan)
    assert context.engine_adapter.table_exists(
        context.get_snapshot("sushi.top_waiters").table_name()
    )


@freeze_time("2023-01-08 15:00:00")
def test_select_unchanged_model_for_backfill(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Modify 2 models.
    model = context.get_model("sushi.waiter_revenue_by_day")
    kwargs = {
        **model.dict(),
        # Make a breaking change.
        "query": d.parse_one(
            f"{model.query.sql(dialect='duckdb')} ORDER BY waiter_id", dialect="duckdb"
        ),
    }
    context.upsert_model(SqlModel.parse_obj(kwargs))

    model = context.get_model("sushi.customer_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    expected_intervals = [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
        (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
        (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
        (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
    ]

    waiter_revenue_by_day_snapshot_id = context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    ).snapshot_id

    # Select one of the modified models.
    plan_builder = context.plan_builder(
        "dev", select_models=["*waiter_revenue_by_day"], skip_tests=True
    )
    snapshot = plan_builder._context_diff.snapshots[waiter_revenue_by_day_snapshot_id]
    plan_builder.set_choice(snapshot, SnapshotChangeCategory.BREAKING)
    plan = plan_builder.build()

    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot_id,
            intervals=expected_intervals,
        ),
    ]

    context.apply(plan)

    # Make sure that we only create a view for the selected model.
    schema_objects = context.engine_adapter.get_data_objects("sushi__dev")
    assert {o.name for o in schema_objects} == {"waiter_revenue_by_day"}

    # Now select a model downstream from the previously modified one in order to backfill it.
    plan = context.plan("dev", select_models=["*top_waiters"], skip_tests=True, no_prompts=True)

    assert not plan.has_changes
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=context.get_snapshot(
                "sushi.top_waiters", raise_if_missing=True
            ).snapshot_id,
            intervals=expected_intervals,
        ),
    ]

    context.apply(plan)

    # Make sure that a view has been created for the downstream selected model.
    schema_objects = context.engine_adapter.get_data_objects("sushi__dev")
    assert {o.name for o in schema_objects} == {"waiter_revenue_by_day", "top_waiters"}


@freeze_time("2023-01-08 15:00:00")
def test_max_interval_end_per_model_not_applied_when_end_is_provided(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    with freeze_time("2023-01-09 00:00:00"):
        context.run()

        plan = context.plan(
            no_prompts=True, restate_models=["*"], start="2023-01-09", end="2023-01-09"
        )
        context.apply(plan)


@freeze_time("2023-01-08 15:00:00")
def test_select_models_for_backfill(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    expected_intervals = [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
        (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
        (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
        (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
    ]

    plan = context.plan(
        "dev", backfill_models=["+*waiter_revenue_by_day"], no_prompts=True, skip_tests=True
    )

    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=context.get_snapshot("sushi.items", raise_if_missing=True).snapshot_id,
            intervals=expected_intervals,
        ),
        SnapshotIntervals(
            snapshot_id=context.get_snapshot(
                "sushi.order_items", raise_if_missing=True
            ).snapshot_id,
            intervals=expected_intervals,
        ),
        SnapshotIntervals(
            snapshot_id=context.get_snapshot("sushi.orders", raise_if_missing=True).snapshot_id,
            intervals=expected_intervals,
        ),
        SnapshotIntervals(
            snapshot_id=context.get_snapshot(
                "sushi.waiter_revenue_by_day", raise_if_missing=True
            ).snapshot_id,
            intervals=expected_intervals,
        ),
    ]

    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert len(dev_df) == 7

    schema_objects = context.engine_adapter.get_data_objects("sushi__dev")
    assert {o.name for o in schema_objects} == {
        "items",
        "order_items",
        "orders",
        "waiter_revenue_by_day",
    }

    assert not context.engine_adapter.table_exists(
        context.get_snapshot("sushi.customer_revenue_by_day").table_name()
    )

    # Make sure that tables are created when deploying to prod.
    plan = context.plan("prod")
    context.apply(plan)
    assert context.engine_adapter.table_exists(
        context.get_snapshot("sushi.customer_revenue_by_day").table_name()
    )


@freeze_time("2023-01-08 15:00:00")
def test_dbt_select_star_is_directly_modified(sushi_test_dbt_context: Context):
    context = sushi_test_dbt_context

    model = context.get_model("sushi.simple_model_a")
    context.upsert_model(
        model,
        query=d.parse_one("SELECT 1 AS a, 2 AS b"),
    )

    snapshot_a_id = context.get_snapshot("sushi.simple_model_a").snapshot_id  # type: ignore
    snapshot_b_id = context.get_snapshot("sushi.simple_model_b").snapshot_id  # type: ignore

    plan = context.plan_builder("dev", skip_tests=True).build()
    assert plan.directly_modified == {snapshot_a_id, snapshot_b_id}
    assert {i.snapshot_id for i in plan.missing_intervals} == {snapshot_a_id, snapshot_b_id}

    assert plan.snapshots[snapshot_a_id].change_category == SnapshotChangeCategory.NON_BREAKING
    assert plan.snapshots[snapshot_b_id].change_category == SnapshotChangeCategory.NON_BREAKING


def test_model_attr(sushi_test_dbt_context: Context, assert_exp_eq):
    context = sushi_test_dbt_context
    model = context.get_model("sushi.top_waiters")
    assert_exp_eq(
        model.render_query(),
        """
        SELECT
          CAST("waiter_id" AS INT) AS "waiter_id",
          CAST("revenue" AS DOUBLE) AS "revenue",
          3 AS "model_columns"
        FROM "memory"."sushi"."waiter_revenue_by_day_v2" AS "waiter_revenue_by_day_v2"
        WHERE
          "ds" = (
             SELECT
               MAX("ds")
             FROM "memory"."sushi"."waiter_revenue_by_day_v2" AS "waiter_revenue_by_day_v2"
           )
        ORDER BY
          "revenue" DESC NULLS FIRST
        LIMIT 10
        """,
    )


@freeze_time("2023-01-08 15:00:00")
def test_incremental_by_partition(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    source_name = "raw.test_incremental_by_partition"
    model_name = "memory.sushi.test_incremental_by_partition"

    expressions = d.parse(
        f"""
        MODEL (
            name {model_name},
            kind INCREMENTAL_BY_PARTITION,
            partitioned_by [key],
            allow_partials true,
        );

        SELECT key, value FROM {source_name};
        """
    )
    model = load_sql_based_model(expressions)
    context.upsert_model(model)

    context.engine_adapter.ctas(
        source_name,
        d.parse_one("SELECT 'key_a' AS key, 1 AS value"),
    )

    context.plan(auto_apply=True, no_prompts=True)
    assert context.engine_adapter.fetchall(f"SELECT * FROM {model_name}") == [
        ("key_a", 1),
    ]

    context.engine_adapter.replace_query(
        source_name,
        d.parse_one("SELECT 'key_b' AS key, 1 AS value"),
    )
    context.run(ignore_cron=True)
    assert context.engine_adapter.fetchall(f"SELECT * FROM {model_name}") == [
        ("key_a", 1),
        ("key_b", 1),
    ]

    context.engine_adapter.replace_query(
        source_name,
        d.parse_one("SELECT 'key_a' AS key, 2 AS value"),
    )
    context.run(ignore_cron=True)
    assert context.engine_adapter.fetchall(f"SELECT * FROM {model_name}") == [
        ("key_b", 1),
        ("key_a", 2),
    ]


@freeze_time("2023-01-08 15:00:00")
def test_custom_materialization(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    custom_insert_called = False

    class CustomFullMaterialization(CustomMaterialization):
        NAME = "test_custom_full"

        def insert(
            self,
            table_name: str,
            query_or_df: QueryOrDF,
            model: Model,
            is_first_insert: bool,
            **kwargs: t.Any,
        ) -> None:
            nonlocal custom_insert_called
            custom_insert_called = True

            self._replace_query_for_model(model, table_name, query_or_df)

    model = context.get_model("sushi.top_waiters")
    kwargs = {
        **model.dict(),
        # Make a breaking change.
        "kind": dict(name="CUSTOM", materialization="test_custom_full"),
    }
    context.upsert_model(SqlModel.parse_obj(kwargs))

    context.plan(auto_apply=True, no_prompts=True)

    assert custom_insert_called


@freeze_time("2023-01-08 15:00:00")
def test_unaligned_start_snapshot_with_non_deployable_downstream(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    downstream_model_name = "memory.sushi.customer_max_revenue"

    expressions = d.parse(
        f"""
        MODEL (
            name {downstream_model_name},
            kind INCREMENTAL_BY_UNIQUE_KEY (
                unique_key customer_id,
                forward_only true,
            ),
        );

        SELECT
          customer_id, MAX(revenue) AS max_revenue
        FROM memory.sushi.customer_revenue_lifetime
        GROUP BY 1;
        """
    )

    downstream_model = load_sql_based_model(expressions)
    assert downstream_model.forward_only
    context.upsert_model(downstream_model)

    context.plan(auto_apply=True, no_prompts=True)

    customer_revenue_lifetime_model = context.get_model("sushi.customer_revenue_lifetime")
    kwargs = {
        **customer_revenue_lifetime_model.dict(),
        "name": "memory.sushi.customer_revenue_lifetime_new",
        "kind": dict(
            name="INCREMENTAL_UNMANAGED"
        ),  # Make it incremental unmanaged to ensure the depends_on_past behavior.
    }
    context.upsert_model(SqlModel.parse_obj(kwargs))
    context.upsert_model(
        downstream_model_name,
        query=d.parse_one(
            "SELECT customer_id, MAX(revenue) AS max_revenue FROM memory.sushi.customer_revenue_lifetime_new GROUP BY 1"
        ),
    )

    plan = context.plan("dev", no_prompts=True, enable_preview=True)
    assert {s.name for s in plan.new_snapshots} == {
        '"memory"."sushi"."customer_revenue_lifetime_new"',
        '"memory"."sushi"."customer_max_revenue"',
    }
    for snapshot_interval in plan.missing_intervals:
        assert not plan.deployability_index.is_deployable(snapshot_interval.snapshot_id)
        assert snapshot_interval.intervals[0][0] == to_timestamp("2023-01-07")


@freeze_time("2023-01-08 15:00:00")
def test_restatement_plan_ignores_changes(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    restated_snapshot = context.get_snapshot("sushi.top_waiters")

    # Simulate a change.
    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    plan = context.plan(no_prompts=True, restate_models=["sushi.top_waiters"], start="2023-01-07")
    assert plan.snapshots != context.snapshots

    assert not plan.directly_modified
    assert not plan.has_changes
    assert not plan.new_snapshots
    assert plan.requires_backfill
    assert plan.restatements == {
        restated_snapshot.snapshot_id: (to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))
    }
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=restated_snapshot.snapshot_id,
            intervals=[(to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))],
        )
    ]

    context.apply(plan)


@freeze_time("2023-01-08 15:00:00")
def test_plan_against_expired_environment(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    modified_models = {model.fqn, context.get_model("sushi.top_waiters").fqn}

    plan = context.plan("dev", no_prompts=True)
    assert plan.has_changes
    assert set(plan.context_diff.modified_snapshots) == modified_models
    assert plan.missing_intervals
    context.apply(plan)

    # Make sure there are no changes when comparing against the existing environment.
    plan = context.plan("dev", no_prompts=True)
    assert not plan.has_changes
    assert not plan.context_diff.modified_snapshots
    assert not plan.missing_intervals

    # Invalidate the environment and make sure that the plan detects the changes.
    context.invalidate_environment("dev")
    plan = context.plan("dev", no_prompts=True)
    assert plan.has_changes
    assert set(plan.context_diff.modified_snapshots) == modified_models
    assert not plan.missing_intervals
    context.apply(plan)


@freeze_time("2023-01-08 15:00:00")
def test_new_forward_only_model_concurrent_versions(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    new_model_expr = d.parse(
        """
        MODEL (
            name memory.sushi.new_model,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                forward_only TRUE,
                on_destructive_change 'allow',
            ),
        );

        SELECT '2023-01-07' AS ds, 1 AS a;
        """
    )
    new_model = load_sql_based_model(new_model_expr)

    # Add the first version of the model and apply it to dev_a.
    context.upsert_model(new_model)
    snapshot_a = context.get_snapshot(new_model.name)
    plan_a = context.plan("dev_a", no_prompts=True)
    snapshot_a = plan_a.snapshots[snapshot_a.snapshot_id]

    assert snapshot_a.snapshot_id in plan_a.context_diff.new_snapshots
    assert snapshot_a.snapshot_id in plan_a.context_diff.added
    assert snapshot_a.change_category == SnapshotChangeCategory.BREAKING

    context.apply(plan_a)

    new_model_alt_expr = d.parse(
        """
        MODEL (
            name memory.sushi.new_model,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                forward_only TRUE,
                on_destructive_change 'allow',
            ),
        );

        SELECT '2023-01-07' AS ds, 1 AS b;
        """
    )
    new_model_alt = load_sql_based_model(new_model_alt_expr)

    # Add the second version of the model and apply it to dev_b.
    context.upsert_model(new_model_alt)
    snapshot_b = context.get_snapshot(new_model_alt.name)
    plan_b = context.plan("dev_b", no_prompts=True)
    snapshot_b = plan_b.snapshots[snapshot_b.snapshot_id]

    assert snapshot_b.snapshot_id in plan_b.context_diff.new_snapshots
    assert snapshot_b.snapshot_id in plan_b.context_diff.added
    assert snapshot_b.change_category == SnapshotChangeCategory.BREAKING

    assert snapshot_b.fingerprint != snapshot_a.fingerprint
    assert snapshot_b.version == snapshot_a.version

    context.apply(plan_b)

    # Apply the 1st version to prod
    context.upsert_model(new_model)
    plan_prod_a = context.plan("prod", no_prompts=True)
    assert snapshot_a.snapshot_id in plan_prod_a.snapshots
    assert (
        plan_prod_a.snapshots[snapshot_a.snapshot_id].change_category
        == SnapshotChangeCategory.BREAKING
    )
    context.apply(plan_prod_a)

    df = context.fetchdf("SELECT * FROM memory.sushi.new_model")
    assert df.to_dict() == {"ds": {0: "2023-01-07"}, "a": {0: 1}}

    # Apply the 2nd version to prod
    context.upsert_model(new_model_alt)
    plan_prod_b = context.plan("prod", no_prompts=True)
    assert snapshot_b.snapshot_id in plan_prod_b.snapshots
    assert (
        plan_prod_b.snapshots[snapshot_b.snapshot_id].change_category
        == SnapshotChangeCategory.BREAKING
    )
    assert not plan_prod_b.requires_backfill
    context.apply(plan_prod_b)

    df = context.fetchdf("SELECT * FROM memory.sushi.new_model").replace({np.nan: None})
    assert df.to_dict() == {"ds": {0: "2023-01-07"}, "b": {0: None}}


def test_plan_twice_with_star_macro_yields_no_diff(tmp_path: Path):
    init_example_project(tmp_path, dialect="duckdb")

    star_model_definition = """
        MODEL (
          name sqlmesh_example.star_model,
          kind FULL
        );

        SELECT @STAR(sqlmesh_example.full_model) FROM sqlmesh_example.full_model
    """

    star_model_path = tmp_path / "models" / "star_model.sql"
    star_model_path.write_text(star_model_definition)

    db_path = str(tmp_path / "db.db")
    config = Config(
        gateways={"main": GatewayConfig(connection=DuckDBConnectionConfig(database=db_path))},
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    )
    context = Context(paths=tmp_path, config=config)
    context.plan(auto_apply=True, no_prompts=True)

    # Instantiate new context to remove caches etc
    new_context = Context(paths=tmp_path, config=config)

    star_model = new_context.get_model("sqlmesh_example.star_model")
    assert (
        star_model.render_query_or_raise().sql()
        == 'SELECT CAST("full_model"."item_id" AS INT) AS "item_id", CAST("full_model"."num_orders" AS BIGINT) AS "num_orders" FROM "db"."sqlmesh_example"."full_model" AS "full_model"'
    )

    new_plan = new_context.plan(no_prompts=True)
    assert not new_plan.has_changes
    assert not new_plan.new_snapshots


@freeze_time("2023-01-08 15:00:00")
def test_create_environment_no_changes_with_selector(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    with pytest.raises(NoChangesPlanError):
        context.plan("dev", no_prompts=True)

    plan = context.plan("dev", no_prompts=True, select_models=["*top_waiters"])
    assert not plan.missing_intervals
    context.apply(plan)

    schema_objects = context.engine_adapter.get_data_objects("sushi__dev")
    assert {o.name for o in schema_objects} == {"top_waiters"}


@freeze_time("2023-01-08 15:00:00")
def test_empty_bacfkill(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    plan = context.plan("prod", no_prompts=True, skip_tests=True, empty_backfill=True)
    assert plan.missing_intervals
    assert plan.empty_backfill
    assert not plan.requires_backfill

    context.apply(plan)

    for model in context.models.values():
        if model.is_seed or model.kind.is_symbolic:
            continue
        row_num = context.engine_adapter.fetchone(f"SELECT COUNT(*) FROM {model.name}")[0]
        assert row_num == 0

    plan = context.plan("prod", no_prompts=True, skip_tests=True)
    assert not plan.requires_backfill
    assert not plan.has_changes
    assert not plan.missing_intervals


@pytest.mark.parametrize(
    "context_fixture",
    ["sushi_context", "sushi_dbt_context", "sushi_test_dbt_context", "sushi_no_default_catalog"],
)
def test_model_add(context_fixture: Context, request):
    initial_add(request.getfixturevalue(context_fixture), "dev")


def test_model_removed(sushi_context: Context):
    environment = "dev"
    initial_add(sushi_context, environment)

    top_waiters_snapshot_id = sushi_context.get_snapshot(
        "sushi.top_waiters", raise_if_missing=True
    ).snapshot_id

    sushi_context._models.pop('"memory"."sushi"."top_waiters"')

    def _validate_plan(context, plan):
        validate_plan_changes(plan, removed=[top_waiters_snapshot_id])
        assert not plan.missing_intervals

    def _validate_apply(context):
        assert not sushi_context.get_snapshot("sushi.top_waiters", raise_if_missing=False)
        assert sushi_context.state_reader.get_snapshots([top_waiters_snapshot_id])
        env = sushi_context.state_reader.get_environment(environment)
        assert env
        assert all(snapshot.name != '"memory"."sushi"."top_waiters"' for snapshot in env.snapshots)

    apply_to_environment(
        sushi_context,
        environment,
        SnapshotChangeCategory.BREAKING,
        plan_validators=[_validate_plan],
        apply_validators=[_validate_apply],
    )


def test_non_breaking_change(sushi_context: Context):
    environment = "dev"
    initial_add(sushi_context, environment)
    validate_query_change(sushi_context, environment, SnapshotChangeCategory.NON_BREAKING, False)


def test_breaking_change(sushi_context: Context):
    environment = "dev"
    initial_add(sushi_context, environment)
    validate_query_change(sushi_context, environment, SnapshotChangeCategory.BREAKING, False)


def test_forward_only(sushi_context: Context):
    environment = "dev"
    initial_add(sushi_context, environment)
    validate_query_change(sushi_context, environment, SnapshotChangeCategory.FORWARD_ONLY, False)


def test_logical_change(sushi_context: Context):
    environment = "dev"
    initial_add(sushi_context, environment)
    previous_sushi_items_version = sushi_context.get_snapshot(
        "sushi.items", raise_if_missing=True
    ).version

    change_data_type(
        sushi_context,
        "sushi.items",
        DataType.Type.DOUBLE,
        DataType.Type.FLOAT,
    )
    apply_to_environment(sushi_context, environment, SnapshotChangeCategory.NON_BREAKING)

    change_data_type(
        sushi_context,
        "sushi.items",
        DataType.Type.FLOAT,
        DataType.Type.DOUBLE,
    )
    apply_to_environment(sushi_context, environment, SnapshotChangeCategory.NON_BREAKING)

    assert (
        sushi_context.get_snapshot("sushi.items", raise_if_missing=True).version
        == previous_sushi_items_version
    )


def validate_query_change(
    context: Context,
    environment: str,
    change_category: SnapshotChangeCategory,
    logical: bool,
):
    versions = snapshots_to_versions(context.snapshots.values())

    change_data_type(
        context,
        "sushi.items",
        DataType.Type.DOUBLE,
        DataType.Type.FLOAT,
    )

    directly_modified = ['"memory"."sushi"."items"']
    indirectly_modified = [
        '"memory"."sushi"."order_items"',
        '"memory"."sushi"."waiter_revenue_by_day"',
        '"memory"."sushi"."customer_revenue_by_day"',
        '"memory"."sushi"."customer_revenue_lifetime"',
        '"memory"."sushi"."top_waiters"',
        "assert_item_price_above_zero",
    ]
    not_modified = [
        snapshot.name
        for snapshot in context.snapshots.values()
        if snapshot.name not in directly_modified and snapshot.name not in indirectly_modified
    ]

    if change_category == SnapshotChangeCategory.BREAKING and not logical:
        models_same = not_modified
        models_different = directly_modified + indirectly_modified
    elif change_category == SnapshotChangeCategory.FORWARD_ONLY:
        models_same = not_modified + directly_modified + indirectly_modified
        models_different = []
    else:
        models_same = not_modified + indirectly_modified
        models_different = directly_modified

    def _validate_plan(context, plan):
        validate_plan_changes(plan, modified=directly_modified + indirectly_modified)
        assert bool(plan.missing_intervals) != logical

    def _validate_apply(context):
        current_versions = snapshots_to_versions(context.snapshots.values())
        validate_versions_same(models_same, versions, current_versions)
        validate_versions_different(models_different, versions, current_versions)

    apply_to_environment(
        context,
        environment,
        change_category,
        plan_validators=[_validate_plan],
        apply_validators=[_validate_apply],
    )


@pytest.mark.parametrize(
    "from_, to",
    [
        (ModelKindName.INCREMENTAL_BY_TIME_RANGE, ModelKindName.FULL),
        (ModelKindName.FULL, ModelKindName.INCREMENTAL_BY_TIME_RANGE),
    ],
)
def test_model_kind_change(from_: ModelKindName, to: ModelKindName, sushi_context: Context):
    environment = f"test_model_kind_change__{from_.value.lower()}__{to.value.lower()}"
    incremental_snapshot = sushi_context.get_snapshot("sushi.items", raise_if_missing=True).copy()

    if from_ != ModelKindName.INCREMENTAL_BY_TIME_RANGE:
        change_model_kind(sushi_context, from_)
        apply_to_environment(sushi_context, environment, SnapshotChangeCategory.NON_BREAKING)

    if to == ModelKindName.INCREMENTAL_BY_TIME_RANGE:
        sushi_context.upsert_model(incremental_snapshot.model)
    else:
        change_model_kind(sushi_context, to)

    logical = to in (ModelKindName.INCREMENTAL_BY_TIME_RANGE, ModelKindName.EMBEDDED)
    validate_model_kind_change(to, sushi_context, environment, logical=logical)


def change_model_kind(context: Context, kind: ModelKindName):
    if kind in (ModelKindName.VIEW, ModelKindName.EMBEDDED, ModelKindName.FULL):
        context.upsert_model(
            "sushi.items",
            partitioned_by=[],
        )
    context.upsert_model("sushi.items", kind=model_kind_type_from_name(kind)())  # type: ignore


def validate_model_kind_change(
    kind_name: ModelKindName,
    context: Context,
    environment: str,
    *,
    logical: bool,
):
    directly_modified = ['"memory"."sushi"."items"']
    indirectly_modified = [
        '"memory"."sushi"."order_items"',
        '"memory"."sushi"."waiter_revenue_by_day"',
        '"memory"."sushi"."customer_revenue_by_day"',
        '"memory"."sushi"."customer_revenue_lifetime"',
        '"memory"."sushi"."top_waiters"',
        "assert_item_price_above_zero",
    ]
    if kind_name == ModelKindName.INCREMENTAL_BY_TIME_RANGE:
        kind: ModelKind = IncrementalByTimeRangeKind(time_column=TimeColumn(column="event_date"))
    elif kind_name == ModelKindName.INCREMENTAL_BY_UNIQUE_KEY:
        kind = IncrementalByUniqueKeyKind(unique_key="id")
    else:
        kind = model_kind_type_from_name(kind_name)()  # type: ignore

    def _validate_plan(context, plan):
        validate_plan_changes(plan, modified=directly_modified + indirectly_modified)
        assert (
            next(
                snapshot
                for snapshot in plan.snapshots.values()
                if snapshot.name == '"memory"."sushi"."items"'
            ).model.kind.name
            == kind.name
        )
        assert bool(plan.missing_intervals) != logical

    apply_to_environment(
        context,
        environment,
        SnapshotChangeCategory.NON_BREAKING,
        plan_validators=[_validate_plan],
    )


def test_environment_isolation(sushi_context: Context):
    prod_snapshots = sushi_context.snapshots.values()

    change_data_type(
        sushi_context,
        "sushi.items",
        DataType.Type.DOUBLE,
        DataType.Type.FLOAT,
    )
    directly_modified = ['"memory"."sushi"."items"']
    indirectly_modified = [
        '"memory"."sushi"."order_items"',
        '"memory"."sushi"."waiter_revenue_by_day"',
        '"memory"."sushi"."customer_revenue_by_day"',
        '"memory"."sushi"."customer_revenue_lifetime"',
        '"memory"."sushi"."top_waiters"',
        "assert_item_price_above_zero",
    ]

    apply_to_environment(sushi_context, "dev", SnapshotChangeCategory.BREAKING)

    # Verify prod unchanged
    validate_apply_basics(sushi_context, "prod", prod_snapshots)

    def _validate_plan(context, plan):
        validate_plan_changes(plan, modified=directly_modified + indirectly_modified)
        assert not plan.missing_intervals

    apply_to_environment(
        sushi_context,
        "prod",
        SnapshotChangeCategory.BREAKING,
        plan_validators=[_validate_plan],
    )


def test_environment_promotion(sushi_context: Context):
    initial_add(sushi_context, "dev")

    # Simulate prod "ahead"
    change_data_type(sushi_context, "sushi.items", DataType.Type.DOUBLE, DataType.Type.FLOAT)
    apply_to_environment(sushi_context, "prod", SnapshotChangeCategory.BREAKING)

    # Simulate rebase
    apply_to_environment(sushi_context, "dev", SnapshotChangeCategory.BREAKING)

    # Make changes in dev
    change_data_type(sushi_context, "sushi.items", DataType.Type.FLOAT, DataType.Type.DECIMAL)
    apply_to_environment(sushi_context, "dev", SnapshotChangeCategory.NON_BREAKING)

    change_data_type(sushi_context, "sushi.top_waiters", DataType.Type.DOUBLE, DataType.Type.INT)
    apply_to_environment(sushi_context, "dev", SnapshotChangeCategory.BREAKING)

    change_data_type(
        sushi_context,
        "sushi.customer_revenue_by_day",
        DataType.Type.DOUBLE,
        DataType.Type.FLOAT,
    )
    apply_to_environment(
        sushi_context,
        "dev",
        SnapshotChangeCategory.FORWARD_ONLY,
        allow_destructive_models=['"memory"."sushi"."customer_revenue_by_day"'],
    )

    # Promote to prod
    def _validate_plan(context, plan):
        sushi_items_snapshot = context.get_snapshot("sushi.items", raise_if_missing=True)
        sushi_top_waiters_snapshot = context.get_snapshot(
            "sushi.top_waiters", raise_if_missing=True
        )
        sushi_customer_revenue_by_day_snapshot = context.get_snapshot(
            "sushi.customer_revenue_by_day", raise_if_missing=True
        )

        assert (
            plan.context_diff.modified_snapshots[sushi_items_snapshot.name][0].change_category
            == SnapshotChangeCategory.NON_BREAKING
        )
        assert (
            plan.context_diff.modified_snapshots[sushi_top_waiters_snapshot.name][0].change_category
            == SnapshotChangeCategory.BREAKING
        )
        assert (
            plan.context_diff.modified_snapshots[sushi_customer_revenue_by_day_snapshot.name][
                0
            ].change_category
            == SnapshotChangeCategory.FORWARD_ONLY
        )

    apply_to_environment(
        sushi_context,
        "prod",
        SnapshotChangeCategory.NON_BREAKING,
        plan_validators=[_validate_plan],
        allow_destructive_models=['"memory"."sushi"."customer_revenue_by_day"'],
    )


def test_no_override(sushi_context: Context) -> None:
    change_data_type(
        sushi_context,
        "sushi.items",
        DataType.Type.INT,
        DataType.Type.BIGINT,
    )

    change_data_type(
        sushi_context,
        "sushi.order_items",
        DataType.Type.INT,
        DataType.Type.BIGINT,
    )

    plan_builder = sushi_context.plan_builder("prod")
    plan = plan_builder.build()

    sushi_items_snapshot = sushi_context.get_snapshot("sushi.items", raise_if_missing=True)
    sushi_order_items_snapshot = sushi_context.get_snapshot(
        "sushi.order_items", raise_if_missing=True
    )
    sushi_water_revenue_by_day_snapshot = sushi_context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    )

    items = plan.context_diff.snapshots[sushi_items_snapshot.snapshot_id]
    order_items = plan.context_diff.snapshots[sushi_order_items_snapshot.snapshot_id]
    waiter_revenue = plan.context_diff.snapshots[sushi_water_revenue_by_day_snapshot.snapshot_id]

    plan_builder.set_choice(items, SnapshotChangeCategory.BREAKING).set_choice(
        order_items, SnapshotChangeCategory.NON_BREAKING
    )
    plan_builder.build()
    assert items.is_new_version
    assert waiter_revenue.is_new_version
    plan_builder.set_choice(items, SnapshotChangeCategory.NON_BREAKING)
    plan_builder.build()
    assert not waiter_revenue.is_new_version


@pytest.mark.parametrize(
    "change_categories, expected",
    [
        ([SnapshotChangeCategory.NON_BREAKING], SnapshotChangeCategory.BREAKING),
        ([SnapshotChangeCategory.BREAKING], SnapshotChangeCategory.BREAKING),
        (
            [SnapshotChangeCategory.NON_BREAKING, SnapshotChangeCategory.NON_BREAKING],
            SnapshotChangeCategory.BREAKING,
        ),
        (
            [SnapshotChangeCategory.NON_BREAKING, SnapshotChangeCategory.BREAKING],
            SnapshotChangeCategory.BREAKING,
        ),
        (
            [SnapshotChangeCategory.BREAKING, SnapshotChangeCategory.NON_BREAKING],
            SnapshotChangeCategory.BREAKING,
        ),
        (
            [SnapshotChangeCategory.BREAKING, SnapshotChangeCategory.BREAKING],
            SnapshotChangeCategory.BREAKING,
        ),
    ],
)
def test_revert(
    sushi_context: Context,
    change_categories: t.List[SnapshotChangeCategory],
    expected: SnapshotChangeCategory,
):
    environment = "prod"
    original_snapshot_id = sushi_context.get_snapshot("sushi.items", raise_if_missing=True)

    types = (DataType.Type.DOUBLE, DataType.Type.FLOAT, DataType.Type.DECIMAL)
    assert len(change_categories) < len(types)

    for i, category in enumerate(change_categories):
        change_data_type(sushi_context, "sushi.items", *types[i : i + 2])
        apply_to_environment(sushi_context, environment, category)
        assert (
            sushi_context.get_snapshot("sushi.items", raise_if_missing=True) != original_snapshot_id
        )

    change_data_type(sushi_context, "sushi.items", types[len(change_categories)], types[0])

    def _validate_plan(_, plan):
        snapshot = next(s for s in plan.snapshots.values() if s.name == '"memory"."sushi"."items"')
        assert snapshot.change_category == expected
        assert not plan.missing_intervals

    apply_to_environment(
        sushi_context,
        environment,
        change_categories[-1],
        plan_validators=[_validate_plan],
    )
    assert sushi_context.get_snapshot("sushi.items", raise_if_missing=True) == original_snapshot_id


def test_revert_after_downstream_change(sushi_context: Context):
    environment = "prod"
    change_data_type(sushi_context, "sushi.items", DataType.Type.DOUBLE, DataType.Type.FLOAT)
    apply_to_environment(sushi_context, environment, SnapshotChangeCategory.BREAKING)

    change_data_type(
        sushi_context,
        "sushi.waiter_revenue_by_day",
        DataType.Type.DOUBLE,
        DataType.Type.FLOAT,
    )
    apply_to_environment(sushi_context, environment, SnapshotChangeCategory.NON_BREAKING)

    change_data_type(sushi_context, "sushi.items", DataType.Type.FLOAT, DataType.Type.DOUBLE)

    def _validate_plan(_, plan):
        snapshot = next(s for s in plan.snapshots.values() if s.name == '"memory"."sushi"."items"')
        assert snapshot.change_category == SnapshotChangeCategory.BREAKING
        assert plan.missing_intervals

    apply_to_environment(
        sushi_context,
        environment,
        SnapshotChangeCategory.BREAKING,
        plan_validators=[_validate_plan],
    )


def test_auto_categorization(sushi_context: Context):
    environment = "dev"
    for config in sushi_context.configs.values():
        config.plan.auto_categorize_changes.sql = AutoCategorizationMode.FULL
    initial_add(sushi_context, environment)

    version = sushi_context.get_snapshot(
        "sushi.waiter_as_customer_by_day", raise_if_missing=True
    ).version
    fingerprint = sushi_context.get_snapshot(
        "sushi.waiter_as_customer_by_day", raise_if_missing=True
    ).fingerprint

    model = t.cast(SqlModel, sushi_context.get_model("sushi.customers", raise_if_missing=True))
    sushi_context.upsert_model("sushi.customers", query=model.query.select("'foo' AS foo"))  # type: ignore
    apply_to_environment(sushi_context, environment)

    assert (
        sushi_context.get_snapshot(
            "sushi.waiter_as_customer_by_day", raise_if_missing=True
        ).change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert (
        sushi_context.get_snapshot(
            "sushi.waiter_as_customer_by_day", raise_if_missing=True
        ).fingerprint
        != fingerprint
    )
    assert (
        sushi_context.get_snapshot("sushi.waiter_as_customer_by_day", raise_if_missing=True).version
        == version
    )


def test_multi(mocker):
    context = Context(paths=["examples/multi/repo_1", "examples/multi/repo_2"], gateway="memory")
    context._new_state_sync().reset(default_catalog=context.default_catalog)
    plan = context.plan()
    assert len(plan.new_snapshots) == 4
    context.apply(plan)

    adapter = context.engine_adapter
    context = Context(
        paths=["examples/multi/repo_1"],
        state_sync=context.state_sync,
        gateway="memory",
    )
    context._engine_adapters["memory"] = adapter

    model = context.get_model("bronze.a")
    assert model.project == "repo_1"
    context.upsert_model(model.copy(update={"query": model.query.select("'c' AS c")}))
    plan = context.plan()

    assert set(snapshot.name for snapshot in plan.directly_modified) == {
        '"memory"."bronze"."a"',
        '"memory"."bronze"."b"',
    }
    assert sorted([x.name for x in list(plan.indirectly_modified.values())[0]]) == [
        '"memory"."silver"."c"',
        '"memory"."silver"."d"',
    ]
    assert len(plan.missing_intervals) == 2
    context.apply(plan)
    validate_apply_basics(context, c.PROD, plan.snapshots.values())


def test_multi_dbt(mocker):
    context = Context(paths=["examples/multi_dbt/bronze", "examples/multi_dbt/silver"])
    context._new_state_sync().reset(default_catalog=context.default_catalog)
    plan = context.plan()
    assert len(plan.new_snapshots) == 4
    context.apply(plan)
    validate_apply_basics(context, c.PROD, plan.snapshots.values())


def test_multi_hybrid(mocker):
    context = Context(
        paths=["examples/multi_hybrid/dbt_repo", "examples/multi_hybrid/sqlmesh_repo"]
    )
    context._new_state_sync().reset(default_catalog=context.default_catalog)
    plan = context.plan()

    assert len(plan.new_snapshots) == 5
    assert context.dag.roots == {'"memory"."dbt_repo"."e"'}
    assert context.dag.graph['"memory"."dbt_repo"."c"'] == {'"memory"."sqlmesh_repo"."b"'}
    assert context.dag.graph['"memory"."sqlmesh_repo"."b"'] == {'"memory"."sqlmesh_repo"."a"'}
    assert context.dag.graph['"memory"."sqlmesh_repo"."a"'] == {'"memory"."dbt_repo"."e"'}
    assert context.dag.downstream('"memory"."dbt_repo"."e"') == [
        '"memory"."sqlmesh_repo"."a"',
        '"memory"."sqlmesh_repo"."b"',
        '"memory"."dbt_repo"."c"',
        '"memory"."dbt_repo"."d"',
    ]

    sqlmesh_model_a = context.get_model("sqlmesh_repo.a")
    dbt_model_c = context.get_model("dbt_repo.c")
    assert sqlmesh_model_a.project == "sqlmesh_repo"

    sqlmesh_rendered = 'SELECT ROUND(CAST(("col_a" / NULLIF(100, 0)) AS DECIMAL(16, 2)), 2) AS "col_a", "col_b" AS "col_b" FROM "memory"."dbt_repo"."e" AS "e"'
    dbt_rendered = 'SELECT DISTINCT ROUND(CAST(("col_a" / NULLIF(100, 0)) AS DECIMAL(16, 2)), 2) AS "rounded_col_a" FROM "memory"."sqlmesh_repo"."b" AS "b"'
    assert sqlmesh_model_a.render_query().sql() == sqlmesh_rendered
    assert dbt_model_c.render_query().sql() == dbt_rendered

    context.apply(plan)
    validate_apply_basics(context, c.PROD, plan.snapshots.values())


def test_incremental_time_self_reference(
    mocker: MockerFixture, sushi_context: Context, sushi_data_validator: SushiDataValidator
):
    start_ts = to_timestamp("1 week ago")
    start_date, end_date = to_date("1 week ago"), to_date("yesterday")
    if to_timestamp(start_date) < start_ts:
        # The start date must be aligned by the interval unit.
        start_date += timedelta(days=1)

    df = sushi_context.engine_adapter.fetchdf(
        "SELECT MIN(event_date) FROM sushi.customer_revenue_lifetime"
    )
    assert df.iloc[0, 0] == pd.to_datetime(start_date)
    df = sushi_context.engine_adapter.fetchdf(
        "SELECT MAX(event_date) FROM sushi.customer_revenue_lifetime"
    )
    assert df.iloc[0, 0] == pd.to_datetime(end_date)
    results = sushi_data_validator.validate("sushi.customer_revenue_lifetime", start_date, end_date)
    plan = sushi_context.plan(
        restate_models=["sushi.customer_revenue_lifetime", "sushi.customer_revenue_by_day"],
        no_prompts=True,
        start=start_date,
        end="5 days ago",
    )
    revenue_lifeteime_snapshot = sushi_context.get_snapshot(
        "sushi.customer_revenue_lifetime", raise_if_missing=True
    )
    revenue_by_day_snapshot = sushi_context.get_snapshot(
        "sushi.customer_revenue_by_day", raise_if_missing=True
    )
    assert sorted(plan.missing_intervals, key=lambda x: x.snapshot_id) == sorted(
        [
            SnapshotIntervals(
                snapshot_id=revenue_lifeteime_snapshot.snapshot_id,
                intervals=[
                    (to_timestamp(to_date("7 days ago")), to_timestamp(to_date("6 days ago"))),
                    (to_timestamp(to_date("6 days ago")), to_timestamp(to_date("5 days ago"))),
                    (to_timestamp(to_date("5 days ago")), to_timestamp(to_date("4 days ago"))),
                    (to_timestamp(to_date("4 days ago")), to_timestamp(to_date("3 days ago"))),
                    (to_timestamp(to_date("3 days ago")), to_timestamp(to_date("2 days ago"))),
                    (to_timestamp(to_date("2 days ago")), to_timestamp(to_date("1 days ago"))),
                    (to_timestamp(to_date("1 day ago")), to_timestamp(to_date("today"))),
                ],
            ),
            SnapshotIntervals(
                snapshot_id=revenue_by_day_snapshot.snapshot_id,
                intervals=[
                    (to_timestamp(to_date("7 days ago")), to_timestamp(to_date("6 days ago"))),
                    (to_timestamp(to_date("6 days ago")), to_timestamp(to_date("5 days ago"))),
                ],
            ),
        ],
        key=lambda x: x.snapshot_id,
    )
    sushi_context.console = mocker.Mock(spec=Console)
    sushi_context.apply(plan)
    num_batch_calls = Counter(
        [x[0][0] for x in sushi_context.console.update_snapshot_evaluation_progress.call_args_list]  # type: ignore
    )
    # Validate that we made 7 calls to the customer_revenue_lifetime snapshot and 1 call to the customer_revenue_by_day snapshot
    assert num_batch_calls == {
        sushi_context.get_snapshot("sushi.customer_revenue_lifetime", raise_if_missing=True): 7,
        sushi_context.get_snapshot("sushi.customer_revenue_by_day", raise_if_missing=True): 1,
    }
    # Validate that the results are the same as before the restate
    assert results == sushi_data_validator.validate(
        "sushi.customer_revenue_lifetime", start_date, end_date
    )


def test_invalidating_environment(sushi_context: Context):
    apply_to_environment(sushi_context, "dev")
    start_environment = sushi_context.state_sync.get_environment("dev")
    assert start_environment is not None
    metadata = DuckDBMetadata.from_context(sushi_context)
    start_schemas = set(metadata.schemas)
    assert "sushi__dev" in start_schemas
    sushi_context.invalidate_environment("dev")
    invalidate_environment = sushi_context.state_sync.get_environment("dev")
    assert invalidate_environment is not None
    schemas_prior_to_janitor = set(metadata.schemas)
    assert invalidate_environment.expiration_ts < start_environment.expiration_ts  # type: ignore
    assert start_schemas == schemas_prior_to_janitor
    sushi_context._run_janitor()
    schemas_after_janitor = set(metadata.schemas)
    assert sushi_context.state_sync.get_environment("dev") is None
    assert start_schemas - schemas_after_janitor == {"sushi__dev"}


def test_environment_suffix_target_table(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi", config="environment_suffix_config")
    context.apply(plan)
    metadata = DuckDBMetadata.from_context(context)
    environments_schemas = {"sushi"}
    internal_schemas = {"sqlmesh", "sqlmesh__sushi"}
    starting_schemas = environments_schemas | internal_schemas
    # Make sure no new schemas are created
    assert set(metadata.schemas) - starting_schemas == {"raw"}
    prod_views = {x for x in metadata.qualified_views if x.db in environments_schemas}
    # Make sure that all models are present
    assert len(prod_views) == 13
    apply_to_environment(context, "dev")
    # Make sure no new schemas are created
    assert set(metadata.schemas) - starting_schemas == {"raw"}
    dev_views = {
        x for x in metadata.qualified_views if x.db in environments_schemas and "__dev" in x.name
    }
    # Make sure that there is a view with `__dev` for each view that exists in prod
    assert len(dev_views) == len(prod_views)
    assert {x.name.replace("__dev", "") for x in dev_views} - {x.name for x in prod_views} == set()
    context.invalidate_environment("dev")
    context._run_janitor()
    views_after_janitor = metadata.qualified_views
    # Make sure that the number of views after the janitor is the same as when you subtract away dev views
    assert len(views_after_janitor) == len(
        {x.sql(dialect="duckdb") for x in views_after_janitor}
        - {x.sql(dialect="duckdb") for x in dev_views}
    )
    # Double check there are no dev views
    assert len({x for x in views_after_janitor if "__dev" in x.name}) == 0
    # Make sure prod views were not removed
    assert {x.sql(dialect="duckdb") for x in prod_views} - {
        x.sql(dialect="duckdb") for x in views_after_janitor
    } == set()


def test_environment_catalog_mapping(init_and_plan_context: t.Callable):
    environments_schemas = {"raw", "sushi"}

    def get_prod_dev_views(metadata: DuckDBMetadata) -> t.Tuple[t.Set[exp.Table], t.Set[exp.Table]]:
        views = metadata.qualified_views
        prod_views = {
            x for x in views if x.catalog == "prod_catalog" if x.db in environments_schemas
        }
        dev_views = {x for x in views if x.catalog == "dev_catalog" if x.db in environments_schemas}
        return prod_views, dev_views

    def get_default_catalog_and_non_tables(
        metadata: DuckDBMetadata, default_catalog: t.Optional[str]
    ) -> t.Tuple[t.Set[exp.Table], t.Set[exp.Table]]:
        tables = metadata.qualified_tables
        user_default_tables = {
            x for x in tables if x.catalog == default_catalog and x.db != "sqlmesh"
        }
        non_default_tables = {x for x in tables if x.catalog != default_catalog}
        return user_default_tables, non_default_tables

    context, plan = init_and_plan_context(
        "examples/sushi", config="environment_catalog_mapping_config"
    )
    context.apply(plan)
    metadata = DuckDBMetadata(context.engine_adapter)
    state_metadata = DuckDBMetadata.from_context(context.state_sync.state_sync)
    prod_views, dev_views = get_prod_dev_views(metadata)
    (
        user_default_tables,
        non_default_tables,
    ) = get_default_catalog_and_non_tables(metadata, context.default_catalog)
    assert len(prod_views) == 13
    assert len(dev_views) == 0
    assert len(user_default_tables) == 13
    assert state_metadata.schemas == ["sqlmesh"]
    assert {x.sql() for x in state_metadata.qualified_tables}.issuperset(
        {
            "physical.sqlmesh._environments",
            "physical.sqlmesh._intervals",
            "physical.sqlmesh._plan_dags",
            "physical.sqlmesh._snapshots",
            "physical.sqlmesh._versions",
        }
    )
    apply_to_environment(context, "dev")
    prod_views, dev_views = get_prod_dev_views(metadata)
    (
        user_default_tables,
        non_default_tables,
    ) = get_default_catalog_and_non_tables(metadata, context.default_catalog)
    assert len(prod_views) == 13
    assert len(dev_views) == 13
    assert len(user_default_tables) == 13
    assert len(non_default_tables) == 0
    assert state_metadata.schemas == ["sqlmesh"]
    assert {x.sql() for x in state_metadata.qualified_tables}.issuperset(
        {
            "physical.sqlmesh._environments",
            "physical.sqlmesh._intervals",
            "physical.sqlmesh._plan_dags",
            "physical.sqlmesh._snapshots",
            "physical.sqlmesh._versions",
        }
    )
    apply_to_environment(context, "prodnot")
    prod_views, dev_views = get_prod_dev_views(metadata)
    (
        user_default_tables,
        non_default_tables,
    ) = get_default_catalog_and_non_tables(metadata, context.default_catalog)
    assert len(prod_views) == 13
    assert len(dev_views) == 26
    assert len(user_default_tables) == 13
    assert len(non_default_tables) == 0
    assert state_metadata.schemas == ["sqlmesh"]
    assert {x.sql() for x in state_metadata.qualified_tables}.issuperset(
        {
            "physical.sqlmesh._environments",
            "physical.sqlmesh._intervals",
            "physical.sqlmesh._plan_dags",
            "physical.sqlmesh._snapshots",
            "physical.sqlmesh._versions",
        }
    )
    context.invalidate_environment("dev")
    context._run_janitor()
    prod_views, dev_views = get_prod_dev_views(metadata)
    (
        user_default_tables,
        non_default_tables,
    ) = get_default_catalog_and_non_tables(metadata, context.default_catalog)
    assert len(prod_views) == 13
    assert len(dev_views) == 13
    assert len(user_default_tables) == 13
    assert len(non_default_tables) == 0
    assert state_metadata.schemas == ["sqlmesh"]
    assert {x.sql() for x in state_metadata.qualified_tables}.issuperset(
        {
            "physical.sqlmesh._environments",
            "physical.sqlmesh._intervals",
            "physical.sqlmesh._plan_dags",
            "physical.sqlmesh._snapshots",
            "physical.sqlmesh._versions",
        }
    )


@pytest.mark.parametrize(
    "context_fixture",
    ["sushi_context", "sushi_no_default_catalog"],
)
def test_unaligned_start_snapshots(context_fixture: Context, request):
    context = request.getfixturevalue(context_fixture)
    environment = "dev"
    apply_to_environment(context, environment)
    # Make breaking change to model upstream of a depends_on_self model
    context.upsert_model("sushi.order_items", stamp="1")
    # Apply the change starting at a date later then the beginning of the downstream depends_on_self model
    plan = apply_to_environment(
        context,
        environment,
        choice=SnapshotChangeCategory.BREAKING,
        plan_start="2 days ago",
        enable_preview=True,
    )
    revenue_lifetime_snapshot = context.get_snapshot(
        "sushi.customer_revenue_lifetime", raise_if_missing=True
    )
    # Validate that the depends_on_self model is non-deployable
    assert not plan.deployability_index.is_deployable(revenue_lifetime_snapshot)


class OldPythonModel(PythonModel):
    kind: ModelKind = ViewKind()


def test_python_model_default_kind_change(init_and_plan_context: t.Callable):
    """
    Around 2024-07-17 Python models had their default Kind changed from VIEW to FULL in order to
    avoid some edge cases where the views might not get updated in certain situations.

    This test ensures that if a user had a Python `kind: VIEW` model stored in state,
    it can still be loaded without error and just show as a breaking change from `kind: VIEW`
    to `kind: FULL`
    """

    # note: we deliberately dont specify a Kind here to allow the defaults to be picked up
    python_model_file = """import typing as t
import pandas as pd
from sqlmesh import ExecutionContext, model

@model(
    "sushi.python_view_model",
    columns={
        "id": "int",
    }
)
def execute(
    context: ExecutionContext,
    **kwargs: t.Any,
) -> pd.DataFrame:
    return pd.DataFrame([
        {"id": 1}
    ])
"""

    context: Context
    context, _ = init_and_plan_context("examples/sushi")

    with open(context.path / "models" / "python_view_model.py", mode="w", encoding="utf8") as f:
        f.write(python_model_file)

    # monkey-patch PythonModel to default to kind: View again
    # and ViewKind to allow python models again
    with mock.patch.object(ViewKind, "supports_python_models", return_value=True), mock.patch(
        "sqlmesh.core.model.definition.PythonModel", OldPythonModel
    ):
        context.load()

    # check the monkey-patching worked
    model = context.get_model("sushi.python_view_model")
    assert model.kind.name == ModelKindName.VIEW
    assert model.source_type == "python"

    # apply plan
    plan: Plan = context.plan(auto_apply=True)

    # check that run() still works even though we have a Python model with kind: View in the state
    snapshot_ids = [s for s in plan.directly_modified if "python_view_model" in s.name]
    snapshot_from_state = list(context.state_sync.get_snapshots(snapshot_ids).values())[0]
    assert snapshot_from_state.model.kind.name == ModelKindName.VIEW
    assert snapshot_from_state.model.source_type == "python"
    context.run()

    # reload context to load model with new defaults
    # this also shows the earlier monkey-patching is no longer in effect
    context.load()
    model = context.get_model("sushi.python_view_model")
    assert model.kind.name == ModelKindName.FULL
    assert model.source_type == "python"

    plan = context.plan(
        categorizer_config=CategorizerConfig.all_full()
    )  # the default categorizer_config doesnt auto-categorize python models

    assert plan.has_changes
    assert not plan.indirectly_modified

    assert len(plan.directly_modified) == 1
    snapshot_id = list(plan.directly_modified)[0]
    assert snapshot_id.name == '"memory"."sushi"."python_view_model"'
    assert plan.modified_snapshots[snapshot_id].change_category == SnapshotChangeCategory.BREAKING

    context.apply(plan)

    df = context.engine_adapter.fetchdf("SELECT id FROM sushi.python_view_model")
    assert df["id"].to_list() == [1]


def initial_add(context: Context, environment: str):
    assert not context.state_reader.get_environment(environment)

    plan = context.plan(environment, start=start(context), create_from="nonexistent_env")
    validate_plan_changes(plan, added={x.snapshot_id for x in context.snapshots.values()})

    context.apply(plan)
    validate_apply_basics(context, environment, plan.snapshots.values())


def apply_to_environment(
    context: Context,
    environment: str,
    choice: t.Optional[SnapshotChangeCategory] = None,
    plan_validators: t.Optional[t.Iterable[t.Callable]] = None,
    apply_validators: t.Optional[t.Iterable[t.Callable]] = None,
    plan_start: t.Optional[TimeLike] = None,
    allow_destructive_models: t.Optional[t.List[str]] = None,
    enable_preview: bool = False,
):
    plan_validators = plan_validators or []
    apply_validators = apply_validators or []

    plan_builder = context.plan_builder(
        environment,
        start=plan_start or start(context) if environment != c.PROD else None,
        forward_only=choice == SnapshotChangeCategory.FORWARD_ONLY,
        include_unmodified=True,
        allow_destructive_models=allow_destructive_models if allow_destructive_models else [],
        enable_preview=enable_preview,
    )
    if environment != c.PROD:
        plan_builder.set_start(plan_start or start(context))

    if choice:
        plan_choice(plan_builder, choice)
    for validator in plan_validators:
        validator(context, plan_builder.build())

    plan = plan_builder.build()
    context.apply(plan)

    validate_apply_basics(context, environment, plan.snapshots.values(), plan.deployability_index)
    for validator in apply_validators:
        validator(context)
    return plan


def change_data_type(
    context: Context, model_name: str, old_type: DataType.Type, new_type: DataType.Type
) -> None:
    model = context.get_model(model_name)
    assert model is not None

    if isinstance(model, SqlModel):
        data_types = model.query.find_all(DataType)
        for data_type in data_types:
            if data_type.this == old_type:
                data_type.set("this", new_type)
        context.upsert_model(model_name, query=model.query)
    elif model.columns_to_types_ is not None:
        for k, v in model.columns_to_types_.items():
            if v.this == old_type:
                model.columns_to_types_[k] = DataType.build(new_type)
        context.upsert_model(model_name, columns=model.columns_to_types_)


def validate_plan_changes(
    plan: Plan,
    *,
    added: t.Optional[t.Iterable[SnapshotId]] = None,
    modified: t.Optional[t.Iterable[str]] = None,
    removed: t.Optional[t.Iterable[SnapshotId]] = None,
) -> None:
    added = added or []
    modified = modified or []
    removed = removed or []
    assert set(added) == plan.context_diff.added
    assert set(modified) == set(plan.context_diff.modified_snapshots)
    assert set(removed) == set(plan.context_diff.removed_snapshots)


def validate_versions_same(
    model_names: t.List[str],
    versions: t.Dict[str, str],
    other_versions: t.Dict[str, str],
) -> None:
    for name in model_names:
        assert versions[name] == other_versions[name]


def validate_versions_different(
    model_names: t.List[str],
    versions: t.Dict[str, str],
    other_versions: t.Dict[str, str],
) -> None:
    for name in model_names:
        assert versions[name] != other_versions[name]


def validate_apply_basics(
    context: Context,
    environment: str,
    snapshots: t.Iterable[Snapshot],
    deployability_index: t.Optional[DeployabilityIndex] = None,
) -> None:
    validate_snapshots_in_state_sync(snapshots, context)
    validate_state_sync_environment(snapshots, environment, context)
    validate_tables(snapshots, context, deployability_index)
    validate_environment_views(snapshots, environment, context, deployability_index)


def validate_snapshots_in_state_sync(snapshots: t.Iterable[Snapshot], context: Context) -> None:
    snapshot_infos = map(to_snapshot_info, snapshots)
    state_sync_table_infos = map(
        to_snapshot_info, context.state_reader.get_snapshots(snapshots).values()
    )
    assert set(snapshot_infos) == set(state_sync_table_infos)


def validate_state_sync_environment(
    snapshots: t.Iterable[Snapshot], env: str, context: Context
) -> None:
    environment = context.state_reader.get_environment(env)
    assert environment
    snapshot_infos = map(to_snapshot_info, snapshots)
    environment_table_infos = map(to_snapshot_info, environment.snapshots)
    assert set(snapshot_infos) == set(environment_table_infos)


def validate_tables(
    snapshots: t.Iterable[Snapshot],
    context: Context,
    deployability_index: t.Optional[DeployabilityIndex] = None,
) -> None:
    adapter = context.engine_adapter
    deployability_index = deployability_index or DeployabilityIndex.all_deployable()
    for snapshot in snapshots:
        is_deployable = deployability_index.is_representative(snapshot)
        if not snapshot.is_model or snapshot.is_external:
            continue
        table_should_exist = not snapshot.is_embedded
        assert adapter.table_exists(snapshot.table_name(is_deployable)) == table_should_exist
        if table_should_exist:
            assert select_all(snapshot.table_name(is_deployable), adapter)


def validate_environment_views(
    snapshots: t.Iterable[Snapshot],
    environment: str,
    context: Context,
    deployability_index: t.Optional[DeployabilityIndex] = None,
) -> None:
    adapter = context.engine_adapter
    deployability_index = deployability_index or DeployabilityIndex.all_deployable()
    for snapshot in snapshots:
        is_deployable = deployability_index.is_representative(snapshot)
        if not snapshot.is_model or snapshot.is_symbolic:
            continue
        view_name = snapshot.qualified_view_name.for_environment(
            EnvironmentNamingInfo.from_environment_catalog_mapping(
                context.config.environment_catalog_mapping,
                name=environment,
                suffix_target=context.config.environment_suffix_target,
            )
        )

        assert adapter.table_exists(view_name)
        assert select_all(snapshot.table_name(is_deployable), adapter) == select_all(
            view_name, adapter
        )


def select_all(table: str, adapter: EngineAdapter) -> t.Iterable:
    return adapter.fetchall(f"select * from {table} order by 1")


def snapshots_to_versions(snapshots: t.Iterable[Snapshot]) -> t.Dict[str, str]:
    return {snapshot.name: snapshot.version or "" for snapshot in snapshots}


def to_snapshot_info(snapshot: SnapshotInfoLike) -> SnapshotTableInfo:
    return snapshot.table_info


def start(context: Context) -> TimeLike:
    env = context.state_sync.get_environment("prod")
    assert env
    return env.start_at


def add_projection_to_model(model: SqlModel, literal: bool = True) -> SqlModel:
    one_expr = exp.Literal.number(1).as_("one") if literal else exp.column("one")
    kwargs = {
        **model.dict(),
        "query": model.query.select(one_expr),  # type: ignore
    }
    return SqlModel.parse_obj(kwargs)
