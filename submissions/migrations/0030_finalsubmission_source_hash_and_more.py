from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("submissions", "0029_finalsubmission_plagiarism_percent_exception_approved_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="finalsubmission",
            name="source_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="finalsubmissionfilestate",
            name="source_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
