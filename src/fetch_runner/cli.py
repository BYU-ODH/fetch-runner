from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from fetch_runner import __version__
from fetch_runner.config import ConfigError, load_config
from fetch_runner.guard import GuardError, render_guard
from fetch_runner.runner import Runner

log = logging.getLogger("fetch_runner")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="fetch-runner",
        description="Poll git branches and run scripts when new commits arrive.",
    )
    p.add_argument("config", type=Path, nargs="?", help="path to jobs.toml")
    p.add_argument(
        "--check",
        action="store_true",
        help="validate the config (including every script's guard) and exit",
    )
    p.add_argument(
        "--print-guard",
        metavar="USER",
        help="print the canonical guard block for USER and exit",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )
    p.add_argument("--version", action="version", version=f"fetch-runner {__version__}")
    args = p.parse_args(argv)

    _configure_logging(args.verbose)

    if args.print_guard:
        try:
            sys.stdout.write(render_guard(args.print_guard))
        except GuardError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0

    if args.config is None:
        p.error("config path is required (or use --print-guard)")

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.check:
        print(
            f"ok: user={cfg.user} jobs={len(cfg.jobs)} "
            f"poll={cfg.poll_interval_seconds}s"
        )
        return 0

    runner = Runner(cfg)

    def _stop(signum, _frame):
        log.info("received signal %s; shutting down", signum)
        runner.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    return runner.run_forever()


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )
