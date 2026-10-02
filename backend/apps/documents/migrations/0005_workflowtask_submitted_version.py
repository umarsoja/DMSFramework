from django.db import migrations, models
import django.db.models.deletion


def bind_identifiable_workflow_versions(apps, schema_editor):
    WorkflowTask = apps.get_model("documents", "WorkflowTask")
    AuditEvent = apps.get_model("documents", "AuditEvent")
    database = schema_editor.connection.alias

    for task in WorkflowTask.objects.using(database).select_related("instance").iterator():
        candidates = []
        events = AuditEvent.objects.using(database).filter(
            document_id=task.instance.document_id,
            action__in=("SUBMITTED", "RESUBMITTED"),
        ).only("version_id", "context")
        for event in events.iterator():
            context = event.context if isinstance(event.context, dict) else {}
            if context.get("cycle") == task.cycle and event.version_id:
                candidates.append(event.version_id)

        if len(candidates) != 1:
            continue

        version = apps.get_model("documents", "DocumentVersion").objects.using(database).filter(
            pk=candidates[0], document_id=task.instance.document_id,
        ).only("pk").first()
        if version:
            WorkflowTask.objects.using(database).filter(pk=task.pk).update(submitted_version_id=version.pk)


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0004_alter_document_options_auditevent_actor_assignment_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowtask",
            name="submitted_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="workflow_tasks",
                to="documents.documentversion",
            ),
        ),
        migrations.RunPython(bind_identifiable_workflow_versions, migrations.RunPython.noop),
    ]
