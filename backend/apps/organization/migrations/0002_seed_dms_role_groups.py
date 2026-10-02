from django.db import migrations


ROLE_GROUPS = (
    "DMS Administrator",
    "DMS Records/Registry Officer",
    "DMS Workflow Administrator",
    "DMS Department Manager/HOD",
    "DMS Document Officer",
    "DMS Standard User",
    "DMS Viewer",
)


def create_role_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    alias = schema_editor.connection.alias
    for name in ROLE_GROUPS:
        Group.objects.using(alias).get_or_create(name=name)


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]
    operations = [migrations.RunPython(create_role_groups, migrations.RunPython.noop)]
