"""``jhin-admin``: server-side administration for a self-hosted Jhin.

The web app creates the first owner and, from then on, invites people. It has
nothing to offer when nobody can sign in, when an install needs a second
account before anyone has one, or when an operator needs to know whether the
database is where the code expects it. That is what this is for; it runs
wherever the API image runs::

    docker compose run --rm --no-deps api jhin-admin doctor

Failures reach the terminal as sentences. A traceback in a container log is
not an answer to "why can I not sign in", so every foreseeable refusal is a
:class:`~jhin_api.cli.runtime.CommandError` and even an unforeseen one is
printed as a line rather than raised at the operator.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys

from fastapi import HTTPException

from jhin_api.cli.parser import PROGRAM, build_parser
from jhin_api.cli.runtime import CommandError, Result, Runtime, emit
from jhin_db import create_engine, create_session_factory
from jhin_domain import new_uuid7
from jhin_observability import noop_tracer


def _fail(message: str, *, as_json: bool) -> None:
    """Report a failure on stderr, so ``--json`` keeps stdout to its object."""
    print(json.dumps({"error": message}) if as_json else f"{PROGRAM}: {message}", file=sys.stderr)


async def _run(args: argparse.Namespace) -> Result:
    # Imported once argv is parsed: the command modules pull in the whole
    # service layer, and `jhin-admin --help` should not wait for it.
    from jhin_api.cli.commands import COMMANDS

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise CommandError(
            "DATABASE_URL environment variable is required. Inside the compose stack, "
            f"`docker compose run --rm --no-deps api {PROGRAM} ...` brings it with it."
        )
    # No observability runtime is bootstrapped here, so SQL tracing has no
    # tracer to report to — the dev seed passes the same no-op.
    engine = create_engine(database_url, tracer=noop_tracer())
    try:
        async with create_session_factory(engine)() as db:
            runtime = Runtime(
                args=args,
                db=db,
                engine=engine,
                database_url=database_url,
                request_id=new_uuid7(),
            )
            try:
                result = await COMMANDS[args.command](runtime)
            except Exception:
                await db.rollback()
                raise
    finally:
        await engine.dispose()
    return result


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "command", None) is None:
        # A bare `jhin-admin` is somebody looking around, not a usage error.
        parser.print_help()
        return

    try:
        # The service layer logs as it works, and some of that lands on stdout
        # (loading a master key from the environment warns, for one). Under
        # --json a single stray line makes the output unparseable, so the whole
        # run is fenced off from stdout and only the result is written to it.
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(_run(args))
        emit(result, as_json=args.json)
        code = result.exit_code
    except CommandError as exc:
        _fail(str(exc), as_json=args.json)
        code = 1
    except HTTPException as exc:
        # The services state their refusals as HTTP errors for the API's sake;
        # on a console the detail is the whole of the message.
        _fail(str(exc.detail), as_json=args.json)
        code = 1
    except KeyboardInterrupt:
        _fail("Interrupted.", as_json=args.json)
        code = 130
    except Exception as exc:
        _fail(f"{type(exc).__name__}: {exc}", as_json=args.json)
        code = 1
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
