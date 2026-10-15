"""Noninteractive travel expense commands."""

import argparse
import json
import sys

from . import __version__
from .core.common import ClaimError, failure, settings, success
from .core.store import Store


class Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise ClaimError("INVALID_ARGUMENT", message)


def parser() -> Parser:
    root = Parser(prog="ech", description="Local travel claims and expense policy checks")
    root.add_argument("--data-dir")
    root.add_argument("--config")
    for key in ["json", "quiet", "yes", "progress"]:
        root.add_argument("--" + key, action="store_true")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    trip = commands.add_parser("trip").add_subparsers(
        dest="action", required=True, parser_class=Parser
    )
    add = trip.add_parser("add")
    for key in ["start", "end", "origin", "destination"]:
        add.add_argument("--" + key, required=True)
    add.add_argument("--city-tier", default="B")
    add.add_argument("--purpose", default="SAMPLE")
    add.add_argument("--participants", type=int, default=1)
    add.add_argument("--transport", default="train")
    trip.add_parser("list")
    expense = commands.add_parser("expense").add_subparsers(
        dest="action", required=True, parser_class=Parser
    )
    add = expense.add_parser("add")
    add.add_argument("--trip", type=int, required=True)
    add.add_argument("--category", required=True)
    add.add_argument("--amount", required=True)
    for key in ["date", "receipt", "currency"]:
        add.add_argument("--" + key)
    add.add_argument("--paid-by", choices=["employee", "company"], default="employee")
    add.add_argument("--payment-method", default="card")
    add.add_argument("--has-receipt", action="store_true")
    add.add_argument("--invoice-type", default="receipt")
    add.add_argument("--invoice-no", default="")
    add.add_argument("--nights", type=int, default=1)
    add.add_argument("--travel-class", default="")
    add.add_argument("--note", default="")
    listing = expense.add_parser("list")
    listing.add_argument("--trip", type=int)
    review = expense.add_parser("review")
    review.add_argument("id", type=int)
    review.add_argument("--trip", type=int)
    review.add_argument("--amount")
    review.add_argument("--note")
    inclusion = review.add_mutually_exclusive_group()
    inclusion.add_argument("--include", action="store_true")
    inclusion.add_argument("--exclude", action="store_true")
    receipt = commands.add_parser("receipt").add_subparsers(
        dest="action", required=True, parser_class=Parser
    )
    add = receipt.add_parser("add")
    add.add_argument("path")
    add.add_argument("--amount")
    importing = commands.add_parser("import-receipts")
    importing.add_argument("path")
    importing.add_argument("--auto-match", action="store_true")
    importing.add_argument("--trip", type=int)
    commands.add_parser("check")
    claim = commands.add_parser("claim").add_subparsers(
        dest="action", required=True, parser_class=Parser
    )
    for name in ["build", "preview"]:
        cmd = claim.add_parser(name)
        cmd.add_argument("--trip", type=int, action="append")
        cmd.add_argument("--month")
        if name == "build":
            cmd.add_argument("--format", default="xlsx,pdf,zip")
    claim.add_parser("list")
    show = claim.add_parser("show")
    show.add_argument("code")
    status = claim.add_parser("status")
    status.add_argument("code")
    status.add_argument("status", choices=["unsubmitted", "submitted", "approved", "paid"])
    status.add_argument("--amount")
    status.add_argument("--date")
    due = commands.add_parser("due")
    due.add_argument("--as-of")
    stats = commands.add_parser("stats")
    stats.add_argument("--year", type=int, required=True)
    days = commands.add_parser("travel-days")
    days.add_argument("--year", type=int, required=True)
    export = commands.add_parser("export")
    export.add_argument("--format", choices=["csv", "json"], required=True)
    export.add_argument("--kind", choices=["archive", "ledger", "expenses"], default="archive")
    export.add_argument("--out", required=True)
    export.add_argument("--no-bom", action="store_true")
    importing = commands.add_parser("import")
    importing.add_argument("path")
    backup = commands.add_parser("backup")
    backup.add_argument("--out")
    serving = commands.add_parser("serve")
    serving.add_argument("--host", default="127.0.0.1")
    serving.add_argument("--port", type=int, default=8898)
    commands.add_parser("mcp")
    return root


def globals_first(argv: list[str]) -> list[str]:
    front, rest = [], []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            rest.extend(argv[index:])
            break
        if arg in {"--json", "--quiet", "--yes", "--progress", "--version"}:
            front.append(arg)
        elif arg in {"--data-dir", "--config"}:
            if index + 1 >= len(argv):
                raise ClaimError("INVALID_ARGUMENT", f"{arg} needs a value")
            front.extend(argv[index : index + 2])
            index += 1
        elif arg.startswith(("--data-dir=", "--config=")):
            front.append(arg)
        else:
            rest.append(arg)
        index += 1
    return front + rest


def dispatch(args, store: Store):
    if args.command == "trip":
        if args.action == "list":
            return store.trips()
        return store.add_trip(
            args.start,
            args.end,
            args.origin,
            args.destination,
            args.city_tier,
            args.purpose,
            args.participants,
            args.transport,
        )
    if args.command == "expense":
        if args.action == "list":
            return store.expenses(args.trip)
        if args.action == "review":
            return store.review_expense(
                args.id,
                args.trip,
                True if args.include else False if args.exclude else None,
                args.amount,
                args.note,
            )
        return store.add_expense(
            args.trip,
            args.category,
            args.amount,
            args.date,
            args.paid_by,
            args.payment_method,
            args.receipt,
            args.has_receipt,
            args.invoice_type,
            args.invoice_no,
            args.nights,
            args.travel_class,
            args.note,
            args.currency,
        )
    if args.command == "receipt":
        return store.add_receipt(args.path, args.amount)
    if args.command == "import-receipts":
        return store.import_receipts(args.path, args.auto_match, args.trip)
    if args.command == "check":
        return store.check()
    if args.command == "claim":
        if args.action == "build":
            return store.build_claim(args.trip, args.format.split(","), args.month)
        if args.action == "preview":
            return store.preview(args.trip, args.month)
        if args.action == "list":
            return store.claims()
        if args.action == "show":
            return store.claim(args.code)
        return store.claim_status(args.code, args.status, args.amount, args.date)
    if args.command == "due":
        return store.due(args.as_of)
    if args.command == "stats":
        return store.stats(args.year)
    if args.command == "travel-days":
        return store.travel_days(args.year)
    if args.command == "export":
        return store.export(args.format, args.out, args.kind, args.yes, not args.no_bom)
    if args.command == "import":
        return store.import_archive(args.path)
    if args.command == "backup":
        return store.backup(args.out)
    raise ClaimError("INVALID_ARGUMENT", "Unknown command")


def main(argv: list[str] | None = None) -> int:
    args, store = None, None
    try:
        args = parser().parse_args(globals_first(list(sys.argv[1:] if argv is None else argv)))
        root, config = settings(args.data_dir, args.config)
        if args.command == "mcp":
            from .mcp_server import create_server

            create_server(root, config).run(transport="stdio")
            return 0
        if args.command == "serve":
            from .web.server import serve

            serve(root, config, args.host, args.port, args.quiet)
            return 0
        store = Store(root, config)
        if args.progress and not args.quiet:
            print(f"Running {args.command} locally", file=sys.stderr)
        result, status = success(dispatch(args, store)), 0
    except Exception as exc:
        result, status = failure(exc)
    finally:
        if store:
            store.close()
    print(json.dumps(result, ensure_ascii=False, indent=None if args and args.json else 2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
