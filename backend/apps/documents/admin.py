from django.contrib import admin

from .models import (
    AuditEvent,
    Document,
    DocumentAccess,
    DocumentType,
    DocumentVersion,
    WorkflowDefinition,
    WorkflowActionType,
    WorkflowDecision,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
    WorkflowTaskReassignment,
    RelatedDocument,
)


@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    search_fields = ("code", "name")


@admin.register(WorkflowDefinition)
class WorkflowDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "document_type", "version", "is_active")
    list_filter = ("document_type", "is_active")


@admin.register(WorkflowActionType)
class WorkflowActionTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "label", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "label")


@admin.register(WorkflowStep)
class WorkflowStepAdmin(admin.ModelAdmin):
    list_display = ("name", "definition", "sequence", "action_type", "is_review")
    list_filter = ("definition", "action_type", "is_review")


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


@admin.register(RelatedDocument)
class RelatedDocumentAdmin(admin.ModelAdmin):
    list_display = ("source_document", "relationship_type", "target_document", "created_by", "created_at", "is_active")
    list_filter = ("relationship_type", "is_active", "created_at")
    search_fields = ("source_document__reference", "source_document__title", "target_document__reference", "target_document__title")
    readonly_fields = tuple(field.name for field in RelatedDocument._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
    list_display = ("instance", "step", "action_type", "assigned_to", "cycle", "status", "assigned_at", "due_at", "completed_at")
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
