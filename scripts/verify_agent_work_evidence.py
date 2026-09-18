"""Read-only checks of retained Ghost editorial and secure-intake evidence.

Set JHIN_LIVE_API_KEY in the process environment. This script only calls Jhin
GET endpoints; it never contacts Ghost, changes grants, or prints credentials,
message bodies, draft HTML, or tool arguments. Use a completed editorial sample.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import httpx

SAFE_SETUP = frozenset(
    {
        "organization.ask_person",
        "organization.report_result",
        "organization.respond_work_request",
        "organization.directory.search",
        "organization.colleague_status",
        "memory.search",
        "variables.list",
        "variables.get",
        "variables.set",
        "variables.delete",
        "variables.copy",
        "schedules.list",
        "schedules.history",
    }
)


class EvidenceError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def moment(value: Any) -> datetime:
    require(isinstance(value, str), "Evidence is missing an execution timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def calls(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = [item["data"] for item in items if item["id"].startswith("tool_call:")]
    return sorted(
        result, key=lambda row: (moment(row.get("started_at") or row["created_at"]), row["id"])
    )


def output(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("sanitized_output_json")
    require(isinstance(value, dict), "A tool call has no retained output")
    return cast(dict[str, Any], value)


def inputs(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("sanitized_input_json")
    require(isinstance(value, dict), "A tool call has no retained input")
    return cast(dict[str, Any], value)


def verify_editorial(
    detail: dict[str, Any],
    items: list[dict[str, Any]],
    review: dict[str, Any] | list[dict[str, Any]],
    *,
    writer: UUID | None = None,
    director: UUID | None = None,
) -> dict[str, Any]:
    reviews = review if isinstance(review, list) else [review]
    handoffs = [
        row
        for row in calls(items)
        if row.get("status") == "completed" and row["tool_name"] == "ghost.review.request"
    ]
    require(
        bool(reviews) and len(handoffs) == len(reviews),
        "Every completed review handoff must have retained review evidence",
    )
    by_id = {row["review_id"]: row for row in reviews}
    require(len(by_id) == len(reviews), "Duplicate review evidence")
    ordered = [by_id.get(output(row)["review_id"]) for row in handoffs]
    require(all(row is not None for row in ordered), "Missing review round evidence")
    previous = None
    report: dict[str, Any] = {}
    for current in ordered:
        assert current is not None
        if previous is not None:
            require(
                previous["status"] == "changes_requested", "A revision followed a final verdict"
            )
            require(
                current.get("prior_review_id") == previous["review_id"],
                "Revision round does not link its preceding review",
            )
            require(
                all(
                    current[key] == previous[key]
                    for key in ("post_id", "connection_id", "author_agent_id", "publisher_agent_id")
                ),
                "Review rounds changed article or identities",
            )
            require(
                current["revision"] != previous["revision"],
                "Requested changes did not produce a new draft revision",
            )
        report = _verify_editorial_round(detail, items, current, writer=writer, director=director)
        previous = current
    require(
        report.get("outcome") in {"approved_draft", "published"},
        "Editorial sample has no final approved outcome",
    )
    return {**report, "review_rounds": len(reviews)}


def _verify_editorial_round(
    detail: dict[str, Any],
    items: list[dict[str, Any]],
    review: dict[str, Any],
    *,
    writer: UUID | None,
    director: UUID | None,
) -> dict[str, Any]:
    completed = [row for row in calls(items) if row.get("status") == "completed"]
    requests = [
        row
        for row in completed
        if row["tool_name"] == "ghost.review.request"
        and output(row).get("review_id") == review["review_id"]
    ]
    require(
        len(requests) == 1, "Editorial sample must contain exactly one completed review handoff"
    )
    handoff = requests[0]
    result, request = output(handoff), inputs(handoff)
    author, publisher = handoff["agent_id"], result["publisher_agent_id"]
    require(author != publisher, "Writer and publisher must be different agents")
    require(writer is None or author == str(writer), "Unexpected draft writer")
    require(director is None or publisher == str(director), "Unexpected publishing director")
    require(
        review["status"] in {"published", "approved", "changes_requested"},
        "Editorial review has not reached a retained decision",
    )
    require(
        review["author_agent_id"] == author and review["publisher_agent_id"] == publisher,
        "Editorial authorship differs from the tool handoff",
    )
    for key in ("review_id", "post_id", "revision", "work_request_id"):
        require(review[key] == result[key], "Review evidence does not match the requested revision")
    require(
        request["expected_revision"] == review["revision"], "Handoff did not pin the draft revision"
    )
    require(request["connection_id"] == review["connection_id"], "Editorial connection differs")
    before = [row for row in completed if completed.index(row) < completed.index(handoff)]
    draft_writes = [
        row
        for row in before
        if row["tool_name"] in {"ghost.draft.create", "ghost.draft.update"}
        and output(row).get("post_id") == review["post_id"]
    ]
    require(bool(draft_writes), "No completed draft creation or revision precedes review")
    require(
        all(row["agent_id"] == author for row in draft_writes), "Another agent changed the draft"
    )
    require(
        output(draft_writes[-1])["revision"] == review["revision"],
        "Reviewed draft differs from the writer's last revision",
    )
    approvals = [
        row
        for row in completed
        if row["tool_name"] == "ghost.review.decide"
        and inputs(row).get("review_id") == review["review_id"]
    ]
    publications = [
        row
        for row in completed
        if row["tool_name"] == "ghost.post.publish"
        and inputs(row).get("review_id") == review["review_id"]
    ]
    require(len(approvals) == 1, "Expected exactly one director decision per round")
    decision = approvals[0]
    require(
        decision["agent_id"] == publisher,
        "Only the director may approve and publish",
    )
    require(
        inputs(decision)["verdict"]
        == ("changes_requested" if review["status"] == "changes_requested" else "approved")
        and output(decision)["revision"] == review["revision"],
        "Director did not approve this exact revision",
    )
    require(
        completed.index(handoff) < completed.index(decision), "Review execution order is invalid"
    )
    if review["status"] == "published":
        require(len(publications) == 1, "Expected exactly one publication")
        publication = publications[0]
        require(publication["agent_id"] == publisher, "Only the director may publish")
        require(
            inputs(publication)["connection_id"] == review["connection_id"],
            "Publication used another connection",
        )
        require(
            output(publication)["status"] == "published"
            and output(publication)["post_id"] == review["post_id"],
            "Publication result does not confirm this post",
        )
        require(
            completed.index(decision) < completed.index(publication),
            "Review and publication execution order is invalid",
        )
    elif review["status"] == "approved":
        require(
            review.get("release_intent") == "draft_only", "Draft approval lacks draft-only intent"
        )
        require(
            not any(row["tool_name"] == "ghost.post.publish" for row in calls(items)),
            "Draft-only sample attempted to publish",
        )
        reads = [
            row
            for row in completed
            if row["tool_name"] == "ghost.post.read"
            and inputs(row).get("connection_id") == review["connection_id"]
            and output(row).get("post_id") == review["post_id"]
            and completed.index(row) > completed.index(decision)
        ]
        require(
            bool(reads)
            and output(reads[-1]).get("status") == "draft"
            and output(reads[-1]).get("revision") == review["revision"],
            "Approved draft lacks exact-version provider read-back",
        )
    else:
        require(not publications, "A revision request cannot authorize publication")
    require(
        not any(
            row["tool_name"] in {"organization.request_work", "organization.delegate_task"}
            for row in completed
        ),
        "Sample delegated an additional handoff",
    )
    work = [
        item["data"]
        for item in items
        if item["id"].startswith("work_request:")
        and item["data"].get("id") == review["work_request_id"]
    ]
    require(
        len(work) == 1
        and work[0]["id"] == review["work_request_id"]
        and work[0]["target_agent_id"] == publisher
        and work[0]["status"] == "completed",
        "Director's work request is missing, duplicated, or incomplete",
    )
    tasks = {task["id"]: task for task in detail["tasks"]}
    parent, child = tasks.get(handoff["task_id"]), tasks.get(result["created_task_id"])
    if parent is None or child is None:
        raise EvidenceError("Writer or director task is missing")
    require(
        parent is not None
        and child is not None
        and parent["state"] == child["state"] == "completed",
        "Writer and director tasks must both finish",
    )
    messages = [item["data"] for item in items if item["id"].startswith("message:")]
    returned = [
        message
        for message in messages
        if message.get("task_id") == parent["id"]
        and message.get("sender_id") == publisher
        and message.get("message_type") == "result"
        and message.get("content_json", {}).get("work_request_id") == review["work_request_id"]
    ]
    require(len(returned) == 1, "Director's answer was not delivered exactly once to the writer")
    require(
        any(
            (
                message.get("task_id") == parent["id"]
                or tasks.get(message.get("task_id"), {})
                .get("metadata_json", {})
                .get("work_request_result", {})
                .get("work_request_id")
                == review["work_request_id"]
            )
            and message.get("sender_id") == author
            and message.get("message_type") == "text"
            and moment(message["created_at"]) > moment(returned[0]["created_at"])
            for message in messages
        ),
        "Writer did not resume and answer after the director completed",
    )
    return {
        "status": "passed",
        "outcome": "approved_draft" if review["status"] == "approved" else review["status"],
        "post_id": review["post_id"],
        "review_id": review["review_id"],
        "approved_revision": review["revision"],
        "writer": author,
        "director": publisher,
        "parent_completed": True,
        "child_completed": True,
    }


def verify_key_only(items: list[dict[str, Any]]) -> dict[str, Any]:
    questions = [
        item["data"]
        for item in items
        if item["id"].startswith("user_question:")
        and item["data"].get("required") is True
        and item["data"].get("input_key") == "ghost_admin_url"
    ]
    require(bool(questions), "Key-only input did not produce a required Ghost Admin URL question")
    question = min(questions, key=lambda row: moment(row["asked_at"]))
    relevant = [row for row in calls(items) if row.get("task_id") == question["task_id"]]
    attempted_effects = [
        row
        for row in relevant
        if row["tool_name"] not in SAFE_SETUP
        and row["status"] in {"completed", "failed", "running"}
    ]
    answered = question.get("answered_at")
    require(
        not attempted_effects or answered is not None,
        "External work began while the Admin URL was missing",
    )
    for row in attempted_effects:
        require(
            moment(row.get("started_at") or row["created_at"]) >= moment(answered),
            "External work began before the required Admin URL answer",
        )
    return {
        "status": "passed",
        "required_question_id": question["id"],
        "external_calls_before_answer": 0,
        "answered": answered is not None,
    }


class EvidenceClient:
    def __init__(self, client: httpx.Client, workspace: UUID) -> None:
        self.client = client
        self.base = f"/api/v1/workspaces/{workspace}"

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.client.get(path, **kwargs)
        require(
            response.is_success, f"Jhin evidence request failed with HTTP {response.status_code}"
        )
        require(len(response.content) <= 8_388_608, "Evidence response exceeds the size limit")
        result = response.json()
        require(isinstance(result, dict), "Jhin evidence response has an unexpected shape")
        return cast(dict[str, Any], result)

    def snapshot(self, conversation: UUID) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        path = f"{self.base}/conversations/{conversation}"
        detail = self.get(path)
        cursor, items, seen = None, [], set()
        params: dict[str, Any] = {"limit": 100}
        for _ in range(20):
            page = self.get(path + "/items", params=params)
            if cursor is None:
                cursor = page["cursor"]
            require(
                page["cursor"] == cursor,
                "Conversation changed while reading evidence; retry after it settles",
            )
            for item in page["items"]:
                require(item["id"] not in seen, "Duplicate conversation item across pages")
                seen.add(item["id"])
                items.append(item)
            if not page["has_more"]:
                return detail, items
            params["before"] = page["next_before"]
        raise EvidenceError("Sample exceeds 2,000 retained items; use a bounded conversation")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--workspace", type=UUID, required=True)
    parser.add_argument("--conversation", type=UUID, help="Completed editorial sample")
    parser.add_argument("--key-only-conversation", type=UUID)
    parser.add_argument("--writer", type=UUID)
    parser.add_argument("--director", type=UUID)
    args = parser.parse_args()
    if not args.conversation and not args.key_only_conversation:
        parser.error("Provide --conversation or --key-only-conversation")
    key = os.environ.get("JHIN_LIVE_API_KEY")
    if not key:
        parser.error("Set JHIN_LIVE_API_KEY in the environment")
    try:
        with httpx.Client(
            base_url=args.url,
            timeout=30,
            trust_env=False,
            follow_redirects=False,
            headers={"Authorization": "Bearer " + key},
        ) as client:
            api = EvidenceClient(client, args.workspace)
            report = {}
            if args.conversation:
                detail, items = api.snapshot(args.conversation)
                handoffs = [
                    row
                    for row in calls(items)
                    if row["tool_name"] == "ghost.review.request" and row["status"] == "completed"
                ]
                require(bool(handoffs), "Expected a completed editorial handoff")
                reviews = []
                for handoff in handoffs:
                    connection = UUID(inputs(handoff)["connection_id"])
                    review_id = UUID(output(handoff)["review_id"])
                    reviews.append(
                        api.get(
                            f"{api.base}/connections/{connection}/editorial-reviews/{review_id}"
                        )
                    )
                report["editorial"] = verify_editorial(
                    detail, items, reviews, writer=args.writer, director=args.director
                )
            if args.key_only_conversation:
                _detail, items = api.snapshot(args.key_only_conversation)
                report["key_only"] = verify_key_only(items)
        print(json.dumps(report, indent=2))
    except EvidenceError as error:
        parser.exit(1, f"Evidence check failed: {error}\n")
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        parser.exit(1, "Evidence unavailable or incomplete; retry after the sample finishes.\n")


if __name__ == "__main__":
    main()
