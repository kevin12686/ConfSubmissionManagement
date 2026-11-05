from django.db import migrations

from submissions.legacy_crosscheck_history import capture_v7_crosscheck_history


def capture_existing(apps, schema_editor):
    capture_v7_crosscheck_history(apps)


class Migration(migrations.Migration):
    dependencies = [("submissions", "0037_crosscheckserviceattempt_crosscheckserviceresult_and_more")]
    operations = [migrations.RunPython(capture_existing, migrations.RunPython.noop)]
