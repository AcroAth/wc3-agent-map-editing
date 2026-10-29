# Implementation mistake log

## 2026-10-29 -- Publish Agent Map Editing for Warcraft III 0.2.0

- Added portable map editing and native testing skills, related references and UI metadata; aligned the existing MCP tool guide with prepared cases, evidence review and task-owned session cleanup. Public workflows support available control agents while honoring explicit local requirements.
- Excluded a pre-existing local rebuild helper that could promote uncompacted maps and create timestamped backups, contrary to the editing workflow. The pack documents project-provided archive/build tools instead.
- Validation setup initially lacked PyYAML. Installed PyYAML 6.0.3 in an ignored local validation environment after authorization; all three skills then passed the skill validator.
- Release preparation found unrelated server-fix commits in the development checkout. Built this release from the verified public main baseline, applying only this skill-pack/branding change and version metadata. Existing public server behavior is preserved.
- One GitHub metadata lookup initially used incorrect argument names and failed schema validation without mutation; corrected the documented parameters and verified the public baseline.
- Prepublication checks cover skill metadata, reference links, manifest wiring, version consistency and unchanged server/build dependencies. Native gameplay was not exercised by this documentation release.

- Release export mistake: git archive applied the repository CRLF export rule to run_server.cmd, initially making an untouched launcher appear changed. The explicit allowed-file check caught it before publication. Restored the launcher directly from its baseline Git blob; byte comparisons verify no launcher change.
