---
name: war3-test-ops
description: Design reproducible Warcraft III custom-map test cases and run them in the actual game client on the selected Windows machine. Verify exact artifacts and report gameplay evidence using available Windows game-control tools. Use for native runtime verification; ordinary map editing and static-only audits belong to war3-map-ops.
---

# Warcraft III test operations

Test the requested behavior in the real client and preserve evidence of what actually happened. Compilation, simulations, file delivery, and command acknowledgements establish different facts; none alone proves gameplay correctness.

## Requirements and execution roles

Native tests require the selected Windows host or VM, a compatible installed Warcraft III client, the exact map candidate and supported game-control tools. The bundled [wc3-map-testing](../wc3-map-testing/SKILL.md) skill documents the WC3 Tester MCP tools. Read it before using those tools; another supported native-control integration may be used when this skill is installed alone. Missing tools or client access are blockers for native testing, not evidence of a map defect.

No specific model or multi-agent feature is required. One agent may design, execute and review a small test. When delegation is available and authorized, the coordinator prepares the cases and an execution agent controls the game on the selected machine. Honor explicit user or project requirements about model choice or delegation; if a required capability is unavailable, report that limitation.

## Keep testing proportionate

- Default to the minimum necessary tests: reproduce the reported issue, verify the change, and check directly affected regression paths. Keep case notes compact and stop when these have credible evidence.
- Useful reruns are allowed to reproduce a failure, confirm a fix, recover from a technical interruption, or verify a changed artifact. Do not repeat a successful check without a concrete reason.
- Do not expand routine acceptance into boundary or permutation matrices, such as 59/60/61-second cases or every combination of difficulty, route and faction. Add such cases only when explicitly requested or when a specific defect or concrete risk depends on them; choose the smallest useful set and briefly explain why.
- A broad historical plan or checklist is not a coverage quota. Select the cases relevant to the current request; record optional uncovered cases as limitations rather than automatically chasing them with more probes, parallel trials or long runs. Expand only for a concrete unresolved failure/risk or an explicit user request.

## Establish the trial

- Read the project's existing `MISTAKES.md` and applicable instructions. Preserve entries; record mistakes as discovered, their evidence, resolution and verification. If no mistakes occur, say so in the task entry.
- Identify the intended map, maintained sources, client executable/version, execution machine, mode, player slots and initial state. Recheck mutable paths and identities rather than reusing old VM addresses or process IDs. Resolve consequential artifact ambiguity before launch.
- The coordinating agent must prepare executable test cases before game control or any delegation, following Design executable cases below. A high-level checklist is not a finished test design. Keep the cases limited to the reported behavior and directly affected regression paths.
- Preserve existing sessions. Use a fresh match when scripts changed or prior inventory/state would confound a comparison. Restart only the identified task-owned test session, or one the user explicitly authorized replacing.
- When diagnostics or map changes are necessary and authorized, use the available `war3-map-ops` workflow for a recoverable Git baseline, narrow source changes, compilation, archive validation and compaction. Keep an isolated probe separate from the release. If that skill is unavailable, establish equivalent preservation and validation before changing the archive.

Before input, verify both the shell's execution machine and the actual computer-use surface. A remote-chat label does not establish Windows-local tools; a Linux fallback cannot control the selected Windows game client. Read the available game-control skill/API and obtain a fresh readable native-game window observation. Do not use an unrelated browser window to test native-game capture.

## Design executable cases

