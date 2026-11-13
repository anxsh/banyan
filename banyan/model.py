"""Core banyan JSON schema, normalization, validation, graph queries, and mutations."""

from __future__ import annotations

from collections import deque
import copy
from datetime import date
import json
from pathlib import Path
import re
from typing import Any

DEFAULT_TITLE = "Family Tree"
PERSON_REQUIRED_FIELDS: tuple[str, ...] = ("first",)
PERSON_OPTIONAL_FIELDS: tuple[str, ...] = (
    "surname",
    "nick",
    "gender",
    "birth",
    "death",
    "note",
)
PERSON_ALL_FIELDS: tuple[str, ...] = PERSON_REQUIRED_FIELDS + PERSON_OPTIONAL_FIELDS
VALID_GENDERS: tuple[str, ...] = ("M", "F", "U")

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def extract_year(val: str | None) -> int | None:
    """Extract the first 4-digit year from a date string like '1996' or '1996-02-28'."""
    if not val:
        return None
    m = re.search(r"\b(\d{4})\b", str(val))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Slug & ID Helpers
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
    """Load and normalize a banyan JSON family tree file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return normalize_tree(data)


def save_tree(path: str | Path, data: dict[str, Any], *, touch_updated: bool = True) -> None:
    """Normalize and write a banyan JSON family tree file."""
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
        meta["title"] = DEFAULT_TITLE
    if "updated" not in meta:
        meta["updated"] = date.today().isoformat()

    clean_people: dict[str, dict[str, Any]] = {}
    for pid, p in (data.get("people") or {}).items():
        cp: dict[str, Any] = {"first": str(p.get("first") or "").strip()}
        for field in PERSON_OPTIONAL_FIELDS:
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
# Validation
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
        if gender and gender not in VALID_GENDERS:
            warnings.append(
                f"Person '{pid}' has non-standard gender '{gender}' (expected {', '.join(VALID_GENDERS)})."
            )
        b, d = p.get("birth"), p.get("death")
        by, dy = extract_year(b), extract_year(d)
        if by is not None and dy is not None and by > dy:
            errors.append(f"Person '{pid}' has birth year ({by}) after death year ({dy}).")
        elif (
            b
            and d
            and _ISO_DATE_RE.fullmatch(str(b))
            and _ISO_DATE_RE.fullmatch(str(d))
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


def ensure_valid_tree(data: dict[str, Any]) -> None:
    """Raise ValueError if `data` has any referential integrity or schema errors."""
    errors, _ = validate_tree(data)
    if errors:
        raise ValueError("; ".join(errors))


# ---------------------------------------------------------------------------
# Display & Graph Queries
# ---------------------------------------------------------------------------

def full_name(p: dict[str, Any]) -> str:
    """Return 'First Surname' display name for a person record."""
    first = p.get("first", "").strip()
    sur = p.get("surname", "").strip()
    return f"{first} {sur}".strip()


def format_person_brief(pid: str, p: dict[str, Any]) -> str:
    """Format a compact one-line summary for a person."""
    name = full_name(p)
    nick = f' "{p["nick"]}"' if p.get("nick") else ""
    b = p.get("birth", "")
    d = p.get("death", "")
    dates = f" ({b}–{d})" if (b or d) else ""
    return f"{name}{nick}{dates} [{pid}]"


def find_birth_family(data: dict[str, Any], pid: str) -> dict[str, Any] | None:
    """Return the family in which `pid` appears as a child, if any."""
    for fam in data.get("families") or []:
        if pid in (fam.get("children") or []):
            return fam
    return None


def find_parent_families(data: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    """Return all families in which `pid` is listed as father or mother."""
    return [
        fam
        for fam in (data.get("families") or [])
        if pid in (fam.get("father"), fam.get("mother"))
    ]


def find_couple_family(
    data: dict[str, Any], person_a: str, person_b: str
) -> dict[str, Any] | None:
    """Return the family linking `person_a` and `person_b` as spouses, if any."""
    pair = {person_a, person_b}
    for fam in data.get("families") or []:
        if fam.get("father") and fam.get("mother") and {fam["father"], fam["mother"]} == pair:
            return fam
    return None


def search_people(data: dict[str, Any], query: str) -> list[tuple[str, dict[str, Any]]]:
    """Return `(pid, person)` pairs matching `query` case-insensitively."""
    q = query.strip().lower()
    if not q:
        return []
    matches: list[tuple[str, dict[str, Any]]] = []
    for pid, p in (data.get("people") or {}).items():
        hay = " ".join(str(p.get(k, "")) for k in PERSON_ALL_FIELDS).lower() + f" {pid.lower()}"
        if q in hay:
            matches.append((pid, p))
    return matches


def build_relationship_graph(data: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """Build directed labeled relationship adjacency list across parents, spouses, and siblings."""
    people = data["people"]
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
                par_g = people[parent].get("gender")
                par_lbl = "father" if par_g == "M" else ("mother" if par_g == "F" else "parent")
                for ch in children:
                    ch_g = people[ch].get("gender")
                    child_lbl = "son" if ch_g == "M" else ("daughter" if ch_g == "F" else "child")
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

    return adj


def find_relationship_path(
    data: dict[str, Any], start_id: str, target_id: str
) -> list[tuple[str, str, str]] | None:
    """BFS across parent/child/sibling/spouse edges returning `(from_id, relation, to_id)` steps."""
    people = data["people"]
    if start_id not in people or target_id not in people:
        return None
    if start_id == target_id:
        return []

    adj = build_relationship_graph(data)
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
# Pure, Validated Tree Mutations
# ---------------------------------------------------------------------------

def _commit_validated_candidate(data: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Validate `candidate` and copy its mutated collections back onto `data`."""
    ensure_valid_tree(candidate)
    data["people"] = candidate["people"]
    data["families"] = candidate["families"]


