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
    sub.add_parser("create-backup", help="Trigger an immediate Anki backup")
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
    p_note.add_argument(
        "--stable-guid",
        default=None,
        help="Stable GUID tag (anki-manager::...). Derived from source+front fields if omitted.",
    )
    p_note.add_argument(
        "--dry-run", action="store_true",
        help="Validate (schema, allowlist, GUID collision) without writing.",
    )

    p_update = sub.add_parser("update-note", help="Update a note's fields by stable GUID")
    p_update.add_argument("--stable-guid", required=True)
    p_update.add_argument(
        "--field", action="append", required=True, type=_parse_field,
        help="Field as NAME=VALUE; only listed fields are updated.",
    )
    p_update.add_argument(
        "--dry-run", action="store_true",
        help="Look up the note without applying the update.",
    )

    p_upsert = sub.add_parser("upsert-note", help="Add the note if missing, update fields if present")
    p_upsert.add_argument("--deck", required=True)
    p_upsert.add_argument("--model", required=True)
    p_upsert.add_argument("--field", action="append", required=True, type=_parse_field)
    p_upsert.add_argument("--tag", action="append", default=[])
    p_upsert.add_argument("--stable-guid", default=None)
    p_upsert.add_argument(
        "--dry-run", action="store_true",
        help="Validate without writing; 'created' reflects what would have happened.",
    )

    p_find = sub.add_parser("find-by-guid", help="Print the note_id for a stable GUID, or 'null'")
    p_find.add_argument("stable_guid")

    p_perm = sub.add_parser("permissions", help="Inspect or mutate the allowlist")
    perm_sub = p_perm.add_subparsers(dest="perm_cmd", required=True)

    p_show = perm_sub.add_parser("show", help="Print the effective allowlist as JSON")
    p_show.add_argument("--agent", default=None, help="Show as if running as this agent's name")

    def add_target(p: argparse.ArgumentParser) -> None:
        grp = p.add_mutually_exclusive_group()
        grp.add_argument("--agent", default=None, help="Section name (defaults to invoker's agent)")
        grp.add_argument("--universal", action="store_true", help="Operate on the universal section")

    p_perm_add = perm_sub.add_parser("add", help="Append a pattern (sudo/polkit gated)")
    p_perm_add.add_argument("--pattern", required=True)
    add_target(p_perm_add)

    p_perm_rm = perm_sub.add_parser("remove", help="Remove a pattern (sudo/polkit gated)")
    p_perm_rm.add_argument("--pattern", required=True)
    add_target(p_perm_rm)

    p_grant = perm_sub.add_parser("grant-new", help="Grant <new> capability to an agent")
    p_grant.add_argument("--agent", default=None)

    p_revoke = perm_sub.add_parser("revoke-new", help="Revoke <new> capability from an agent")
    p_revoke.add_argument("--agent", default=None)

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
        case "create-backup":
            mgr.create_backup()
            print("backup created")
        case "list-models":
            print(json.dumps(mgr.list_models(), indent=2))
        case "add-deck":
            deck_id = mgr.add_deck(args.name)
            print(deck_id)
        case "add-note":
            result = mgr.add_note(
                deck=args.deck,
                model=args.model,
                fields=dict(args.field),
                tags=args.tag or None,
                stable_guid=args.stable_guid,
                dry_run=args.dry_run,
            )
            print(json.dumps({
                "note_id": result.note_id,
                "stable_guid": result.stable_guid,
                "dry_run": result.dry_run,
            }))
        case "update-note":
            note_id = mgr.update_note(args.stable_guid, dict(args.field), dry_run=args.dry_run)
            print(json.dumps({"note_id": note_id, "dry_run": args.dry_run}))
        case "upsert-note":
            result = mgr.upsert_note(
                deck=args.deck,
                model=args.model,
                fields=dict(args.field),
                tags=args.tag or None,
                stable_guid=args.stable_guid,
                dry_run=args.dry_run,
            )
            print(json.dumps({
                "note_id": result.note_id,
                "stable_guid": result.stable_guid,
                "created": result.created,
                "dry_run": result.dry_run,
            }))
        case "find-by-guid":
            note_id = mgr.find_by_guid(args.stable_guid)
            print(json.dumps(note_id))
        case "permissions":
            return _dispatch_permissions(mgr, args)
    return 0


def _resolve_section(mgr: AnkiManager, args: argparse.Namespace) -> str:
    if getattr(args, "universal", False):
        return "universal"
    if args.agent is not None:
        return args.agent
    agent = mgr._get_agent()  # noqa: SLF001  — small CLI helper
    if agent is None:
        raise SystemExit("error: no agent section claims this user; pass --agent or --universal")
    return agent.name


def _dispatch_permissions(mgr: AnkiManager, args: argparse.Namespace) -> int:
    from . import permissions

    match args.perm_cmd:
        case "show":
            allowlist = mgr._get_allowlist()  # noqa: SLF001
            if args.agent:
                agent = allowlist.agents.get(args.agent)
            else:
                agent = mgr._get_agent()  # noqa: SLF001
            print(json.dumps({
                "agent": agent.name if agent else None,
                "has_new": allowlist.has_new_capability(agent),
                "patterns": list(allowlist.effective_patterns(agent)),
                "universal": list(allowlist.universal),
                "agent_patterns": list(agent.patterns) if agent else [],
            }, indent=2))
        case "add":
            section = _resolve_section(mgr, args)
            permissions.add_pattern(section, args.pattern)
            print(f"added {args.pattern!r} to [{section}]")
        case "remove":
            section = _resolve_section(mgr, args)
            permissions.remove_pattern(section, args.pattern)
            print(f"removed {args.pattern!r} from [{section}]")
        case "grant-new":
            section = _resolve_section(mgr, args)
            permissions.grant_new(section)
            print(f"granted <new> to [{section}]")
        case "revoke-new":
            section = _resolve_section(mgr, args)
            permissions.revoke_new(section)
            print(f"revoked <new> from [{section}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
