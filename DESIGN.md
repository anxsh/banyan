# DESIGN.md — `banyan` Architecture & Design Log

## Overview

`banyan` is a zero-dependency toolkit for managing family trees primarily through AI chat and rendering them as a single-page interactive HTML visualization. It replaces traditional desktop genealogy UIs (like Gramps) with:

1. **A human- and LLM-friendly JSON representation** (`tree.json`) that can be imported directly from Gramps XML (`.gramps`) or Gramps SQLite databases (`sqlite.db`) and exported back to Gramps XML.
2. **A single-file HTML viewer** (`index.html`) that loads the sibling `.json` file (`fetch` from the same folder by default, or embedded via `--embed`) and provides pan/zoom layout, search, lineage highlighting, relationship calculation, and an inline `{ } JSON` panel with no frontend build step or external runtime dependencies.
3. **A Python stdlib CLI (`banyan.py`) and Agent Skill (`skills/banyan/SKILL.md`)** for querying, mutating, validating, and re-rendering the tree via conversational prompts.

---

## Architecture & File Layout

```
banyan/
├── banyan.py                       # CLI: import/export Gramps, validate, query, mutate, render HTML
├── AGENTS.md / CLAUDE.md           # Repo-level AI agent instructions & privacy rules
├── skills/banyan/SKILL.md          # Portable agent skill definition
├── .claude/skills/banyan/SKILL.md  # Project-scoped skill for Claude Code / Antigravity
├── examples/
│   ├── sample.json                 # Generic 4-generation sample family tree (21 people, 9 families)
│   ├── sample.gramps               # Gramps XML 1.7.1 export of the sample tree
│   └── sample.html                 # Rendered single-page interactive HTML for the sample tree
└── private/                        # Gitignored directory for personal family trees (never committed)
```

---

## Key Design Decisions

### 1. JSON Schema: Inlined Person Attributes + Single-Direction Family Links

**Context:** Gramps XML 1.7.1 stores `<events>`, `<people>`, `<families>`, and `<notes>` in separate tables connected by 28-character hex handles (`_eebc4f5...`), and duplicates relationships on both `<person>` (`<childof>`, `<parentin>`) and `<family>` (`<father>`, `<mother>`, `<childref>`).

**Decision:**
- Inline `birth`, `death`, `nick`, and `note` directly onto each person object in `people` (omitting empty/unknown fields).
- Key `people` by readable kebab-case slugs (`first-surname`, disambiguated on collision by `-birth`, `-nick`, or `-2`) rather than opaque handles or `I0001` IDs.
- Store all parent/spouse/child relationships exclusively in `families` (`id`, `father`, `mother`, optional `married`, `children`).

**Why:**
- When editing via AI chat (or reviewing a `git diff`), `"father": "arthur-sterling"` is immediately self-explanatory without cross-referencing an ID table.
- Eliminating bidirectional back-pointers on `people` makes it impossible for a chat edit to leave `person.childof` and `family.children` out of sync.
- Keeping explicit `families` objects (rather than just `father`/`mother` pointers on each child) cleanly preserves three real-world cases found in Gramps exports:
  1. **Childless couples** (`father` + `mother`, `children: []`)
  2. **Single-parent families** (`mother` or `father` only + `children`)
  3. **Parentless sibling groups** (`children: [sibA, sibB]` with unknown parents, used when two in-laws married into a tree are known to be siblings)

### 2. Static HTML Viewer + Sibling `tree.json` Source of Truth

**Context:** Static SVG exports from Graphviz/Gramps become hard to read and navigate once a tree passes ~50–150 people across 5–6 generations. When hosting or committing edits, embedding JSON inside the HTML also duplicates data edits into HTML diffs and prevents serving raw `.json` at its own independent URL.

**Decision:**
- `banyan.py render` produces a static `.html` viewer that fetches its sibling `.json` file (`cache: "no-cache"`) from the same folder by default (`const JSON_URL = "<input>.json"`), making `tree.json` the single source of truth that can also be served directly at its own URL.
- Passing `--embed` to `banyan.py render` keeps the single-file inline JSON option (`EMBEDDED_TREE_DATA`) for offline `file://` opening where browsers restrict `fetch`.
- Locks in the **Classic Descendant Left-to-Right** layout: generational columns from left to right with subtle `Generation N` column headers, each parent top-aligned with their first child, spouses stacked directly below with a left indent, and orthogonal bus lines connecting couples to children.
- Cross-branch sibling groups (where both siblings are married into another branch of the tree) are rendered as dashed curved arcs on the left edge of the sibling cards.
- Locks in the **Rich Dossier** card architecture with adaptive 1–3 row heights (`38px` / `52px` / `56px` / `72px`) and uniform all-around `1px` borders (zero single-edge stripes):
  - **Row 1:** Full name (`+ ⚭` mark on indented spouse cards; generation pills removed from individual boxes because the top `Generation N` headers already convey generation).
  - **Row 2 *(omitted if empty)*:** Compact lifespan (`b. YYYY`, `d. YYYY`, or `YYYY–YYYY`), living vs. deceased age pill (`66 yrs` in soft botanical sage for living people within `0–115` years; `lived 87 yrs` in muted sepia-stone when deceased), and nickname. No `"Dates unknown"` placeholder when dates are absent.
  - **Row 3 *(omitted if empty)*:** Biographical note (`📝 ...`) or child count (`N child(ren)`), plus optional marriage year (`m. YYYY`) when recorded—omitting spouse names (`m. <spouse>`) as redundant with the tree layout, as well as `"Child of X & Y"` and `"Family member"` fallbacks.
