---
name: banyan
description: Manage, query, edit, validate, and render JSON family trees and Gramps files (sqlite.db, .gramps) using the banyan CLI or direct JSON edits. Use this skill whenever the user asks to add, update, query, or visualize people and family relationships.
---

# `banyan` — Family Tree Management Skill

See [skills/banyan/SKILL.md](../../skills/banyan/SKILL.md) for the full schema and CLI reference.

Quick workflow when editing a family tree (`private/tree.json` or `examples/sample.json`):
1. Search existing people first: `./banyan.py search <tree.json> "<query>"` and `./banyan.py show <tree.json> <slug>`
2. Mutate via `./banyan.py add-person | update-person | add-spouse | add-child` or direct JSON edit
3. Validate (and render `<output.html>` initially, on viewer updates, or with `--embed`):
   ```bash
   ./banyan.py validate <tree.json>
   ./banyan.py render <tree.json> -o <output.html>
   ```
4. Never commit files from `private/` or any personal family data to git.
