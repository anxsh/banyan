"""Gramps SQLite (`sqlite.db`) and XML (`.gramps`) import and export codecs."""

from __future__ import annotations

import builtins
from datetime import date, datetime
import gzip
import html
import io
from pathlib import Path
import pickle
import re
import sqlite3
from typing import Any
import xml.etree.ElementTree as ET

from banyan.model import (
    DEFAULT_TITLE,
    PERSON_OPTIONAL_FIELDS,
    make_family_id,
    make_person_slug,
    normalize_tree,
    slugify,
)

GRAMPS_XML_NS = "http://gramps-project.org/xml/1.7.1/"
GRAMPS_XML_VERSION = "5.1.4"

# Gramps 5.x SQLite serialized tuple field indices (from gramps.gen.lib.serialize)
_EV_HANDLE = 0
_EV_TYPE = 2
_EV_DATE = 3
_EV_CHANGE = 10

_NT_HANDLE = 0
_NT_TEXT = 2
_NT_CHANGE = 5

_PR_HANDLE = 0
_PR_GENDER = 2
_PR_PRIMARY_NAME = 3
_PR_DEATH_IDX = 5
_PR_BIRTH_IDX = 6
_PR_EVENT_REFS = 7
_PR_NOTE_REFS = 16
_PR_CHANGE = 17

_NM_FIRST = 4
_NM_SURNAME_LIST = 5
_NM_NICK = 13

_SUR_SURNAME = 0
_SUR_PREFIX = 1
_SUR_PRIMARY = 2

_FM_FATHER = 2
_FM_MOTHER = 3
_FM_CHILD_REFS = 4
_FM_EVENT_REFS = 6
_FM_CHANGE = 12

_REF_HANDLE = 3

# Gramps Date modifier codes (gramps.gen.lib.date)
_DATE_MOD_PREFIXES: dict[int, str] = {1: "before ", 2: "after ", 3: "about "}
_DATE_MOD_RANGE = 4
_DATE_MOD_SPAN = 5
_XML_MOD_TYPES: tuple[str, ...] = ("before", "after", "about")
_GRAMPS_GENDER_MAP: dict[int, str] = {0: "F", 1: "M"}

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

    if modifier in (_DATE_MOD_RANGE, _DATE_MOD_SPAN) and isinstance(dateval, (tuple, list)) and len(dateval) >= 8:
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

    prefix = _DATE_MOD_PREFIXES.get(modifier, "")
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
        if not isinstance(s, (tuple, list)) or len(s) <= _SUR_PRIMARY:
            continue
        sur = str(s[_SUR_SURNAME] or "").strip()
        prefix = str(s[_SUR_PREFIX] or "").strip()
        combined = " ".join(part for part in (prefix, sur) if part)
        if combined:
            if bool(s[_SUR_PRIMARY]):
                primary_parts.append(combined)
            else:
                fallback_parts.append(combined)
    return " ".join(primary_parts or fallback_parts)


def _resolve_person_events_and_notes(
    erefs: list[str],
    nrefs: list[str],
    events: dict[str, tuple[str, str]],
    notes: dict[str, str],
    *,
    birth_idx: int = -1,
    death_idx: int = -1,
) -> tuple[str, str, str]:
    """Resolve `(birth, death, note)` strings from person event/note handle lists."""
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
    return birth, death, "\n\n".join(person_notes)


