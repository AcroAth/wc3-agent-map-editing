---
name: war3-map-ops
description: Inspect, localize, modify, rebuild, validate, or debug any Warcraft III .w3x or .w3m custom map within a project folder, including MPQ/JASS/object-data edits, external-header preservation, legacy-client checks, and optional host-bot metadata updates.
---

# War3 Map Ops

For any Warcraft III map project, work from that project's folder. Discover its maps, extracted sources, tools, scripts, backups, and configs before choosing a workflow; never assume a particular map name, source directory, Windows user, bot, or client version.

## Start With Discovery

1. List `.w3x` and `.w3m` files recursively, excluding obvious generated copies only when selecting a default (`*.rebuilt.*`, backups, and bot mirrors).
2. If exactly one playable candidate exists, use it. If several exist, identify the likely current map from filenames, timestamps, configs, and source references; ask only when the choice remains consequential and ambiguous.
3. Inspect the archive without changing it:
   - Detect `HM3W` at byte 0 and locate the MPQ header instead of assuming offset 512.
   - Inventory available archive tools and extracted payloads such as `war3map.j`, `war3map.wts`, `war3map.w3i`, object-data files, SLKs, and string TXT files.
   - Determine the target client/version from project evidence or the user request. Do not impose 1.29c restrictions on newer maps.
4. Treat maintained extracted payloads as source of truth when the project has them. Otherwise extract to a new project-local source directory before substantial edits, if the archive is readable.

## Requirements and related skills

Use the project's existing archive tools, build scripts and target-client compiler/native definitions. This skill supplies a workflow, not a bundled MPQ editor, compiler or game client. Discover compatible tools and report a missing prerequisite before attempting a rebuild; do not assume project-specific scripts exist. Static inspection and editing can run wherever those tools work. Native gameplay verification requires Windows, a compatible installed Warcraft III client and supported game-control tools.

For real-client verification, use [war3-test-ops](../war3-test-ops/SKILL.md). When installed alone, follow equivalent case design, artifact verification and native-evidence requirements; report runtime checks as unperformed if game access is unavailable.

## Git Version Control

- Use local Git history for recoverable map versions instead of accumulating `.bak`, `before-*`, or timestamped map copies. Inspect the repository root and working tree first; reuse an existing project repository, or initialize one in the map project folder if none exists.
- Before changing a map, record its exact current bytes in a baseline commit. Include the playable `.w3x`/`.w3m`, maintained build sources, and required build configuration. Scope staging to this task; preserve unrelated uncommitted work and do not change global Git settings.
- Prevent Git newline or encoding conversion for maps and byte-sensitive MPQ payloads with project-local `.gitattributes`. Verify that retrieving the committed map produces the same SHA256 as the baseline. Use existing Git LFS configuration when present; do not introduce LFS or a remote service without user authorization.
- Rebuild into a temporary candidate, compact it as required below, validate the compacted result, then replace the intended playable map. Commit the validated map, source changes, and validation evidence together; record the baseline and final commit IDs and map hashes.
- Make rebuild tools read historical base maps from an explicit Git revision. Export binary content through a byte-preserving API to a temporary file when the archive tool requires a path; do not depend on deleted backup filenames or shell text redirection.
- Remove only task-created duplicate backups and temporary candidates after verifying their needed versions are recoverable from Git. Keep user-owned maps and distinct release branches. Ignore generated candidates and caches rather than committing a pile of duplicate maps.
- For rollback, restore only the requested map and its corresponding maintained sources/configuration from the chosen revision, verify the map hash, and record the rollback in a new commit. Do not reset the entire repository or discard unrelated work.
- Local version control does not authorize pushing, publishing, or changing remotes. If Git cannot preserve a recoverable baseline, report that limitation before overwriting the only map; do not silently substitute permanent backup copies.

## Edit and Validate

