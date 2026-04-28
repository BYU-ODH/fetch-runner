from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from fetch_runner import __version__
from fetch_runner.config import ConfigError
from fetch_runner.config import load_config
from fetch_runner.guard import GuardError
from fetch_runner.guard import render_canonical_script_guard
from fetch_runner.runner import GitPollingRunner

log = logging.getLogger("fetch_runner")


def main(argv: list[str] | None = None) -> int:
    argument_parser = argparse.ArgumentParser(
        prog="fetch-runner",
        description="Poll git branches and run scripts when new commits arrive.",
    )
    argument_parser.add_argument("config", type=Path, nargs="?", help="path to jobs.toml")
    argument_parser.add_argument(
        "--check",
        action="store_true",
        help="validate the config (including every script's guard) and exit",
    )
    argument_parser.add_argument(
        "--print-guard",
        metavar="USER",
        help="print the canonical guard block for USER and exit (for pasting into a new script)",
    )
    argument_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    argument_parser.add_argument(
        "--version",
        action="version",
        version=f"fetch-runner {__version__}",
    )
    cli_args = argument_parser.parse_args(argv)

    _configure_logging(cli_args.verbose)

    if cli_args.print_guard:
        try:
            sys.stdout.write(render_canonical_script_guard(cli_args.print_guard))
        except GuardError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0

    if cli_args.config is None:
        argument_parser.error("config path is required (or use --print-guard)")

    try:
        runner_config = load_config(cli_args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if cli_args.check:
        print(
            f"ok: user={runner_config.runtime_user} "
            f"jobs={len(runner_config.jobs)} "
            f"poll={runner_config.poll_interval_seconds}s"
        )
        return 0

    runner = GitPollingRunner(runner_config)

    def _handle_stop_signal(signum, _frame):
        log.info("received signal %s; shutting down", signum)
        runner.request_stop()

    signal.signal(signal.SIGTERM, _handle_stop_signal)
    signal.signal(signal.SIGINT, _handle_stop_signal)
    return runner.run_forever()


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )
