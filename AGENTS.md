# Agent rules

- Verify with `make test`.
- Branches use the `cursor/` prefix.
- Sync with `main` by merge; no rebase, no force-push.
- Never push to `main`, never merge PRs, never tag or release.
- PR body has exactly three sections: Why / Verified (commands, results, and whether run in the agent VM or in GitHub CI) / Not verified.
- Before changing compatibility or version wording, check existing decisions in `docs/evidence/*/review-response.md` (lesson from M9).

| Rule | Enforced by |
|---|---|
| Verify with `make test` | CI |
| `cursor/` branch prefix | docs only |
| Sync by merge; no rebase; no force-push | docs only |
| No push to `main`; no merging PRs; no tag or release | docs only |
| PR body sections Why / Verified / Not verified | docs only |
| Check `docs/evidence/*/review-response.md` before compatibility or version wording | docs only |

CI (`.github/workflows/ci.yml` on `main`) runs `make test` on push and pull request to `main`. It does not check the other rows. Branch protection was not readable (HTTP 403), so a failing check is not claimed to block merge.
