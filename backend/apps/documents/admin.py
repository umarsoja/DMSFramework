from django.contrib import admin

from .models import (
    AuditEvent,
    Document,
    DocumentAccess,
    DocumentType,
    DocumentVersion,
    WorkflowDefinition,
    WorkflowDecision,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
    WorkflowTaskReassignment,
)


@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    search_fields = ("code", "name")


@admin.register(WorkflowDefinition)
class WorkflowDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "document_type", "version", "is_active")
    list_filter = ("document_type", "is_active")


@admin.register(WorkflowStep)
class WorkflowStepAdmin(admin.ModelAdmin):
    list_display = ("name", "definition", "sequence", "is_review")
    list_filter = ("definition", "is_review")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("reference", "title", "document_type", "status", "created_by", "modified_at")
    list_filter = ("document_type", "status", "classification")
    search_fields = ("reference", "title", "created_by__username")
    readonly_fields = tuple(field.name for field in Document._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DocumentAccess)
class DocumentAccessAdmin(admin.ModelAdmin):
    list_display = ("document", "user", "can_view", "can_download", "granted_by", "granted_at")
    list_filter = ("can_view", "can_download")
    search_fields = ("document__reference", "document__title", "user__username")
    readonly_fields = ("granted_at",)


@admin.register(DocumentVersion)
class DocumentVersionAdmin(admin.ModelAdmin):
    list_display = ("document", "number", "created_by", "created_at")
    readonly_fields = tuple(field.name for field in DocumentVersion._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WorkflowInstance)
class WorkflowInstanceAdmin(admin.ModelAdmin):
    list_display = ("document", "definition", "status", "started_at", "completed_at")
    readonly_fields = tuple(field.name for field in WorkflowInstance._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WorkflowTask)
class WorkflowTaskAdmin(admin.ModelAdmin):
    list_display = ("instance", "step", "assigned_to", "cycle", "status", "assigned_at", "completed_at")
    readonly_fields = tuple(field.name for field in WorkflowTask._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WorkflowDecision)
class WorkflowDecisionAdmin(admin.ModelAdmin):
    list_display = ("task", "actor", "outcome", "decided_at")
    readonly_fields = tuple(field.name for field in WorkflowDecision._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("document", "action", "actor", "occurred_at")
    list_filter = ("action", "occurred_at")
    search_fields = ("document__reference", "document__title", "actor__username")
    readonly_fields = tuple(field.name for field in AuditEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WorkflowTaskReassignment)
class WorkflowTaskReassignmentAdmin(admin.ModelAdmin):
    list_display = ("task", "previous_user", "new_user", "actor", "created_at")
    readonly_fields = tuple(field.name for field in WorkflowTaskReassignment._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
