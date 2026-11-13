# banyan

Like a banyan tree whose branches drop new roots across generations, `banyan` is a lightweight, zero-dependency toolkit for managing family trees in a clean JSON format, converting from Gramps XML (`.gramps`) or Gramps SQLite databases (`sqlite.db`), modifying trees via AI chat, and rendering an interactive Warm Parchment HTML visualization.

## Features

- **Clean, AI-Friendly JSON Format**: Human-readable slug IDs (`"eleanor-sterling"`), inlined birth/death/nickname/note fields, and a single source of truth for family relationships (`families`).
- **Gramps XML & Database Interoperability**: Import directly from a Gramps `sqlite.db` database (`import-gramps-db`) or `.gramps` XML archive (`import-gramps-xml`, plain or gzipped), and export back to Gramps XML 1.7.1.
- **Separate JSON Source of Truth + Static HTML Viewer**:
  - **Clean Separation**: `banyan.py render` generates an HTML viewer that fetches the sibling `.json` file from the same folder by default, so both can be served at independent URLs and edits to `tree.json` take effect immediately without regenerating HTML (pass `--embed` if you need a single `file://`-openable `.html`).
  - **Classic Left-to-Right Descendant Layout**: Top-aligned generational columns (`Generation 1..N`), indented stacked spouses (`⚭`), orthogonal bus connectors, and collapsible branch nodes (`−` / `+N`).
  - **Clean Rich Dossier Cards**: Displays full name, lifespan (`YYYY–YYYY` or `b. YYYY`), visually distinct living (sage green) vs. deceased (warm amber-gold) age pills (`N yrs`), nickname, and biographical note or child summary (with optional marriage year, omitting redundant spouse names), with empty sections omitted cleanly.
  - **Warm Parchment Theme**: Ivory cards on a smooth dot-free cream parchment canvas with espresso ink, burnt-sienna accents, uniform `1px` borders, and spotlight + surface-tint lineage highlighting.
  - **Inspector & `{ } JSON` Side Panel**: Click any person for lineage highlighting and the **Relationship Finder**, or toggle the syntax-highlighted **`{ } JSON`** panel (`Full Tree` / `Selected`, `Copy`, and clickable person slugs).
- **Agent Skill Included**: Ready-to-use `SKILL.md` (`skills/banyan/SKILL.md` & `.claude/skills/banyan/SKILL.md`) for modifying the tree through natural-language chat in Claude Code, Antigravity, or other coding agents.

---

## Quickstart

Requires **Python 3.10+** (standard library only, no `pip install` needed).

```bash
# 1. Convert an existing Gramps XML file into JSON
./banyan.py import-gramps-xml examples/sample.gramps -o examples/sample.json

# 2. Validate referential integrity
./banyan.py validate examples/sample.json

# 3. Render a single-page HTML viewer (fetches sample.json over HTTP; pass --embed for file://)
./banyan.py render examples/sample.json -o examples/sample.html
```

To manage your own personal family tree without committing private data to git, store it inside the gitignored `private/` directory:

```bash
./banyan.py import-gramps-db /path/to/sqlite.db -o private/tree.json --title "My Family Tree"
# Or from an exported XML archive:
# ./banyan.py import-gramps-xml /path/to/my-family.gramps -o private/tree.json --title "My Family Tree"
./banyan.py render private/tree.json -o private/index.html
```

---

## JSON Schema

```json
{
  "meta": {
    "title": "Sterling & Okafor Family Tree",
    "updated": "2026-11-12"
  },
  "people": {
    "clara-sterling": {
      "first": "Clara",
      "surname": "Sterling",
      "nick": "Nana",
      "gender": "F",
      "birth": "1904",
      "death": "1991",
      "note": "Founded the Sterling community library in 1948"
    },
    "arthur-sterling": {
      "first": "Arthur",
      "surname": "Sterling",
      "gender": "M",
      "birth": "1901",
      "death": "1978"
    }
  },
  "families": [
    {
      "id": "arthur-sterling+clara-sterling",
      "father": "arthur-sterling",
      "mother": "clara-sterling",
      "children": [
        "eleanor-sterling",
        "walter-sterling"
      ]
    }
  ]
}
```

### Field Reference

- **`people.<slug>`**:
  - `first` *(required)*: Given name.
  - `surname` *(optional)*: Family name / surname.
  - `nick` *(optional)*: Nickname or informal name.
  - `gender` *(optional)*: `"M"`, `"F"`, or `"U"`.
  - `birth` *(optional)*: Birth year or date string (e.g. `"1950"`).
  - `death` *(optional)*: Death year or date string (e.g. `"1996"`).
  - `note` *(optional)*: Biographical note.
- **`families[]`**:
  - `id`: Readable identifier for the family unit.
  - `father` *(optional)*: Person slug ID.
  - `mother` *(optional)*: Person slug ID.
  - `married` *(optional)*: Marriage year or date string (e.g. `"2007"`).
  - `children`: Ordered list of child person slug IDs (`[]` for childless couples). Sibling groups with unknown parents omit both `father` and `mother`.

---

## CLI Reference

```bash
# Importing from a Gramps SQLite database or XML export
./banyan.py import-gramps-db /path/to/sqlite.db -o private/tree.json --title "My Tree"
./banyan.py import-gramps-xml /path/to/family.gramps -o private/tree.json --title "My Tree"

# Querying
./banyan.py stats private/tree.json
./banyan.py search private/tree.json "Nana"
./banyan.py show private/tree.json eleanor-sterling
./banyan.py relate private/tree.json aria-patel clara-sterling

# Modifying via CLI (or edit the JSON directly)
./banyan.py add-person private/tree.json --first "Maya" --surname "Vance" --gender F --birth 1986
./banyan.py update-person private/tree.json maya-vance --nick "May" --note "Lives in Seattle"
./banyan.py add-spouse private/tree.json liam-patel maya-vance
./banyan.py add-child private/tree.json aria-patel --father liam-patel --mother maya-vance

# Validating & Rendering
./banyan.py validate private/tree.json
./banyan.py render private/tree.json -o private/index.html        # fetches sibling tree.json over HTTP(S)
# ./banyan.py render private/tree.json -o private/index.html --embed  # standalone file:// bundle
```
