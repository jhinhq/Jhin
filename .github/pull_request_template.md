## Summary

<!-- What changes and why. Link the issue, plan, or spec this implements
     (docs/superpowers/plans, docs/superpowers/specs, docs/implementation-plan.md). -->

## Gates

- [ ] `make lint` passes (ruff check, ruff format --check, eslint)
- [ ] `make typecheck` passes (mypy strict, tsc)
- [ ] `make test-unit` passes (pytest, vitest)
- [ ] Integration coverage added or updated when behaviour crosses a service boundary
      (`tests/integration`, run through the Phase 10 harness)
- [ ] `uv run python scripts/release_preflight.py` passes when touching release,
      docs, versions, workflows, or `.env.example`

## Compatibility

- [ ] Database migrations are forward-only and included (`packages/db`), or no schema change
- [ ] Temporal workflow changes preserve replay of existing histories, or no workflow change
- [ ] Event envelope / subject changes are additive, or no event change
- [ ] Documentation updated (`README.md`, `docs/`) for user-visible changes
- [ ] `CHANGELOG.md` "Unreleased" section updated

## Security impact

<!-- Does this touch secrets, authorization, the tool gateway, connectors,
     webhooks, sandboxing, or the Docker-socket boundary? Describe the impact
     and the tests that prove it. Write "none" if not applicable. -->

## Checklist

- [ ] No credentials, tokens, master keys, `.env` files, or generated local state are included
- [ ] Commits follow Conventional Commits and are signed off (DCO)
- [ ] I agree my contribution is licensed under Apache-2.0
