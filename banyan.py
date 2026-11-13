#!/usr/bin/env python3
"""
banyan — Lightweight CLI for managing family trees in JSON, converting from/to
Gramps XML (.gramps) or Gramps SQLite databases (sqlite.db), querying/mutating
via AI chat, and rendering a self-contained interactive single-page HTML visualization.

Zero external dependencies (Python 3 standard library only).
"""

from __future__ import annotations

import argparse
import builtins
from collections import deque
from datetime import date, datetime
import gzip
import html
import io
import json
from pathlib import Path
import pickle
import re
import sqlite3
import sys
from typing import Any
import xml.etree.ElementTree as ET

GRAMPS_NS = "http://gramps-project.org/xml/1.7.1/"
NS = {"g": GRAMPS_NS}

# Gramps EventType integer codes (from gramps.gen.lib.eventtype)
_GRAMPS_EVENT_TYPES: dict[int, str] = {
    -1: "Unknown",
    0: "Custom",
    1: "Marriage",
    2: "Marriage Settlement",
    3: "Marriage License",
    4: "Marriage Contract",
    5: "Marriage Banns",
    6: "Engagement",
    7: "Divorce",
    8: "Divorce Filing",
    9: "Annulment",
    10: "Alternate Marriage",
    11: "Adopted",
    12: "Birth",
    13: "Death",
    14: "Adult Christening",
    15: "Baptism",
    16: "Bar Mitzvah",
    17: "Bat Mitzvah",
    18: "Blessing",
    19: "Burial",
    20: "Cremation",
    21: "Confirmation",
    22: "First Communion",
    23: "Christening",
}


class _GrampsSafeUnpickler(pickle.Unpickler):
    """Restrict unpickling of Gramps SQLite blobs to harmless built-in containers."""

    _SAFE_BUILTINS = frozenset({"set", "frozenset"})

    def find_class(self, module: str, name: str) -> Any:
        if module == "builtins" and name in self._SAFE_BUILTINS:
            return getattr(builtins, name)
        raise pickle.UnpicklingError(f"Disallowed pickle class in Gramps blob: {module}.{name}")


def _safe_unpickle(blob: bytes) -> Any:
    """Safely deserialize a pickled tuple blob from a Gramps SQLite database."""
    return _GrampsSafeUnpickler(io.BytesIO(blob)).load()


def extract_year(val: str | None) -> int | None:
    """Extract the first 4-digit year from a date string like '1996' or '1996-02-28'."""
    if not val:
        return None
    m = re.search(r"\b(\d{4})\b", str(val))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Slug & ID helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """Convert a string into a lowercase kebab-case slug."""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "person"


def make_person_slug(
    first: str,
    surname: str,
    nick: str,
    birth: str,
    existing: set[str],
    has_name_collision: bool = False,
) -> str:
    """Create a readable, unique slug ID for a person."""
    parts = [p for p in (first.strip(), surname.strip()) if p]
    base = slugify(" ".join(parts)) if parts else "person"

    if not has_name_collision and base not in existing:
        return base

    yr = extract_year(birth)
    if yr is not None:
        cand = f"{base}-{yr}"
        if cand not in existing:
            return cand
    if nick:
        cand = f"{base}-{slugify(nick)}"
        if cand not in existing:
            return cand
    if base not in existing:
        return base

    idx = 2
    while f"{base}-{idx}" in existing:
        idx += 1
    return f"{base}-{idx}"


def make_family_id(
    father: str | None,
    mother: str | None,
    children: list[str],
    existing: set[str],
) -> str:
    """Create a readable, unique ID for a family unit."""
    if father and mother:
        base = f"{father}+{mother}"
    elif father:
        base = f"{father}-family"
    elif mother:
        base = f"{mother}-family"
    elif children:
        base = "siblings-" + "+".join(children[:2])
    else:
        base = "family"

    if base not in existing:
        return base
    idx = 2
    while f"{base}-{idx}" in existing:
        idx += 1
    return f"{base}-{idx}"


# ---------------------------------------------------------------------------
# File I/O & Normalization
# ---------------------------------------------------------------------------

