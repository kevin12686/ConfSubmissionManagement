# Documentation

This is the documentation home for Conference Final Manager.

Each guide has one primary responsibility. Start with the document for your
current task instead of reading the set in order.

## Choose A Guide

| Audience or task | Guide | Owns |
| --- | --- | --- |
| Conference editor | [Operator Guide](operator_guide.md) | Normal editorial workflow and expected results |
| Operator diagnosing a problem | [Troubleshooting](troubleshooting.md) | Symptom-based diagnosis and recovery |
| Docker operator | [Docker Guide](docker_guide.md) | Docker deployment, update, backup, migration, and rollback |
| Developer | [Developer Guide](developer_guide.md) | Local development, code ownership, tests, and releases |
| Maintainer reviewing design | [Architecture Notes](architecture.md) | System boundaries, data flow, concurrency, and safety rationale |
| Maintainer changing publication behavior | [Publication Rules](publication_rules.md) | Canonical publication scope, version, file, readiness, and export rules |
| Maintainer changing shared UI | [UI Conventions](ui_conventions.md) | Canonical UI, worklist, navigation, and accessibility contracts |
| Release reviewer | [Editorial Acceptance Runbook](editorial_acceptance_runbook.md) | Disposable end-to-end release validation |
| Anyone reviewing history | [Changelog](../CHANGELOG.md) | User-visible release history |

The repository [README](../README.md) is the product overview and quick-start
page. It intentionally does not repeat the complete operational or development
manuals.

## Recommended Reading Paths

### Running A Conference

1. [README quick start](../README.md#quick-start)
2. [Operator Guide](operator_guide.md)
3. [Troubleshooting](troubleshooting.md) when an observed result differs from
   the guide
4. [Publication Rules](publication_rules.md) only when the reason behind a
   blocker or selected file matters

### Developing A Change

1. [Developer Guide](developer_guide.md)
2. [Architecture Notes](architecture.md) for the affected boundary
3. The canonical owner for the changed behavior:
   [Publication Rules](publication_rules.md) or
   [UI Conventions](ui_conventions.md)
4. [Editorial Acceptance Runbook](editorial_acceptance_runbook.md) before a
   real handoff

### Operating Docker

1. [Docker Guide](docker_guide.md)
2. [Troubleshooting](troubleshooting.md) for a specific symptom
3. [Architecture Notes](architecture.md#docker-runtime-boundary) when the
   proxy, storage, or recovery design needs explanation

## Content Ownership

To keep the documentation from becoming a collection of repeated patches, each
kind of information has one canonical home:

| Information | Canonical home |
| --- | --- |
| Product purpose and first launch | Repository README |
| Editorial steps and expected outcomes | Operator Guide |
| Docker procedures and commands | Docker Guide |
| Publication scope, active versions, file priority, readiness, export | Publication Rules |
| Shared UI, worklists, navigation, accessibility | UI Conventions |
| Internal components and design rationale | Architecture Notes |
| Code placement, tests, and release mechanics | Developer Guide |
| Symptom-to-recovery instructions | Troubleshooting |
| Manual release proof | Editorial Acceptance Runbook |
| Historical changes | Changelog |

Other guides may give a short audience-specific summary, but they should link
to the canonical owner instead of reproducing its full rule or procedure.

## Documentation Change Checklist

When behavior changes:

1. Update the canonical owner first.
2. Update other guides only where that audience's task or expected result
   changed.
3. Add or update acceptance coverage for publication-facing behavior.
4. Run the documentation and regression gates in the
   [Developer Guide](developer_guide.md#regression-commands).
5. Evaluate `APP_VERSION`; evaluate `STATE_ARCHIVE_VERSION` only when System
   State compatibility changes.
