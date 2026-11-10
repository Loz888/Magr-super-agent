"""Bounded operator replay client; uses the running sole Engine via its authenticated API."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import httpx

from mgar_v3.domain import DomainError
from mgar_v3.sources.ingress_crypto import _private_file, private_directory
from mgar_v3.sources.ingress_domain import ReplayRequest
from mgar_v3.web_access import SESSION_COOKIE


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Revalidate bounded source inputs as context; never starts tasks"
    )
    parser.add_argument("--origin", default="http://127.0.0.1:18080")
    parser.add_argument(
        "--session-file",
        type=Path,
        required=True,
        help="Private .local JSON: cookie and csrf_token",
    )
    parser.add_argument("--source", type=UUID, required=True)
    parser.add_argument(
        "operation",
        choices=["receipts", "inputs", "gaps", "cursors", "conflicts", "purge", "replay"],
    )
    parser.add_argument("--generation", type=int)
    parser.add_argument("--input", type=UUID, action="append", default=[])
    parser.add_argument("--review-ref", default="operator-replay-review")
    args = parser.parse_args()
    parsed = urlparse(args.origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        parser.error("Use an explicit local Engine origin without path or credentials")
    with private_directory(args.session_file.parent) as directory:
        fd = os.open(args.session_file.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            if _private_file(fd).st_size > 4096:
                raise DomainError("INGRESS_SESSION_BOUND", "Session file exceeds 4 KiB", 403)
            session = json.loads(os.read(fd, 4097))
        finally:
            os.close(fd)
    if set(session) != {"cookie", "csrf_token"} or not all(
        isinstance(v, str) and v for v in session.values()
    ):
        parser.error("Session file must contain cookie and csrf_token only")
    prefix = f"/api/v3/source-ingress/{args.source}"
    with httpx.Client(
        base_url=args.origin,
        timeout=30,
        follow_redirects=False,
        trust_env=False,
        cookies={SESSION_COOKIE: session["cookie"]},
        headers={"Origin": args.origin, "X-CSRF-Token": session["csrf_token"]},
    ) as client:
        if args.operation == "replay":
            body = ReplayRequest(
                expected_generation=args.generation,
                input_ids=tuple(args.input),
                review_ref=args.review_ref,
            )
            response = client.post(prefix + "/replay", json=body.model_dump(mode="json"))
        elif args.operation == "purge":
            response = client.post(prefix + "/purge")
        else:
            response = client.get(prefix + "/view/" + args.operation)
        if response.status_code != 200:
            raise SystemExit(
                f"Ingress operation rejected (HTTP {response.status_code}); "
                "no task or publication requested"
            )
        print(json.dumps(response.json(), ensure_ascii=False))


if __name__ == "__main__":
    main()
