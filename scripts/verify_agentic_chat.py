"""Live API acceptance against a dedicated, idle sample conversation.

Set JHIN_LIVE_API_KEY in the process environment; credentials are never written.
Use --phase review only with the sample sales.csv/results.csv document workflow.
It edits and restores results.csv and retains an inspectable branch/checkpoint.
The journal and recovery phases are read-only. Browser PTY/preview connections
must be verified separately with a normal owner/admin session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx


class Acceptance:
    def __init__(self, client: httpx.Client, workspace: UUID, conversation: UUID) -> None:
        self.client = client
        self.base = f"/api/v1/workspaces/{workspace}"
        self.chat = f"{self.base}/conversations/{conversation}"

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        if not response.is_success:
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code}")
        return response.json()

    def idle(self) -> dict[str, Any]:
        detail = self.request("GET", self.chat)
        chat = detail.get("conversation", detail)
        assert chat["active_task_id"] is None, "Wait for the sample conversation to become idle"
        return cast(dict[str, Any], chat)

    def files(self) -> list[dict[str, Any]]:
        page = self.request("GET", self.chat + "/files")
        assert not page["has_more"], "Use a bounded sample conversation with at most 100 files"
        return cast(list[dict[str, Any]], page["items"])

    def contents(self, file: dict[str, Any]) -> bytes:
        response = self.client.get(
            f"{self.base}/files/{file['id']}/download",
            params={"revision_id": file["current_revision_id"]},
        )
        response.raise_for_status()
        assert hashlib.sha256(response.content).hexdigest() == file["sha256"]
        return response.content

    def snapshot(self) -> dict[str, Any]:
        self.idle()
        items: dict[str, Any] = {}
        params: dict[str, Any] = {"limit": 100}
        cursor = None
        for _ in range(20):
            page = self.request("GET", self.chat + "/items", params=params)
            if cursor is None:
                cursor = page["cursor"]
            assert page["cursor"] == cursor, "Sample conversation changed during pagination"
            for item in page["items"]:
                assert item["id"] not in items, "Duplicate item across history pages"
                items[item["id"]] = item
            if not page["has_more"]:
                break
            params["before"] = page["next_before"]
        else:
            raise AssertionError("Sample history exceeds the bounded acceptance limit")
        files = self.files()
        for file in files:
            self.contents(file)
        return {"conversation": self.chat, "cursor": cursor, "items": items, "files": files}

    def events(self, after: int, through: int) -> list[dict[str, Any]]:
        if after == through:
            return []
        frames: list[dict[str, Any]] = []
        with self.client.stream(
            "GET", self.chat + "/events", headers={"Last-Event-ID": str(after)}
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                assert line != "event: snapshot_required", "Replay unexpectedly lost its cursor"
                if not line.startswith("data: "):
                    continue
                item = json.loads(line[6:])
                assert item["sequence"] == after + len(frames) + 1, "Missing/duplicate event"
                frames.append(item)
                if item["sequence"] >= through:
                    break
                assert len(frames) < 20_000, "Sample journal exceeds acceptance limit"
        assert frames and frames[-1]["sequence"] == through
        return frames

    def journal(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        cursor = snapshot["cursor"]
        midpoint = cursor // 2
        first = self.events(0, midpoint)
        # Closing the first stream deliberately simulates a dropped connection.
        resumed = self.events(midpoint, cursor)
        latest = {item["id"]: item for item in first + resumed}
        assert latest == snapshot["items"], "Replay differs from the authoritative snapshot"
        selected = list(latest)[:5]
        recovered = self.request(
            "GET", self.chat + "/items", params=[("item_id", item_id) for item_id in selected]
        )
        assert recovered["cursor"] == cursor
        assert {item["id"]: item for item in recovered["items"]} == {
            item_id: latest[item_id] for item_id in selected
        }, "Bounded recovery changed or omitted loaded items"
        return {
            "events": cursor,
            "items": len(latest),
            "reconnect_cursor": midpoint,
            "recovered_loaded_items": len(selected),
        }

    def review(self) -> dict[str, Any]:
        chat = self.idle()
        assert chat["title"].startswith("Agentic workspace release acceptance"), (
            "Review modifies only a dedicated acceptance conversation"
        )
        file = next(item for item in self.files() if item["path"] == "results.csv")
        original = self.contents(file)
        assert original.replace(b"\r\n", b"\n") == b"metric,value\ntotal,60\n"
        control = self.request("POST", self.chat + "/runtime/control", json={"action": "take"})
        generation = control["lease_generation"]
        try:
            checkpoint = self.request(
                "POST", self.chat + "/checkpoints", json={"label": "Live review acceptance"}
            )
            file = self.request("GET", f"{self.base}/files/{file['id']}")
            payload = {
                "content": "metric,value\ntotal,90\n",
                "expected_revision_id": file["current_revision_id"],
                "lease_generation": generation,
            }
            route = f"{self.base}/files/{file['id']}/content"
            saved = self.request("PUT", route, json=payload)
            stale = self.client.put(route, json={**payload, "content": "stale edit"})
            assert stale.status_code == 409, "Stale editor overwrote a newer revision"
            changes = self.request("GET", self.chat + "/changes")
            changed = next(item for item in changes["items"] if item["path"] == "results.csv")
            assert "-total,60" in changed["diff"] and "+total,90" in changed["diff"]
            restore_path = self.chat + f"/checkpoints/{checkpoint['id']}/restore"
            restore = {
                "paths": ["results.csv"],
                "expected_revisions": {"results.csv": file["sha256"]},
                "lease_generation": generation,
            }
            stale = self.client.post(restore_path, json=restore)
            assert stale.status_code == 409, "Restoration overwrote a newer file"
            restore["expected_revisions"] = {"results.csv": saved["sha256"]}
            restored = self.request("POST", restore_path, json=restore)
            assert restored["restored"] == ["results.csv"]
            current = self.request("GET", f"{self.base}/files/{file['id']}")
            assert self.contents(current) == original
        finally:
            self.request("POST", self.chat + "/runtime/control", json={"action": "return"})
        items = self.snapshot()["items"].values()
        messages = [item for item in items if item["kind"] == "message"]
        last = max(messages, key=lambda item: item["sequence"])
        branch = self.request(
            "POST",
            self.chat + "/branches",
            json={
                "message_id": last["data"]["id"],
                "checkpoint_id": checkpoint["id"],
                "title": "Agentic workspace release acceptance — retained branch",
            },
        )
        branch_id = branch["conversation_id"]
        branch_files = self.request("GET", f"{self.base}/conversations/{branch_id}/files")["items"]
        copied = next(item for item in branch_files if item["path"] == "results.csv")
        assert copied["id"] != file["id"] and self.contents(copied) == original
        assert self.request("GET", self.chat + "/runtime")["owner"] is None
        return {
            "checkpoint": checkpoint["id"],
            "branch": branch_id,
            "restored_version": current["version"],
            "stale_save": 409,
            "stale_restore": 409,
        }

    def project(self) -> dict[str, Any]:
        chat = self.idle()
        assert chat["title"].startswith("Agentic workspace release acceptance")
        checkpoint = self.request(
            "POST", self.chat + "/checkpoints", json={"label": "Reusable project acceptance"}
        )
        project = self.request(
            "POST",
            self.base + "/projects",
            json={"name": "Agentic acceptance source", "context": "Sample sales total is 60."},
        )
        self.request(
            "POST",
            f"{self.base}/projects/{project['id']}/source",
            json={"conversation_id": chat["id"], "checkpoint_id": checkpoint["id"]},
        )
        created = self.request(
            "POST",
            self.base + "/conversations",
            json={
                "agent_id": chat["primary_agent_id"],
                "project_id": project["id"],
                "title": "Agentic workspace release acceptance — project copy",
            },
        )["conversation"]
        target = f"{self.base}/conversations/{created['id']}"
        runtime = self.request("GET", target + "/runtime")
        original_runtime = self.request("GET", self.chat + "/runtime")
        assert runtime["workspace_key"] != original_runtime["workspace_key"]
        self.request("POST", target + "/checkpoints", json={"label": "Seeded project source"})
        files = self.request("GET", target + "/files")["items"]
        results = next(file for file in files if file["path"] == "results.csv")
        original = next(file for file in self.files() if file["path"] == "results.csv")
        assert results["id"] != original["id"] and self.contents(results) == self.contents(original)
        return {"project": project["id"], "conversation": created["id"], "files": len(files)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--workspace", type=UUID, required=True)
    parser.add_argument("--conversation", type=UUID, required=True)
    parser.add_argument(
        "--phase", choices=["journal", "review", "project", "baseline", "recover"], required=True
    )
    parser.add_argument(
        "--evidence", type=Path, default=Path(".tmp/agentic-recovery-baseline.json")
    )
    args = parser.parse_args()
    with httpx.Client(
        base_url=args.url,
        timeout=110,
        trust_env=False,
        headers={"Authorization": "Bearer " + os.environ["JHIN_LIVE_API_KEY"]},
    ) as client:
        acceptance = Acceptance(client, args.workspace, args.conversation)
        if args.phase == "journal":
            result = acceptance.journal()
        elif args.phase == "review":
            result = acceptance.review()
        elif args.phase == "project":
            result = acceptance.project()
        else:
            current = acceptance.snapshot()
            if args.phase == "baseline":
                args.evidence.parent.mkdir(parents=True, exist_ok=True)
                args.evidence.write_text(json.dumps(current, indent=2), encoding="utf-8")
            else:
                previous = json.loads(args.evidence.read_text(encoding="utf-8"))
                assert previous == current, "Authoritative transcript/files changed across restart"
            result = {
                "phase": args.phase,
                "items": len(current["items"]),
                "files": len(current["files"]),
            }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
