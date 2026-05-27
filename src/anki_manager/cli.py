"""anki-manager CLI — thin wrapper around AnkiManager."""

from __future__ import annotations

import argparse
import json
import sys

from .errors import AnkiManagerError
from .manager import AnkiManager


def _parse_field(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"--field expects NAME=VALUE, got {spec!r}")
    name, _, value = spec.partition("=")
    return name, value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anki-manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("start", help="Start the kryshanti-anki unit and wait until ready")
    sub.add_parser("stop", help="Stop the kryshanti-anki unit")
    sub.add_parser("restart", help="Restart the unit and wait until ready")
    sub.add_parser("status", help="Print active/ready/sub_state as JSON")
    sub.add_parser("sync", help="Sync the local collection with AnkiWeb")
    sub.add_parser("force-upload", help="Upload local collection to AnkiWeb (FULL_SYNC)")
    sub.add_parser("force-download", help="Download AnkiWeb collection (FULL_SYNC)")
    sub.add_parser("list-models", help="Print {model: [fields]} as JSON")

    p_deck = sub.add_parser("add-deck", help="Create a deck if it does not exist")
    p_deck.add_argument("name", help="Deck name (e.g. 'Myrzka::Daily')")

    p_note = sub.add_parser("add-note", help="Add a note to a deck")
    p_note.add_argument("--deck", required=True)
    p_note.add_argument("--model", required=True)
    p_note.add_argument(
        "--field",
        action="append",
        required=True,
        type=_parse_field,
        help="Field as NAME=VALUE; repeat for each field on the model.",
    )
    p_note.add_argument("--tag", action="append", default=[], help="Tag to apply (repeatable)")

    args = parser.parse_args(argv)
    mgr = AnkiManager()

    try:
        return _dispatch(mgr, args)
    except AnkiManagerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(mgr: AnkiManager, args: argparse.Namespace) -> int:
    match args.cmd:
        case "start":
            mgr.ensure_running()
            print("started")
        case "stop":
            mgr.stop()
            print("stopped")
        case "restart":
            mgr.restart()
            print("restarted")
        case "status":
            s = mgr.status()
            print(json.dumps({
                "active": s.active,
                "ready": s.ready,
                "sub_state": s.sub_state,
            }))
        case "sync":
            mgr.sync()
            print("synced")
        case "force-upload":
            mgr.force_upload()
            print("uploaded")
        case "force-download":
            mgr.force_download()
            print("downloaded")
        case "list-models":
            print(json.dumps(mgr.list_models(), indent=2))
        case "add-deck":
            deck_id = mgr.add_deck(args.name)
            print(deck_id)
        case "add-note":
            fields = dict(args.field)
            note_id = mgr.add_note(
                deck=args.deck,
                model=args.model,
                fields=fields,
                tags=args.tag or None,
            )
            print(note_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
