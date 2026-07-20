from django.db import migrations, models
from django.db.models.functions import Lower, Trim


class Migration(migrations.Migration):
    dependencies = [
        ("submissions", "0030_finalsubmission_source_hash_and_more"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="initialpaper",
            constraint=models.UniqueConstraint(
                Lower(Trim("paper_id")),
                name="initialpaper_paper_id_normalized_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="initialpaper",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(paper_id=Trim(models.F("paper_id")))
                    & ~models.Q(paper_id="")
                ),
                name="initialpaper_paper_id_trimmed_nonempty",
            ),
        ),
    ]
