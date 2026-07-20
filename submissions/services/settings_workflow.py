from django.db import transaction

from submissions.models import AppSetting
from submissions.services.workflow_evidence import (
    app_setting_evidence,
    require_evidence_token,
)


def _locked_settings():
    current = AppSetting.objects.select_for_update().filter(pk=1).first()
    if current is None:
        current = AppSetting.objects.create(pk=1)
    return current


@transaction.atomic
def validate_settings_evidence(expected_evidence_token):
    current = _locked_settings()
    require_evidence_token(
        expected_evidence_token,
        "app-settings-edit",
        app_setting_evidence(current),
    )
    return current


@transaction.atomic
def apply_app_settings_form(form, *, expected_evidence_token):
    current = _locked_settings()
    require_evidence_token(
        expected_evidence_token,
        "app-settings-edit",
        app_setting_evidence(current),
    )
    before = app_setting_evidence(current)
    for field_name in form.fields:
        if field_name in form.cleaned_data:
            setattr(current, field_name, form.cleaned_data[field_name])
    current.full_clean()
    current.save()
    return current, before


@transaction.atomic
def reset_app_setting_folders(
    default_folder_values,
    *,
    expected_evidence_token,
):
    current = _locked_settings()
    require_evidence_token(
        expected_evidence_token,
        "app-settings-edit",
        app_setting_evidence(current),
    )
    before = app_setting_evidence(current)
    for field_name, default_value in default_folder_values.items():
        setattr(current, field_name, default_value)
    current.full_clean()
    current.save(update_fields=list(default_folder_values))
    return current, before