- Locks in the **Warm Parchment** light palette (ivory `#fffdf8` / parchment `#f7f1e6` smooth dot-free canvas, espresso ink `#2c1e12`, burnt-sienna `#b45309` accents; no dark mode or multi-palette switcher).
- Includes a tabbed right-hand drawer combining the **Inspector** (relatives + BFS Relationship Finder) and an interactive syntax-highlighted **`{ } JSON` Side Panel** (`Full Tree` / `Selected` scope toggle, `Copy` button, clickable person slugs, and auto-scroll/highlight of the active person).
- Selection highlighting dims unrelated nodes (`opacity: 0.24`) and applies subtle full-card surface tints and matching `1px` `border-color` shifts (avoiding external `outline` frames that clashed with dashed spouse borders, while keeping descendant spouses highlighted alongside descendant bloodline nodes).

- **Why:**
  - `tree.json` remains the single source of truth: routine chat or CLI data edits only touch `tree.json`, keeping `git diff`s tiny and requiring zero HTML regeneration when served over HTTP(S).
  - Zero external JS/CSS dependencies or CDN links means the HTML + JSON pair works on any static host or local Python HTTP server without a build step, while `--embed` still supports opening standalone `.html` files directly over `file://`.

### 3. Direct Gramps SQLite (`sqlite.db`) & XML Import (Zero Dependencies)

**Context:** Gramps stores its live working database inside `~/.gramps/grampsdb/<hex_id>/sqlite.db`, while manual `.gramps` XML exports on disk can easily drift behind edits made inside Gramps. Requiring users to install the full `gramps` desktop/Python package just to read `sqlite.db` would violate `banyan`'s zero-dependency philosophy. At the same time, directory-walking, `--tree` disambiguation, legacy BSDDB probing, and speculative multi-schema branching add unnecessary complexity when the user can simply pass a `sqlite.db` file or `.gramps` file directly.

**Decision:**
- Provide two explicit file-only importers and matching CLI subcommands:
  - `import_gramps_db` (`./banyan.py import-gramps-db` / `import_gramps_db <sqlite.db>`): opens `sqlite.db` in read-only URI mode (`?mode=ro`) and deserializes `blob_data` tuples via a restricted `pickle.Unpickler` (`_GrampsSafeUnpickler`, allowing only `builtins.{set, frozenset}`), reading sibling `name.txt` if `--title` is omitted.
  - `import_gramps_xml` (`./banyan.py import-gramps-xml` / `import_gramps_xml <file.gramps>`): reads plain or gzip-compressed `.gramps` XML with namespace auto-detection.
- Extract full ISO date precision (`YYYY-MM-DD`, date ranges/modifiers when present), `birth_ref_index`/`death_ref_index` resolution, person notes, and family `Marriage` events (`families[].married`), deriving `meta.updated` from the database's latest object `change` timestamp (or `<header><created date>` for XML).
- Emit/parse `<eventref>` `Marriage` events in `export_gramps` / `import_gramps_xml` for round-trip XML compatibility.

**Why:**
- Explicit `import-gramps-db` and `import-gramps-xml` entry points keep `banyan.py` simple and predictable with zero folder discovery or `--tree` flag heuristics.
- Because Gramps serializes tuples/lists/strings/ints into `blob_data`, `_GrampsSafeUnpickler` reads `sqlite.db` safely using only Python's stdlib `sqlite3` and `pickle` without importing `gramps` or executing arbitrary pickle opcodes.
- Compact tree cards display 4-digit years (`shortDate`) to keep cards crisp while preserving exact dates (`YYYY-MM-DD` and `married`) in the JSON payload, `{ } JSON` panel, and Inspector pills.

---

## Changelog

### 2026-11-12 — Renamed Project to `banyan` & Published Open-Source Repo
- Renamed the CLI (`ftree.py` -> `banyan.py`, `prog="banyan"`), skill folders (`skills/ftree/` -> `skills/banyan/` and `.claude/skills/ftree/` -> `.claude/skills/banyan/`), and documentation/instructions (`README.md`, `DESIGN.md`, `AGENTS.md`, `CLAUDE.md`) to **`banyan`**, inspired by banyan trees branching into new roots across generations.
- Reset git history onto a single clean `main` commit audited for zero private family references before publishing publicly.

### 2026-11-12 — External Sibling JSON by Default (`fetch` from Same Folder)
- Switched `banyan.py render` so generated `.html` viewers fetch their sibling `.json` file from the same directory (`cache: "no-cache"`) by default, making `tree.json` the single source of truth and allowing HTML and JSON to be served at independent URLs without rebuilding HTML on data edits.
- Added `--json-url` (custom relative path/URL) and `--embed` (inline JSON bundle for `file://` viewing) flags to `banyan.py render`, plus a clickable raw-JSON link in the `{ } JSON` panel header.

