from jhin_secrets import get_redactor
from jhin_tool_worker.sandbox_reconcile import _closure_for


def test_reconciled_output_scrubs_worker_secret_edges_before_persisting() -> None:
    redactor = get_redactor()
    redactor.register("worker-only-canary-secret")
    try:
        closure = _closure_for(
            {
                "status": "completed",
                "stdout": "before\nworker-only-canary-",
                "stderr": "canary-secret\n\x00after",
                "exit_code": 0,
            }
        )
        assert closure is not None
        values, _, _ = closure
        assert values["stdout_tail"] == "before\n[REDACTED]"
        assert values["stderr_tail"] == "[REDACTED]\n?after"
        assert values["status"] == "completed" and values["exit_code"] == 0
    finally:
        redactor.clear()


def test_reconciled_output_keeps_notice_without_exposing_secret_prefix() -> None:
    redactor = get_redactor()
    redactor.register("worker-only-canary-secret")
    notice = "\n…[truncated by sandbox runner]"
    try:
        closure = _closure_for(
            {
                "status": "completed",
                "stdout": "界" * 20 + "worker-only-canary-" + notice,
                "stderr": "",
                "exit_code": 0,
            }
        )
        assert closure is not None
        output = closure[0]["stdout_tail"]
        assert "worker-only-canary-" not in output and "[REDACTED]" in output
        assert output.endswith(notice)
    finally:
        redactor.clear()
