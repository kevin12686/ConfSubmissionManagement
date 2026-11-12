# Documentation

New to CFM? Read the [repository overview](../README.md), then choose one path:

- **Installing or updating:** [Deployment](deployment.md).
- **Preparing proceedings:** [Operator Guide](operator_guide.md).
- **Changing code:** [Developer Guide](developer_guide.md).

## Guides

| Document | What belongs here |
| --- | --- |
| [Deployment](deployment.md) | Copy/edit/run instructions for CFM, shared CPU worker and remote GPU worker |
| [Operations](operations.md) | Storage ownership, backup, restore, cleanup and deployment recovery |
| [Operator Guide](operator_guide.md) | Editorial tasks, page map and expected workflow |
| [Troubleshooting](troubleshooting.md) | Symptoms, checks and safe next steps |
| [Publication Rules](publication_rules.md) | Authoritative scope, version, file, readiness and reset rules |
| [PDF Comparison](pdf_comparison.md) | OCR algorithm, tolerances, limits, cache and inspection behavior |
| [Architecture](architecture.md) | Components, state ownership and trust boundaries |
| [UI Conventions](ui_conventions.md) | Shared interaction and presentation contracts |
| [Developer Guide](developer_guide.md) | Setup, code map, regression commands and release requirements |
| [Acceptance Runbook](editorial_acceptance_runbook.md) | Isolated end-to-end release checks |
| [Validation Record](deployment_validation.md) | Measured test evidence and what it does not establish |
| [Changelog](../CHANGELOG.md) | Historical releases, not current setup instructions |
| [Worker Protocol](../comparison_slave/README.md) | Standalone compute API and retention contract |

## Maintenance Rules

Write for the task, not for the sequence in which features were added.

- Keep commands in Deployment and operational recovery in Operations.
- Keep business rules in Publication Rules; link instead of copying them.
- Keep OCR details in PDF Comparison, not in the README or every guide.
- Remove obsolete instructions. Use Git history and Changelog for old behavior.
- Keep test measurements separate from promises about speed or correctness.
- Update links and run the [documentation gate](developer_guide.md#regression-commands)
  when moving or removing a document.
