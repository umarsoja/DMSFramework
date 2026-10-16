from django.db import migrations


ROLE_PERMISSION_MAP = {
    "DMS Administrator": (
        ("organization", "employeeprofile", "manage_roles", "Can manage DMS role and group assignments"),
    ),
    "DMS Workflow Administrator": (
        ("documents", "document", "reassign_workflow_tasks", "Can reassign pending DMS workflow tasks"),
    ),
}


def assign_enforced_role_permissions(apps, schema_editor):
    alias = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    for group_name, permission_specs in ROLE_PERMISSION_MAP.items():
        group = Group.objects.using(alias).get(name=group_name)
        for app_label, model, codename, permission_name in permission_specs:
            content_type, _ = ContentType.objects.using(alias).get_or_create(
                app_label=app_label,
                model=model,
            )
            permission, _ = Permission.objects.using(alias).get_or_create(
                content_type_id=content_type.pk,
                codename=codename,
                defaults={"name": permission_name},
            )
            group.permissions.add(permission)


def unassign_enforced_role_permissions(apps, schema_editor):
    alias = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    for group_name, permission_specs in ROLE_PERMISSION_MAP.items():
        group = Group.objects.using(alias).filter(name=group_name).first()
        if not group:
            continue
        for app_label, model, codename, _ in permission_specs:
            content_type = ContentType.objects.using(alias).filter(app_label=app_label, model=model).first()
            if content_type:
                permission = Permission.objects.using(alias).filter(
                    content_type_id=content_type.pk,
                    codename=codename,
                ).first()
                if permission:
                    group.permissions.remove(permission)


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0006_alter_organizationauditevent_action"),
        ("documents", "0005_workflowtask_submitted_version"),
    ]

    operations = [
        migrations.RunPython(assign_enforced_role_permissions, unassign_enforced_role_permissions),
    ]
