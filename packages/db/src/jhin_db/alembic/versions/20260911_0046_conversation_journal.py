"""Atomic, ordered conversation updates and isolated generation attempts.

Revision ID: 0046
Revises: 0045
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

TABLES = (
    "message",
    "task",
    "tool_call",
    "approval",
    "user_question",
    "work_request",
    "work_review",
    "sandbox_job",
    "model_generation",
    "managed_file",
)

FUNCTION = r"""
CREATE OR REPLACE FUNCTION jhin_conversation_journal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  row_data jsonb; previous jsonb; chat uuid; workspace uuid; task_uuid uuid;
  next_seq bigint; item_kind text; item_id uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN row_data := to_jsonb(OLD); ELSE row_data := to_jsonb(NEW); END IF;
  IF TG_OP = 'UPDATE' AND to_jsonb(OLD) = row_data THEN RETURN NEW; END IF;
  workspace := (row_data->>'workspace_id')::uuid;
  item_kind := TG_TABLE_NAME; item_id := (row_data->>'id')::uuid;
  chat := (row_data->>'conversation_id')::uuid;
  task_uuid := COALESCE((row_data->>'task_id')::uuid, (row_data->>'requester_task_id')::uuid);
  IF TG_TABLE_NAME = 'task' THEN task_uuid := item_id; END IF;
  IF task_uuid IS NULL AND row_data->>'run_id' IS NOT NULL THEN
    SELECT task_id INTO task_uuid FROM agent_run WHERE id=(row_data->>'run_id')::uuid AND
      workspace_id=workspace;
  END IF;
  IF chat IS NULL AND task_uuid IS NOT NULL THEN
    SELECT conversation_id INTO chat FROM task WHERE id=task_uuid AND workspace_id=workspace;
  END IF;
  IF chat IS NULL THEN RETURN COALESCE(NEW, OLD); END IF;
  IF TG_TABLE_NAME='message' AND row_data->>'visibility' <> 'visible' THEN
    IF TG_OP <> 'UPDATE' OR OLD.visibility <> 'visible' THEN RETURN NEW; END IF;
    row_data := jsonb_build_object('id',item_id,'visibility','internal');
  END IF;
  IF TG_TABLE_NAME='sandbox_job' THEN
    -- No file wrapper or private evidence can enter the chat journal.
    SELECT to_jsonb(t) INTO previous FROM tool_call t
      WHERE t.id=(row_data->>'tool_call_id')::uuid AND t.run_id=(row_data->>'run_id')::uuid AND
      t.workspace_id=workspace
      AND t.tool_name IN ('cli.command.execute','cli.test.run');
    IF previous IS NULL THEN RETURN COALESCE(NEW, OLD); END IF;
    item_kind := 'tool_call'; item_id := (previous->>'id')::uuid;
    row_data := previous || jsonb_build_object('sandbox_job', jsonb_build_object(
'job_id',row_data->>'id','status',row_data->>'status','network_policy',row_data->>'network_policy',
'stdout',row_data->>'stdout_tail','stderr',row_data->>'stderr_tail','exit_code',row_data->'exit_code',
'started_at',row_data->'started_at','completed_at',row_data->'completed_at','duration_ms',row_data->'duration_ms','output_is_tail',true));
  END IF;
  IF TG_TABLE_NAME='model_generation' THEN item_kind := 'generation'; END IF;
  IF TG_TABLE_NAME='tool_call' AND row_data->>'tool_name' IN
      ('cli.command.execute','cli.test.run') THEN
    SELECT jsonb_build_object('job_id',j.id,'status',j.status,'network_policy',j.network_policy,
'stdout',j.stdout_tail,'stderr',j.stderr_tail,'exit_code',j.exit_code,'started_at',j.started_at,
      'completed_at',j.completed_at,'duration_ms',j.duration_ms,'output_is_tail',true)
      INTO previous FROM sandbox_job j WHERE j.tool_call_id=item_id AND
      j.run_id=(row_data->>'run_id')::uuid AND j.workspace_id=workspace
      ORDER BY j.created_at DESC,j.id DESC LIMIT 1;
    IF previous IS NOT NULL THEN row_data := row_data ||
      jsonb_build_object('sandbox_job',previous); END IF;
  END IF;
  row_data := row_data || jsonb_build_object('task_id',task_uuid);
  UPDATE conversation SET timeline_sequence=timeline_sequence+1
    WHERE id=chat AND workspace_id=workspace RETURNING timeline_sequence INTO next_seq;
  IF next_seq IS NULL THEN RETURN COALESCE(NEW, OLD); END IF;
  INSERT INTO
      conversation_event(conversation_id,sequence,workspace_id,source_kind,source_id,operation,payload_json,created_at)
    VALUES(chat,next_seq,workspace,item_kind,item_id,CASE WHEN TG_OP='DELETE' THEN 'delete' ELSE
      'upsert' END,row_data,clock_timestamp());
  PERFORM
      pg_notify('jhin_conversation',json_build_object('conversation_id',chat,'sequence',next_seq)::text);
  RETURN COALESCE(NEW, OLD);
