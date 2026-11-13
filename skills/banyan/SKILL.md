---
name: banyan
description: Manage, query, edit, validate, and render JSON family trees and Gramps files (sqlite.db, .gramps) using the banyan CLI or direct JSON edits. Use this skill whenever the user asks to add, update, query, or visualize people and family relationships.
---

# `banyan` — Family Tree Management Skill

Use this skill to inspect, modify, validate, and render family trees stored in `banyan` JSON format.

## 1. JSON Schema Overview

A family tree JSON file has three top-level keys: `meta`, `people`, and `families`.

```json
{
  "meta": {
    "title": "Family Tree Title",
    "updated": "YYYY-MM-DD"
  },
  "people": {
    "clara-sterling": {
      "first": "Clara",
      "surname": "Sterling",
      "nick": "Nana",
      "gender": "F",
      "birth": "1904",
      "death": "1991",
      "note": "Optional biographical note"
    }
  },
  "families": [
    {
      "id": "arthur-sterling+clara-sterling",
      "father": "arthur-sterling",
      "mother": "clara-sterling",
      "married": "1927",
      "children": ["eleanor-sterling", "walter-sterling"]
    }
  ]
}
```

### Schema Rules
1. **Readable Slug IDs**: Keys in `people` are lowercase kebab-case slugs (`first-surname`, disambiguated on collision by `-birth`, `-nick`, or `-2`).
2. **Omit Empty Fields**: On a person object, only `first` is required. Omit `surname`, `nick`, `gender` (`"M"`, `"F"`, `"U"`), `birth`, `death`, and `note` when unknown or empty.
3. **Single Source of Truth for Relationships**: Never add parent/spouse/child pointers onto person objects. All relationships live in `families` (which may also carry an optional `"married"` date/year string):
   - Married/partnered couple with children: `father`, `mother`, `children: [...]`
   - Childless couple: `father`, `mother`, `children: []`
   - Single-parent family: `mother` (or `father`) only + `children: [...]`
   - Sibling group with unknown parents: omit both `father` and `mother`, list siblings in `children: [...]` (`id: "siblings-<a>+<b>"`).
4. **No Duplicate Birth Families**: A person may appear in `children` of at most one family.

---

## 2. Standard Workflow for AI Chat Edits

Whenever the user asks to query or modify the family tree:

1. **Locate the target person(s)** first using `./banyan.py search` or `./banyan.py show` so you use exact slug IDs:
   ```bash
   ./banyan.py search <tree.json> "<name or nickname>"
   ./banyan.py show <tree.json> <person-slug>
   ```
2. **Apply the change** either by editing `<tree.json>` directly or using the atomic CLI subcommands:
   ```bash
   # Add a new person
   ./banyan.py add-person <tree.json> --first "Maya" --surname "Vance" --gender F --birth 1986 --nick "May"

   # Update an existing person's fields (pass "" to clear a field)
   ./banyan.py update-person <tree.json> maya-vance --birth 1987 --note "Moved to Seattle"

   # Link two people as spouses (optionally with --married)
   ./banyan.py add-spouse <tree.json> liam-patel maya-vance --married 2010

   # Add a child to parents (creates or updates their family unit)
   ./banyan.py add-child <tree.json> aria-patel --father liam-patel --mother maya-vance
   ```
3. **Always validate after any modification** (since rendered HTML fetches its sibling `.json` file by default, you only need to re-run `render` when initially setting up `<index.html>`, changing the viewer, or producing an `--embed` file):
   ```bash
   ./banyan.py validate <tree.json>
   # Initial setup or when regenerating the HTML viewer:
   ./banyan.py render <tree.json> -o <index.html>
   ```

---

## 3. Useful CLI Reference

| Task | Command |
|------|---------|
| Import Gramps SQLite DB (`sqlite.db`) | `./banyan.py import-gramps-db <sqlite.db> -o tree.json --title "My Tree"` |
| Import Gramps XML (`.gramps`) | `./banyan.py import-gramps-xml <file.gramps> -o tree.json --title "My Tree"` |
| Export JSON back to `.gramps` XML | `./banyan.py export-gramps tree.json -o output.gramps` |
| Validate integrity & check for cycles | `./banyan.py validate tree.json` |
| Render HTML viewer (`fetch` sibling JSON) | `./banyan.py render tree.json -o index.html` |
| Tree summary statistics | `./banyan.py stats tree.json` |
| Search by name, nick, year, or note | `./banyan.py search tree.json "library"` |
| Inspect a person's immediate relatives | `./banyan.py show tree.json <person-id>` |
| Find relationship path between two people | `./banyan.py relate tree.json <person-a> <person-b>` |

## 4. Privacy Policy

- Keep personal/real family trees inside `private/` (which is gitignored) or `*.local.json` / `*.local.html`.
- Never commit real family names, dates, `.gramps` exports, or DB dumps into tracked repository files. Use `examples/sample.json` for public tests and examples.
