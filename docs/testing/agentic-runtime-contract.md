# Runtime session contract

Base: `/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}`.

- GET `/runtime`: `{workspace_key,cwd,owner,owner_user_id,lease_generation,terminal,previews}`. Owner is `agent`, `user`, or null. Working directory is `/workspace`.
- POST `/runtime/control`: `{action: 'take'|'return'}`. Returns the runtime object; 409 if active work prevents confirmed transfer. Terminal control requires owner/admin.
- POST `/terminals`: `{network:'none'|'internet'}`. Starts one writable PTY under the caller's fenced workspace lease.
- GET `/terminals/{id}`; POST `/terminals/{id}/interrupt`; POST `/terminals/{id}/close`.
- POST `/terminals/{id}/ticket`: `{ticket,websocket_url:'/runtime/sessions/{id}/ws'}`. Connect with subprotocols `['jhin-session', ticket]`. No token in the terminal URL.
- GET `/previews`; POST `/previews`: `{file_id,revision_id?,framework:'static'|'vite'|'next'|'http',command?,port?}`.
- POST `/previews/{id}/ticket`: `{url}`; POST `/previews/{id}/stop`; POST `/previews/{id}/restart`.

Session: `{id,kind,status,cwd,network,exit_code,output,output_offset,lease_generation,created_at,revision_id?,url?}`. Status is starting/running/stopping/completed/failed/stopped. Published file revisions survive sessions.

WebSocket input: `{type:'input',data,seq}`, `{type:'resize',cols,rows}`, `{type:'interrupt'}`.
Output: `{type:'output',data,offset}`, `{type:'status',status,exit_code}`, `{type:'ack',seq}`, `{type:'error',message}`. Reconnect requests carry `after` byte offset in the URL; never replay unacknowledged keystrokes.

Root will implement a tool-worker gateway for scoped sessions and workspace file operations. Only the runner holds Docker authority. API does not gain the runner token or join its network. Preview mount capabilities are bounded/revocable, never dashboard credentials; response-level opaque-origin sandboxing applies to direct navigation as well as frames.
