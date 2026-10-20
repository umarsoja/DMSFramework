from django.db import migrations


GROUP_NAME = "DMS Executive Authority"
PERMISSIONS = ("issue_management_directives", "assign_workflow_tasks")


def seed_executive_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    group, _ = Group.objects.using(schema_editor.connection.alias).get_or_create(name=GROUP_NAME)
    alias = schema_editor.connection.alias
    content_type, _ = ContentType.objects.using(alias).get_or_create(
        app_label="documents", model="document",
    )
    for codename in PERMISSIONS:
        permission, _ = Permission.objects.using(alias).get_or_create(
            content_type_id=content_type.pk,
            codename=codename,
            defaults={"name": codename.replace("_", " ").capitalize()},
        )
        group.permissions.add(permission)


class Migration(migrations.Migration):
    dependencies = [("documents", "0018_alter_emailoutbox_message_type")]
    operations = [migrations.RunPython(seed_executive_group, migrations.RunPython.noop)]
