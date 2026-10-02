from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin, UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError

from .models import Department, EmployeeAssignment, EmployeeProfile, OrganizationAuditEvent, Position
from .services import save_department, save_position


def _audit_role_change(request, user, before, after):
    if before == after:
        return
    from .services import current_assignment
    OrganizationAuditEvent.objects.create(
        actor=request.user,
        actor_assignment=current_assignment(request.user),
        action=OrganizationAuditEvent.Action.ROLE_CHANGED,
        subject_user=user,
        context={"added": sorted(after - before), "removed": sorted(before - after), "source": "Django admin"},
    )


admin.site.unregister(get_user_model())


@admin.register(get_user_model())
class AuditedUserAdmin(DjangoUserAdmin):
    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and EmployeeProfile.objects.filter(user_id=obj.pk).exists() and "is_active" not in fields:
            fields.append("is_active")
        return tuple(fields)

    def save_model(self, request, obj, form, change):
        previous_is_active = None
        if change and obj.pk:
            previous_is_active = get_user_model().objects.filter(pk=obj.pk).values_list("is_active", flat=True).first()
        if previous_is_active is not None and previous_is_active != obj.is_active:
            if EmployeeProfile.objects.filter(user_id=obj.pk).exists():
                raise ValidationError("Manage employee account status through the organisation employee record.")
        super().save_model(request, obj, form, change)
        if previous_is_active is not None and previous_is_active != obj.is_active:
            from .services import current_assignment
            OrganizationAuditEvent.objects.create(
                actor=request.user,
                actor_assignment=current_assignment(request.user),
                action=OrganizationAuditEvent.Action.ACCOUNT_STATUS_CHANGED,
                subject_user=obj,
                context={
                    "from": "ACTIVE" if previous_is_active else "INACTIVE",
                    "to": "ACTIVE" if obj.is_active else "INACTIVE",
                    "source": "Django admin",
                },
            )

    def save_related(self, request, form, formsets, change):
        before = set(form.instance.groups.filter(name__startswith="DMS ").values_list("name", flat=True))
        before_permissions = set(form.instance.user_permissions.values_list("content_type__app_label", "codename"))
        super().save_related(request, form, formsets, change)
        after = set(form.instance.groups.filter(name__startswith="DMS ").values_list("name", flat=True))
        _audit_role_change(request, form.instance, before, after)
        after_permissions = set(form.instance.user_permissions.values_list("content_type__app_label", "codename"))
        if before_permissions != after_permissions:
            from .services import current_assignment
            OrganizationAuditEvent.objects.create(
                actor=request.user,
                actor_assignment=current_assignment(request.user),
                action=OrganizationAuditEvent.Action.USER_PERMISSIONS_CHANGED,
                subject_user=form.instance,
                context={"added": sorted(f"{app}.{code}" for app, code in after_permissions - before_permissions),
                         "removed": sorted(f"{app}.{code}" for app, code in before_permissions - after_permissions),
                         "source": "Django admin"},
            )


admin.site.unregister(Group)


@admin.register(Group)
class AuditedGroupAdmin(DjangoGroupAdmin):
    def save_related(self, request, form, formsets, change):
        before = set(form.instance.permissions.values_list("content_type__app_label", "codename"))
        super().save_related(request, form, formsets, change)
        after = set(form.instance.permissions.values_list("content_type__app_label", "codename"))
        if before != after:
            from .services import current_assignment
            OrganizationAuditEvent.objects.create(
                actor=request.user,
                actor_assignment=current_assignment(request.user),
                action=OrganizationAuditEvent.Action.GROUP_PERMISSIONS_CHANGED,
                subject_group=form.instance,
                context={"added": sorted(f"{app}.{code}" for app, code in after - before),
                         "removed": sorted(f"{app}.{code}" for app, code in before - after)},
            )


@admin.register(EmployeeProfile)
class EmployeeProfileAdmin(admin.ModelAdmin):
    list_display = ("staff_id", "user", "employment_status", "account_status")
    search_fields = ("staff_id", "user__username", "user__first_name", "user__last_name", "user__email")
    list_filter = ("employment_status", "account_status")
    readonly_fields = tuple(field.name for field in EmployeeProfile._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "parent", "head", "is_active")
    search_fields = ("code", "name")
    list_filter = ("is_active",)

    def save_model(self, request, obj, form, change):
        save_department(actor=request.user, department=obj, values=form.cleaned_data)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "department", "is_active")
    search_fields = ("code", "title", "department__name")
    list_filter = ("department", "is_active")

    def save_model(self, request, obj, form, change):
        save_position(actor=request.user, position=obj, values=form.cleaned_data)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EmployeeAssignment)
class EmployeeAssignmentAdmin(admin.ModelAdmin):
    list_display = ("employee", "department", "position", "supervisor", "start_date", "end_date")
    list_filter = ("department", "position")
    readonly_fields = tuple(field.name for field in EmployeeAssignment._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OrganizationAuditEvent)
class OrganizationAuditEventAdmin(admin.ModelAdmin):
    list_display = ("action", "subject_user", "subject_group", "actor", "occurred_at")
    list_filter = ("action", "occurred_at")
    readonly_fields = tuple(field.name for field in OrganizationAuditEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
