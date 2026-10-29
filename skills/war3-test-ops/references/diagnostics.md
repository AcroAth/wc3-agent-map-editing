# Diagnostics and native evidence

## Choose an observable outcome

Use existing diagnostics first. If the test cannot see the state needed to decide the result, add the smallest authorized probe to an isolated map, validate it, and transfer the new hash before continuing. Do not guess hidden state from an unrelated visible counter.

| Claim | Evidence that can establish it | Insufficient by itself |
| --- | --- | --- |
| Correct AI hero | Player slot, native owner, registered hero handle/type/name | A nearby hero or camera position |
| Gold grant | Target player's native balance immediately before and after | Human player's top-bar gold or an acknowledgement |
| Build switch | Stored override plus resolved effective route | Chat accepting the route string |
| Successful purchase | Transaction committed, actual charge, material/output IDs and resulting inventory | Purchase attempt or an item icon of unknown origin |
| Spell/displacement works | Full native order/cast/landing path with the relevant state | Calling the effect handler directly |
| AI leaves base/follows lane | Hero positions/actions with actual allied waves and lane records | Role flags or a simulated movement decision |
| XP multiplier/restoration | Controlled native XP gain or actual native factor and restoration | A message stating the configured difficulty |
| Salary timing | Native target balances across a scheduled tick with spending/other income accounted for | A noisy net balance change near a minute boundary |

Use names for readable UI and raw IDs/handles for disambiguation. Translate a UI player number to its zero-based player index and hero-array index explicitly; confirm the actual native owner. Do not assume every map uses the same array conventions.

## Design a narrow probe

- Observe the shared transaction/lifecycle boundary that decides the result. Log successful purchase commits after actual charging; failed or rolled-back purchases must not be reported as successes. Include consumed materials and all requested outputs, and compare with actual resulting inventory because native item pickup/recipe callbacks may transform outputs.
- Show the target, timestamp/sequence, relevant configuration, before/after values and inventory/state needed to interpret them. Bound history and subscriptions. Use on-demand snapshots or existing ticks when sufficient; avoid adding a polling timer for simple state inspection.
- Keep local camera/selection effects local. Avoid local-only writes to synchronized gameplay state. Preserve ongoing casts, displacement, ownership and existing AI decisions unless changing them is the explicit test subject.
- Restrict diagnostic mutations to the intended test context, such as true single-player and eligible allied targets. Count-of-human-players alone is not a reliable multiplayer gate. Test invalid targets and rejected commands as well as the successful path.
- Validate the modified source and freshly extracted archive against the target native set, archive/header preservation and applicable client limits. For legacy 1.29c, also enforce the 1,023-byte complete JASS string-literal limit; compiler success alone does not cover it.
- Keep fixture semantics aligned with native behavior: JASS integer division differs from Python `/`; destroying a hashtable is not equivalent to clearing a reusable dictionary. Simulations remain preflight evidence even after these corrections.

Do not ship a particular project's diagnostic commands as assumed universal game commands. Discover the candidate's actual interface and supply it in the trial brief. A probe's logging feature itself remains runtime-unverified until observed in the client.

## Compare build routes without confounding state

For an AI purchase-route test, start each route from a fresh match or another explicitly controlled starting state. Keep the relevant configuration comparable, record the hero identity/default attribute, and observe normal purchase execution after any authorized test grant. Existing items, consumed supplies, limited inventory, missing precursors and recipe callbacks can alter what the AI is able to buy.

Separate two questions:

1. With a fresh starting inventory, does the effective route produce its expected successful transactions and final equipment?
2. When switching an equipped hero, does the override change while existing equipment remains, followed by purchases allowed by the new route and current state?

An `auto` route may resolve to a hero's existing default route rather than define another item tree. Derive expectations from the map's source instead of requiring different outputs for every route name. Do not clear gear, force buys or inject income as an unreported substitute for the actual AI behavior.

## Preserve and report the result

Record exact commands, native values, named/raw outputs, final inventory and game time. Save supported screenshots/logs when available; chat history can retain messages that scroll away. If a command's submission is interrupted, mark it unknown and inspect current state before reissuing any non-idempotent action such as a gold grant.

Separate static checks, simulated behavior, native diagnostic observations and release/multiplayer testing in the report. A blocked scenario is inconclusive, with the exact failing stage and missing observation. Keep evidence files tied to the tested hash so a later build cannot inherit an earlier map's runtime pass.
