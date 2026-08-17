"""CLI connector (plan 11.6): sandboxed command execution.

CLI is not "run anything on host" — every tool routes to the sandbox runner
(plan 14), which executes it in a fresh locked-down container. The connector
holds no credential; a connection is just a named bundle of defaults (image,
network) plus an optional reference to a GitHub connection for repository
jobs (plan 14.5).
"""