### 2026-11-12 — Warm Parchment UI Refinements & Spotlight Highlighting
- Locked in the **Warm Parchment** palette (removing the top-bar 6-palette switcher) and removed the radial dot pattern on `#viewport` for a clean solid parchment canvas.
- Distinguished living vs. deceased age badges (`N yrs` in soft botanical sage vs. `lived N yrs` in muted sepia-stone) across cards and the Inspector without religious symbols.
- Removed redundant spouse names (`m. <spouse>`) from Rich Dossier card footers while preserving child counts and optional marriage years when present.
- Replaced external CSS `outline` selection frames with spotlight dimming + full-card surface-tint and `1px` `border-color` highlights (including spouses of highlighted descendants), eliminating double dashed/solid border artifacts on spouse cards.

### 2026-11-12 — Simplified Gramps Import to Explicit DB & XML File Importers
- Replaced the unified `import-gramps` directory/auto-detection command with two explicit file-only importers: `import-gramps-db` (`import_gramps_db`) for `sqlite.db` and `import-gramps-xml` (`import_gramps_xml`) for `.gramps` XML files (keeping snake_case aliases for CLI convenience).
- Removed directory-tree scanning, `--tree` disambiguation, BSDDB folder probing, and unused Gramps 5.2 `json_data` schema branching to keep `banyan.py` minimal and easy to maintain.

### 2026-11-12 — Refined Rich Dossier Cards, Living & Deceased Age, and 6 Light Color Palettes
- Locked in the **Rich Dossier** card architecture and removed redundant/noisy elements: dropped per-card generation pills (relying on top `Generation N` headers), dropped `"Dates unknown"`, and dropped `"Child of X & Y"` / `"Family member"` fallbacks.
- Extended age calculation (`getAgeInfo`) to show current age (`N yrs`) for living people with known birth years (`0–115` sanity guard) alongside age at death for deceased people, plus adaptive card heights (`38px`/`52px`/`56px`/`72px`) so sparse cards stay compact without blank space.
- Dropped dark mode and replaced the 6 card-style toggle with **6 Light Color Palettes** (`Slate & Indigo`, `Botanical Sage`, `Warm Parchment`, `Coastal Fjord`, `Rosewater & Plum`, `Sky & Rose`), keeping uniform all-around borders across every palette.

### 2026-11-12 — Direct Gramps SQLite DB (`grampsdb`) Import & Date Precision
- Extended `import-gramps` to read native Gramps SQLite databases (`sqlite.db`, tree directories, or `grampsdb` parent folders with optional `--tree`) in addition to plain/gzipped `.gramps` XML files, using Python stdlib only and a locked-down `_GrampsSafeUnpickler`.
- Added support for both Gramps 5.0/5.1 pickled tuple blobs (`blob_data`) and Gramps 5.2+ JSON columns (`json_data`), auto-reading `name.txt` and deriving `meta.updated` from max row change timestamps.
- Preserved full ISO dates (`YYYY-MM-DD`) and optional family `married` event dates across JSON, validation, XML round-trip, Inspector badges, and `{ } JSON` syntax highlighting while keeping tree card badges compact (`YYYY`).
- Added a validation warning for degenerate single-parent, childless families and a clean error message if pointed at legacy BSDDB databases.

### 2026-11-12 — Classic Descendant Layout, 6 Card Designs & JSON Side Panel
- Locked in **Classic Descendant (Left-to-Right)** layout with `Generation N` column headers and orthogonal bus connectors.
- Replaced the 6 layout modes with **6 Card Styles** (`Soft Botanical`, `Gen Spectrum`, `Editorial Band`, `Warm Archival`, `Nordic Capsule`, `Rich Dossier`) exploring distinct colors and information architectures with uniform all-around borders (no single-edge stripes).
- Added a syntax-highlighted **`{ } JSON` Side Panel** inside the right-hand drawer with `Full Tree` / `Selected` scope toggle, `Copy` button, clickable slug navigation, and auto-scroll sync.

### 2026-11-12 — Initial Design & Implementation
- Designed minimal `banyan` JSON format supporting readable slug IDs, inlined birth/death/nickname/note fields, and explicit `families` units covering couples, single parents, childless marriages, and parentless sibling groups.
- Built `banyan.py` (stdlib-only Python CLI) with `import-gramps`, `export-gramps`, `validate`, `render`, `stats`, `search`, `show`, `relate`, `add-person`, `update-person`, `add-spouse`, and `add-child`.
- Built single-page Modern Minimalist HTML viewer with pan/zoom canvas, Top-Down / Left-to-Right orientation toggle, branch collapse/expand, search autocomplete, lineage highlighting, and interactive relationship path finder.
- Added agent skill (`skills/banyan/SKILL.md` and `.claude/skills/banyan/SKILL.md`) and generic sample tree (`examples/sample.*`), keeping personal family data isolated in gitignored `private/`.
