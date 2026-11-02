# Agent rules

- Verify with `make test`.
- Branches use the `cursor/` prefix.
- Sync with `main` by merge; no rebase, no force-push.
- Never push to `main`, never merge PRs, never tag or release.
- PR body has exactly three sections: Why / Verified (commands, results, and whether run in the agent VM or in GitHub CI) / Not verified.
- Before changing compatibility or version wording, check existing decisions in `docs/evidence/*/review-response.md` (lesson from M9).

| Rule | Enforced by |
|---|---|
| Verify with `make test` | docs only |
| `cursor/` branch prefix | docs only |
| Sync by merge; no rebase; no force-push | docs only |
| No push to `main`; no merging PRs; no tag or release | docs only |
| PR body sections Why / Verified / Not verified | docs only |
| Check `docs/evidence/*/review-response.md` before compatibility or version wording | docs only |

These rows are docs only. The `make test` workflow is on open PR #16 and is not on `main`. Branch protection could not be verified (HTTP 403).