def add_person(
    data: dict[str, Any],
    *,
    first: str,
    person_id: str | None = None,
    **fields: str | None,
) -> tuple[str, dict[str, Any]]:
    """Add a validated person entry to `data` and return `(pid, person_dict)`."""
    first_clean = (first or "").strip()
    if not first_clean:
        raise ValueError("Required field 'first' cannot be empty.")

    gender = (fields.get("gender") or "").strip()
    if gender and gender not in VALID_GENDERS:
        raise ValueError(f"Invalid gender '{gender}' (expected {', '.join(VALID_GENDERS)}).")

    candidate = copy.deepcopy(data)
    people = candidate.setdefault("people", {})
    used = set(people.keys())
    pid = person_id.strip() if person_id else make_person_slug(
        first_clean,
        (fields.get("surname") or "").strip(),
        (fields.get("nick") or "").strip(),
        (fields.get("birth") or "").strip(),
        used,
    )
    if pid in people:
        raise ValueError(f"Person ID '{pid}' already exists.")

    entry: dict[str, Any] = {"first": first_clean}
    for key in PERSON_OPTIONAL_FIELDS:
        val = fields.get(key)
        if val is not None and str(val).strip() != "":
            entry[key] = str(val).strip()
    people[pid] = entry

    _commit_validated_candidate(data, candidate)
    return pid, data["people"][pid]


def update_person(
    data: dict[str, Any],
    person_id: str,
    **fields: str | None,
) -> dict[str, Any]:
    """Update fields on an existing person (`""` clears optional fields) and validate."""
    if person_id not in data.get("people", {}):
        raise ValueError(f"Person '{person_id}' not found.")

    candidate = copy.deepcopy(data)
    p = candidate["people"][person_id]

    for key in PERSON_ALL_FIELDS:
        val = fields.get(key)
        if val is None:
            continue
        cleaned = str(val).strip()
        if cleaned == "":
            if key in PERSON_REQUIRED_FIELDS:
                raise ValueError(f"Cannot clear required field '{key}'.")
            p.pop(key, None)
        else:
            if key == "gender" and cleaned not in VALID_GENDERS:
                raise ValueError(
                    f"Invalid gender '{cleaned}' (expected {', '.join(VALID_GENDERS)})."
                )
            p[key] = cleaned

    _commit_validated_candidate(data, candidate)
    return data["people"][person_id]