END $$;
"""


def upgrade() -> None:
    op.add_column(
        "conversation",
        sa.Column("timeline_sequence", sa.BigInteger(), nullable=False, server_default="0"),
    )
    for name in ("source_conversation_id", "source_message_id", "source_checkpoint_id"):
        op.add_column("conversation", sa.Column(name, sa.Uuid(), nullable=True))
    op.create_table(
        "conversation_event",
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversation.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("payload_json", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_conversation_event_workspace_id", "conversation_event", ["workspace_id"])
    op.create_index(
        "ix_conversation_event_item",
        "conversation_event",
        ["conversation_id", "source_kind", "source_id", "sequence"],
    )
    op.create_table(
        "model_generation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id", sa.Uuid(), sa.ForeignKey("conversation.id", ondelete="CASCADE")
        ),
        sa.Column(
            "task_id", sa.Uuid(), sa.ForeignKey("task.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "agent_id", sa.Uuid(), sa.ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("metadata_json", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_model_generation_run_step", "model_generation", ["run_id", "step"])
    op.execute(FUNCTION)
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER journal_{table} AFTER INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION jhin_conversation_journal()"
        )
    # Existing chats get the same canonical snapshot contract at upgrade.
    op.execute(r"""
      WITH sources AS (
        SELECT m.workspace_id, COALESCE(m.conversation_id,t.conversation_id) AS chat,
          'message'::text AS kind,m.id,to_jsonb(m) AS data,m.created_at
        FROM message m LEFT JOIN task t ON t.id=m.task_id AND t.workspace_id=m.workspace_id
        WHERE m.visibility='visible'
        UNION ALL
        SELECT
      c.workspace_id,t.conversation_id,'tool_call',c.id,to_jsonb(c)||jsonb_build_object('task_id',t.id),c.created_at
        FROM tool_call c JOIN agent_run r ON r.id=c.run_id AND r.workspace_id=c.workspace_id
        JOIN task t ON t.id=r.task_id AND t.workspace_id=c.workspace_id
        UNION ALL SELECT t.workspace_id,t.conversation_id,'task',t.id,to_jsonb(t),t.created_at
      FROM task t
        UNION ALL SELECT a.workspace_id,t.conversation_id,'approval',a.id,to_jsonb(a),a.created_at
          FROM approval a JOIN task t ON t.id=a.task_id AND t.workspace_id=a.workspace_id
        UNION ALL SELECT
      q.workspace_id,COALESCE(q.conversation_id,t.conversation_id),'user_question',q.id,to_jsonb(q),q.created_at
          FROM user_question q LEFT JOIN task t ON t.id=q.task_id AND t.workspace_id=q.workspace_id
        UNION ALL SELECT
      r.workspace_id,COALESCE(r.conversation_id,t.conversation_id),'work_request',r.id,to_jsonb(r),r.created_at
          FROM work_request r LEFT JOIN task t ON t.id=r.requester_task_id AND
      t.workspace_id=r.workspace_id
        UNION ALL SELECT
      r.workspace_id,t.conversation_id,'work_review',r.id,to_jsonb(r),r.created_at
          FROM work_review r JOIN task t ON t.id=r.task_id AND t.workspace_id=r.workspace_id
        UNION ALL SELECT
      f.workspace_id,f.conversation_id,'managed_file',f.id,to_jsonb(f),f.created_at FROM
      managed_file f
      ), ranked AS (
        SELECT *,row_number() OVER(PARTITION BY chat ORDER BY created_at,id) AS seq FROM sources
      WHERE chat IS NOT NULL
      ) INSERT INTO
      conversation_event(conversation_id,sequence,workspace_id,source_kind,source_id,operation,payload_json,created_at)
        SELECT chat,seq,workspace_id,kind,id,'upsert',data,created_at FROM ranked;

""")
    op.execute(
        "UPDATE conversation c SET timeline_sequence=s.seq FROM "
        "(SELECT conversation_id,max(sequence) AS seq FROM conversation_event "
        "GROUP BY conversation_id) s WHERE c.id=s.conversation_id"
    )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS journal_{table} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS jhin_conversation_journal()")
    op.drop_table("model_generation")
    op.drop_table("conversation_event")
    op.drop_column("conversation", "timeline_sequence")
    for name in ("source_conversation_id", "source_message_id", "source_checkpoint_id"):
        op.drop_column("conversation", name)
