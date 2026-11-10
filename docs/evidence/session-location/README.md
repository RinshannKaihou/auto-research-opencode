# Research session location guard

A node can call `execute -> tools.opencode.session_move` and change its host
location to its file workspace. The workspace plugin instance previously discarded
its research membership, removed every research tool, and left ordinary tools
available. The root controller still scheduled ledger-owned work until
`no_progress` paused it.

This change keeps research sessions at their registered root (strategy A). The
node's file workspace remains the task's explicit `cwd`. A foreign registration
is diagnostic data, not authority to attach a second controller or run a node.

## Boundaries

- OpenCode 2.2.5 and 2.2.6 invoke `execute.before` with `opencode_session_move` for both
  direct calls and dynamically constructed nested calls. That inner hook rejects
  moves from registered research sessions. Ordinary sessions retain native tools.
- Context and tool execution check the actual host location. Autonomous submission
  checks location and registered research tools before reserving a turn. A normal
  continuation performs that submission check once.
- An authorized working context also checks its current `request.tools`. A known
  catalog gap pauses through the existing root controller and ledger, with a
  diagnostic and no tools. Valid pause/drain, wait/finish, read-only, execution
  token and controller handoff behavior remain intact.
- Budget queries/updates and other root management operations do not depend on an
  unrelated model tool being registered. Resume validates eligible targets before
  changing control or reserving budget; it does not move or restart unverified work.

No schema, Python backend, checkpoint, evidence delivery or finish API changes.

## Reproduction and verification

`verification.json` records the clean PR checkout's validation, separately from
prior validation of the installed-version hotfix. `native-results.json` contains
sanitized local scripted-provider results; neither file contains real research
outputs or the affected project's identifiers.

Run from a clean repository checkout with plugin dependencies available:

```bash
make test
python3 scripts/probe_session_location.py --plugin ./plugin --mode direct --output /tmp/aro-location-direct
python3 scripts/probe_session_location.py --plugin ./plugin --mode nested --output /tmp/aro-location-nested
python3 scripts/probe_session_location.py --plugin ./plugin --mode drift --output /tmp/aro-location-drift
python3 scripts/probe_session_location.py --plugin ./plugin --mode catalog --output /tmp/aro-location-catalog
```

Each output directory must be new. Probes accept the verified OpenCode 2.2.5/2.2.6 binaries and local loopback
listeners, use private XDG configuration/storage, and call only a local scripted
model. They persist raw model requests, hook traces and `result.json` in the output
directory. Use `--opencode /path/to/opencode` to test a separate binary without replacing the global host. Every successful repaired case publishes once, checkpoints once and
finishes the same single task. The `baseline` mode can reproduce tool loss and
`no_progress` against an unmodified plugin in an independent checkout.

The location-boundary regressions cover nested/direct hook checks, foreign
and unrelated instances, ordinary sessions, first-turn drift pause and recovery,
old tool handles, duplicate terminals/notifications, missing registered tools,
management operations, actual context tool loss, one submission check, and
unverified-node recovery. Existing tests cover controller replacement, pause,
stop, execution authorization, pending finish and tail context races.

## Recovery, rollout and limits

This PR has not been deployed. Coordinate a reload window with the research
operator; preserve the previous installation and switch only the plugin reference.
Do not package a dirty checkout or automatically restart a shared server. Revert
the plugin reference and reload in a coordinated window to roll back; there is no
database migration to undo.

Takeover now rejects a session outside the registered root before changing membership; rejected sessions keep their ordinary-session status. Existing registrations in subdirectories still require the explicit recovery below.

For an active, recoverable session already in a workspace, the operator must move
that **same session** back to its registered root, wait for the host's actual
location change, reconcile its execution state, then explicitly resume. A move
HTTP 204 may only acknowledge queuing. Do not replace the node, edit the ledger,
or treat a local JSON draft as a published result or accepted finish. Unverified
executions retain the existing manual reconciliation path.

Host-level moves made outside the tool hook remain possible and are detected at
subsequent entry checks; these checks are not a directory lock. Context validation
covers the map visible to this hook; a different plugin mutating the map afterward
is not covered by a final host catalog callback. Native validation uses a scripted
provider, not a real research model. Host versions other than the tested 2.2.5/2.2.6 and
production deployment are not verified here; existing compatibility policy is
unchanged.

Review dispositions and failure-path regressions are in [review-response.md](review-response.md).
