"""Filter DSL: validation, every op, groups, and transition matching."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from jhin_triggers import FilterError, evaluate_filter, validate_filter
from jhin_triggers.filters import MISSING, changed_from_paths, resolve_path


def issue_event(
    *,
    state: str = "Todo",
    team: str = "ENG",
    changed_from: dict[str, Any] | None = None,
    include_changed_from: bool = True,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "external_id": "ENG-142",
        "title": "Fix the failing test",
        "priority": 2,
        "labels": ["bug", "backend"],
        "team": {"id": "team-eng", "key": team, "name": "Engineering"},
        "state": {"id": f"state-{state.lower()}", "name": state, "type": "unstarted"},
    }
    if include_changed_from:
        data["changed_from"] = changed_from if changed_from is not None else {}
    return {"event_type": "connector.linear.issue.updated", "data": data}


BACKLOG_TO_TODO = {"state": {"id": "state-backlog"}}


class TestValidation:
    def test_empty_filter_is_valid(self) -> None:
        validate_filter({})

    def test_valid_nested_groups(self) -> None:
        validate_filter(
            {
                "all": [
                    {"path": "data.team.key", "op": "eq", "value": "ENG"},
                    {"any": [{"path": "data.priority", "op": "gte", "value": 1}]},
                ]
            }
        )

    @pytest.mark.parametrize(
        "document",
        [
            "not an object",
            {"all": [{"path": "a", "op": "regex", "value": ".*"}]},
            {"all": [{"path": "", "op": "eq", "value": 1}]},
            {"all": [{"path": "a", "op": "eq"}]},
            {"all": [{"path": "a", "op": "in", "value": "x"}]},
            {"all": [{"path": "a", "op": "exists", "value": "yes"}]},
            {"all": [{"path": "a", "op": "gt", "value": [1]}]},
            {"all": [{"path": "a", "op": "eq", "value": 1, "extra": True}]},
            {"all": [1], "any": [2]},
            {"nor": []},
            {"all": "not a list"},
        ],
    )
    def test_rejects_malformed(self, document: Any) -> None:
        with pytest.raises(FilterError):
            validate_filter(document)

    def test_rejects_excessive_nesting(self) -> None:
        node: dict[str, Any] = {"path": "a", "op": "exists"}
        for _ in range(12):
            node = {"all": [node]}
        with pytest.raises(FilterError, match="depth"):
            validate_filter(node)

    def test_rejects_too_many_conditions(self) -> None:
        conditions = [{"path": f"a{i}", "op": "exists"} for i in range(51)]
        with pytest.raises(FilterError, match="maximum is 50"):
            validate_filter({"all": conditions})


class TestPathResolution:
    def test_nested_and_list_index(self) -> None:
        event = issue_event()
        assert resolve_path(event, "data.state.name") == "Todo"
        assert resolve_path(event, "data.labels.1") == "backend"

    def test_missing_paths(self) -> None:
        event = issue_event()
        assert resolve_path(event, "data.nope") is MISSING
        assert resolve_path(event, "data.labels.9") is MISSING
        assert resolve_path(event, "data.labels.x") is MISSING
        assert resolve_path(event, "data.title.deeper") is MISSING

    def test_changed_from_paths(self) -> None:
        assert changed_from_paths("data.state.name") == (
            "data.changed_from.state",
            "data.changed_from.state.name",
        )
        assert changed_from_paths("data.title") == (
            "data.changed_from.title",
            "data.changed_from.title",
        )
        assert changed_from_paths("event_type") is None
        assert changed_from_paths("data.changed_from.state.name") is None


class TestOps:
    @pytest.mark.parametrize(
        ("condition", "expected"),
        [
            ({"path": "data.state.name", "op": "eq", "value": "Todo"}, True),
            ({"path": "data.state.name", "op": "eq", "value": "Done"}, False),
            ({"path": "data.missing", "op": "eq", "value": None}, False),
            ({"path": "data.state.name", "op": "neq", "value": "Done"}, True),
            ({"path": "data.missing", "op": "neq", "value": "x"}, True),
            ({"path": "data.team.key", "op": "in", "value": ["ENG", "OPS"]}, True),
            ({"path": "data.team.key", "op": "in", "value": ["OPS"]}, False),
            ({"path": "data.team.key", "op": "not_in", "value": ["OPS"]}, True),
            ({"path": "data.missing", "op": "not_in", "value": ["OPS"]}, True),
            ({"path": "data.title", "op": "contains", "value": "failing"}, True),
            ({"path": "data.labels", "op": "contains", "value": "bug"}, True),
            ({"path": "data.labels", "op": "contains", "value": "ui"}, False),
            ({"path": "data.priority", "op": "contains", "value": 2}, False),
            ({"path": "data.state.name", "op": "exists"}, True),
            ({"path": "data.state.name", "op": "exists", "value": True}, True),
            ({"path": "data.missing", "op": "exists", "value": False}, True),
            ({"path": "data.missing", "op": "exists"}, False),
            ({"path": "data.priority", "op": "gt", "value": 1}, True),
            ({"path": "data.priority", "op": "gte", "value": 2}, True),
            ({"path": "data.priority", "op": "lt", "value": 2}, False),
            ({"path": "data.priority", "op": "lte", "value": 2}, True),
            ({"path": "data.priority", "op": "gt", "value": "1"}, False),
            ({"path": "data.state.name", "op": "gt", "value": "Sodo"}, True),
        ],
    )
    def test_single_condition(self, condition: dict[str, Any], expected: bool) -> None:
        result = evaluate_filter({"all": [condition]}, issue_event())
        assert result.matched is expected
        assert result.conditions[0].passed is expected

    def test_booleans_are_not_numbers(self) -> None:
        event = {"data": {"flag": True}}
        assert (
            evaluate_filter({"all": [{"path": "data.flag", "op": "gt", "value": 0}]}, event).matched
            is False
        )


class TestGroups:
    def test_empty_filter_matches_everything(self) -> None:
        assert evaluate_filter({}, issue_event()).matched is True
        assert evaluate_filter({"all": []}, issue_event()).matched is True

    def test_empty_any_matches_nothing(self) -> None:
        assert evaluate_filter({"any": []}, issue_event()).matched is False

    def test_all_requires_every_child(self) -> None:
        document = {
            "all": [
                {"path": "data.team.key", "op": "eq", "value": "ENG"},
                {"path": "data.state.name", "op": "eq", "value": "Done"},
            ]
        }
        result = evaluate_filter(document, issue_event())
        assert result.matched is False
        # No short-circuit: both conditions are explained.
        assert [c.passed for c in result.conditions] == [True, False]

    def test_any_needs_one_child(self) -> None:
        document = {
            "any": [
                {"path": "data.team.key", "op": "eq", "value": "OPS"},
                {"path": "data.state.name", "op": "eq", "value": "Todo"},
            ]
        }
        assert evaluate_filter(document, issue_event()).matched is True

    def test_nested_groups(self) -> None:
        document = {
            "all": [
                {"path": "data.team.key", "op": "eq", "value": "ENG"},
                {
                    "any": [
                        {"path": "data.priority", "op": "gte", "value": 3},
                        {"path": "data.labels", "op": "contains", "value": "bug"},
                    ]
                },
            ]
        }
        assert evaluate_filter(document, issue_event()).matched is True

    def test_evaluation_never_raises_on_garbage(self) -> None:
        assert evaluate_filter({"all": [{"op": "??"}]}, issue_event()).matched is False
        assert evaluate_filter({"weird": True}, issue_event()).matched is False
        assert evaluate_filter(None, issue_event()).matched is False


class TestTransitionedTo:
    CONDITION: ClassVar[dict[str, Any]] = {
        "path": "data.state.name",
        "op": "transitioned_to",
        "value": "Todo",
    }

    def test_backlog_to_todo_matches(self) -> None:
        event = issue_event(state="Todo", changed_from=BACKLOG_TO_TODO)
        result = evaluate_filter({"all": [self.CONDITION]}, event)
        assert result.matched is True
        assert result.conditions[0].detail == "changed to 'Todo'"

    def test_no_state_change_does_not_match(self) -> None:
        # Title edit: state stays Todo, changed_from carries only the title.
        event = issue_event(state="Todo", changed_from={"title": "Old title"})
        result = evaluate_filter({"all": [self.CONDITION]}, event)
        assert result.matched is False
        assert result.conditions[0].detail == "field did not change in this event"

    def test_transition_away_does_not_match(self) -> None:
        event = issue_event(state="Done", changed_from={"state": {"id": "state-todo"}})
        assert evaluate_filter({"all": [self.CONDITION]}, event).matched is False

    def test_created_event_without_changed_from_does_not_match(self) -> None:
        event = issue_event(state="Todo", include_changed_from=False)
        assert evaluate_filter({"all": [self.CONDITION]}, event).matched is False

    def test_previous_name_equal_target_does_not_match(self) -> None:
        # Full-mirror connector reporting Todo -> Todo (no real transition).
        event = issue_event(state="Todo", changed_from={"state": {"name": "Todo"}})
        result = evaluate_filter({"all": [self.CONDITION]}, event)
        assert result.matched is False
        assert result.conditions[0].detail == "previous value was already 'Todo'"

    def test_full_mirror_previous_name_differs_matches(self) -> None:
        event = issue_event(
            state="Todo", changed_from={"state": {"id": "state-backlog", "name": "Backlog"}}
        )
        result = evaluate_filter({"all": [self.CONDITION]}, event)
        assert result.matched is True
        assert result.conditions[0].previous == "Backlog"

    def test_requires_data_rooted_path(self) -> None:
        document = {"all": [{"path": "event_type", "op": "transitioned_to", "value": "x"}]}
        result = evaluate_filter(document, issue_event())
        assert result.matched is False
        assert "requires a path under 'data.'" in result.conditions[0].detail

    def test_explanation_shape_serializes(self) -> None:
        event = issue_event(state="Todo", changed_from=BACKLOG_TO_TODO)
        result = evaluate_filter({"all": [self.CONDITION]}, event)
        explained = result.conditions[0].as_dict()
        assert explained["passed"] is True
        assert explained["actual"] == "Todo"
        # Linear mirrors only the previous state id; the changed branch is
        # surfaced as the previous-value evidence.
        assert explained["previous"] == {"id": "state-backlog"}
        assert explained["previous_present"] is True