def add_spouse(
    data: dict[str, Any],
    person_a: str,
    person_b: str,
    *,
    married: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Link two people as spouses (or update `married`), returning `(family, created)`."""
    people = data.get("people", {})
    for pid in (person_a, person_b):
        if pid not in people:
            raise ValueError(f"Person '{pid}' not found.")
    if person_a == person_b:
        raise ValueError(f"Cannot link '{person_a}' as spouse to themselves.")

    candidate = copy.deepcopy(data)
    existing = find_couple_family(candidate, person_a, person_b)
    if existing is not None:
        if married is not None:
            cleaned = married.strip()
            if cleaned:
                existing["married"] = cleaned
            else:
                existing.pop("married", None)
        _commit_validated_candidate(data, candidate)
        matched = find_couple_family(data, person_a, person_b)
        assert matched is not None
        return matched, False

    ga = people[person_a].get("gender")
    gb = people[person_b].get("gender")
    if ga == "F" or gb == "M":
        father, mother = person_b, person_a
    else:
        father, mother = person_a, person_b

    used_fids = {f["id"] for f in candidate.get("families", []) if f.get("id")}
    fid = make_family_id(father, mother, [], used_fids)
    new_fam: dict[str, Any] = {"id": fid, "father": father, "mother": mother}
    if married is not None and married.strip():
        new_fam["married"] = married.strip()
    new_fam["children"] = []
    candidate.setdefault("families", []).append(new_fam)

    _commit_validated_candidate(data, candidate)
    return data["families"][-1], True


def add_child(
    data: dict[str, Any],
    child_id: str,
    *,
    father: str | None = None,
    mother: str | None = None,
    family_id: str | None = None,
) -> dict[str, Any]:
    """Add `child_id` to an existing or new family and validate the resulting tree."""
    people = data.get("people", {})
    if child_id not in people:
        raise ValueError(f"Child '{child_id}' not found in people.")
    if not family_id and not father and not mother:
        raise ValueError("Specify --family, --father, or --mother when adding a child.")
    for parent_role, parent_id in (("father", father), ("mother", mother)):
        if parent_id and parent_id not in people:
            raise ValueError(f"{parent_role.capitalize()} '{parent_id}' not found in people.")

    candidate = copy.deepcopy(data)
    families = candidate.setdefault("families", [])
    target_idx: int | None = None

    if family_id:
        for idx, fam in enumerate(families):
            if fam.get("id") == family_id:
                target_idx = idx
                break
        if target_idx is None:
            raise ValueError(f"Family '{family_id}' not found.")
    else:
        for idx, fam in enumerate(families):
            if (father and fam.get("father") == father and (not mother or fam.get("mother") == mother)) or (
                mother and fam.get("mother") == mother and (not father or fam.get("father") == father)
            ):
                target_idx = idx
                break
        if target_idx is None:
            used_fids = {f["id"] for f in families if f.get("id")}
            fid = make_family_id(father, mother, [child_id], used_fids)
            new_fam: dict[str, Any] = {"id": fid, "children": []}
            if father:
                new_fam["father"] = father
            if mother:
                new_fam["mother"] = mother
            families.append(new_fam)
            target_idx = len(families) - 1

    target_fam = families[target_idx]
    children = target_fam.setdefault("children", [])
    if child_id not in children:
        children.append(child_id)

    _commit_validated_candidate(data, candidate)
    return data["families"][target_idx]