- **Coordinator responsibility:** Specify the test method and observation plan before executing or handing off the case. Do not leave the tester to discover the method through ad hoc play and occasional screenshots. Keep a simple case to a few concrete lines; do not turn this requirement into an exhaustive test matrix.
- **Required case content:** State the objective; exact fixture and how to establish and verify its initial state; ordered inputs and readiness checks; any timing-sensitive action sequence; the data/signals to collect and how/when to collect them; expected observations, pass/fail criteria, and the stopping condition. Distinguish an invalid setup or missing observation from a product failure.
- **Observation must resolve the claim:** Choose native events/logs, continuous capture or scheduled native snapshots as appropriate before execution. Match their precision to the relevant transition. Screenshots roughly ten or more seconds apart cannot establish a short cast, handoff, recovery-threshold crossing or complete transaction sequence. Screenshots may corroborate the result, but must not substitute for missing critical-event evidence. If the necessary observation is unavailable, improve the method before running the case rather than hoping a later image proves it.
- **Prearrange critical operations:** Define the supported API calls, waits, readiness conditions and stop-on-error points in advance. For a short timing window, execute the prepared sequence without inserting another model decision round-trip inside it. Do not queue blind retries or continue after an unknown/error state.
- **Execution follows the case:** When delegating, give the tester the prepared procedure and required evidence, not just the desired result. The execution agent may adapt coordinates to a fresh UI observation and perform bounded technical recovery, but must report an unready fixture or insufficient observability to the coordinator instead of independently redesigning the test or repeatedly improvising setup attempts. The coordinator reviews the collected evidence against the predefined criteria.

## Choose the execution path

- **Execution machine:** Use the Windows machine selected by the user, either the local host or a VM. Verify that the control tools target that machine. If it is inaccessible, report the blocker without silently switching machines. For VM testing, read [VM handoff and transfer](references/vm-handoff.md); use an existing VM chat only when messaging it is authorized.
- **Control agent:** The executing agent performs launches, attaches, screenshots, inputs and cleanup through supported tools on the selected machine. Before delegating, provide the prepared cases, exact artifact identity, machine, permitted actions and required evidence, and verify that the execution agent can access that machine. The coordinator reviews returned evidence. For a single-agent run, complete the same preparation and evidence review directly. Follow any explicit local delegation/model requirements.
- **Insufficient visibility:** read [Diagnostics and evidence](references/diagnostics.md) before adding instrumentation or claiming a result from a visible proxy.

Keep a compact project-local trial record: exact map path/size/SHA-256, client version, machine, configuration, commands/actions, native observations, outcome and limitations. Distinguish these milestones: prepared, receiver hash verified, loaded into gameplay, command submission confirmed, behavior observed. Record only reached milestones.

## Execute and recover

Perform the selected minimum necessary scenarios until their acceptance criteria are met or a specific blocker prevents progress. Stop after sufficient evidence; do not continue solely to fill an exhaustive checklist. A tester saying “started” is progress, not the result. Follow an asynchronous tester with compact status waits, carrying its host identity and cursor when supported; read detailed turns only to resolve missing evidence. Avoid repeating unchanged status to the user.

For technical UI failures, make one bounded recovery with the documented API: refresh the native window and screenshot state, use the actual returned identifiers, and reobserve. This recovery may include one supported alternative, such as a documented keyboard path based on the current image or direct client loading of the exact map through an authorized shell. Observe the resulting game before further input. Repeated identifier errors end the failing operation; they need not rule out that supported alternative when fresh observation works. Never invent screenshot IDs or replace supported UI tools with custom automation.

If recovery fails, record the exact operation/error and stop that dependent path. Do not repeatedly redispatch the same failing action. Distinguish an inaccessible desktop from an unknown screenshot ID, an obscured window, a map-load failure and a gameplay defect. Access Denied alone does not prove the desktop is locked. If the tool detects human input or explicitly requires stopping, stop interaction and await the user's readiness; do not evade the stop through another agent or input mechanism.

A later request to write documentation or a skill does not resume an interrupted game test. Work from saved evidence for that request and preserve unknown command submissions until the user resumes testing.

## Finish with evidence

Report passed, failed or inconclusive **per scenario**, supported by observations. A diagnostic probe proves the instrumented path only; claim the release was runtime-tested only when the tested artifact is the release, or clearly explain the instrumentation difference and limits. Single-player success does not establish multiplayer or long-match stability.

Close task-owned temporary shares/listeners and dispose of test artifacts only when their evidence is preserved. Stop only verified task-owned game processes when cleanup is appropriate; otherwise report the remaining session. For unfinished necessary trials, state the blocker and concrete next step without presenting partial work as complete. Optional uncovered cases are coverage limits, not automatic follow-up work.
