# scripts/ — upstream attribution

The Python sidecar packaging pipeline under `scripts/cpython/`,
`scripts/prepare-resources*`, and `scripts/ci/` was originally lifted
verbatim from [AstrBotDevs/AstrBot-desktop](https://github.com/AstrBotDevs/AstrBot-desktop)
(AGPL-3.0, the same license Marginalia uses).

Source revision:
- repo: https://github.com/AstrBotDevs/AstrBot-desktop
- commit: `12e83bdc40cc14fddde8f3cc99b3b2a1506a1251`
- date: 2026-05-27

## Files copied verbatim, then adapted

| Marginalia path                                          | AstrBot upstream path                                |
| -------------------------------------------------------- | ---------------------------------------------------- |
| scripts/cpython/resolve_packaged_cpython_runtime.py      | scripts/cpython/resolve_packaged_cpython_runtime.py  |
| scripts/prepare-resources.mjs                            | scripts/prepare-resources.mjs                        |
| scripts/prepare-resources/context.mjs                    | scripts/prepare-resources/context.mjs                |
| scripts/prepare-resources/backend-runtime.mjs            | scripts/prepare-resources/backend-runtime.mjs        |
| scripts/prepare-resources/mode-dispatch.mjs              | scripts/prepare-resources/mode-dispatch.mjs          |
| scripts/prepare-resources/mode-tasks.mjs                 | scripts/prepare-resources/mode-tasks.mjs             |
| scripts/prepare-resources/version-sync.mjs               | scripts/prepare-resources/version-sync.mjs           |
| scripts/ci/codesign-macos-nested.sh                      | scripts/ci/codesign-macos-nested.sh                  |
| scripts/ci/backend-smoke-test.mjs                        | scripts/ci/backend-smoke-test.mjs                    |

The first commit on `feat/python-sidecar` is the verbatim copy. Adaptations
to Marginalia's pyproject layout, env-var names (`ASTRBOT_*` →
`MARGINALIA_*`), and single-repo source layout follow in subsequent
commits, so the diff is the change set.

Files intentionally NOT copied:
- `scripts/prepare-resources/source-repo.mjs` — AstrBot uses a two-repo
  layout (desktop shell + backend in separate repos). Marginalia is
  single-repo, so the source-fetch step is unnecessary.
- `*.test.mjs` — kept out for the initial spike. Worth porting once the
  pipeline stabilises.
- `desktop-bridge-*.mjs` and `bridge-bootstrap-updater-contract.*` —
  AstrBot-specific IPC bridge and Tauri updater contracts that don't
  apply here.
