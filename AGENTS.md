# AGENTS.md

This repository is a local Django + SQLite system for managing conference final
submission versions, editorial checks, and publication exports. Publication-facing
mistakes can be severe and effectively irreversible after IEEE release.

## Critical Rules

1. Publication output must use publication-facing helpers, not ad hoc file paths.
2. Paper Master List defines publication scope.
3. Editor Upload outranks Start2, but mixed undiscarded sources block final export.
4. Review/check state may only reset according to documented dependency rules.
5. State-changing workflows must be audited.
6. Code, tests, docs, and version metadata must be updated together when behavior changes.
7. Keep personal information out of docs, screenshots and synthetic fixtures.
   Use Kevin Hsu for public attribution and fictional names for examples.

## Required Workflow

- Read `README.md` and affected docs before editing.
- Inspect implementation before trusting docs.
- Update affected docs after behavior, UI, schema, export, or workflow changes.
- Run the required test gate from `docs/developer_guide.md` before handoff.
- Add acceptance coverage for publication-facing behavior changes.
- Evaluate `APP_VERSION` for user-visible changes and `STATE_ARCHIVE_VERSION` for System
  State archive compatibility changes.

## See Also

- `docs/README.md`
- `docs/deployment.md`
- `docs/operations.md`
- `docs/publication_rules.md`
- `docs/operator_guide.md`
- `docs/developer_guide.md`
- `docs/architecture.md`
- `docs/ui_conventions.md`
- `docs/troubleshooting.md`
- `docs/editorial_acceptance_runbook.md`
- `CHANGELOG.md`