def load_tree(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return normalize_tree(data)


def save_tree(path: str | Path, data: dict[str, Any], *, touch_updated: bool = True) -> None:
    norm = normalize_tree(data)
    if touch_updated:
        norm.setdefault("meta", {})["updated"] = date.today().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(norm, f, indent=2, ensure_ascii=False)
        f.write("\n")


def normalize_tree(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure clean ordering of keys and omit empty fields on people/families."""
    meta = dict(data.get("meta") or {})
    if "title" not in meta:
        meta["title"] = "Family Tree"
    if "updated" not in meta:
        meta["updated"] = date.today().isoformat()

    clean_people: dict[str, dict[str, Any]] = {}
    for pid, p in (data.get("people") or {}).items():
        cp: dict[str, Any] = {"first": str(p.get("first") or "").strip()}
        for field in ("surname", "nick", "gender", "birth", "death", "note"):
            val = p.get(field)
            if val is not None and str(val).strip() != "":
                cp[field] = str(val).strip()
        clean_people[pid] = cp

    clean_families: list[dict[str, Any]] = []
    seen_fids: set[str] = set()
    for fam in data.get("families") or []:
        father = fam.get("father") or None
        mother = fam.get("mother") or None
        married = fam.get("married")
        children = [str(c) for c in (fam.get("children") or []) if c]
        fid = fam.get("id")
        if not fid or fid in seen_fids:
            fid = make_family_id(father, mother, children, seen_fids)
        seen_fids.add(fid)
        cf: dict[str, Any] = {"id": fid}
        if father:
            cf["father"] = father
        if mother:
            cf["mother"] = mother
        if married is not None and str(married).strip() != "":
            cf["married"] = str(married).strip()
        cf["children"] = children
        clean_families.append(cf)

    return {
        "meta": meta,
        "people": clean_people,
        "families": clean_families,
    }


# ---------------------------------------------------------------------------
# Gramps Import (XML & SQLite sqlite.db) & Export
# ---------------------------------------------------------------------------

def _build_tree_from_raw(
    raw_people: list[dict[str, Any]],
    raw_families: list[dict[str, Any]],
    title: str,
    updated_date: str,
) -> dict[str, Any]:
    """Convert raw person/family records with opaque handles into canonical banyan JSON."""
    name_counts: dict[str, int] = {}
    for rp in raw_people:
        base_key = slugify(f"{rp['first']} {rp['surname']}".strip())
        rp["base_key"] = base_key
        name_counts[base_key] = name_counts.get(base_key, 0) + 1

    handle_to_slug: dict[str, str] = {}
    people_dict: dict[str, dict[str, Any]] = {}
    used_slugs: set[str] = set()

    for rp in raw_people:
        has_collision = name_counts.get(rp["base_key"], 0) > 1
        slug = make_person_slug(
            rp["first"],
            rp["surname"],
            rp["nick"],
            rp["birth"],
            used_slugs,
            has_name_collision=has_collision,
        )
        used_slugs.add(slug)
        handle_to_slug[rp["handle"]] = slug

        entry: dict[str, Any] = {"first": rp["first"]}
        for field in ("surname", "nick", "gender", "birth", "death", "note"):
            if rp.get(field):
                entry[field] = rp[field]
        people_dict[slug] = entry

    families_list: list[dict[str, Any]] = []
    used_fids: set[str] = set()
    for rf in raw_families:
        father_slug = handle_to_slug.get(rf.get("father_handle") or "")
        mother_slug = handle_to_slug.get(rf.get("mother_handle") or "")
        children_slugs = [
            handle_to_slug[ch]
            for ch in (rf.get("child_handles") or [])
            if ch in handle_to_slug
        ]
        fid = make_family_id(father_slug, mother_slug, children_slugs, used_fids)
        used_fids.add(fid)
        f_entry: dict[str, Any] = {"id": fid}
        if father_slug:
            f_entry["father"] = father_slug
        if mother_slug:
            f_entry["mother"] = mother_slug
        if rf.get("married"):
            f_entry["married"] = rf["married"]
        f_entry["children"] = children_slugs
        families_list.append(f_entry)

    return normalize_tree({
        "meta": {
            "title": title,
            "updated": updated_date,
        },
        "people": people_dict,
        "families": families_list,
    })


def _fmt_dmy(day: Any, month: Any, year: Any) -> str:
    """Format (day, month, year) integers into ISO date string 'YYYY[-MM[-DD]]'."""
    y = int(year or 0)
    m = int(month or 0)
    d = int(day or 0)
    if y and m and d:
        return f"{y:04d}-{m:02d}-{d:02d}"
    if y and m:
        return f"{y:04d}-{m:02d}"
    if y:
        return str(y)
    return ""


def _format_gramps_date(dt: Any) -> str:
    """Format a Gramps serialized Date tuple into an ISO or modified date string."""
    if not isinstance(dt, (tuple, list)) or len(dt) < 5:
        return ""
    modifier = int(dt[1] or 0)
    dateval = dt[3] or ()
    text = str(dt[4] or "").strip()

    if modifier in (4, 5) and isinstance(dateval, (tuple, list)) and len(dateval) >= 8:
        start = _fmt_dmy(dateval[0], dateval[1], dateval[2])
        end = _fmt_dmy(dateval[4], dateval[5], dateval[6])
        if start and end:
            return f"{start}–{end}"
        return start or end or text

    base = ""
    if isinstance(dateval, (tuple, list)) and len(dateval) >= 3:
        base = _fmt_dmy(dateval[0], dateval[1], dateval[2])
    if not base:
        return text

    prefix = {1: "before ", 2: "after ", 3: "about "}.get(modifier, "")
    return f"{prefix}{base}"


def _resolve_event_type(type_field: Any) -> str:
    """Decode a Gramps EventType tuple or int code into its canonical name."""
    code: int | None = None
    custom = ""
    if isinstance(type_field, (tuple, list)) and len(type_field) >= 2:
        code = int(type_field[0]) if type_field[0] is not None else None
        custom = str(type_field[1] or "").strip()
    elif isinstance(type_field, int):
        code = type_field
    elif isinstance(type_field, str):
        return type_field.strip()

    if custom:
        return custom
    if code is not None:
        return _GRAMPS_EVENT_TYPES.get(code, "")
    return ""


def _extract_surname(surname_list: Any) -> str:
    """Build display surname from Gramps Surname tuple entries."""
    if not surname_list:
        return ""
    primary_parts: list[str] = []
    fallback_parts: list[str] = []
    for s in surname_list:
        if not isinstance(s, (tuple, list)) or len(s) < 3:
            continue
        sur = str(s[0] or "").strip()
        prefix = str(s[1] or "").strip()
        combined = " ".join(part for part in (prefix, sur) if part)
        if combined:
            if bool(s[2]):
                primary_parts.append(combined)
            else:
                fallback_parts.append(combined)
    return " ".join(primary_parts or fallback_parts)


def import_gramps_db(db_path: str | Path, title: str | None = None) -> dict[str, Any]:
    """Import a Gramps SQLite database file (`sqlite.db`) directly into banyan JSON."""
    resolved = Path(db_path).resolve()
    if not resolved.is_file():
        raise ValueError(f"SQLite file not found: '{db_path}'.")

    con = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        max_change = 0

        def track_change(val: Any) -> None:
            nonlocal max_change
            if isinstance(val, int) and val > max_change:
                max_change = val

        # 1. Events
        events: dict[str, tuple[str, str]] = {}
        for (blob,) in cur.execute("SELECT blob_data FROM event"):
            if blob is None:
                continue
            obj = _safe_unpickle(blob)
            handle = str(obj[0] or "")
            ev_type = _resolve_event_type(obj[2])
            val = _format_gramps_date(obj[3])
            if len(obj) > 10:
                track_change(obj[10])
            if handle and ev_type and val:
                events[handle] = (ev_type, val)

        # 2. Notes
        notes: dict[str, str] = {}
        for (blob,) in cur.execute("SELECT blob_data FROM note"):
            if blob is None:
                continue
            obj = _safe_unpickle(blob)
            handle = str(obj[0] or "")
            text_tuple = obj[2] if len(obj) > 2 else ""
            text = str(text_tuple[0] if isinstance(text_tuple, (tuple, list)) else (text_tuple or "")).strip()
            if len(obj) > 5:
                track_change(obj[5])
            if handle and text:
                notes[handle] = text

        # 3. People
        raw_people: list[dict[str, Any]] = []
        for (blob,) in cur.execute("SELECT blob_data FROM person ORDER BY gramps_id, handle"):
            if blob is None:
                continue
            obj = _safe_unpickle(blob)
            handle = str(obj[0] or "")
            gender = {0: "F", 1: "M"}.get(obj[2], "U")
            pn = obj[3]
            first = str(pn[4] or "").strip() if len(pn) > 4 else ""
            surname = _extract_surname(pn[5] if len(pn) > 5 else [])
            nick = str(pn[13] or "").strip() if len(pn) > 13 else ""
            death_idx = obj[5] if len(obj) > 5 else -1
            birth_idx = obj[6] if len(obj) > 6 else -1
            erefs = [str(er[3]) for er in (obj[7] or []) if len(er) > 3]
            nrefs = [str(n) for n in (obj[16] or [])]
            if len(obj) > 17:
                track_change(obj[17])

            birth = ""
            death = ""
            if isinstance(birth_idx, int) and 0 <= birth_idx < len(erefs):
                eh = erefs[birth_idx]
                if eh in events:
                    birth = events[eh][1]
            if isinstance(death_idx, int) and 0 <= death_idx < len(erefs):
                eh = erefs[death_idx]
                if eh in events:
                    death = events[eh][1]
            for eh in erefs:
                if eh in events:
                    etype, eval_str = events[eh]
                    if etype == "Birth" and not birth:
                        birth = eval_str
                    elif etype == "Death" and not death:
                        death = eval_str

            person_notes = [notes[nh] for nh in nrefs if nh in notes]
            raw_people.append({
                "handle": handle,
                "first": first,
                "surname": surname,
                "nick": nick,
                "gender": gender,
                "birth": birth,
                "death": death,
                "note": "\n\n".join(person_notes),
            })

        # 4. Families
        raw_families: list[dict[str, Any]] = []
        for (blob,) in cur.execute("SELECT blob_data FROM family ORDER BY gramps_id, handle"):
            if blob is None:
                continue
            obj = _safe_unpickle(blob)
            father_handle = obj[2] if len(obj) > 2 else None
            mother_handle = obj[3] if len(obj) > 3 else None
            child_handles = [str(cr[3]) for cr in (obj[4] or []) if len(cr) > 3]
            erefs = [str(er[3]) for er in (obj[6] or []) if len(er) > 3]
            if len(obj) > 12:
                track_change(obj[12])

            married = ""
            for eh in erefs:
                if eh in events and events[eh][0] == "Marriage" and not married:
                    married = events[eh][1]

            raw_families.append({
                "father_handle": father_handle,
                "mother_handle": mother_handle,
                "child_handles": child_handles,
                "married": married,
            })
    finally:
        con.close()

    updated_date = (
        datetime.fromtimestamp(max_change).date().isoformat()
        if max_change > 0
        else date.today().isoformat()
    )
    resolved_title = title
    if not resolved_title:
        name_file = resolved.parent / "name.txt"
        if name_file.is_file():
            resolved_title = name_file.read_text(encoding="utf-8", errors="replace").strip() or None
    return _build_tree_from_raw(raw_people, raw_families, resolved_title or "Family Tree", updated_date)


def read_gramps_xml(path: str | Path) -> ET.Element:
    """Read a .gramps file (supports both plain XML and gzip-compressed XML)."""
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return ET.fromstring(raw.decode("utf-8"))


def import_gramps_xml(path: str | Path, title: str | None = None) -> dict[str, Any]:
    """Import a plain or gzip-compressed Gramps XML (.gramps) file into banyan JSON."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"XML file not found: '{path}'.")
    root = read_gramps_xml(p)
    ns_uri = root.tag.split("}")[0].strip("{") if root.tag.startswith("{") else GRAMPS_NS
    ns = {"g": ns_uri}

    events: dict[str, tuple[str, str]] = {}
    for ev in root.findall("g:events/g:event", ns):
        handle = ev.attrib.get("handle", "")
        ev_type = (ev.findtext("g:type", "", ns) or "").strip()
        dateval = ev.find("g:dateval", ns)
        val = (dateval.attrib.get("val", "") if dateval is not None else "").strip()
        if handle and ev_type and val:
            events[handle] = (ev_type, val)

    notes: dict[str, str] = {}
    for nt in root.findall("g:notes/g:note", ns):
        handle = nt.attrib.get("handle", "")
        text = (nt.findtext("g:text", "", ns) or "").strip()
        if handle and text:
            notes[handle] = text

    raw_people: list[dict[str, Any]] = []
    for p in root.findall("g:people/g:person", ns):
        handle = p.attrib.get("handle", "")
        gender = (p.findtext("g:gender", "", ns) or "").strip()
        name_el = p.find("g:name", ns)
        first = (name_el.findtext("g:first", "", ns) if name_el is not None else "") or ""
        surname = (name_el.findtext("g:surname", "", ns) if name_el is not None else "") or ""
        nick = (name_el.findtext("g:nick", "", ns) if name_el is not None else "") or ""
        first, surname, nick = first.strip(), surname.strip(), nick.strip()

        birth = ""
        death = ""
        for er in p.findall("g:eventref", ns):
            hlink = er.attrib.get("hlink", "")
            if hlink in events:
                etype, eval_str = events[hlink]
                if etype == "Birth" and not birth:
                    birth = eval_str
                elif etype == "Death" and not death:
                    death = eval_str

        person_notes: list[str] = []
        for nr in p.findall("g:noteref", ns):
            hlink = nr.attrib.get("hlink", "")
            if hlink in notes:
                person_notes.append(notes[hlink])

        raw_people.append({
            "handle": handle,
            "first": first,
            "surname": surname,
            "nick": nick,
            "gender": gender,
            "birth": birth,
            "death": death,
            "note": "\n\n".join(person_notes),
        })

    raw_families: list[dict[str, Any]] = []
    for fam in root.findall("g:families/g:family", ns):
        fa_el = fam.find("g:father", ns)
        mo_el = fam.find("g:mother", ns)
        married = ""
        for er in fam.findall("g:eventref", ns):
            hlink = er.attrib.get("hlink", "")
            if hlink in events and events[hlink][0] == "Marriage" and not married:
                married = events[hlink][1]
        raw_families.append({
            "father_handle": fa_el.attrib.get("hlink", "") if fa_el is not None else None,
            "mother_handle": mo_el.attrib.get("hlink", "") if mo_el is not None else None,
            "child_handles": [c.attrib.get("hlink", "") for c in fam.findall("g:childref", ns)],
            "married": married,
        })

    created_el = root.find("g:header/g:created", ns)
    updated_date = (
        created_el.attrib.get("date")
        if created_el is not None and created_el.attrib.get("date")
        else date.today().isoformat()
    )
    resolved_title = title or Path(path).stem.replace("-", " ").replace("_", " ").title()
    return _build_tree_from_raw(raw_people, raw_families, resolved_title, updated_date)


def export_gramps(data: dict[str, Any], out_path: str | Path) -> None:
    """Export JSON tree back to Gramps XML 1.7.1 format."""
    tree = normalize_tree(data)
    people = tree["people"]
    families = tree["families"]

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE database PUBLIC "-//Gramps//DTD Gramps XML 1.7.1//EN"',
        '"http://gramps-project.org/xml/1.7.1/grampsxml.dtd">',
        f'<database xmlns="{GRAMPS_NS}">',
        "  <header>",
        f'    <created date="{html.escape(tree["meta"].get("updated", date.today().isoformat()))}" version="5.1.4"/>',
        "    <researcher/>",
        "  </header>",
    ]

    event_rows: list[tuple[str, str, str, str]] = []
    note_rows: list[tuple[str, str, str]] = []
    person_events: dict[str, list[str]] = {pid: [] for pid in people}
    person_notes: dict[str, list[str]] = {pid: [] for pid in people}
    family_events: dict[str, list[str]] = {}
    child_of: dict[str, list[str]] = {pid: [] for pid in people}
    parent_in: dict[str, list[str]] = {pid: [] for pid in people}

    ev_idx = 0
    nt_idx = 0
    for pid, p in people.items():
        if p.get("birth"):
            eh = f"_ev_birth_{ev_idx:04d}"
            eid = f"E{ev_idx:04d}"
            ev_idx += 1
            event_rows.append((eh, eid, "Birth", p["birth"]))
            person_events[pid].append(eh)
        if p.get("death"):
            eh = f"_ev_death_{ev_idx:04d}"
            eid = f"E{ev_idx:04d}"
            ev_idx += 1
            event_rows.append((eh, eid, "Death", p["death"]))
            person_events[pid].append(eh)
        if p.get("note"):
            nh = f"_nt_{nt_idx:04d}"
            nid = f"N{nt_idx:04d}"
            nt_idx += 1
            note_rows.append((nh, nid, p["note"]))
            person_notes[pid].append(nh)

    fam_handles: dict[str, str] = {}
    for idx, fam in enumerate(families):
        fh = f"_fam_{idx:04d}"
        fam_handles[fam["id"]] = fh
        if fam.get("married"):
            eh = f"_ev_marr_{ev_idx:04d}"
            eid = f"E{ev_idx:04d}"
            ev_idx += 1
            event_rows.append((eh, eid, "Marriage", str(fam["married"])))
            family_events[fam["id"]] = [eh]
        if fam.get("father") in parent_in:
            parent_in[fam["father"]].append(fh)
        if fam.get("mother") in parent_in:
            parent_in[fam["mother"]].append(fh)
        for ch in fam.get("children", []):
            if ch in child_of:
                child_of[ch].append(fh)

    person_handles = {pid: f"_person_{idx:04d}" for idx, pid in enumerate(people)}

    lines.append("  <events>")
    for eh, eid, etype, val in event_rows:
        lines.append(f'    <event handle="{eh}" change="1700000000" id="{eid}">')
        lines.append(f"      <type>{html.escape(etype)}</type>")
        lines.append(f'      <dateval val="{html.escape(val)}"/>')
        lines.append("    </event>")
    lines.append("  </events>")

    lines.append("  <people>")
    for idx, (pid, p) in enumerate(people.items()):
        ph = person_handles[pid]
        gid = f"I{idx:04d}"
        lines.append(f'    <person handle="{ph}" change="1700000000" id="{gid}">')
        lines.append(f'      <gender>{html.escape(p.get("gender", "U"))}</gender>')
        lines.append('      <name type="Birth Name">')
        lines.append(f'        <first>{html.escape(p.get("first", ""))}</first>')
        if p.get("surname"):
            lines.append(f'        <surname>{html.escape(p["surname"])}</surname>')
        if p.get("nick"):
            lines.append(f'        <nick>{html.escape(p["nick"])}</nick>')
        lines.append("      </name>")
        for eh in person_events[pid]:
            lines.append(f'      <eventref hlink="{eh}" role="Primary"/>')
        for fh in child_of[pid]:
            lines.append(f'      <childof hlink="{fh}"/>')
        for fh in parent_in[pid]:
            lines.append(f'      <parentin hlink="{fh}"/>')
        for nh in person_notes[pid]:
            lines.append(f'      <noteref hlink="{nh}"/>')
        lines.append("    </person>")
    lines.append("  </people>")

    lines.append("  <families>")
    for idx, fam in enumerate(families):
        fh = fam_handles[fam["id"]]
        fid = f"F{idx:04d}"
        lines.append(f'    <family handle="{fh}" change="1700000000" id="{fid}">')
        lines.append('      <rel type="Unknown"/>')
        if fam.get("father") in person_handles:
            lines.append(f'      <father hlink="{person_handles[fam["father"]]}"/>')
        if fam.get("mother") in person_handles:
            lines.append(f'      <mother hlink="{person_handles[fam["mother"]]}"/>')
        for eh in family_events.get(fam["id"], []):
            lines.append(f'      <eventref hlink="{eh}" role="Family"/>')
        for ch in fam.get("children", []):
            if ch in person_handles:
                lines.append(f'      <childref hlink="{person_handles[ch]}"/>')
        lines.append("    </family>")
    lines.append("  </families>")

    if note_rows:
        lines.append("  <notes>")
        for nh, nid, text in note_rows:
            lines.append(f'    <note handle="{nh}" change="1700000000" id="{nid}" type="Person Note">')
            lines.append(f"      <text>{html.escape(text)}</text>")
            lines.append("    </note>")
        lines.append("  </notes>")

    lines.append("</database>\n")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Validation & Graph Queries
# ---------------------------------------------------------------------------

def validate_tree(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a tree dictionary."""
    errors: list[str] = []
    warnings: list[str] = []

    people = data.get("people")
    families = data.get("families")
    if not isinstance(people, dict):
        return (["'people' must be an object mapping slug IDs to person objects."], [])
    if not isinstance(families, list):
        return (["'families' must be a list of family objects."], [])

    for pid, p in people.items():
        if not p.get("first"):
            errors.append(f"Person '{pid}' is missing required field 'first'.")
        gender = p.get("gender")
        if gender and gender not in ("M", "F", "U"):
            warnings.append(f"Person '{pid}' has non-standard gender '{gender}' (expected M, F, or U).")
        b, d = p.get("birth"), p.get("death")
        by, dy = extract_year(b), extract_year(d)
        if by is not None and dy is not None and by > dy:
            errors.append(f"Person '{pid}' has birth year ({by}) after death year ({dy}).")
        elif (
            b
            and d
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(b))
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d))
            and str(b) > str(d)
        ):
            errors.append(f"Person '{pid}' has birth date ({b}) after death date ({d}).")

    child_in_fam: dict[str, str] = {}
    linked_people: set[str] = set()
    seen_fids: set[str] = set()

    for idx, fam in enumerate(families):
        fid = fam.get("id") or f"families[{idx}]"
        if fid in seen_fids:
            errors.append(f"Duplicate family id '{fid}'.")
        seen_fids.add(fid)

        fa = fam.get("father")
        mo = fam.get("mother")
        children = fam.get("children") or []

        if fa:
            if fa not in people:
                errors.append(f"Family '{fid}' references unknown father '{fa}'.")
            else:
                linked_people.add(fa)
        if mo:
            if mo not in people:
                errors.append(f"Family '{fid}' references unknown mother '{mo}'.")
            else:
                linked_people.add(mo)
        if fa and mo and fa == mo:
            errors.append(f"Family '{fid}' has the same person '{fa}' as both father and mother.")

        if not fa and not mo and not children:
            warnings.append(f"Family '{fid}' is empty (no father, mother, or children).")
        elif (bool(fa) != bool(mo)) and not children:
            warnings.append(
                f"Family '{fid}' has a single person ('{fa or mo}') with no spouse or children."
            )

        seen_ch: set[str] = set()
        for ch in children:
            if ch not in people:
                errors.append(f"Family '{fid}' references unknown child '{ch}'.")
                continue
            linked_people.add(ch)
            if ch in seen_ch:
                errors.append(f"Family '{fid}' lists child '{ch}' more than once.")
            seen_ch.add(ch)
            if ch in (fa, mo):
                errors.append(f"Family '{fid}' lists '{ch}' as both parent and child.")
            if ch in child_in_fam:
                errors.append(
                    f"Person '{ch}' is a child in multiple families ('{child_in_fam[ch]}' and '{fid}')."
                )
            child_in_fam[ch] = fid

    parent_map: dict[str, set[str]] = {pid: set() for pid in people}
    for fam in families:
        parents = [p for p in (fam.get("father"), fam.get("mother")) if p in people]
        for ch in fam.get("children") or []:
            if ch in people:
                parent_map[ch].update(parents)

    for pid in people:
        visited: set[str] = set()
        stack = list(parent_map.get(pid, ()))
        while stack:
            cur = stack.pop()
            if cur == pid:
                errors.append(f"Cycle detected in ancestry involving '{pid}'.")
                break
            if cur not in visited:
                visited.add(cur)
                stack.extend(parent_map.get(cur, ()))

    unlinked = set(people.keys()) - linked_people
    if unlinked and len(people) > 1:
        warnings.append(f"{len(unlinked)} unlinked person(s): {', '.join(sorted(unlinked)[:5])}")

    return errors, warnings


def full_name(p: dict[str, Any]) -> str:
    first = p.get("first", "").strip()
    sur = p.get("surname", "").strip()
    return f"{first} {sur}".strip()


def format_person_brief(pid: str, p: dict[str, Any]) -> str:
    name = full_name(p)
    nick = f' "{p["nick"]}"' if p.get("nick") else ""
    b = p.get("birth", "")
    d = p.get("death", "")
    dates = f" ({b}–{d})" if (b or d) else ""
    return f"{name}{nick}{dates} [{pid}]"


def find_relationship_path(
    data: dict[str, Any], start_id: str, target_id: str
) -> list[tuple[str, str, str]] | None:
    """
    BFS across parent/child/sibling/spouse edges.
    Returns list of (from_id, relation_label, to_id).
    """
    people = data["people"]
    if start_id not in people or target_id not in people:
        return None
    if start_id == target_id:
        return []

    adj: dict[str, list[tuple[str, str]]] = {pid: [] for pid in people}

    def add_edge(u: str, v: str, label: str) -> None:
        if u in adj and v in adj and u != v:
            adj[u].append((label, v))

    for fam in data["families"]:
        fa = fam.get("father")
        mo = fam.get("mother")
        children = [c for c in (fam.get("children") or []) if c in people]

        if fa in people and mo in people:
            add_edge(fa, mo, "spouse")
            add_edge(mo, fa, "spouse")

        for parent in (fa, mo):
            if parent in people:
                for ch in children:
                    ch_g = people[ch].get("gender")
                    child_lbl = "son" if ch_g == "M" else ("daughter" if ch_g == "F" else "child")
                    par_g = people[parent].get("gender")
                    par_lbl = "father" if par_g == "M" else ("mother" if par_g == "F" else "parent")
                    add_edge(parent, ch, child_lbl)
                    add_edge(ch, parent, par_lbl)

        for i, c1 in enumerate(children):
            for c2 in children[i + 1 :]:
                g2 = people[c2].get("gender")
                g1 = people[c1].get("gender")
                lbl12 = "brother" if g2 == "M" else ("sister" if g2 == "F" else "sibling")
                lbl21 = "brother" if g1 == "M" else ("sister" if g1 == "F" else "sibling")
                add_edge(c1, c2, lbl12)
                add_edge(c2, c1, lbl21)

    queue: deque[tuple[str, list[tuple[str, str, str]]]] = deque([(start_id, [])])
    visited = {start_id}
    while queue:
        cur, path = queue.popleft()
        for rel, nxt in adj.get(cur, ()):
            if nxt in visited:
                continue
            new_path = path + [(cur, rel, nxt)]
            if nxt == target_id:
                return new_path
            visited.add(nxt)
            queue.append((nxt, new_path))
    return None


# ---------------------------------------------------------------------------
# Single-Page HTML Generator (Classic LR Rich Dossier — Warm Parchment)
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>__PAGE_TITLE__</title>
  <style>
    /* -----------------------------------------------------------------------
       Warm Parchment Theme (Uniform all-around 1px borders, NO dark mode)
       ----------------------------------------------------------------------- */
    :root {
      --bg: #f7f1e6;
      --surface: rgba(255, 253, 248, 0.96);
      --card: #fffdf8;
      --card-border: #d8c3a5;
      --card-spouse: #efe5d4;
      --card-spouse-border: #bda382;
      --card-hover: #f7efe2;
      --text: #2c1e12;
      --text-muted: #6e5a44;
      --divider: #eadfcd;
      --border: #d8c3a5;
      --border-subtle: #eadfcd;
      --border-strong: #ad906c;
      --gen-bg: #f5e9d6;
      --gen-border: #d8be98;
      --gen-text: #7c2d12;
      --age-living-bg: #e7f3ea;
      --age-living-text: #1b5e36;
      --age-living-border: #b5d6bd;
      --age-deceased-bg: #eee4d5;
      --age-deceased-text: #6b5640;
      --age-deceased-border: #d1bea3;
      --nick-text: #9f1239;
      --accent: #b45309;
      --accent-soft: #fef3e2;
      --emerald: #2f7a4c;
      --amber: #9a3412;
      --amber-soft: #fff7ed;
      --rose: #be123c;
      --edge: #836c53;
      --shadow-sm: 0 1px 2px rgba(44, 30, 18, 0.06);
      --shadow-md: 0 8px 20px -4px rgba(44, 30, 18, 0.10), 0 2px 6px -1px rgba(44, 30, 18, 0.05);
      --j-bg: #fbf6ec;
      --j-key: #9a3412;
      --j-str: #166534;
      --j-num: #b45309;
      --j-punc: #78634d;
      --j-slug: #1d4ed8;
      --j-active-bg: #fef3e2;
    }

    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      -webkit-font-smoothing: antialiased;
    }

    /* Top Navigation Bar */
    .topbar {
      position: fixed;
      top: 10px;
      left: 12px;
      right: 12px;
      min-height: 52px;
      background: var(--surface);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: var(--shadow-md);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 6px 12px;
      z-index: 30;
      gap: 10px;
      flex-wrap: wrap;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .brand-title {
      font-weight: 700;
      font-size: 0.92rem;
      white-space: nowrap;
    }
    .brand-stats {
      font-size: 0.74rem;
      color: var(--text-muted);
      background: var(--bg);
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--border);
      white-space: nowrap;
    }

    /* Search & Right Controls */
    .right-bar {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .search-wrap {
      position: relative;
      width: 200px;
    }
    .search-input {
      width: 100%;
      height: 32px;
      padding: 0 26px 0 28px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--text);
      font-size: 0.8rem;
      outline: none;
    }
    .search-input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px var(--accent-soft);
    }
    .search-icon {
      position: absolute;
      left: 8px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-muted);
      font-size: 0.78rem;
      pointer-events: none;
    }
    .search-kbd {
      position: absolute;
      right: 6px;
      top: 50%;
      transform: translateY(-50%);
      font-size: 0.66rem;
      color: var(--text-muted);
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 0 4px;
      pointer-events: none;
    }
    .search-results {
      position: absolute;
      top: 38px;
      left: 0;
      width: 280px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      box-shadow: var(--shadow-md);
      max-height: 320px;
      overflow-y: auto;
      display: none;
      z-index: 60;
    }
    .search-results.open { display: block; }
    .search-item {
      padding: 8px 11px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      cursor: pointer;
      border-bottom: 1px solid var(--border-subtle);
      font-size: 0.82rem;
    }
    .search-item:last-child { border-bottom: none; }
    .search-item:hover { background: var(--accent-soft); }
    .search-item-sub { font-size: 0.72rem; color: var(--text-muted); }

    .btn {
      height: 32px;
      padding: 0 9px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--text);
      font-size: 0.76rem;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      white-space: nowrap;
      transition: all 0.12s ease;
    }
    .btn:hover {
      background: var(--card-hover);
      border-color: var(--border-strong);
    }
    .btn.active {
      background: var(--accent-soft);
      color: var(--accent);
      border-color: var(--accent);
    }

    /* Canvas Viewport */
    #viewport {
      width: 100%;
      height: 100%;
      cursor: grab;
      user-select: none;
      touch-action: none;
    }
    #viewport.dragging { cursor: grabbing; }

    #scene {
      transform-origin: 0 0;
      position: absolute;
      top: 0;
      left: 0;
      will-change: transform;
    }

    #edges-svg {
      position: absolute;
      top: 0;
      left: 0;
      overflow: visible;
      pointer-events: none;
    }

    .gen-header {
      position: absolute;
      top: -32px;
      font-size: 0.7rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--gen-text);
      background: var(--gen-bg);
      border: 1px solid var(--gen-border);
      border-radius: 999px;
      padding: 2px 10px;
      white-space: nowrap;
      pointer-events: none;
    }

    /* Edges */
    .edge-path {
      fill: none;
      stroke: var(--edge);
      stroke-width: 1.45px;
      stroke-linecap: round;
      stroke-linejoin: round;
      transition: opacity 0.18s, stroke 0.18s, stroke-width 0.18s;
    }
    .cross-sibling-arc {
      fill: none;
      stroke: var(--amber);
      stroke-width: 1.4px;
      stroke-dasharray: 4 4;
      opacity: 0.65;
      transition: opacity 0.18s, stroke-width 0.18s;
    }

    /* Rich Dossier Person Card — Uniform All-Around Borders (NO single-edge stripes!) */
    .person-card {
      position: absolute;
      background: var(--card);
      border: 1px solid var(--card-border);
      border-radius: 9px;
      padding: 7px 11px;
      box-shadow: var(--shadow-sm);
      cursor: pointer;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 4px;
      transition: background-color 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease, opacity 0.18s ease;
      overflow: hidden;
    }
    .person-card:hover {
      box-shadow: var(--shadow-md);
      z-index: 6;
    }
    .person-card.is-spouse {
      background: var(--card-spouse);
      border: 1px dashed var(--card-spouse-border);
    }

    .pill {
      display: inline-block;
      padding: 1px 6px;
      border-radius: 999px;
      font-size: 0.66rem;
      font-weight: 600;
      background: var(--accent-soft);
      color: var(--accent);
      max-width: 110px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .pill.pill-living {
      background: var(--age-living-bg);
      color: var(--age-living-text);
    }
    .pill.pill-deceased {
      background: var(--age-deceased-bg);
      color: var(--age-deceased-text);
    }

    .dos-row1 {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
    }
    .dos-name {
      font-weight: 700;
      font-size: 0.83rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .person-card.is-spouse .dos-name { font-weight: 600; }
    .dos-spouse-mark {
      font-size: 0.76rem;
      color: var(--text-muted);
      flex-shrink: 0;
      line-height: 1;
    }
    .dos-row2 {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      font-size: 0.71rem;
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      overflow: hidden;
    }
    .dos-life-group {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
    }
    .dos-age {
      display: inline-block;
      padding: 0 6px;
      border-radius: 999px;
      font-size: 0.66rem;
      font-weight: 600;
      line-height: 1.45;
      flex-shrink: 0;
    }
    .dos-age.living {
      background: var(--age-living-bg);
      color: var(--age-living-text);
      border: 1px solid var(--age-living-border);
    }
    .dos-age.deceased {
      background: var(--age-deceased-bg);
      color: var(--age-deceased-text);
      border: 1px solid var(--age-deceased-border);
    }
    .dos-nick {
      color: var(--nick-text);
      font-weight: 600;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .dos-row3 {
      padding-top: 4px;
      border-top: 1px solid var(--divider);
      font-size: 0.68rem;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .dos-row3.has-note {
      color: var(--amber);
      font-weight: 500;
    }

    /* Collapse / Marriage Node */
    .marriage-node {
      position: absolute;
      width: 18px;
      height: 18px;
      margin-left: -9px;
      margin-top: -9px;
      border-radius: 999px;
      background: var(--card);
      border: 1px solid var(--border-strong);
      color: var(--text-muted);
      font-size: 0.64rem;
      font-weight: 700;
      display: grid;
      place-items: center;
      cursor: pointer;
      z-index: 4;
      box-shadow: var(--shadow-sm);
    }
    .marriage-node:hover {
      border-color: var(--accent);
      color: var(--accent);
      transform: scale(1.12);
    }
    .marriage-node.collapsed {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
      width: auto;
      min-width: 22px;
      padding: 0 5px;
    }

    /* Lineage Highlight States (Spotlight + surface tint without extra outline frames) */
    body.has-selection .person-card {
      opacity: 0.24;
      box-shadow: none;
    }
    body.has-selection .edge-path,
    body.has-selection .marriage-node,
    body.has-selection .cross-sibling-arc {
      opacity: 0.14;
    }

    body.has-selection .person-card.hl-self {
      opacity: 1;
      background: #fcedd2;
      border-color: #b45309;
      box-shadow: 0 8px 22px -4px rgba(146, 64, 14, 0.24), 0 2px 6px -1px rgba(146, 64, 14, 0.12);
      z-index: 10;
    }
    body.has-selection .person-card.hl-ancestor {
      opacity: 1;
      background: #fdf4e7;
      border-color: #c27838;
      box-shadow: var(--shadow-sm);
    }
    body.has-selection .person-card.is-spouse.hl-ancestor {
      background: #f5e7d3;
      border-color: #b56e32;
    }
    body.has-selection .person-card.hl-descendant {
      opacity: 1;
      background: #f2f8f3;
      border-color: #5c9670;
      box-shadow: var(--shadow-sm);
    }
    body.has-selection .person-card.is-spouse.hl-descendant {
      background: #e6f0e8;
      border-color: #528764;
    }
    body.has-selection .person-card.hl-relative {
      opacity: 1;
      background: #fcf5ea;
      border-color: #ad8b65;
      box-shadow: var(--shadow-sm);
    }
    body.has-selection .person-card.is-spouse.hl-relative {
      background: #f2e6d5;
      border-color: #9e7d58;
    }
    body.has-selection .person-card.hl-path {
      opacity: 1;
      background: #fdf0f2;
      border-color: #be123c;
      box-shadow: 0 6px 16px -3px rgba(190, 18, 60, 0.18);
      z-index: 9;
    }
    body.has-selection .person-card.is-spouse.hl-path {
      background: #f8e4e8;
      border-color: #be123c;
    }
    body.has-selection .edge-path.hl-edge,
    body.has-selection .marriage-node.hl-edge { opacity: 1; }
    body.has-selection .edge-path.hl-ancestor {
      stroke: var(--accent);
      stroke-width: 2.3px;
      opacity: 1;
    }
    body.has-selection .edge-path.hl-descendant {
      stroke: var(--emerald);
      stroke-width: 2.3px;
      opacity: 1;
    }
    body.has-selection .edge-path.hl-path {
      stroke: var(--rose);
      stroke-width: 2.7px;
      opacity: 1;
    }

    /* Right-Hand Drawer (Inspector + Syntax-Highlighted JSON Tabs) */
    .drawer {
      position: fixed;
      top: 72px;
      right: 12px;
      bottom: 12px;
      width: 400px;
      max-width: calc(100vw - 24px);
      background: var(--surface);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: var(--shadow-md);
      z-index: 25;
      display: flex;
      flex-direction: column;
      transform: translateX(430px);
      transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1);
      overflow: hidden;
    }
    .drawer.open { transform: translateX(0); }

    .drawer-header {
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .drawer-tabs {
      display: flex;
      align-items: center;
      background: var(--bg);
      padding: 3px;
      border-radius: 8px;
      border: 1px solid var(--border);
      gap: 2px;
    }
    .dtab-btn {
      appearance: none;
      border: 1px solid transparent;
      background: transparent;
      color: var(--text-muted);
      font-size: 0.76rem;
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 6px;
      cursor: pointer;
    }
    .dtab-btn.active {
      background: var(--card);
      color: var(--accent);
      border-color: var(--border);
      box-shadow: var(--shadow-sm);
    }

    .drawer-pane {
      display: none;
      flex: 1;
      overflow: hidden;
      flex-direction: column;
    }
    .drawer-pane.active { display: flex; }

    .drawer-body {
      padding: 14px 15px;
      overflow-y: auto;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 13px;
    }
    .drawer-title { font-size: 1.05rem; font-weight: 700; margin: 0; }
    .drawer-sub {
      font-size: 0.74rem;
      color: var(--text-muted);
      margin-top: 2px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .info-section-title {
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-muted);
      font-weight: 700;
      margin-bottom: 6px;
    }
    .chip-list {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .person-chip {
      padding: 5px 9px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--text);
      font-size: 0.78rem;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .person-chip:hover {
      border-color: var(--accent);
      background: var(--accent-soft);
    }
    .note-box {
      padding: 9px 11px;
      border-radius: 8px;
      background: var(--amber-soft);
      border: 1px solid var(--amber);
      font-size: 0.81rem;
      line-height: 1.45;
    }
    .rel-finder-select {
      width: 100%;
      height: 32px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--text);
      padding: 0 8px;
      font-size: 0.8rem;
    }
    .rel-path-steps {
      margin-top: 8px;
      padding: 9px 11px;
      border-radius: 8px;
      background: var(--card);
      border: 1px solid var(--border);
      font-size: 0.78rem;
      line-height: 1.5;
    }

    /* Syntax-Highlighted JSON Side Panel */
    .json-toolbar {
      padding: 8px 12px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      background: var(--card);
    }
    .json-scope-group {
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .json-scope-btn {
      appearance: none;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text-muted);
      font-size: 0.71rem;
      font-weight: 600;
      padding: 3px 8px;
      border-radius: 6px;
      cursor: pointer;
    }
    .json-scope-btn.active {
      background: var(--accent-soft);
      color: var(--accent);
      border-color: var(--accent);
    }
    .json-meta-label {
      font-size: 0.7rem;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .json-meta-label a {
      color: var(--accent);
      text-decoration: none;
      font-weight: 600;
    }
    .json-meta-label a:hover { text-decoration: underline; }
    .json-viewer {
      margin: 0;
      padding: 12px;
      flex: 1;
      overflow: auto;
      background: var(--j-bg);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.75rem;
      line-height: 1.5;
      tab-size: 2;
      white-space: pre;
    }
    .j-key { color: var(--j-key); font-weight: 600; }
    .j-str { color: var(--j-str); }
    .j-num { color: var(--j-num); }
    .j-punc { color: var(--j-punc); }
    .j-slug {
      color: var(--j-slug);
      cursor: pointer;
      text-decoration: underline;
      text-decoration-style: dotted;
      text-underline-offset: 2px;
    }
    .j-slug:hover {
      background: var(--accent-soft);
      border-radius: 3px;
    }
    .json-entry {
      border-radius: 6px;
      border: 1px solid transparent;
      padding: 1px 4px;
      margin: 0 -4px;
    }
    .json-entry-active {
      background: var(--j-active-bg);
      border: 1px solid var(--accent);
    }

    @media (max-width: 960px) {
      .brand-stats { display: none; }
      .drawer { top: auto; height: 54vh; left: 12px; width: auto; transform: translateY(110%); }
      .drawer.open { transform: translateY(0); }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <div class="brand-title" id="tree-title">Family Tree</div>
      <div class="brand-stats" id="tree-stats"></div>
    </div>

    <div class="right-bar">
      <div class="search-wrap">
        <span class="search-icon">🔍</span>
        <input type="text" id="search-input" class="search-input" placeholder="Search person..." autocomplete="off" />
        <span class="search-kbd">/</span>
        <div id="search-results" class="search-results"></div>
      </div>
      <button class="btn" id="btn-json" title="Toggle Syntax-Highlighted JSON Side Panel">{ } JSON</button>
      <button class="btn" id="btn-expand-all" title="Expand all collapsed branches">Expand</button>
      <button class="btn" id="btn-fit" title="Fit tree to screen">Fit</button>
    </div>
  </header>

  <div id="viewport">
    <div id="scene">
      <svg id="edges-svg"></svg>
      <div id="nodes-layer"></div>
    </div>
  </div>

  <aside class="drawer" id="drawer">
    <div class="drawer-header">
      <div class="drawer-tabs">
        <button class="dtab-btn active" data-dtab="inspector">Inspector</button>
        <button class="dtab-btn" data-dtab="json">{ } JSON</button>
      </div>
      <button class="btn" id="btn-close-drawer" title="Close (Esc)">✕</button>
    </div>

    <div class="drawer-pane active" id="drawer-inspector-pane">
      <div class="drawer-body" id="drawer-body"></div>
    </div>

    <div class="drawer-pane" id="drawer-json-pane">
      <div class="json-toolbar">
        <div class="json-scope-group">
          <button class="json-scope-btn active" data-jscope="full">Full Tree</button>
          <button class="json-scope-btn" data-jscope="selected">Selected</button>
        </div>
        <span class="json-meta-label" id="json-meta-label"></span>
        <button class="btn" id="btn-copy-json" style="height:26px;padding:0 8px;font-size:0.71rem">Copy</button>
      </div>
      <pre class="json-viewer" id="json-viewer"></pre>
    </div>
  </aside>

  <script>
    const JSON_URL = __JSON_URL__;
    const EMBEDDED_TREE_DATA = __EMBEDDED_JSON__;

    // --- State ---
    let selectedPersonId = null;
    let drawerTab = "inspector"; // "inspector" | "json"
    let jsonScope = "full";      // "full" | "selected"
    let relTargetId = "";
    const collapsedFamilies = new Set();

    const BOX_W = 240;
    const INDENT = 18;
    const SPOUSE_VGAP = 7;
    const ROW_GAP = 14;

    // Pan & Zoom state
    let scale = 1;
    let panX = 40;
    let panY = 85;
    let isDragging = false;
    let dragStartX = 0;
    let dragStartY = 0;

    // --- Graph Indexes ---
    let TREE_DATA = { meta: {}, people: {}, families: [] };
    let people = {};
    let families = [];
    let familyById = {};
    let childOfFamily = {};
    let parentInFamilies = {};

    function resolveJsonUrl(relUrl) {
      if (/^(https?:)?\/\//i.test(relUrl) || relUrl.startsWith("/")) return relUrl;
      const p = window.location.pathname;
      const lastSeg = p.split("/").pop() || "";
      const baseHref = (!p.endsWith("/") && lastSeg && !lastSeg.includes("."))
        ? `${window.location.origin}${p}/${window.location.search}`
        : window.location.href;
      return new URL(relUrl, baseHref).toString();
    }

    function buildGraphIndexes(data) {
      TREE_DATA = data || { meta: {}, people: {}, families: [] };
      people = TREE_DATA.people || {};
      families = TREE_DATA.families || [];
      familyById = {};
      childOfFamily = {};
      parentInFamilies = {};
      Object.keys(people).forEach(pid => { parentInFamilies[pid] = []; });
      families.forEach(f => {
        familyById[f.id] = f;
        if (f.father && parentInFamilies[f.father]) parentInFamilies[f.father].push(f);
        if (f.mother && parentInFamilies[f.mother]) parentInFamilies[f.mother].push(f);
        (f.children || []).forEach(cid => {
          if (people[cid]) childOfFamily[cid] = f;
        });
      });
    }

    function countDescendants(famId, seen = new Set()) {
      const f = familyById[famId];
      if (!f) return 0;
      let total = 0;
      (f.children || []).forEach(cid => {
        if (seen.has(cid)) return;
        seen.add(cid);
        total += 1;
        (parentInFamilies[cid] || []).forEach(cf => {
          total += countDescendants(cf.id, seen);
        });
      });
      return total;
    }

    function getRootFamilies() {
      const roots = [];
      const crossSibs = [];
      families.forEach(f => {
        const faHas = f.father && childOfFamily[f.father] && (childOfFamily[f.father].father || childOfFamily[f.father].mother);
        const moHas = f.mother && childOfFamily[f.mother] && (childOfFamily[f.mother].father || childOfFamily[f.mother].mother);
        if (!f.father && !f.mother) {
          const allMarried = (f.children || []).every(cid => (parentInFamilies[cid] || []).length > 0);
          if (allMarried && (f.children || []).length > 0) {
            crossSibs.push(f);
            return;
          }
        }
        if (!faHas && !moHas) roots.push(f);
      });
      roots.sort((a, b) => countDescendants(b.id) - countDescendants(a.id));
      return { roots, crossSibs };
    }

    function fullName(pid) {
      const p = people[pid];
      if (!p) return pid;
      return [p.first, p.surname].filter(Boolean).join(" ");
    }

    function initials(pid) {
      const p = people[pid];
      if (!p) return "?";
      const a = (p.first || "").trim().charAt(0);
      const b = (p.surname || "").trim().charAt(0);
      return (a + b).toUpperCase() || a.toUpperCase() || "?";
    }

    function extractYear(s) {
      if (!s) return null;
      const m = String(s).match(/\b(\d{4})\b/);
      return m ? parseInt(m[1], 10) : null;
    }

    function shortDate(s) {
      const y = extractYear(s);
      return y !== null ? String(y) : String(s || "");
    }

    function parseDateParts(s) {
      if (!s) return null;
      const full = String(s).match(/^(\d{4})-(\d{2})-(\d{2})$/);
      if (full) {
        return {
          y: parseInt(full[1], 10),
          m: parseInt(full[2], 10),
          d: parseInt(full[3], 10),
        };
      }
      const ym = String(s).match(/^(\d{4})-(\d{2})$/);
      if (ym) {
        return { y: parseInt(ym[1], 10), m: parseInt(ym[2], 10), d: null };
      }
      const y = extractYear(s);
      return y !== null ? { y, m: null, d: null } : null;
    }

    function getAgeInfo(p) {
      if (!p || !p.birth) return null;
      const b = parseDateParts(p.birth);
      if (!b || b.y === null) return null;

      if (p.death) {
        const d = parseDateParts(p.death);
        if (!d || d.y === null) return null;
        let years = d.y - b.y;
        if (b.m && d.m && (d.m < b.m || (d.m === b.m && b.d && d.d && d.d < b.d))) {
          years -= 1;
        }
        if (years < 0) return null;
        return {
          years,
          isDeceased: true,
          cardLabel: `lived ${years} ${years === 1 ? "yr" : "yrs"}`,
          inspectorLabel: `Lived ${years} ${years === 1 ? "yr" : "yrs"}`,
        };
      }

      const now = new Date();
      let years = now.getFullYear() - b.y;
      if (b.m) {
        const curM = now.getMonth() + 1;
        const curD = now.getDate();
        if (curM < b.m || (curM === b.m && b.d && curD < b.d)) {
          years -= 1;
        }
      }
      if (years < 0 || years > 115) return null;
      return {
        years,
        isDeceased: false,
        cardLabel: `${years} ${years === 1 ? "yr" : "yrs"}`,
        inspectorLabel: `Age ${years}`,
      };
    }

    function lifespan(pid, mode = "dash") {
      const p = people[pid];
      if (!p) return "";
      const compact = (mode === "short" || mode === "card");
      const b = compact ? shortDate(p.birth) : (p.birth || "");
      const d = compact ? shortDate(p.death) : (p.death || "");
      if (b && d) return `${b}–${d}`;
      if (b) return mode === "short" ? `${b}–` : `b. ${b}`;
      if (d) return mode === "short" ? `–${d}` : `d. ${d}`;
      return "";
    }

    function dossierFooter(pid) {
      const p = people[pid];
      if (p && p.note) return { text: `📝 ${p.note}`, isNote: true };

      const mYears = [];
      const childSet = new Set();
      (parentInFamilies[pid] || []).forEach(f => {
        if (f.married) {
          const mYr = shortDate(f.married);
          if (mYr && !mYears.includes(mYr)) mYears.push(mYr);
        }
        (f.children || []).forEach(cid => {
          if (people[cid]) childSet.add(cid);
        });
      });

      const parts = [];
      if (mYears.length > 0) parts.push(`m. ${mYears.join(", ")}`);
      if (childSet.size > 0) parts.push(`${childSet.size} ${childSet.size === 1 ? "child" : "children"}`);
      if (parts.length > 0) return { text: parts.join(" · "), isNote: false };
      return null;
    }

    function getCardData(pid) {
      const p = people[pid] || {};
      const dt = lifespan(pid, "card");
      const ageInfo = getAgeInfo(p);
      const nick = p.nick || "";
      const footer = dossierFooter(pid);
      const hasRow2 = Boolean(dt || ageInfo || nick);
      const hasRow3 = Boolean(footer);
      let h = 38;
      if (hasRow2 && hasRow3) h = 72;
      else if (hasRow3) h = 56;
      else if (hasRow2) h = 52;
      return { dt, ageInfo, nick, footer, hasRow2, hasRow3, h };
    }

    function escapeHtml(s) {
      return String(s || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    // =========================================================================
    // Classic Gramps Descendant Layout (Left-to-Right Top-Aligned Columns)
    // =========================================================================
    function computeClassicSvgLayout() {
      const COL_STEP = BOX_W + 66;

      const placedPeople = new Set();
      const placedFamilies = new Set();
      const personPos = {};
      const familyPos = {};
      const { roots, crossSibs } = getRootFamilies();
      let maxGen = 0;

      function layoutSubtree(pid, gen, yStart) {
        placedPeople.add(pid);
        if (gen > maxGen) maxGen = gen;
        const x = gen * COL_STEP;
        const pH = getCardData(pid).h;
        personPos[pid] = {
          x,
          y: yStart,
          w: BOX_W,
          h: pH,
          gen,
          isSpouse: false,
        };

        const pFams = (parentInFamilies[pid] || []).filter(f => !placedFamilies.has(f.id));
        let curCoupleBottom = yStart + pH;
        let maxChildrenBottom = yStart;

        pFams.forEach(f => {
          placedFamilies.add(f.id);
          const sp = f.father === pid ? f.mother : f.father;
          let connectorOriginY = yStart + pH / 2;

          if (sp && people[sp] && !placedPeople.has(sp)) {
            placedPeople.add(sp);
            const spY = curCoupleBottom + SPOUSE_VGAP;
            const spH = getCardData(sp).h;
            personPos[sp] = {
              x: x + INDENT,
              y: spY,
              w: BOX_W - INDENT,
              h: spH,
              gen,
              isSpouse: true,
            };
            curCoupleBottom = spY + spH;
            connectorOriginY = spY + spH / 2;
          }

          const validChildren = (f.children || []).filter(cid => people[cid] && !placedPeople.has(cid));
          const renderedChildren = [];
          let childY = yStart;

          if (!collapsedFamilies.has(f.id)) {
            validChildren.forEach(cid => {
              const chHeight = layoutSubtree(cid, gen + 1, childY);
              renderedChildren.push(cid);
              childY += chHeight + ROW_GAP;
            });
            if (validChildren.length > 0) {
              maxChildrenBottom = Math.max(maxChildrenBottom, childY - ROW_GAP);
            }
          }

          familyPos[f.id] = {
            id: f.id,
            jX: x + BOX_W,
            jY: connectorOriginY,
            busX: x + BOX_W + 30,
            children: renderedChildren,
            totalChildren: (f.children || []).length,
            collapsed: collapsedFamilies.has(f.id),
          };
        });

        const totalBottom = Math.max(curCoupleBottom, maxChildrenBottom);
        return totalBottom - yStart;
      }

      let cursorY = 0;
      roots.forEach(rf => {
        if (placedFamilies.has(rf.id)) return;
        const rootPid = rf.father || rf.mother || (rf.children || [])[0];
        if (!rootPid || placedPeople.has(rootPid)) return;
        const h = layoutSubtree(rootPid, 0, cursorY);
        cursorY += h + 32;
      });

      Object.keys(people).forEach(pid => {
        if (!placedPeople.has(pid)) {
          const h = layoutSubtree(pid, 0, cursorY);
          cursorY += h + 32;
        }
      });

      return { personPos, familyPos, crossSiblingFamilies: crossSibs, maxGen, colStep: COL_STEP };
    }

    // =========================================================================
    // Rich Dossier Card Renderer (1–3 adaptive rows, no generation pills)
    // =========================================================================
    function buildCardContent(pid, pos) {
      const name = fullName(pid);
      const cd = getCardData(pid);
      const spouseMark = pos.isSpouse
        ? `<span class="dos-spouse-mark" title="Spouse">⚭</span>`
        : "";

      let row2Html = "";
      if (cd.hasRow2) {
        const dtHtml = cd.dt ? `<span>${escapeHtml(cd.dt)}</span>` : "";
        const ageHtml = cd.ageInfo
          ? `<span class="dos-age ${cd.ageInfo.isDeceased ? "deceased" : "living"}">${escapeHtml(cd.ageInfo.cardLabel)}</span>`
          : "";
        const nickHtml = cd.nick
          ? `<span class="dos-nick" title="Nickname">“${escapeHtml(cd.nick)}”</span>`
          : "";
        row2Html = `
          <div class="dos-row2">
            <span class="dos-life-group">${dtHtml}${ageHtml}</span>
            ${nickHtml}
          </div>
        `;
      }

      let row3Html = "";
      if (cd.hasRow3) {
        row3Html = `<div class="dos-row3${cd.footer.isNote ? " has-note" : ""}" title="${escapeHtml(cd.footer.text)}">${escapeHtml(cd.footer.text)}</div>`;
      }

      return `
        <div class="dos-row1">
          <div class="dos-name" title="${escapeHtml(name)}">${escapeHtml(name)}</div>
          ${spouseMark}
        </div>
        ${row2Html}
        ${row3Html}
      `;
    }

    // =========================================================================
    // Render Tree Canvas
    // =========================================================================
    let currentLayout = null;

    function renderTree() {
      const nodesLayer = document.getElementById("nodes-layer");
      const svg = document.getElementById("edges-svg");
      nodesLayer.innerHTML = "";
      svg.innerHTML = "";

      currentLayout = computeClassicSvgLayout();
      const { personPos, familyPos, crossSiblingFamilies, maxGen, colStep } = currentLayout;

      // 0. Generation Column Headers
      for (let g = 0; g <= maxGen; g++) {
        const hdr = document.createElement("div");
        hdr.className = "gen-header";
        hdr.style.left = `${g * colStep}px`;
        hdr.textContent = `Generation ${g + 1}`;
        nodesLayer.appendChild(hdr);
      }

      // 1. Orthogonal Family Bus Connectors & Collapse Nodes
      Object.values(familyPos).forEach(fp => {
        const { id, jX, jY, busX, children, totalChildren, collapsed } = fp;

        if (!collapsed && children.length > 0) {
          children.forEach(cid => {
            const chPos = personPos[cid];
            const cy = chPos.y + chPos.h / 2;
            const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
            path.setAttribute("d", `M ${jX} ${jY} L ${busX} ${jY} L ${busX} ${cy} L ${chPos.x} ${cy}`);
            path.setAttribute("class", "edge-path");
            path.dataset.famId = id;
            path.dataset.childId = cid;
            svg.appendChild(path);
          });
        }

        if (totalChildren > 0) {
          const mNode = document.createElement("div");
          mNode.className = "marriage-node" + (collapsed ? " collapsed" : "");
          mNode.style.left = `${jX + 14}px`;
          mNode.style.top = `${jY}px`;
          mNode.dataset.famId = id;
          const descCount = countDescendants(id);
          mNode.textContent = collapsed ? `+${descCount}` : "−";
          mNode.title = collapsed ? `Expand ${descCount} descendant(s)` : "Collapse branch";
          mNode.addEventListener("click", (e) => {
            e.stopPropagation();
            if (collapsedFamilies.has(id)) collapsedFamilies.delete(id);
            else collapsedFamilies.add(id);
            renderTree();
            applySelectionHighlights();
          });
          nodesLayer.appendChild(mNode);
        }
      });

      // 2. Cross-Sibling Dashed Arcs
      crossSiblingFamilies.forEach(f => {
        const ch = (f.children || []).filter(cid => personPos[cid]);
        for (let i = 0; i < ch.length - 1; i++) {
          const p1 = personPos[ch[i]];
          const p2 = personPos[ch[i + 1]];
          const arc = document.createElementNS("http://www.w3.org/2000/svg", "path");
          const y1 = p1.y + p1.h / 2;
          const y2 = p2.y + p2.h / 2;
          const ctrlX = Math.min(p1.x, p2.x) - 38;
          arc.setAttribute("d", `M ${p1.x} ${y1} C ${ctrlX} ${y1}, ${ctrlX} ${y2}, ${p2.x} ${y2}`);
          arc.setAttribute("class", "cross-sibling-arc");
          arc.dataset.sibA = ch[i];
          arc.dataset.sibB = ch[i + 1];
          svg.appendChild(arc);
        }
      });

      // 3. Person Cards
      Object.entries(personPos).forEach(([pid, pos]) => {
        const p = people[pid];
        const card = document.createElement("div");
        card.className = "person-card" + (pos.isSpouse ? " is-spouse" : "");
        card.style.left = `${pos.x}px`;
        card.style.top = `${pos.y}px`;
        card.style.width = `${pos.w}px`;
        card.style.height = `${pos.h}px`;
        card.dataset.pid = pid;
        card.dataset.gender = p.gender || "U";
        card.innerHTML = buildCardContent(pid, pos);

        card.addEventListener("click", (e) => {
          e.stopPropagation();
          selectPerson(pid, false);
        });
        nodesLayer.appendChild(card);
      });
    }

    // --- Lineage & Relationship Calculation ---
    function getAncestors(pid) {
      const anc = new Set();
      const famIds = new Set();
      function walk(cur) {
        const cf = childOfFamily[cur];
        if (!cf) return;
        famIds.add(cf.id);
        [cf.father, cf.mother].forEach(par => {
          if (par && people[par] && !anc.has(par)) {
            anc.add(par);
            walk(par);
          }
        });
      }
      walk(pid);
      return { anc, famIds };
    }

    function getDescendants(pid) {
      const desc = new Set();
      const descSpouses = new Set();
      const famIds = new Set();
      function walk(cur) {
        (parentInFamilies[cur] || []).forEach(f => {
          famIds.add(f.id);
          if (cur !== pid) {
            const sp = f.father === cur ? f.mother : f.father;
            if (sp && people[sp]) descSpouses.add(sp);
          }
          (f.children || []).forEach(ch => {
            if (people[ch] && !desc.has(ch)) {
              desc.add(ch);
              walk(ch);
            }
          });
        });
      }
      walk(pid);
      return { desc, descSpouses, famIds };
    }

    function getImmediateRelatives(pid) {
      const parents = [];
      const siblings = [];
      const spouses = [];
      const children = [];

      const cf = childOfFamily[pid];
      if (cf) {
        if (cf.father && people[cf.father]) parents.push(cf.father);
        if (cf.mother && people[cf.mother]) parents.push(cf.mother);
        (cf.children || []).forEach(cid => {
          if (cid !== pid && people[cid]) siblings.push(cid);
        });
      }

      (parentInFamilies[pid] || []).forEach(f => {
        const sp = f.father === pid ? f.mother : f.father;
        if (sp && people[sp] && !spouses.includes(sp)) spouses.push(sp);
        (f.children || []).forEach(cid => {
          if (people[cid] && !children.includes(cid)) children.push(cid);
        });
      });

      return { parents, siblings, spouses, children };
    }

    function findPathJS(startId, targetId) {
      if (!people[startId] || !people[targetId] || startId === targetId) return null;
      const adj = {};
      Object.keys(people).forEach(k => { adj[k] = []; });
      function add(u, v, lbl) {
        if (adj[u] && adj[v] && u !== v) adj[u].push({ to: v, lbl });
      }
      families.forEach(f => {
        const fa = f.father, mo = f.mother;
        const ch = (f.children || []).filter(c => people[c]);
        if (fa && mo && people[fa] && people[mo]) {
          add(fa, mo, "spouse");
          add(mo, fa, "spouse");
        }
        [fa, mo].forEach(par => {
          if (par && people[par]) {
            const parLbl = people[par].gender === "M" ? "father" : (people[par].gender === "F" ? "mother" : "parent");
            ch.forEach(c => {
              const chLbl = people[c].gender === "M" ? "son" : (people[c].gender === "F" ? "daughter" : "child");
              add(par, c, chLbl);
              add(c, par, parLbl);
            });
          }
        });
        for (let i = 0; i < ch.length; i++) {
          for (let j = i + 1; j < ch.length; j++) {
            const c1 = ch[i], c2 = ch[j];
            add(c1, c2, people[c2].gender === "M" ? "brother" : (people[c2].gender === "F" ? "sister" : "sibling"));
            add(c2, c1, people[c1].gender === "M" ? "brother" : (people[c1].gender === "F" ? "sister" : "sibling"));
          }
        }
      });

      const queue = [[startId, []]];
      const visited = new Set([startId]);
      while (queue.length > 0) {
        const [cur, path] = queue.shift();
        for (const edge of adj[cur] || []) {
          if (visited.has(edge.to)) continue;
          const nextPath = [...path, { from: cur, lbl: edge.lbl, to: edge.to }];
          if (edge.to === targetId) return nextPath;
          visited.add(edge.to);
          queue.push([edge.to, nextPath]);
        }
      }
      return null;
    }

    // --- Selection, Highlighting & Drawer Tabs ---
    function renderActiveView(shouldFit = true) {
      renderTree();
      applySelectionHighlights();
      if (shouldFit) fitToScreen();
    }

    function setDrawerTab(tab, openDrawer = true) {
      drawerTab = tab;
      document.querySelectorAll(".dtab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.dtab === drawerTab);
      });
      document.getElementById("drawer-inspector-pane").classList.toggle("active", drawerTab === "inspector");
      document.getElementById("drawer-json-pane").classList.toggle("active", drawerTab === "json");
      document.getElementById("btn-json").classList.toggle("active", openDrawer && drawerTab === "json");

      const drawer = document.getElementById("drawer");
      if (openDrawer) drawer.classList.add("open");
      else drawer.classList.remove("open");

      if (drawerTab === "inspector") {
        renderInspector(selectedPersonId);
      } else if (openDrawer) {
        renderJsonPanel(true);
      }
    }

    function selectPerson(pid, panToCard = true) {
      selectedPersonId = pid;
      const drawer = document.getElementById("drawer");
      const isDrawerOpen = drawer.classList.contains("open");

      if (!pid || !people[pid]) {
        document.body.classList.remove("has-selection");
        clearHighlights();
        if (isDrawerOpen && drawerTab === "json") {
          renderJsonPanel(false);
        } else {
          drawer.classList.remove("open");
          document.getElementById("btn-json").classList.remove("active");
        }
        renderInspector(null);
        return;
      }

      if (currentLayout && !currentLayout.personPos[pid]) {
        collapsedFamilies.clear();
        renderTree();
      }
      applySelectionHighlights();
      if (panToCard && currentLayout && currentLayout.personPos[pid]) {
        centerOnPerson(pid);
      }

      renderInspector(pid);
      if (isDrawerOpen && drawerTab === "json") {
        setDrawerTab("json", true);
      } else {
        setDrawerTab("inspector", true);
      }
    }

    function clearHighlights() {
      document.querySelectorAll(".person-card").forEach(el => {
        el.classList.remove("hl-self", "hl-ancestor", "hl-descendant", "hl-relative", "hl-path");
      });
      document.querySelectorAll(".edge-path, .marriage-node, .cross-sibling-arc").forEach(el => {
        el.classList.remove("hl-edge", "hl-ancestor", "hl-descendant", "hl-path");
      });
    }

    function applySelectionHighlights() {
      clearHighlights();
      if (!selectedPersonId || !people[selectedPersonId]) {
        document.body.classList.remove("has-selection");
        return;
      }
      document.body.classList.add("has-selection");

      const { anc, famIds: ancFams } = getAncestors(selectedPersonId);
      const { desc, descSpouses, famIds: descFams } = getDescendants(selectedPersonId);
      const { siblings, spouses } = getImmediateRelatives(selectedPersonId);
      const relSet = new Set([...siblings, ...spouses]);

      const pathSteps = relTargetId ? findPathJS(selectedPersonId, relTargetId) : null;
      const pathNodes = new Set();
      if (pathSteps) {
        pathSteps.forEach(s => { pathNodes.add(s.from); pathNodes.add(s.to); });
      }

      document.querySelectorAll(".person-card").forEach(card => {
        const pid = card.dataset.pid;
        if (pid === selectedPersonId) card.classList.add("hl-self");
        else if (pathNodes.has(pid)) card.classList.add("hl-path");
        else if (anc.has(pid)) card.classList.add("hl-ancestor");
        else if (desc.has(pid) || descSpouses.has(pid)) card.classList.add("hl-descendant");
        else if (relSet.has(pid)) card.classList.add("hl-relative");
      });

      document.querySelectorAll(".edge-path").forEach(edge => {
        const cid = edge.dataset.childId;
        const fid = edge.dataset.famId;
        if (pathNodes.size > 0 && pathNodes.has(cid)) {
          edge.classList.add("hl-path");
        } else if (cid === selectedPersonId || anc.has(cid)) {
          edge.classList.add("hl-ancestor");
        } else if (desc.has(cid)) {
          edge.classList.add("hl-descendant");
        } else if (relSet.has(cid) && childOfFamily[selectedPersonId] && fid === childOfFamily[selectedPersonId].id) {
          edge.classList.add("hl-edge");
        }
      });

      document.querySelectorAll(".marriage-node").forEach(el => {
        const fid = el.dataset.famId;
        if (ancFams.has(fid) || descFams.has(fid)) {
          el.classList.add("hl-edge");
        }
      });

      document.querySelectorAll(".cross-sibling-arc").forEach(arc => {
        if (arc.dataset.sibA === selectedPersonId || arc.dataset.sibB === selectedPersonId) {
          arc.classList.add("hl-edge");
        }
      });
    }

    function renderInspector(pid) {
      const body = document.getElementById("drawer-body");
      if (!pid || !people[pid]) {
        body.innerHTML = `<div style="color:var(--text-muted);font-size:0.84rem;padding:12px 0;">
          Select a person on the tree to inspect their details and find relationships.
        </div>`;
        return;
      }

      const p = people[pid];
      const pos = currentLayout && currentLayout.personPos ? currentLayout.personPos[pid] : null;
      const { parents, siblings, spouses, children } = getImmediateRelatives(pid);
      const { anc } = getAncestors(pid);
      const { desc } = getDescendants(pid);

      const chipGroup = (title, list) => {
        if (!list || list.length === 0) return "";
        const chips = list.map(id => {
          const dt = lifespan(id, "dash");
          return `<button class="person-chip" data-goto="${escapeHtml(id)}">
            <span>${escapeHtml(fullName(id))}</span>
            ${dt ? `<span style="color:var(--text-muted);font-size:0.7rem">${escapeHtml(dt)}</span>` : ""}
          </button>`;
        }).join("");
        return `<div>
          <div class="info-section-title">${title} (${list.length})</div>
          <div class="chip-list">${chips}</div>
        </div>`;
      };

      const metaBadges = [];
      if (p.nick) metaBadges.push(`<span class="pill">Nickname: "${escapeHtml(p.nick)}"</span>`);
      if (p.gender) metaBadges.push(`<span class="pill">${p.gender === "M" ? "Male" : (p.gender === "F" ? "Female" : p.gender)}</span>`);
      if (pos && pos.gen !== undefined) metaBadges.push(`<span class="pill">Generation ${pos.gen + 1}</span>`);
      if (p.birth) metaBadges.push(`<span class="pill">Born ${escapeHtml(p.birth)}</span>`);
      if (p.death) metaBadges.push(`<span class="pill">Died ${escapeHtml(p.death)}</span>`);
      (parentInFamilies[pid] || []).forEach(f => {
        if (f.married) metaBadges.push(`<span class="pill">Married ${escapeHtml(f.married)}</span>`);
      });
      const ageInfo = getAgeInfo(p);
      if (ageInfo) {
        const ageCls = ageInfo.isDeceased ? "pill-deceased" : "pill-living";
        metaBadges.push(`<span class="pill ${ageCls}">${escapeHtml(ageInfo.inspectorLabel)}</span>`);
      }

      const sortedPeople = Object.keys(people)
        .filter(k => k !== pid)
        .sort((a, b) => fullName(a).localeCompare(fullName(b)));

      const optionsHtml = [`<option value="">Select another person...</option>`]
        .concat(sortedPeople.map(k => {
          const sel = k === relTargetId ? "selected" : "";
          const dt = lifespan(k, "dash");
          return `<option value="${escapeHtml(k)}" ${sel}>${escapeHtml(fullName(k))}${dt ? ` (${escapeHtml(dt)})` : ""}</option>`;
        })).join("");

      let relStepsHtml = "";
      if (relTargetId && people[relTargetId] && relTargetId !== pid) {
        const steps = findPathJS(pid, relTargetId);
        if (steps && steps.length > 0) {
          const lines = steps.map((s, idx) =>
            `<div>${idx + 1}. <strong>${escapeHtml(fullName(s.from))}</strong> → <em>${escapeHtml(s.lbl)}</em> → <strong>${escapeHtml(fullName(s.to))}</strong></div>`
          ).join("");
          relStepsHtml = `<div class="rel-path-steps">${lines}</div>`;
        } else {
          relStepsHtml = `<div class="rel-path-steps">No connected path found.</div>`;
        }
      }

      body.innerHTML = `
        <div>
          <h2 class="drawer-title">${escapeHtml(fullName(pid))}</h2>
          <div class="drawer-sub">#${escapeHtml(pid)}</div>
        </div>
        <div class="chip-list">${metaBadges.join("")}</div>
        ${p.note ? `<div class="note-box">${escapeHtml(p.note)}</div>` : ""}
        <div style="font-size:0.76rem;color:var(--text-muted)">
          ${anc.size} direct ancestor(s) · ${desc.size} descendant(s)
        </div>
        ${chipGroup("Parents", parents)}
        ${chipGroup("Spouses", spouses)}
        ${chipGroup("Siblings", siblings)}
        ${chipGroup("Children", children)}
        <div>
          <div class="info-section-title">Relationship Finder</div>
          <select id="rel-finder-select" class="rel-finder-select">${optionsHtml}</select>
          ${relStepsHtml}
        </div>
      `;

      body.querySelectorAll("[data-goto]").forEach(btn => {
        btn.addEventListener("click", () => {
          selectPerson(btn.dataset.goto, true);
        });
      });

      const relSelect = document.getElementById("rel-finder-select");
      if (relSelect) {
        relSelect.addEventListener("change", (e) => {
          relTargetId = e.target.value;
          applySelectionHighlights();
          renderInspector(pid);
        });
      }
    }

    // =========================================================================
    // Syntax-Highlighted JSON Side Panel
    // =========================================================================
    function formatJsonScalar(val, isPersonRef = false) {
      if (val === null) return `<span class="j-num">null</span>`;
      if (typeof val === "number" || typeof val === "boolean") {
        return `<span class="j-num">${String(val)}</span>`;
      }
      const s = String(val);
      const q = escapeHtml(JSON.stringify(s));
      if (isPersonRef && people[s]) {
        return `<span class="j-slug" data-jslug="${escapeHtml(s)}" title="Jump to ${escapeHtml(fullName(s))}">${q}</span>`;
      }
      if (/^\d{4}(-\d{2}(-\d{2})?)?$/.test(s)) {
        return `<span class="j-num">${q}</span>`;
      }
      return `<span class="j-str">${q}</span>`;
    }

    function formatJsonKey(key, isPersonKey = false) {
      const q = escapeHtml(JSON.stringify(key));
      if (isPersonKey && people[key]) {
        return `<span class="j-slug" data-jslug="${escapeHtml(key)}" title="Jump to ${escapeHtml(fullName(key))}">${q}</span>`;
      }
      return `<span class="j-key">${q}</span>`;
    }

    function renderPersonJsonBlock(pid, indent, trailingComma) {
      const p = people[pid];
      const keys = Object.keys(p);
      const lines = [];
      lines.push(`${indent}${formatJsonKey(pid, true)}<span class="j-punc">: {</span>`);
      keys.forEach((k, idx) => {
        const comma = idx < keys.length - 1 ? `<span class="j-punc">,</span>` : "";
        lines.push(`${indent}  ${formatJsonKey(k)}<span class="j-punc">: </span>${formatJsonScalar(p[k])}${comma}`);
      });
      lines.push(`${indent}<span class="j-punc">}${trailingComma ? "," : ""}</span>`);
      const activeCls = pid === selectedPersonId ? " json-entry-active" : "";
      return `<div class="json-entry${activeCls}" id="json-person-${escapeHtml(pid)}">${lines.join("\n")}</div>`;
    }

    function renderFamilyJsonBlock(fam, indent, trailingComma) {
      const keys = Object.keys(fam);
      const isRel = selectedPersonId && (
        fam.father === selectedPersonId ||
        fam.mother === selectedPersonId ||
        (fam.children || []).includes(selectedPersonId)
      );
      const lines = [];
      lines.push(`${indent}<span class="j-punc">{</span>`);
      keys.forEach((k, idx) => {
        const comma = idx < keys.length - 1 ? `<span class="j-punc">,</span>` : "";
        if (k === "children") {
          const ch = fam.children || [];
          if (ch.length === 0) {
            lines.push(`${indent}  ${formatJsonKey("children")}<span class="j-punc">: []</span>${comma}`);
          } else {
            lines.push(`${indent}  ${formatJsonKey("children")}<span class="j-punc">: [</span>`);
            ch.forEach((cid, cidx) => {
              const cComma = cidx < ch.length - 1 ? `<span class="j-punc">,</span>` : "";
              lines.push(`${indent}    ${formatJsonScalar(cid, true)}${cComma}`);
            });
            lines.push(`${indent}  <span class="j-punc">]</span>${comma}`);
          }
        } else {
          const isRef = (k === "father" || k === "mother");
          lines.push(`${indent}  ${formatJsonKey(k)}<span class="j-punc">: </span>${formatJsonScalar(fam[k], isRef)}${comma}`);
        }
      });
      lines.push(`${indent}<span class="j-punc">}${trailingComma ? "," : ""}</span>`);
      const activeCls = isRel ? " json-entry-active" : "";
      return `<div class="json-entry${activeCls}" data-json-fam="${escapeHtml(fam.id)}">${lines.join("\n")}</div>`;
    }

    function getSelectedJsonPayload() {
      if (!selectedPersonId || !people[selectedPersonId]) return TREE_DATA;
      const relatedFamilies = families.filter(f =>
        f.father === selectedPersonId ||
        f.mother === selectedPersonId ||
        (f.children || []).includes(selectedPersonId)
      );
      return {
        person: { [selectedPersonId]: people[selectedPersonId] },
        families: relatedFamilies,
      };
    }

    function renderJsonPanel(scrollToSelected = false) {
      const viewer = document.getElementById("json-viewer");
      const metaLabel = document.getElementById("json-meta-label");
      document.querySelectorAll(".json-scope-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.jscope === jsonScope);
      });

      if (jsonScope === "selected" && selectedPersonId && people[selectedPersonId]) {
        metaLabel.textContent = `#${selectedPersonId}`;
        const relFams = families.filter(f =>
          f.father === selectedPersonId ||
          f.mother === selectedPersonId ||
          (f.children || []).includes(selectedPersonId)
        );
        const out = [];
        out.push(`<span class="j-punc">{</span>`);
        out.push(`  ${formatJsonKey("person")}<span class="j-punc">: {</span>`);
        out.push(renderPersonJsonBlock(selectedPersonId, "    ", false));
        out.push(`  <span class="j-punc">},</span>`);
        if (relFams.length === 0) {
          out.push(`  ${formatJsonKey("families")}<span class="j-punc">: []</span>`);
        } else {
          out.push(`  ${formatJsonKey("families")}<span class="j-punc">: [</span>`);
          relFams.forEach((fam, idx) => {
            out.push(renderFamilyJsonBlock(fam, "    ", idx < relFams.length - 1));
          });
          out.push(`  <span class="j-punc">]</span>`);
        }
        out.push(`<span class="j-punc">}</span>`);
        viewer.innerHTML = out.join("\n");
      } else {
        if (jsonScope === "selected") {
          metaLabel.textContent = "No person selected (showing full)";
        } else if (EMBEDDED_TREE_DATA) {
          metaLabel.textContent = "Embedded in HTML";
        } else {
          const href = resolveJsonUrl(JSON_URL);
          metaLabel.innerHTML = `<a href="${escapeHtml(href)}" target="_blank" rel="noopener" title="Open raw JSON">${escapeHtml(JSON_URL)} ↗</a>`;
        }
        const out = [];
        out.push(`<span class="j-punc">{</span>`);
        // meta
        const meta = TREE_DATA.meta || {};
        const metaKeys = Object.keys(meta);
        out.push(`  ${formatJsonKey("meta")}<span class="j-punc">: {</span>`);
        metaKeys.forEach((mk, idx) => {
          const comma = idx < metaKeys.length - 1 ? `<span class="j-punc">,</span>` : "";
          out.push(`    ${formatJsonKey(mk)}<span class="j-punc">: </span>${formatJsonScalar(meta[mk])}${comma}`);
        });
        out.push(`  <span class="j-punc">},</span>`);

        // people
        const pKeys = Object.keys(people);
        out.push(`  ${formatJsonKey("people")}<span class="j-punc">: {</span>`);
        pKeys.forEach((pid, idx) => {
          out.push(renderPersonJsonBlock(pid, "    ", idx < pKeys.length - 1));
        });
        out.push(`  <span class="j-punc">},</span>`);

        // families
        out.push(`  ${formatJsonKey("families")}<span class="j-punc">: [</span>`);
        families.forEach((fam, idx) => {
          out.push(renderFamilyJsonBlock(fam, "    ", idx < families.length - 1));
        });
        out.push(`  <span class="j-punc">]</span>`);
        out.push(`<span class="j-punc">}</span>`);
        viewer.innerHTML = out.join("\n");
      }

      viewer.querySelectorAll("[data-jslug]").forEach(el => {
        el.addEventListener("click", (e) => {
          e.stopPropagation();
          selectPerson(el.dataset.jslug, true);
        });
      });

      if (scrollToSelected && selectedPersonId && jsonScope === "full") {
        const targetEl = document.getElementById(`json-person-${selectedPersonId}`);
        if (targetEl) {
          targetEl.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      }
    }

    // --- Pan, Zoom & Fit-to-Screen ---
    const viewport = document.getElementById("viewport");
    const scene = document.getElementById("scene");

    function updateTransform() {
      scene.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
    }

    function fitToScreen() {
      if (!currentLayout || !currentLayout.personPos) return;
      const positions = Object.values(currentLayout.personPos);
      if (positions.length === 0) return;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      positions.forEach(p => {
        if (p.x < minX) minX = p.x;
        if (p.y < minY) minY = p.y;
        if (p.x + p.w > maxX) maxX = p.x + p.w;
        if (p.y + p.h > maxY) maxY = p.y + p.h;
      });
      const treeW = Math.max(200, maxX - minX);
      const padX = 50;
      const padTop = 108;
      const availW = window.innerWidth - padX * 2;

      scale = Math.min(1.0, Math.max(0.42, availW / (treeW + 40)));
      panX = Math.max(36, (window.innerWidth - treeW * scale) / 2 - minX * scale);
      panY = padTop - minY * scale;
      updateTransform();
    }

    function centerOnPerson(pid) {
      if (!currentLayout || !currentLayout.personPos[pid]) return;
      const pos = currentLayout.personPos[pid];
      scale = Math.max(scale, 0.85);
      const drawerOffset = window.innerWidth > 960 ? 200 : 0;
      panX = (window.innerWidth - drawerOffset) / 2 - (pos.x + pos.w / 2) * scale;
      panY = window.innerHeight / 2 - (pos.y + pos.h / 2) * scale;
      updateTransform();
    }

    viewport.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".person-card") || e.target.closest(".marriage-node")) return;
      isDragging = true;
      dragStartX = e.clientX - panX;
      dragStartY = e.clientY - panY;
      viewport.classList.add("dragging");
    });

    window.addEventListener("pointermove", (e) => {
      if (!isDragging) return;
      panX = e.clientX - dragStartX;
      panY = e.clientY - dragStartY;
      updateTransform();
    });

    window.addEventListener("pointerup", () => {
      isDragging = false;
      viewport.classList.remove("dragging");
    });

    viewport.addEventListener("click", (e) => {
      if (e.target === viewport || e.target === scene || e.target.id === "nodes-layer") {
        relTargetId = "";
        selectPerson(null);
      }
    });

    viewport.addEventListener("wheel", (e) => {
      e.preventDefault();
      const zoomFactor = Math.exp(-e.deltaY * 0.0015);
      const nextScale = Math.min(2.5, Math.max(0.15, scale * zoomFactor));
      const rect = viewport.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      panX = cx - (cx - panX) * (nextScale / scale);
      panY = cy - (cy - panY) * (nextScale / scale);
      scale = nextScale;
      updateTransform();
    }, { passive: false });

    // --- Search ---
    const searchInput = document.getElementById("search-input");
    const searchResults = document.getElementById("search-results");

    searchInput.addEventListener("input", () => {
      const q = searchInput.value.trim().toLowerCase();
      if (!q) {
        searchResults.classList.remove("open");
        return;
      }
      const matches = Object.keys(people).filter(pid => {
        const p = people[pid];
        const hay = `${p.first || ""} ${p.surname || ""} ${p.nick || ""} ${p.birth || ""} ${p.death || ""} ${p.note || ""} ${pid}`.toLowerCase();
        return hay.includes(q);
      }).slice(0, 12);

      if (matches.length === 0) {
        searchResults.innerHTML = `<div class="search-item"><span class="search-item-sub">No matching person</span></div>`;
      } else {
        searchResults.innerHTML = matches.map(pid => {
          const p = people[pid];
          const nick = p.nick ? ` "${escapeHtml(p.nick)}"` : "";
          return `<div class="search-item" data-pid="${escapeHtml(pid)}">
            <div><strong>${escapeHtml(fullName(pid))}</strong>${nick}</div>
            <div class="search-item-sub">${escapeHtml(lifespan(pid, "dash") || pid)}</div>
          </div>`;
        }).join("");
      }
      searchResults.classList.add("open");

      searchResults.querySelectorAll("[data-pid]").forEach(item => {
        item.addEventListener("click", () => {
          searchResults.classList.remove("open");
          searchInput.value = "";
          selectPerson(item.dataset.pid, true);
        });
      });
    });

    // --- Drawer Tabs & Toolbar ---
    document.querySelectorAll(".dtab-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        setDrawerTab(btn.dataset.dtab, true);
      });
    });

    document.querySelectorAll(".json-scope-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        jsonScope = btn.dataset.jscope;
        renderJsonPanel(true);
      });
    });

    document.getElementById("btn-json").addEventListener("click", () => {
      const drawer = document.getElementById("drawer");
      if (drawer.classList.contains("open") && drawerTab === "json") {
        drawer.classList.remove("open");
        document.getElementById("btn-json").classList.remove("active");
      } else {
        setDrawerTab("json", true);
      }
    });

    document.getElementById("btn-copy-json").addEventListener("click", () => {
      const btn = document.getElementById("btn-copy-json");
      const payload = jsonScope === "selected" ? getSelectedJsonPayload() : TREE_DATA;
      const text = JSON.stringify(payload, null, 2);
      const done = () => {
        btn.textContent = "Copied ✓";
        setTimeout(() => { btn.textContent = "Copy"; }, 1400);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(() => {});
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        done();
      }
    });

    document.getElementById("btn-expand-all").addEventListener("click", () => {
      collapsedFamilies.clear();
      renderActiveView(true);
    });

    document.getElementById("btn-fit").addEventListener("click", fitToScreen);
    document.getElementById("btn-close-drawer").addEventListener("click", () => {
      document.getElementById("drawer").classList.remove("open");
      document.getElementById("btn-json").classList.remove("active");
    });

    window.addEventListener("keydown", (e) => {
      if (e.key === "/" && document.activeElement !== searchInput) {
        e.preventDefault();
        searchInput.focus();
      } else if (e.key === "Escape") {
        searchResults.classList.remove("open");
        relTargetId = "";
        selectPerson(null);
        document.getElementById("drawer").classList.remove("open");
        document.getElementById("btn-json").classList.remove("active");
      }
    });

    // --- Initialize ---
    async function initTreeViewer() {
      try {
        let data = EMBEDDED_TREE_DATA;
        if (!data) {
          const res = await fetch(resolveJsonUrl(JSON_URL), { cache: "no-cache" });
          if (!res.ok) throw new Error(`HTTP ${res.status} loading ${JSON_URL}`);
          data = await res.json();
        }
        buildGraphIndexes(data);
        const title = (TREE_DATA.meta && TREE_DATA.meta.title) || "Family Tree";
        document.title = title;
        document.getElementById("tree-title").textContent = title;
        document.getElementById("tree-stats").textContent =
          `${Object.keys(people).length} people · ${families.length} families`;
        renderActiveView(true);
      } catch (err) {
        const msg = window.location.protocol === "file:"
          ? `Cannot fetch "${JSON_URL}" over file://. Serve this folder over HTTP (e.g. python3 -m http.server) or render with --embed.`
          : `Failed to load "${JSON_URL}": ${err.message}`;
        document.getElementById("nodes-layer").innerHTML =
          `<div style="padding:18px 22px;background:var(--card);border:1px solid var(--border);border-radius:10px;color:var(--amber);max-width:520px;font-size:0.86rem;line-height:1.5;box-shadow:var(--shadow-md)"><strong>Unable to load family tree JSON</strong><br/>${escapeHtml(msg)}</div>`;
      }
    }
    initTreeViewer();
  </script>
</body>
</html>
"""


def render_html(
    data: dict[str, Any],
    out_path: str | Path,
    title: str | None = None,
    json_url: str = "tree.json",
    embed: bool = False,
) -> None:
    tree = normalize_tree(data)
    if title:
        tree["meta"]["title"] = title
    page_title = html.escape(tree["meta"].get("title", "Family Tree"))
    json_url_literal = json.dumps(json_url, ensure_ascii=False)
    embedded_literal = (
        json.dumps(tree, ensure_ascii=False).replace("</", "<\\/")
        if embed
        else "null"
    )
    content = (
        HTML_TEMPLATE.replace("__PAGE_TITLE__", page_title)
        .replace("__JSON_URL__", json_url_literal)
        .replace("__EMBEDDED_JSON__", embedded_literal)
    )
    Path(out_path).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI Subcommands
# ---------------------------------------------------------------------------

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
    q = args.query.lower()
    matches = []
    for pid, p in data["people"].items():
        hay = " ".join(
            str(p.get(k, "")) for k in ("first", "surname", "nick", "birth", "death", "note")
        ).lower() + f" {pid}"
        if q in hay:
            matches.append((pid, p))
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

    for fam in data["families"]:
        if pid in (fam.get("children") or []):
            print(f"Parents (family {fam['id']}):")
            if fam.get("father") in people:
                print(f"  Father: {format_person_brief(fam['father'], people[fam['father']])}")
            if fam.get("mother") in people:
                print(f"  Mother: {format_person_brief(fam['mother'], people[fam['mother']])}")
            sibs = [c for c in (fam.get("children") or []) if c != pid and c in people]
            if sibs:
                print("  Siblings:")
                for s in sibs:
                    print(f"    - {format_person_brief(s, people[s])}")

    for fam in data["families"]:
        if pid in (fam.get("father"), fam.get("mother")):
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
    people = data["people"]
    used = set(people.keys())
    pid = args.id or make_person_slug(
        args.first, args.surname or "", args.nick or "", args.birth or "", used
    )
    if pid in people:
        print(f"ERROR: Person ID '{pid}' already exists.", file=sys.stderr)
        return 1
    entry: dict[str, Any] = {"first": args.first.strip()}
    for k in ("surname", "nick", "gender", "birth", "death", "note"):
        val = getattr(args, k, None)
        if val:
            entry[k] = val.strip()
    people[pid] = entry
    save_tree(args.json_file, data)
    print(f"Added {format_person_brief(pid, entry)}")
    return 0


def cmd_update_person(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    people = data["people"]
    pid = args.person_id
    if pid not in people:
        print(f"ERROR: Person '{pid}' not found.", file=sys.stderr)
        return 1
    p = people[pid]
    for k in ("first", "surname", "nick", "gender", "birth", "death", "note"):
        val = getattr(args, k, None)
        if val is not None:
            if val == "":
                p.pop(k, None)
            else:
                p[k] = val.strip()
    save_tree(args.json_file, data)
    print(f"Updated {format_person_brief(pid, p)}")
    return 0


def cmd_add_spouse(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    people = data["people"]
    a, b = args.person_a, args.person_b
    for pid in (a, b):
        if pid not in people:
            print(f"ERROR: Person '{pid}' not found.", file=sys.stderr)
            return 1

    ga, gb = people[a].get("gender"), people[b].get("gender")
    if ga == "F" or gb == "M":
        father, mother = b, a
    else:
        father, mother = a, b

    for fam in data["families"]:
        if (fam.get("father") == father and fam.get("mother") == mother) or (
            fam.get("father") == mother and fam.get("mother") == father
        ):
            if getattr(args, "married", None):
                fam["married"] = args.married.strip()
                save_tree(args.json_file, data)
                print(f"Updated marriage date on family '{fam['id']}' ({fam['married']}).")
            else:
                print(f"Family '{fam['id']}' already links '{a}' and '{b}'.")
            return 0

    used_fids = {f["id"] for f in data["families"]}
    fid = make_family_id(father, mother, [], used_fids)
    new_fam: dict[str, Any] = {"id": fid, "father": father, "mother": mother}
    if getattr(args, "married", None):
        new_fam["married"] = args.married.strip()
    new_fam["children"] = []
    data["families"].append(new_fam)
    save_tree(args.json_file, data)
    print(f"Created family '{fid}' linking {father} and {mother}.")
    return 0


def cmd_add_child(args: argparse.Namespace) -> int:
    data = load_tree(args.json_file)
    people = data["people"]
    child_id = args.child_id
    if child_id not in people:
        print(f"ERROR: Child '{child_id}' not found in people.", file=sys.stderr)
        return 1

    target_fam = None
    if args.family:
        for fam in data["families"]:
            if fam["id"] == args.family:
                target_fam = fam
                break
        if not target_fam:
            print(f"ERROR: Family '{args.family}' not found.", file=sys.stderr)
            return 1
    else:
        fa, mo = args.father, args.mother
        for fam in data["families"]:
            if (fa and fam.get("father") == fa and (not mo or fam.get("mother") == mo)) or (
                mo and fam.get("mother") == mo and (not fa or fam.get("father") == fa)
            ):
                target_fam = fam
                break
        if not target_fam:
            used_fids = {f["id"] for f in data["families"]}
            fid = make_family_id(fa, mo, [child_id], used_fids)
            target_fam = {"id": fid, "children": []}
            if fa:
                target_fam["father"] = fa
            if mo:
                target_fam["mother"] = mo
            data["families"].append(target_fam)

    if child_id not in target_fam["children"]:
        target_fam["children"].append(child_id)
    save_tree(args.json_file, data)
    print(f"Added child '{child_id}' to family '{target_fam['id']}'.")
    return 0


def build_parser() -> argparse.ArgumentParser:
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
    p_addp.add_argument("--first", required=True, help="First name")
    p_addp.add_argument("--surname", help="Surname")
    p_addp.add_argument("--nick", help="Nickname")
    p_addp.add_argument("--gender", choices=["M", "F", "U"], help="Gender (M/F/U)")
    p_addp.add_argument("--birth", help="Birth year")
    p_addp.add_argument("--death", help="Death year")
    p_addp.add_argument("--note", help="Person note")
    p_addp.add_argument("--id", help="Explicit slug ID")
    p_addp.set_defaults(func=cmd_add_person)

    p_updp = sub.add_parser("update-person", help="Update fields on an existing person")
    p_updp.add_argument("json_file", help="Path to .json file")
    p_updp.add_argument("person_id", help="Person slug ID")
    p_updp.add_argument("--first", help="First name")
    p_updp.add_argument("--surname", help="Surname (pass '' to clear)")
    p_updp.add_argument("--nick", help="Nickname (pass '' to clear)")
    p_updp.add_argument("--gender", help="Gender (M/F/U)")
    p_updp.add_argument("--birth", help="Birth year (pass '' to clear)")
    p_updp.add_argument("--death", help="Death year (pass '' to clear)")
    p_updp.add_argument("--note", help="Note (pass '' to clear)")
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


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
