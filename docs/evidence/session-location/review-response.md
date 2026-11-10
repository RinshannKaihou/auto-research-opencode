# PR #40 review response

Reviewed head: `0e4f78d`. The original head passed GitHub CI on Python 3.11 and
3.12 ([run](https://github.com/RinshannKaihou/auto-research-opencode/actions/runs/42792297315)).
This follow-up addresses the findings rather than treating those green checks as
proof of the missing failure paths. No production deployment or research control
operation was performed.

## Dispositions

| Finding | Decision and implementation | Verification |
|---|---|---|
| [Blocking: subdirectory takeover](https://github.com/RinshannKaihou/auto-research-opencode/pull/40#pullrequestreview-5841238447) | Fixed. `takeover` requires canonical session directory = registered root before calling the takeover operation or changing membership. A rejected candidate remains an ordinary session and the old main remains owner. Canonical root aliases still work. Existing subdirectory registrations require the documented operator-coordinated move back; this change does not silently migrate them. | Regression rejects a subdirectory, checks unchanged main/membership, then verifies the same candidate can take over at the root. |
| [Tools cleared after fallible persistence](https://github.com/RinshannKaihou/auto-research-opencode/pull/40#discussion_r4524423188) | Fixed. Clear tools and replace system instructions before awaiting `blockTools`. Persistence errors still propagate to the host rather than pretending the pause was committed. The request stays tool-free even if persistence fails. | Inject a failure in the workflow fault write; context rejects, tools are empty, the diagnostic remains, and no successful durable pause is claimed. |
| [Foreign membership lookup on every hook](https://github.com/RinshannKaihou/auto-research-opencode/pull/40#discussion_r4524423198) | Fixed without treating a registered research session as a nonmember. Foreign diagnostic records now carry their own one-second cache timestamp and registry/WAL stamp, under the existing 2048-entry bound. They still grant no local role or controller authority. | Two confirms use one lookup; expiry forces another; detaching changes the registry and immediately clears the diagnostic. |
| [Stale continuation pause snapshot](https://github.com/RinshannKaihou/auto-research-opencode/pull/40#discussion_r4524423203) | Risk accepted and fixed, with a different queue implementation. `continueSession` already runs inside `serial(root)` through terminal processing or resume. Nesting another serial decision would wait on itself. Both context and continuation paths instead share `pauseForEntry`, which rereads state inside the existing decision and preserves any existing pause or non-running state. | Deterministically write a budget pause during failed submission preflight; terminal processing completes without deadlock, budget is preserved, no fault notification or extra turn is sent. |
| [Probe restricted to 2.2.6](https://github.com/RinshannKaihou/auto-research-opencode/pull/40#discussion_r4524423208) | Fixed. Accept 2.2.5 and 2.2.6 and add `--opencode` for a separate executable. Read existing M9/context-cache review decisions first; no general compatibility/version policy or SDK pins changed. | Four native modes pass on each version. The 2.2.5 platform package was extracted into private temporary storage; the global 2.2.6 executable and shared backend were not replaced. |
| Helper unit tests and host lookup failures | Added focused canonical-path/role/error/name tests, plus full-hook/submission tests for unavailable tool catalogs and failed session-location queries. | `plugin/test/session-location.test.ts` and the location-boundary tests in `plugin/test/plugin.test.ts`. |

## Validation

- The four new defect regressions failed on the reviewed implementation before the
  fixes: takeover, fault-write failure, concurrent budget pause, and foreign-cache
  lookup count. They pass after the changes.
- `make test` on the local macOS agent host: **886 Python tests, 372 plugin tests,
  TypeScript passed**. Full-hook/failure-path/foreign-cache/helper subset:
  **20 passed**. These are local checks, not a claim that the new head's GitHub CI
  has completed.
- OpenCode **2.2.5** and **2.2.6**, each with isolated XDG state and a local scripted
  provider: `direct`, `nested`, `drift`, `catalog`. Every successful repaired mode
  publishes once, checkpoints once and finishes the same single task. Sanitized
  results are in `native-results.json`.
- Helper model/formal specifications were not changed. The review's reported
  formal verification of the old head is not claimed as a new formal run.

## Remaining limits

There is no support for running registered research sessions from workspace
plugin instances, no automatic move/takeover, and no new finish API. Failure to
persist a fault is surfaced rather than swallowed. Context visibility checks
still cannot observe a later plugin rewriting the tool map after this hook.
Native tests use scripted local providers. Production deployment and actual
research data remain outside this review response.
