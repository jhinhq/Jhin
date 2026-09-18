from dataclasses import dataclass

ACTIVITY_ADVANCE_BLOG_CORPUS_SYNC = "advance_blog_corpus_sync"


@dataclass
class BlogCorpusSyncInput:
    workspace_id: str
    sync_id: str
    # Failure projection uses the same activity, without touching credentials.
    fail: bool = False


@dataclass
class BlogCorpusSyncResult:
    sync_id: str
    status: str
    discovered: int = 0
    indexed: int = 0
    failed: int = 0


def blog_corpus_workflow_id(sync_id: str) -> str:
    return f"blog-corpus-{sync_id}"
