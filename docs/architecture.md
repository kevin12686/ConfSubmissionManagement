# Architecture Notes

## Boundaries

The Django app remains local and no-login, but the source code now has clearer internal boundaries:

- Controllers handle HTTP forms, redirects, messages, and template rendering.
- Selectors build read-only page data and keep query composition out of controllers.
- Commands wrap state-changing workflows and return result objects instead of Django messages.
- Services contain domain logic for imports, verification, processing, reports, integrations, and backup/restore.
- State models mirror `FinalSubmission` lifecycle domains and provide the migration path away from the large legacy record.

## FinalSubmission State Split

`FinalSubmission` still stores the existing fields for compatibility. New saves synchronize these one-to-one models:

- `FinalSubmissionIdentityState`
- `FinalSubmissionFileState`
- `FinalSubmissionReviewState`
- `FinalSubmissionPublicationState`
- `FinalSubmissionPlagiarismState`

The migration populates these records for existing submissions. Future refactors should move reads first, then writes, and only remove legacy columns after the acceptance tests cover the migrated behavior.

## Regression Gate

Run these after each restructuring step:

```bash
./.venv/bin/python manage.py check
./.venv/bin/python manage.py test
./.venv/bin/python -m compileall -q submissions conference_final_manager manage.py
```
