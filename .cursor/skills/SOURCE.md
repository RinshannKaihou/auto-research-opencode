# SOURCE

Vendored copy of the pstack plugin skills for Cursor Cloud Agents.

- Upstream: https://github.com/cursor/plugins
- Upstream path: `pstack/skills/`
- Pinned commit: `de8ee15dd1194627d21eb5fcd41cb1873208dabb`
- Plugin version at that commit (`pstack/plugin.json`): `0.15.36`
- License: MIT, Copyright (c) 2026 Lauren Tan. The `LICENSE` file in this directory is `pstack/LICENSE` from the pinned commit.

Each skill is one directory here, with its `SKILL.md` and every helper file copied unchanged from upstream. The repository-root `skills/` directory is a separate, project-local skill and is not part of this vendor copy.

`poteto-help` links to `../../README.md` and `../../docs/...` in the upstream plugin. Those files are not required for the skills to run, so they are not included. The skill text is unchanged.

## Refresh

1. Pick a commit on https://github.com/cursor/plugins and read the `version` field in `pstack/plugin.json` at that commit.
2. Replace the skill directories in this folder with that commit's `pstack/skills/` tree. Copy every file as-is, including references, playbooks, and scripts. Do not edit the skill files.
3. Replace `LICENSE` here with `pstack/LICENSE` from the same commit.
4. Update the pinned commit and version in this file.
5. Do not copy `pstack/agents`, `pstack/automations`, `pstack/docs`, or other plugin files unless a skill's own files need them to run. If one does, copy that dependency or make the smallest path fix, and prefer leaving the skill file intact.
6. Leave the repository-root `skills/` directory unchanged.
