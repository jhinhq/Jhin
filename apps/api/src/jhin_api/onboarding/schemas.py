"""Request/response contracts for first-run onboarding state."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

#: How far this person got through the guided introduction.
#:
#: ``pending``     never shown — the only state that opens it unprompted;
#: ``in_progress`` opened, then left to go and do one of the steps;
#: ``dismissed``   skipped on purpose;
#: ``completed``   walked to the end.
#:
#: Only ``pending`` triggers the tour by itself. Everything else is reachable
#: on demand, so the difference is a matter of how insistent the workspace is,
#: not of whether the tour is still available.
OnboardingStatus = Literal["pending", "in_progress", "dismissed", "completed"]

MAX_STEP_ID_LENGTH = 40


class OnboardingStateOut(BaseModel):
    status: OnboardingStatus
    #: The step the client should resume on. Free-form on purpose: the step
    #: list is a property of the web client's copy, and the API has no business
    #: failing a request because someone reordered a tour.
    last_step: str | None = None
    updated_at: datetime | None = None


class OnboardingStateIn(BaseModel):
    status: OnboardingStatus
    last_step: str | None = Field(default=None, max_length=MAX_STEP_ID_LENGTH)
