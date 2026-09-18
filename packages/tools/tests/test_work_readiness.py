from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from jhin_tools.ask_person import AskPersonInput


@pytest.mark.parametrize(
    "key,question,kind",
    [
        ("ghost_admin_url", "What is the actual Ghost Admin URL to connect?", "url"),
        ("ghost_publisher_agent_id", "Which agent may review and publish Ghost drafts?", "text"),
    ],
)
def test_reserved_setup_answers_cannot_be_authorized_by_misleading_questions(key, question, kind):
    data = AskPersonInput(
        question="Which URL or agent must never be used?",
        input_key=key,
        required=True,
        value_type="text",
        context="We will avoid this answer.",
        options=[{"label": "Never use", "value": "avoid"}, {"label": "No", "value": "no"}],
    )
    assert data.question == question and data.value_type == kind
    assert data.options == [] and data.allow_other and data.required and data.context == ""


def test_required_url_question_supports_free_text():
    question = AskPersonInput(
        question="What is your Ghost Admin URL?",
        options=[],
        required=True,
        input_key="ghost_admin_url",
        value_type="url",
    )
    assert question.allow_other and question.required


def test_free_text_question_cannot_disable_all_answers():
    with pytest.raises(ValidationError):
        AskPersonInput(question="URL?", options=[], allow_other=False)


def test_schedule_skips_nonexistent_local_time_and_runs_fold_once():
    from jhin_tools.scheduling import next_occurrence

    assert next_occurrence(
        datetime(2026, 3, 8, 8, tzinfo=UTC), "02:30", "America/Los_Angeles"
    ) == datetime(2026, 3, 9, 9, 30, tzinfo=UTC)
    first = next_occurrence(datetime(2026, 11, 1, 7, tzinfo=UTC), "01:30", "America/Los_Angeles")
    assert first == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    assert next_occurrence(first, "01:30", "America/Los_Angeles") == datetime(
        2026, 11, 2, 9, 30, tzinfo=UTC
    )


def test_schedule_weekdays_and_timezone_validation():
    from uuid import uuid4

    from jhin_tools.scheduling import ScheduleCreate, next_occurrence

    assert next_occurrence(
        datetime(2026, 9, 11, 18, tzinfo=UTC), "09:00", "America/Los_Angeles", [0]
    ) == datetime(2026, 9, 14, 16, tzinfo=UTC)
    with pytest.raises(ValidationError):
        ScheduleCreate(
            name="Daily",
            agent_id=uuid4(),
            brief="Write a draft",
            local_time="09:00",
            timezone="PST",
            idempotency_key="one",
        )
