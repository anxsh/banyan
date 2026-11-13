# Agent Instructions (`banyan`)

This repository contains tools for managing family trees in a clean JSON format, converting from Gramps XML (`.gramps`) or Gramps SQLite databases (`sqlite.db`) and exporting to `.gramps`, and rendering a self-contained single-page interactive HTML family tree.

## Privacy Rule (Critical)

- This is a generic, open-source repository. **Never** commit personal or family-specific data (names, dates, `.gramps` files, or rendered personal HTML) into tracked files.
- Personal family trees live in `private/` (e.g. `private/tree.json`, `private/index.html`), which is gitignored.
- Public examples and tests must use `examples/sample.json` (`examples/sample.gramps`, `examples/sample.html`).

## Editing Family Trees via Chat

See [`skills/banyan/SKILL.md`](skills/banyan/SKILL.md) for the full JSON schema and CLI reference.

When asked to modify the user's family tree (by default `private/tree.json` if present):
1. Search for referenced people first so you use exact slug IDs:
   ```bash
   ./banyan.py search private/tree.json "<name>"
   ./banyan.py show private/tree.json <person-slug>
   ```
2. Edit `private/tree.json` (directly or via `./banyan.py add-person`, `update-person`, `add-spouse`, `add-child`).
3. Always validate after editing; render `<index.html>` when initially setting up the folder or updating the viewer template (rendered HTML fetches sibling `tree.json` by default; pass `--embed` for standalone `file://` files):
   ```bash
   ./banyan.py validate private/tree.json
   # If index.html isn't generated yet or you want a standalone --embed bundle:
   ./banyan.py render private/tree.json -o private/index.html
   ```
