from django.db import migrations


def create_internal_memo_workflow(apps, schema_editor):
    DocumentType = apps.get_model("documents", "DocumentType")
    WorkflowDefinition = apps.get_model("documents", "WorkflowDefinition")
    WorkflowStep = apps.get_model("documents", "WorkflowStep")
    document_type, _ = DocumentType.objects.using(schema_editor.connection.alias).get_or_create(
        code="IM",
        defaults={"name": "Internal Memo", "is_active": True},
    )
    definition, _ = WorkflowDefinition.objects.using(schema_editor.connection.alias).get_or_create(
        document_type=document_type,
        version=1,
        defaults={"name": "Internal Memo Review", "is_active": True},
    )
    WorkflowStep.objects.using(schema_editor.connection.alias).get_or_create(
        definition=definition,
        sequence=1,
        defaults={"name": "Reviewer approval", "is_review": True},
    )


class Migration(migrations.Migration):
    dependencies = [("documents", "0001_initial")]

    operations = [migrations.RunPython(create_internal_memo_workflow, migrations.RunPython.noop)]