- Make the smallest source-level edit and trace every relevant caller or raw object field. User-observed in-game behavior outranks tooltips or inferred defaults.
- Preserve raw IDs, Chinese hash keys/identifiers, color codes, import paths, and binary structure unless the requested change requires them.
- For display text, preserve the map's existing language and style; do not force a language globally.
- After JASS changes, run the compiler and native set appropriate to that map when available. Do not introduce unsupported natives or syntax for the target client.
- For 1.29c targets, reject any complete JASS string literal over 1,023 UTF-8 bytes, including multiline literals. `pjass` alone does not catch this load-crash condition.
- For object-data changes, inspect the raw MPQ/object fields and verify the edited value after rebuilding.

### Legacy Localization and CJK Text

- Translate only player-facing values. Preserve commands, raw IDs, paths, filenames, URLs, color/control codes, hash keys, `Preload(...)` payloads, and other strings consumed by game logic. Do not classify a field by its English-looking contents alone; trace how the map uses it.
- Treat each payload as a separate encoding surface. A working encoding for `war3map.wts`, `war3mapSkin.txt`, or JASS does not prove that the same bytes are safe in object data. Never apply CP950, UTF-8, or a BOM globally without a target-client runtime check.
- On legacy clients such as 1.29c, do not store translated CJK directly in `.w3a`, `.w3b`, `.w3h`, `.w3q`, `.w3t`, or `.w3u` string fields unless an in-game test has already proved that exact path safe. Prefer collision-free ASCII `TRIGSTR_<id>` references and store their Traditional Chinese values in UTF-8 `war3map.wts`:
  - Allocate IDs outside the existing WTS set, normally above its current maximum.
  - Deduplicate identical translated values when practical.
  - Verify every generated `TRIGSTR` resolves to exactly one WTS entry and that changed direct object strings remain ASCII.
  - Preserve formatting codes and literal commands inside the WTS value.
- Keep `war3map.w3i` in the rebuild even when most translated text lives elsewhere. Verify its map name, description, player/force names, and team names after extraction and again in map selection or the lobby; a structurally valid archive can still silently lose these strings.
- For `war3mapSkin.txt`, preserve the client-compatible BOM and newline form. If this was not established by the source map, test the smallest candidate first; do not infer it from WTS or object-file behavior. A proven 1.29c failure pattern is UTF-8 without a BOM crashing during load while CP950 loads with corrupted text; UTF-8 with a BOM and LF line endings fixed that map. Use this as a diagnostic case, not a universal encoding rule, and retest the exact target client.

## Rebuild Safely

- Prefer the project's existing verified rebuild path and compact base. Confirm that its archive tool supports this map's storage layout and preserves all live entries before rebuilding.
- Patch all maintained payloads in one archive session. Repeatedly patching the latest output can inflate MPQs and break hosting.
- Inspect protected-archive metadata such as `(attributes)` before patching. If changed-file CRC/hash metadata cannot be regenerated correctly, remove or invalidate that metadata using the archive format's supported mechanism; never leave stale integrity records. Round-trip extraction alone is insufficient evidence when the client rejects the map.
- Preserve each patched MPQ file's original block flags and storage semantics. When a file is compressed, encrypted, and split into sectors, rebuild its sector-offset table, compression markers, and filename-derived encryption keys consistently; never append plaintext or a single raw blob while retaining encrypted/compressed flags. Reject unfamiliar flag combinations instead of guessing.
- Write to a separate rebuilt file first. Never edit the only playable map in place.
- Preserve an external Warcraft header when present. Copy the full pre-MPQ prefix from the current map only after confirming the rebuilt archive's MPQ starts at the same offset; do not blindly copy 512 bytes.
- For renames, update every project-maintained title surface: archive metadata, script-set names, external header title, filename, and any explicitly in-scope host config. Verify the old name is absent from those surfaces.
- After all edits, complete Mandatory Compaction below. Extract the final compacted payloads and compare hashes with maintained sources, then validate the extracted JASS.
- Replace the playable map only after verification and after confirming its baseline is recoverable from Git. Follow the Git Version Control section for commits and cleanup; do not accumulate permanent backup copies.

