# VM handoff and transfer

## Find the right tester

Identify the intended VM and its current host identity through the available remote-control or session tools. If authorized chat/thread coordination is available, inspect the existing tester session to avoid duplicate tests or conflicting interaction. Do not create a new user chat unless requested, and do not automate Codex/ChatGPT UI when a messaging tool is available.

Send instructions only within the user's authorization to communicate with that tester. Ordinary status inspection does not authorize new messages to other people or unrelated chats. Verify that the executing agent's supported tools target the selected VM; a host-side agent without VM access is not a substitute for VM-local execution. Delegate only when available and authorized, and honor explicit local model or delegation requirements.

## Deliver the exact artifact

1. Compute the source's absolute path, byte count and SHA-256. Use a distinct filename for a probe so it cannot be mistaken for a prior release. Create a dedicated task-owned test-selection folder containing the intended candidate; preserve unrelated maps and user files.
2. Prefer an existing working transfer route. On a configured Tailscale pair, discover current peers and eligible file targets, then use Taildrop to the verified VM. Confirm receiver retrieval and hash independently: a sender exit code establishes neither retrieval nor gameplay readiness.
3. If Taildrop is unavailable, use an already authorized transfer method such as LocalSend. An installed or discoverable sender is not proof that the receiver is reachable. Do not assume a listener proves a VM can connect.
4. If temporary sharing is needed, expose only the intended artifact, disclose plain HTTP when applicable, and close the share after confirmed receipt or a failed attempt. Do not change firewall, VPN or managed policy simply to make a transfer work.

Do not declare a utility absent because the current session's TEMP or PATH does not contain it. Check project-documented locations and ordinary user temporary/install locations without assuming a particular username or historical path. Session-specific temporary roots can differ. Use an existing executable/runtime rather than downloading a replacement without evidence it is missing. Apply normal execution permissions; do not modify protected helper folders, ACLs or security settings to repair tool routing.

On Windows, current CLI capabilities can be checked with `tailscale status`, `tailscale file cp --targets` and the installed client's help. A typical send is `tailscale file cp 'ABSOLUTE_MAP_PATH' VERIFIED_PEER:`. Have the receiver inspect its configured inbox or supported receive command and verify the final file. Never assume a historical peer name, IP, inbox path or CLI version is still correct.

## Send a concrete trial brief

Supply the tester with these facts in a cohesive message:

```text
Task and authorized execution machine:
Exact artifact name, byte count, SHA-256, and how it was sent:
Required client/mode/slot configuration and fresh-match conditions:
Scenarios: starting state -> actions/commands -> observable expected result:
Diagnostics available and how to read them:
Evidence required for each scenario:
Existing session ownership and permitted cleanup:
Report observations and blockers in this chat; the coordinator will poll.
```

Do not pass host-only source paths as if they were VM-local files. Require the tester to confirm its actual received path and client version. Include whether a prior match can be replaced and which process/session belongs to the test.

Keep the next step concrete: download, verify, load, execute, or collect a named missing observation. Once the map is received and hash-verified, do not repeat transfer debugging for a later screenshot or input failure. After an unlock, first recheck desktop access; do not continue treating a stale lock diagnosis as the current blocker.

When coordinating asynchronously, use the available session tool's compact wait/snapshot facility for progress. Keep the returned cursor to suppress stale final text, and compare the latest turn with the dispatched trial. Completion means completed scenarios or an evidenced blocker, not merely delivery or an agent turn ending. If a fix enables a materially different supported path, issue a focused continuation; if a tool requires user readiness, wait for that readiness rather than sending repeated recovery turns.
