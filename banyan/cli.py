"""Command-line interface for banyan."""

from __future__ import annotations

import argparse
from pathlib import Path
import pickle
import sqlite3
import sys
import xml.etree.ElementTree as ET
from typing import Any

from banyan.gramps import export_gramps, import_gramps_db, import_gramps_xml
from banyan.model import (
    VALID_GENDERS,
    add_child,
    add_person,
    add_spouse,
    find_birth_family,
    find_parent_families,
    find_relationship_path,
    format_person_brief,
    load_tree,
    save_tree,
    search_people,
    update_person,
    validate_tree,
)
from banyan.render import render_html


def _finish_import(data: dict[str, Any], out: Path) -> int:
    """Save an imported tree, validate it, print diagnostics, and return exit code."""
    save_tree(out, data, touch_updated=False)
    errors, warnings = validate_tree(data)
    print(f"Imported {len(data['people'])} people and {len(data['families'])} families -> {out}")
    for w in warnings:
        print(f"  WARN: {w}")
    for e in errors:
        print(f"  ERROR: {e}")
    return 1 if errors else 0


def cmd_import_gramps_db(args: argparse.Namespace) -> int:
    try:
        data = import_gramps_db(args.db_file, title=args.title)
    except (ValueError, sqlite3.Error, pickle.UnpicklingError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    out = Path(args.output) if args.output else Path(args.db_file).with_suffix(".json")
    return _finish_import(data, out)


def cmd_import_gramps_xml(args: argparse.Namespace) -> int:
    try:
        data = import_gramps_xml(args.xml_file, title=args.title)
    except (ValueError, ET.ParseError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    out = Path(args.output) if args.output else Path(args.xml_file).with_suffix(".json")
    return _finish_import(data, out)


def cmd_export_gramps(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    export_gramps(data, args.output)
    print(f"Exported {len(data['people'])} people and {len(data['families'])} families -> {args.output}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    errors, warnings = validate_tree(data)
    if not errors and not warnings:
        print(f"OK: {args.json_file} is valid ({len(data['people'])} people, {len(data['families'])} families).")
        return 0
    for w in warnings:
        print(f"WARN: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    return 1 if errors else 0


def cmd_render(args: argparse.Namespace) -> int:
    json_path = Path(args.json_file)
    data = load_tree(json_path)
    errors, _ = validate_tree(data)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    out = Path(args.output) if args.output else json_path.with_suffix(".html")
    json_url = args.json_url or json_path.name
    render_html(data, out, title=args.title, json_url=json_url, embed=args.embed)
    mode = "embedded JSON" if args.embed else f"fetches {json_url}"
    print(f"Rendered interactive HTML ({len(data['people'])} people, {mode}) -> {out}")
    if (
        not args.embed
        and not args.json_url
        and out.resolve().parent != json_path.resolve().parent
        and not (out.parent / json_url).exists()
    ):
        print(f"  WARN: '{out}' expects '{json_url}' in '{out.parent}'.")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    people = data["people"]
    families = data["families"]
    with_birth = sum(1 for p in people.values() if p.get("birth"))
    with_death = sum(1 for p in people.values() if p.get("death"))
    with_nick = sum(1 for p in people.values() if p.get("nick"))
    with_note = sum(1 for p in people.values() if p.get("note"))

    print(f"Title:    {data['meta'].get('title')}")
    print(f"Updated:  {data['meta'].get('updated')}")
    print(f"People:   {len(people)} (birth year: {with_birth}, death year: {with_death}, nickname: {with_nick}, note: {with_note})")
    print(f"Families: {len(families)}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    matches = search_people(data, args.query)
    if not matches:
        print(f"No matches for '{args.query}'.")
        return 0
    for pid, p in matches:
        note_str = f" — Note: {p['note']}" if p.get("note") else ""
        print(f"- {format_person_brief(pid, p)}{note_str}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    people = data["people"]
    pid = args.person_id
    if pid not in people:
        print(f"Person '{pid}' not found.", file=sys.stderr)
        return 1
    p = people[pid]
    print(f"Person:   {format_person_brief(pid, p)}")
    print(f"Gender:   {p.get('gender', 'Unknown')}")
    if p.get("note"):
        print(f"Note:     {p['note']}")

    birth_fam = find_birth_family(data, pid)
    if birth_fam is not None:
        print(f"Parents (family {birth_fam['id']}):")
        if birth_fam.get("father") in people:
            print(f"  Father: {format_person_brief(birth_fam['father'], people[birth_fam['father']])}")
        if birth_fam.get("mother") in people:
            print(f"  Mother: {format_person_brief(birth_fam['mother'], people[birth_fam['mother']])}")
        sibs = [c for c in (birth_fam.get("children") or []) if c != pid and c in people]
        if sibs:
            print("  Siblings:")
            for s in sibs:
                print(f"    - {format_person_brief(s, people[s])}")

    for fam in find_parent_families(data, pid):
        sp = fam.get("mother") if fam.get("father") == pid else fam.get("father")
        print(f"Family ({fam['id']}):")
        if fam.get("married"):
            print(f"  Married: {fam['married']}")
        if sp and sp in people:
            print(f"  Spouse: {format_person_brief(sp, people[sp])}")
        for ch in fam.get("children") or []:
            if ch in people:
                print(f"  Child:  {format_person_brief(ch, people[ch])}")
    return 0


def cmd_relate(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    people = data["people"]
    path = find_relationship_path(data, args.person_a, args.person_b)
    if path is None:
        print(f"No relationship path found between '{args.person_a}' and '{args.person_b}'.")
        return 1
    if not path:
        print("Same person.")
        return 0
    print(f"Relationship path ({len(path)} steps):")
    for idx, (u, rel, v) in enumerate(path, 1):
        print(f"  {idx}. {format_person_brief(u, people[u])} -> {rel} -> {format_person_brief(v, people[v])}")
    return 0


def cmd_add_person(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    try:
        pid, entry = add_person(
            data,
            first=args.first,
            person_id=args.id,
            surname=args.surname,
            nick=args.nick,
            gender=args.gender,
            birth=args.birth,
            death=args.death,
            note=args.note,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    save_tree(args.json_file, data)
    print(f"Added {format_person_brief(pid, entry)}")
    return 0


def cmd_update_person(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    try:
        updated = update_person(
            data,
            args.person_id,
            first=args.first,
            surname=args.surname,
            nick=args.nick,
            gender=args.gender,
            birth=args.birth,
            death=args.death,
            note=args.note,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    save_tree(args.json_file, data)
    print(f"Updated {format_person_brief(args.person_id, updated)}")
    return 0


def cmd_add_spouse(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    try:
        fam, created = add_spouse(data, args.person_a, args.person_b, married=args.married)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    save_tree(args.json_file, data)
    if created:
        print(f"Created family '{fam['id']}' linking {fam['father']} and {fam['mother']}.")
    elif fam.get("married") and args.married is not None:
        print(f"Updated marriage date on family '{fam['id']}' ({fam['married']}).")
    else:
        print(f"Family '{fam['id']}' already links '{args.person_a}' and '{args.person_b}'.")
    return 0


def cmd_add_child(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    try:
        fam = add_child(
            data,
            args.child_id,
            father=args.father,
            mother=args.mother,
            family_id=args.family,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    save_tree(args.json_file, data)
    print(f"Added child '{args.child_id}' to family '{fam['id']}'.")
    return 0


def _add_person_field_args(parser: argparse.ArgumentParser, *, allow_clear: bool) -> None:
    """Register shared person attribute flags on `add-person` and `update-person` parsers."""
    clear_hint = " (pass '' to clear)" if allow_clear else ""
    gender_choices = list(VALID_GENDERS) + ([""] if allow_clear else [])
    parser.add_argument("--first", required=not allow_clear, help="First name")
    parser.add_argument("--surname", help=f"Surname{clear_hint}")
    parser.add_argument("--nick", help=f"Nickname{clear_hint}")
    parser.add_argument("--gender", choices=gender_choices, help=f"Gender ({'/'.join(VALID_GENDERS)})")
    parser.add_argument("--birth", help=f"Birth year or date{clear_hint}")
    parser.add_argument("--death", help=f"Death year or date{clear_hint}")
    parser.add_argument("--note", help=f"Biographical note{clear_hint}")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for banyan."""
    parser = argparse.ArgumentParser(
        prog="banyan",
        description="Manage JSON family trees, convert Gramps XML or SQLite DBs, and render single-page HTML visualizations.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_imp_db = sub.add_parser(
        "import-gramps-db",
        aliases=["import_gramps_db"],
        help="Import a Gramps SQLite database file (sqlite.db) into JSON",
    )
    p_imp_db.add_argument("db_file", help="Path to sqlite.db file")
    p_imp_db.add_argument("-o", "--output", help="Path to output .json file")
    p_imp_db.add_argument("--title", help="Tree title stored in meta.title")
    p_imp_db.set_defaults(func=cmd_import_gramps_db)

    p_imp_xml = sub.add_parser(
        "import-gramps-xml",
        aliases=["import_gramps_xml"],
        help="Import a Gramps XML (.gramps) file into JSON",
    )
    p_imp_xml.add_argument("xml_file", help="Path to .gramps XML file")
    p_imp_xml.add_argument("-o", "--output", help="Path to output .json file")
    p_imp_xml.add_argument("--title", help="Tree title stored in meta.title")
    p_imp_xml.set_defaults(func=cmd_import_gramps_xml)

    p_exp = sub.add_parser("export-gramps", help="Export a JSON tree back to .gramps XML")
    p_exp.add_argument("json_file", help="Path to input .json file")
    p_exp.add_argument("-o", "--output", required=True, help="Path to output .gramps file")
    p_exp.set_defaults(func=cmd_export_gramps)

    p_val = sub.add_parser("validate", help="Validate referential integrity of a JSON tree")
    p_val.add_argument("json_file", help="Path to .json file")
    p_val.set_defaults(func=cmd_validate)

    p_ren = sub.add_parser("render", help="Generate single-page interactive HTML from JSON")
    p_ren.add_argument("json_file", help="Path to .json file")
    p_ren.add_argument("-o", "--output", help="Path to output .html file")
    p_ren.add_argument("--title", help="Override page title")
    p_ren.add_argument("--json-url", help="Relative filename or URL of JSON to fetch (default: input JSON basename)")
    p_ren.add_argument("--embed", action="store_true", help="Embed JSON inline for standalone file:// viewing")
    p_ren.set_defaults(func=cmd_render)

    p_st = sub.add_parser("stats", help="Display summary statistics for a JSON tree")
    p_st.add_argument("json_file", help="Path to .json file")
    p_st.set_defaults(func=cmd_stats)

    p_sr = sub.add_parser("search", help="Search people by name, nickname, year, or note")
    p_sr.add_argument("json_file", help="Path to .json file")
    p_sr.add_argument("query", help="Search query string")
    p_sr.set_defaults(func=cmd_search)

    p_sh = sub.add_parser("show", help="Show a person's full details and immediate family")
    p_sh.add_argument("json_file", help="Path to .json file")
    p_sh.add_argument("person_id", help="Person slug ID")
    p_sh.set_defaults(func=cmd_show)

    p_rel = sub.add_parser("relate", help="Find shortest relationship path between two people")
    p_rel.add_argument("json_file", help="Path to .json file")
    p_rel.add_argument("person_a", help="First person slug ID")
    p_rel.add_argument("person_b", help="Second person slug ID")
    p_rel.set_defaults(func=cmd_relate)

    p_addp = sub.add_parser("add-person", help="Add a new person to the JSON tree")
    p_addp.add_argument("json_file", help="Path to .json file")
    _add_person_field_args(p_addp, allow_clear=False)
    p_addp.add_argument("--id", help="Explicit slug ID")
    p_addp.set_defaults(func=cmd_add_person)

    p_updp = sub.add_parser("update-person", help="Update fields on an existing person")
    p_updp.add_argument("json_file", help="Path to .json file")
    p_updp.add_argument("person_id", help="Person slug ID")
    _add_person_field_args(p_updp, allow_clear=True)
    p_updp.set_defaults(func=cmd_update_person)

    p_sp = sub.add_parser("add-spouse", help="Link two people as spouses in a family")
    p_sp.add_argument("json_file", help="Path to .json file")
    p_sp.add_argument("person_a", help="First spouse slug ID")
    p_sp.add_argument("person_b", help="Second spouse slug ID")
    p_sp.add_argument("--married", help="Optional marriage year or date")
    p_sp.set_defaults(func=cmd_add_spouse)

    p_ch = sub.add_parser("add-child", help="Add a child to a family (by parents or family ID)")
    p_ch.add_argument("json_file", help="Path to .json file")
    p_ch.add_argument("child_id", help="Child person slug ID")
    p_ch.add_argument("--father", help="Father slug ID")
    p_ch.add_argument("--mother", help="Mother slug ID")
    p_ch.add_argument("--family", help="Explicit family ID")
    p_ch.set_defaults(func=cmd_add_child)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