def _resolve_family_marriage(erefs: list[str], events: dict[str, tuple[str, str]]) -> str:
    """Resolve the first Marriage event date string from a family's event handle list."""
    for eh in erefs:
        if eh in events and events[eh][0] == "Marriage":
            return events[eh][1]
    return ""


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
        for field in PERSON_OPTIONAL_FIELDS:
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
            handle = str(obj[_EV_HANDLE] or "")
            ev_type = _resolve_event_type(obj[_EV_TYPE])
            val = _format_gramps_date(obj[_EV_DATE])
            if len(obj) > _EV_CHANGE:
                track_change(obj[_EV_CHANGE])
            if handle and ev_type and val:
                events[handle] = (ev_type, val)

        # 2. Notes
        notes: dict[str, str] = {}
        for (blob,) in cur.execute("SELECT blob_data FROM note"):
            if blob is None:
                continue
            obj = _safe_unpickle(blob)
            handle = str(obj[_NT_HANDLE] or "")
            text_tuple = obj[_NT_TEXT] if len(obj) > _NT_TEXT else ""
            text = str(
                text_tuple[0] if isinstance(text_tuple, (tuple, list)) else (text_tuple or "")
            ).strip()
            if len(obj) > _NT_CHANGE:
                track_change(obj[_NT_CHANGE])
            if handle and text:
                notes[handle] = text

        # 3. People
        raw_people: list[dict[str, Any]] = []
        for (blob,) in cur.execute("SELECT blob_data FROM person ORDER BY gramps_id, handle"):
            if blob is None:
                continue
            obj = _safe_unpickle(blob)
            handle = str(obj[_PR_HANDLE] or "")
            gender = _GRAMPS_GENDER_MAP.get(obj[_PR_GENDER], "U")
            pn = obj[_PR_PRIMARY_NAME]
            first = str(pn[_NM_FIRST] or "").strip() if len(pn) > _NM_FIRST else ""
            surname = _extract_surname(pn[_NM_SURNAME_LIST] if len(pn) > _NM_SURNAME_LIST else [])
            nick = str(pn[_NM_NICK] or "").strip() if len(pn) > _NM_NICK else ""
            death_idx = obj[_PR_DEATH_IDX] if len(obj) > _PR_DEATH_IDX else -1
            birth_idx = obj[_PR_BIRTH_IDX] if len(obj) > _PR_BIRTH_IDX else -1
            erefs = [str(er[_REF_HANDLE]) for er in (obj[_PR_EVENT_REFS] or []) if len(er) > _REF_HANDLE]
            nrefs = [str(n) for n in (obj[_PR_NOTE_REFS] or [])]
            if len(obj) > _PR_CHANGE:
                track_change(obj[_PR_CHANGE])

            birth, death, note = _resolve_person_events_and_notes(
                erefs, nrefs, events, notes, birth_idx=birth_idx, death_idx=death_idx
            )
            raw_people.append({
                "handle": handle,
                "first": first,
                "surname": surname,
                "nick": nick,
                "gender": gender,
                "birth": birth,
                "death": death,
                "note": note,
            })

        # 4. Families
        raw_families: list[dict[str, Any]] = []
        for (blob,) in cur.execute("SELECT blob_data FROM family ORDER BY gramps_id, handle"):
            if blob is None:
                continue
            obj = _safe_unpickle(blob)
            father_handle = obj[_FM_FATHER] if len(obj) > _FM_FATHER else None
            mother_handle = obj[_FM_MOTHER] if len(obj) > _FM_MOTHER else None
            child_handles = [
                str(cr[_REF_HANDLE]) for cr in (obj[_FM_CHILD_REFS] or []) if len(cr) > _REF_HANDLE
            ]
            erefs = [str(er[_REF_HANDLE]) for er in (obj[_FM_EVENT_REFS] or []) if len(er) > _REF_HANDLE]
            if len(obj) > _FM_CHANGE:
                track_change(obj[_FM_CHANGE])

            raw_families.append({
                "father_handle": father_handle,
                "mother_handle": mother_handle,
                "child_handles": child_handles,
                "married": _resolve_family_marriage(erefs, events),
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
    return _build_tree_from_raw(raw_people, raw_families, resolved_title or DEFAULT_TITLE, updated_date)


def read_gramps_xml(path: str | Path) -> ET.Element:
    """Read a .gramps file (supports both plain XML and gzip-compressed XML)."""
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return ET.fromstring(raw.decode("utf-8"))


def _parse_xml_event_date(ev: ET.Element, ns: dict[str, str]) -> str:
    """Extract formatted date/range/modifier string from a Gramps XML `<event>` element."""
    dateval = ev.find("g:dateval", ns)
    if dateval is not None:
        val = (dateval.attrib.get("val", "")).strip()
        if not val:
            return ""
        dtype = (dateval.attrib.get("type", "")).strip().lower()
        if dtype in _XML_MOD_TYPES and not val.lower().startswith(f"{dtype} "):
            return f"{dtype} {val}"
        return val

    for tag in ("g:daterange", "g:datespan"):
        el = ev.find(tag, ns)
        if el is not None:
            start = (el.attrib.get("start", "")).strip()
            stop = (el.attrib.get("stop", "")).strip()
            if start and stop:
                return f"{start}–{stop}"
            return start or stop

    datestr = ev.find("g:datestr", ns)
    if datestr is not None:
        return (datestr.attrib.get("val", "")).strip()
    return ""


def import_gramps_xml(path: str | Path, title: str | None = None) -> dict[str, Any]:
    """Import a plain or gzip-compressed Gramps XML (.gramps) file into banyan JSON."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"XML file not found: '{path}'.")
    root = read_gramps_xml(p)
    ns_uri = root.tag.split("}")[0].strip("{") if root.tag.startswith("{") else GRAMPS_XML_NS
    ns = {"g": ns_uri}

    events: dict[str, tuple[str, str]] = {}
    for ev in root.findall("g:events/g:event", ns):
        handle = ev.attrib.get("handle", "")
        ev_type = (ev.findtext("g:type", "", ns) or "").strip()
        val = _parse_xml_event_date(ev, ns)
        if handle and ev_type and val:
            events[handle] = (ev_type, val)

    notes: dict[str, str] = {}
    for nt in root.findall("g:notes/g:note", ns):
        handle = nt.attrib.get("handle", "")
        text = (nt.findtext("g:text", "", ns) or "").strip()
        if handle and text:
            notes[handle] = text

    raw_people: list[dict[str, Any]] = []
    for person_el in root.findall("g:people/g:person", ns):
        handle = person_el.attrib.get("handle", "")
        gender = (person_el.findtext("g:gender", "", ns) or "").strip()
        name_el = person_el.find("g:name", ns)
        first = (name_el.findtext("g:first", "", ns) if name_el is not None else "") or ""
        surname = (name_el.findtext("g:surname", "", ns) if name_el is not None else "") or ""
        nick = (name_el.findtext("g:nick", "", ns) if name_el is not None else "") or ""

        erefs = [er.attrib.get("hlink", "") for er in person_el.findall("g:eventref", ns)]
        nrefs = [nr.attrib.get("hlink", "") for nr in person_el.findall("g:noteref", ns)]
        birth, death, note = _resolve_person_events_and_notes(erefs, nrefs, events, notes)

        raw_people.append({
            "handle": handle,
            "first": first.strip(),
            "surname": surname.strip(),
            "nick": nick.strip(),
            "gender": gender,
            "birth": birth,
            "death": death,
            "note": note,
        })

    raw_families: list[dict[str, Any]] = []
    for fam in root.findall("g:families/g:family", ns):
        fa_el = fam.find("g:father", ns)
        mo_el = fam.find("g:mother", ns)
        erefs = [er.attrib.get("hlink", "") for er in fam.findall("g:eventref", ns)]
        raw_families.append({
            "father_handle": fa_el.attrib.get("hlink", "") if fa_el is not None else None,
            "mother_handle": mo_el.attrib.get("hlink", "") if mo_el is not None else None,
            "child_handles": [c.attrib.get("hlink", "") for c in fam.findall("g:childref", ns)],
            "married": _resolve_family_marriage(erefs, events),
        })

    created_el = root.find("g:header/g:created", ns)
    updated_date = (
        created_el.attrib.get("date")
        if created_el is not None and created_el.attrib.get("date")
        else date.today().isoformat()
    )
    resolved_title = title or Path(path).stem.replace("-", " ").replace("_", " ").title()
    return _build_tree_from_raw(raw_people, raw_families, resolved_title, updated_date)


def _format_xml_date_tag(val: str) -> str:
    """Serialize a banyan date string into a Gramps `<dateval>` or `<daterange>` XML tag."""
    s = val.strip()
    mod_match = re.match(r"^(before|after|about)\s+(.+)$", s, re.IGNORECASE)
    if mod_match:
        mod = mod_match.group(1).lower()
        inner = mod_match.group(2).strip()
        return f'<dateval val="{html.escape(inner)}" type="{mod}"/>'

    if "–" in s:
        start, stop = [part.strip() for part in s.split("–", 1)]
        if start and stop:
            return f'<daterange start="{html.escape(start)}" stop="{html.escape(stop)}"/>'

    return f'<dateval val="{html.escape(s)}"/>'


def _change_timestamp_from_meta(meta: dict[str, Any]) -> str:
    """Derive a Unix epoch string from `meta.updated` for Gramps XML change attributes."""
    updated = str(meta.get("updated") or "").strip()
    if updated:
        try:
            return str(int(datetime.fromisoformat(updated).timestamp()))
        except ValueError:
            pass
    return str(int(datetime.now().timestamp()))


def export_gramps(data: dict[str, Any], out_path: str | Path) -> None:
    """Export JSON tree back to Gramps XML 1.7.1 format."""
    tree = normalize_tree(data)
    people = tree["people"]
    families = tree["families"]
    updated_str = tree["meta"].get("updated", date.today().isoformat())
    change_ts = _change_timestamp_from_meta(tree["meta"])

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE database PUBLIC "-//Gramps//DTD Gramps XML 1.7.1//EN"',
        '"http://gramps-project.org/xml/1.7.1/grampsxml.dtd">',
        f'<database xmlns="{GRAMPS_XML_NS}">',
        "  <header>",
        f'    <created date="{html.escape(updated_str)}" version="{GRAMPS_XML_VERSION}"/>',
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
        lines.append(f'    <event handle="{eh}" change="{change_ts}" id="{eid}">')
        lines.append(f"      <type>{html.escape(etype)}</type>")
        lines.append(f"      {_format_xml_date_tag(val)}")
        lines.append("    </event>")
    lines.append("  </events>")

    lines.append("  <people>")
    for idx, (pid, p) in enumerate(people.items()):
        ph = person_handles[pid]
        gid = f"I{idx:04d}"
        lines.append(f'    <person handle="{ph}" change="{change_ts}" id="{gid}">')
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
        lines.append(f'    <family handle="{fh}" change="{change_ts}" id="{fid}">')
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
            lines.append(f'    <note handle="{nh}" change="{change_ts}" id="{nid}" type="Person Note">')
            lines.append(f"      <text>{html.escape(text)}</text>")
            lines.append("    </note>")
        lines.append("  </notes>")

    lines.append("</database>\n")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