### Mandatory Compaction

- Every map modification must finish with MPQ compaction before final validation, promotion, Git commit of the final map, and delivery. This includes script, object-data, metadata, and tooltip-only changes. An append-only patch is an intermediate candidate, not a completed deliverable.
- Compact the candidate into a temporary output to remove superseded payloads and unused archive space. A verified compact rebuild that packs only live blocks satisfies this requirement; a smaller file size alone does not prove compaction.
- Preserve all live archive entries, including unnamed or protected entries when the listfile is incomplete. Do not rebuild from only the known extracted filenames. Preserve the external Warcraft header and compatible MPQ layout, block flags, compression, sector tables, and integrity metadata. Relocated encrypted blocks whose keys depend on offsets must be re-encrypted correctly; never blindly move their stored bytes.
- Validate the compacted result, not just the pre-compaction candidate: compare its live-entry inventory with the edited candidate, verify changed payloads against maintained sources and untouched payloads against the baseline, and check unknown entries and encryption through the available archive metadata/tools. Re-extract and compile the final script as applicable. Run any runtime verification on this exact compacted artifact.
- Record the compaction method, pre/post-compaction byte sizes, and final SHA256. Promote and commit only the verified compacted artifact; verify the promoted file has that same hash. Clean up task-created intermediate candidates once Git recovery is confirmed.
- The purpose of compaction is to remove obsolete payloads and duplicate tables, preventing meaningless map-file growth. If standard compaction fails, use a documented compact-base or all-live-block rebuild supported by the project's archive tools that preserves this map's own contents. Do not import another map's data. If complete archive preservation cannot be verified, try an appropriate layout-preserving compact rebuild. If safe compaction remains unavailable, preserve the current playable map and report the task as incomplete with the specific blocker; do not silently skip compaction or claim completion.

## Runtime Verification

- Use an isolated test folder containing only the exact candidate when duplicate or similarly named maps exist. Before launch, record the candidate's absolute path and hash; after success, require the promoted final artifact to have the same hash. Do not launch from a broad folder where Warcraft can select the original map by mistake.
- Distinguish the failure stage: map-selection crash, lobby/metadata loss, loading-screen crash, gameplay crash, or rendered-text corruption. Each stage exercises a different payload path.
- For localization, static parsing is only a preflight. On the target Warcraft version, verify at minimum:
  1. The candidate appears in map selection with its intended map metadata.
  2. The lobby shows player/force/team names.
  3. Loading reaches gameplay without a crash.
  4. At least one translated object name and one real tooltip render as readable target-language text rather than mojibake, blank text, or a raw `TRIGSTR`.
  5. A quest/message surface renders when the change includes those surfaces.
- User screenshots and observed runtime behavior override a successful archive parser, checksum, compiler, or extraction test. Do not call a localization complete until the visible surface that previously failed passes.

Host-bot metadata updates and local or remote synchronization require an explicit request. Discover the project's bot tooling and validate checksums against the final compacted map before synchronizing.

## Completion Evidence

Report the checks that apply:

- Baseline and final Git commit IDs, map SHA256 values, and verified recovery from the baseline revision.
- Source and rebuilt-map JASS validation.
- Target-client compatibility checks, including maximum string-literal size for 1.29c.
- Extracted payload hashes match maintained sources.
- Patched MPQ block flags remain compatible with their rebuilt sector tables, compression, and encryption; all patched files round-trip through the same semantics the client will read.
- Compaction completed: method, pre/post-compaction sizes, final SHA256, and preservation checks for all live entries.
- Actual external-header and MPQ offsets remain valid.
- Renamed title surfaces contain the new name and not the old one.
- For localization, generated object `TRIGSTR` references all resolve, direct changed object strings use a client-safe representation, and `war3map.w3i` text remains present.
- Exact runtime-tested candidate path/hash and promoted final hash match; report which in-game text surfaces were visually checked.
- Host fields and local/remote hashes match, only if bot sync was requested.
- Any check that could not be run and why.
