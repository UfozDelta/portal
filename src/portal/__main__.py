"""`python -m portal <command> ...` — a thin shell convenience over api.py.
No prompts, no separate init step. All real logic lives in api.py; this just
prints its results."""

from __future__ import annotations

import argparse
import logging
import sys

from . import api
from .errors import PortalError


def _print_checks(checks) -> None:
    for c in checks:
        print(f"{'ok' if c.ok else 'FAIL':4}  {c.name:11} {c.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m portal")
    parser.add_argument("-v", "--verbose", action="store_true", help="show progress")
    sub = parser.add_subparsers(dest="command", required=True)

    p_deploy = sub.add_parser("deploy", help="deploy PATH and publish it at --host")
    p_deploy.add_argument("path")
    p_deploy.add_argument("--host", required=True)
    p_deploy.add_argument("--port", type=int, default=None)
    p_deploy.add_argument("--service", default=None)
    p_deploy.add_argument("--name", default=None, help="deployment name (default: dir name)")
    p_deploy.add_argument(
        "--docker-host",
        default=None,
        help="run containers on a remote docker daemon, e.g. ssh://user@1.2.3.4",
    )

    p_deploy.add_argument(
        "--allow-missing-env",
        action="store_true",
        help="warn instead of failing when .env lacks keys from .env.example",
    )

    p_destroy = sub.add_parser("destroy", help="tear down a deployment")
    p_destroy.add_argument("slug")
    p_destroy.add_argument("--volumes", action="store_true")
    p_destroy.add_argument(
        "--docker-host", default=None, help="override the host recorded at deploy time"
    )

    p_status = sub.add_parser("status", help="report what portal has deployed")
    p_status.add_argument("slug", nargs="?", default=None)
    p_status.add_argument("--no-probe", action="store_true")

    p_verify = sub.add_parser("verify", help="health-check a deployment, read-only")
    p_verify.add_argument("slug")
    p_verify.add_argument("--no-probe", action="store_true")

    p_check = sub.add_parser("check", help="check the Cloudflare token, read-only")
    p_check.add_argument("--host", default=None, help="also check zone and DNS access for it")

    p_inspect = sub.add_parser("inspect", help="report what would be deployed")
    p_inspect.add_argument("path")
    p_inspect.add_argument("--port", type=int, default=None)
    p_inspect.add_argument("--service", default=None)
    p_inspect.add_argument("--name", default=None)

    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        if args.command == "deploy":
            result = api.deploy(
                args.path,
                args.host,
                port=args.port,
                service=args.service,
                name=args.name,
                docker_host=args.docker_host,
                strict_env=not args.allow_missing_env,
            )
            print(f"live at {result.live_url}  ({result.probe})")
            _print_checks(result.health.checks)
            if not result.health.ok:
                return 1
        elif args.command == "destroy":
            result = api.destroy(args.slug, volumes=args.volumes, docker_host=args.docker_host)
            if result.state_removed:
                print(f"destroyed {result.slug}")
            else:
                print(f"partially destroyed {result.slug}; run destroy again to finish")
                return 1
        elif args.command == "status":
            for r in api.status(args.slug, probe=not args.no_probe):
                print(f"{r.record.slug}  https://{r.record.fqdn}  {r.probe or ''}")
        elif args.command == "verify":
            report = api.verify(args.slug, probe=not args.no_probe)
            _print_checks(report.checks)
            if not report.ok:
                return 1
        elif args.command == "check":
            report = api.check(args.host)
            _print_checks(report.checks)
            if not report.ok:
                return 1
        elif args.command == "inspect":
            plan = api.inspect(args.path, port=args.port, service=args.service, name=args.name)
            print(plan.to_dict())
    except PortalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
