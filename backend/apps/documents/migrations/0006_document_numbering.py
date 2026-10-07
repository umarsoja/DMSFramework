from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0005_workflowtask_submitted_version"),
        ("organization", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="issuing_office",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="issued_documents",
                to="organization.department",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="issuing_office_code",
            field=models.CharField(blank=True, editable=False, max_length=32),
        ),
        migrations.AddField(
            model_name="document",
            name="numbering_year",
            field=models.PositiveSmallIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="number_sequence",
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.UniqueConstraint(
                condition=models.Q(number_sequence__isnull=False),
                fields=("issuing_office_code", "numbering_year", "number_sequence"),
                name="uniq_document_scoped_number",
            ),
        ),
        migrations.CreateModel(
            name="DocumentNumberSequence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("office_code", models.CharField(max_length=32)),
                ("year", models.PositiveSmallIntegerField()),
                ("last_sequence", models.PositiveIntegerField(default=0)),
            ],
            options={
                "ordering": ["office_code", "year"],
            },
        ),
        migrations.AddConstraint(
            model_name="documentnumbersequence",
            constraint=models.UniqueConstraint(
                fields=("office_code", "year"), name="uniq_document_number_office_year",
            ),
        ),
    ]
