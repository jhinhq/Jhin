# Agentic workspace files API contract

Prefix `W=/api/v1/workspaces/{workspace_id}`. All operations enforce workspace membership; read is chats:read, writes chats:write. Editor/restore/import require owner/admin and confirmed runtime workspace ownership. Paths are relative POSIX paths; `.git`, traversal, absolute paths and symlinks are refused.

## Files

- `GET W/conversations/{c}/files` -> `{items: File[], has_more: bool}` (limit 100, cursor optional UUID).
- `POST W/conversations/{c}/files` multipart field `file` and optional `path` -> File (201). Maximum 25 MiB; synchronous bounded extraction off event loop. Clients can cancel the request; completed uploads remain managed files. `status` is `ready` or `failed` and `error` explains failed extraction.
- `GET W/files/{id}` -> File; `GET W/files/{id}/versions` -> `{items: Revision[]}`.
- `GET W/files/{id}/content?revision_id=...` -> `{file_id, revision_id, path, content, truncated, editable, sha256}`.
- `PUT W/files/{id}/content` `{content:string, expected_revision_id:UUID, lease_generation:int}` -> File. 409 revision/lease conflict; guarded runtime write precedes new immutable publication.
- `GET W/files/{id}/download?revision_id=...` authenticated download with attachment disposition and nosniff. `GET .../preview` returns safe image bytes, PDF bytes with sandbox headers, or extracted text; HTML/SVG never served executable from this endpoint.
- `POST W/conversations/{c}/files/publish` `{path, title?:string}` snapshots one actual sandbox file into immutable storage -> File.
- `POST W/files/{id}/annotations` `{revision_id, text, location?:object}` -> Annotation. `GET .../annotations` -> `{items: Annotation[]}`.

File: `{id, workspace_id, conversation_id, name, path, kind:'upload'|'artifact'|'working', status:'ready'|'failed', error:null|string, mime_type, size_bytes, preview_kind:'text'|'code'|'image'|'pdf'|'document'|'slides'|'spreadsheet', current_revision_id, version:int, sha256, extracted_text, extraction_truncated:bool, created_at, updated_at, download_url, preview_url}`.

Revision: `{id,file_id,version,sha256,size_bytes,mime_type,preview_kind,created_at,created_by_user_id:null|UUID,source_run_id:null|UUID}`. Artifact records use same file shape, `kind:'artifact'`.

## Projects and review

- `GET/POST W/projects`; `GET/PATCH/DELETE W/projects/{id}`. Fields `{name,description:'',repository_url:null|string,source_revision:null|string,context:''}`. DELETE archives; list omits archived. Project includes `{id,workspace_id,created_at,updated_at,...}`.
- `GET W/conversations/{c}/changes` -> `{items:[{path,status:'added'|'modified'|'deleted',before_revision_id,after_revision_id,diff}], excluded:[]}` from managed checkpoints and current sandbox snapshot.
- `GET/POST W/conversations/{c}/checkpoints` -> list `{items:Checkpoint[]}` or creates `{label?:string}` -> Checkpoint `{id,label,manifest_json:{path:revision_id},excluded_json:[],created_at}`.
- `POST W/conversations/{c}/checkpoints/{id}/restore` `{paths:string[], expected_revisions:{path:sha256|null},lease_generation:int}` -> `{restored:string[]}`; atomically runtime guarded; only specified files.

## Agent/runtime integration

`jhin_media.managed_files.pin_attachments(db,workspace_id,conversation_id,attachment_ids,context_refs=[])` -> immutable metadata references for persisted turn. References use `{type:'file',id:file_id,revision_id,name,mime_type,size_bytes,sha256}`. Files must belong to the workspace; cross-chat reuse is explicit by selected ID.

`jhin_media.managed_files.attachment_content(db,workspace_id,references,store=None)` -> list of `{type:'text',text}` / `{type:'image',mime_type,data_base64}` bounded at 200k extracted characters/file; references are revision-pinned and validated again. Image capability checks are caller responsibility.

`jhin_media.managed_files.publish_file(db,workspace_id,conversation_id,path,data,kind='artifact',user_id=None,run_id=None,store=None,expected_revision_id=None)` -> ManagedFile (flushes; caller commits). Shared API and connector publication uses this function.

Runtime bridge expected: `jhin_api.runtime.service.workspace_operation(db,ctx,conversation_id,operation,args,write=False,expected_generation=None)->dict`; read `{path}` -> `{content_base64,sha256,size_bytes}`, write `{path,content_base64,expected_sha256}`; snapshot -> `{files:[{path,content_base64,sha256,size_bytes}],excluded:[]}`; restore `{files:[{path,content_base64,expected_sha256}]}`. DB sandbox model has conversation_id, nullable agent_id, holder_user_id, lease_generation and kind='conversation' unique per workspace/conversation.
