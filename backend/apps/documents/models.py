from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class DocumentType(models.Model):
    code = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class WorkflowDefinition(models.Model):
    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT, related_name="workflows")
    name = models.CharField(max_length=100)
    version = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document_type", "version"], name="uniq_doc_workflow_version")]
        ordering = ["document_type__name", "version"]

    def __str__(self):
        return f"{self.document_type}: {self.name} v{self.version}"


class WorkflowStep(models.Model):
    class ActorType(models.TextChoices):
        SELECTED_USER = "SELECTED_USER", "Selected reviewer"
        DOCUMENT_CREATOR = "DOCUMENT_CREATOR", "Document creator"
        CREATOR_SUPERVISOR = "CREATOR_SUPERVISOR", "Creator supervisor"
        DEPARTMENT_HEAD = "DEPARTMENT_HEAD", "Department head"

    definition = models.ForeignKey(WorkflowDefinition, on_delete=models.CASCADE, related_name="steps")
    sequence = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=100)
    is_review = models.BooleanField(default=True)
    actor_type = models.CharField(max_length=32, choices=ActorType.choices, default=ActorType.SELECTED_USER)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["definition", "sequence"], name="uniq_workflow_step_order")]
        ordering = ["sequence"]

    def __str__(self):
        return self.name


class Document(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        RETURNED = "RETURNED", "Returned for revision"
        APPROVED = "APPROVED", "Approved"
        FINALIZED = "FINALIZED", "Finalized"
        ARCHIVED = "ARCHIVED", "Archived"

    class Classification(models.TextChoices):
        INTERNAL = "INTERNAL", "Internal"
        CONFIDENTIAL = "CONFIDENTIAL", "Confidential"
        RESTRICTED = "RESTRICTED", "Restricted"

    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT, related_name="documents")
    reference = models.CharField(max_length=80, unique=True, null=True, blank=True, editable=False)
    title = models.CharField(max_length=240)
    classification = models.CharField(max_length=20, choices=Classification.choices, default=Classification.INTERNAL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_dms_documents")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    modified_at = models.DateTimeField(auto_now=True)
    current_version_number = models.PositiveIntegerField(default=1)
    finalized_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="finalized_dms_documents")
    finalized_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="archived_dms_documents")
    archived_at = models.DateTimeField(null=True, blank=True)
    originating_assignment = models.ForeignKey(
        "organization.EmployeeAssignment", null=True, blank=True, on_delete=models.PROTECT,
        related_name="originating_documents",
    )

    class Meta:
        ordering = ["-modified_at", "-pk"]
        permissions = [
            ("download_document", "Can download permitted DMS documents"),
            ("review_document", "Can review assigned DMS documents"),
            ("reassign_workflow_tasks", "Can reassign pending DMS workflow tasks"),
        ]

    def __str__(self):
        return f"{self.reference or 'Unnumbered'} — {self.title}"

    @property
    def current_version(self):
        return self.versions.get(number=self.current_version_number)

    @property
    def memo_date(self):
        return timezone.localtime(self.created_at).date()


def private_attachment_path(instance, filename):
    extension = Path(filename).suffix.lower()[:12]
    return f"private_documents/{instance.version.document_id}/{instance.version.number}/{uuid4().hex}{extension}"


class DocumentVersion(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    content = models.JSONField(default=dict)
    revision_note = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_document_versions")
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "number"], name="uniq_document_version_number")]
        ordering = ["-number"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Document versions are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Document versions cannot be deleted.")

    def __str__(self):
        return f"{self.document.reference} v{self.number}"


class Attachment(models.Model):
    version = models.ForeignKey(DocumentVersion, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=private_attachment_path)
    original_filename = models.CharField(max_length=255)
    media_type = models.CharField(max_length=120, default="application/octet-stream")
    size = models.PositiveBigIntegerField()
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_attachments")
    uploaded_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["original_filename", "pk"]

    def __str__(self):
        return self.original_filename


class WorkflowInstance(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        RETURNED = "RETURNED", "Returned"
        APPROVED = "APPROVED", "Approved"
        COMPLETED = "COMPLETED", "Completed"

    document = models.OneToOneField(Document, on_delete=models.CASCADE, related_name="workflow")
    definition = models.ForeignKey(WorkflowDefinition, on_delete=models.PROTECT, related_name="instances")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_PROGRESS)
    started_at = models.DateTimeField(default=timezone.now, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Workflow for {self.document.reference}"


class WorkflowTask(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending review"
        APPROVED = "APPROVED", "Approved"
        RETURNED = "RETURNED", "Returned"

    instance = models.ForeignKey(WorkflowInstance, on_delete=models.CASCADE, related_name="tasks")
    step = models.ForeignKey(WorkflowStep, on_delete=models.PROTECT, related_name="tasks")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_workflow_tasks")
    assigned_assignment = models.ForeignKey(
        "organization.EmployeeAssignment", null=True, blank=True, on_delete=models.PROTECT,
        related_name="workflow_tasks",
    )
    cycle = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    assigned_at = models.DateTimeField(default=timezone.now, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["instance", "step", "cycle"], name="uniq_workflow_task_cycle")]
        ordering = ["cycle", "step__sequence"]

    def __str__(self):
        return f"{self.instance.document.reference}: {self.step} ({self.get_status_display()})"


class WorkflowDecision(models.Model):
    class Outcome(models.TextChoices):
        APPROVE = "APPROVE", "Approved"
        RETURN = "RETURN", "Returned for revision"

    task = models.OneToOneField(WorkflowTask, on_delete=models.PROTECT, related_name="decision")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_workflow_decisions")
    actor_assignment = models.ForeignKey(
        "organization.EmployeeAssignment", null=True, blank=True, on_delete=models.PROTECT,
        related_name="workflow_decisions",
    )
    outcome = models.CharField(max_length=12, choices=Outcome.choices)
    comment = models.TextField(blank=True)
    decided_at = models.DateTimeField(default=timezone.now, editable=False)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Workflow decisions are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Workflow decisions cannot be deleted.")

    def clean(self):
        if self.outcome == self.Outcome.RETURN and not self.comment.strip():
            raise ValidationError({"comment": "A reason is required when returning a document."})

    def __str__(self):
        return f"{self.get_outcome_display()} by {self.actor}"


class DocumentAccess(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="access_grants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="dms_document_access")
    can_view = models.BooleanField(default=False)
    can_download = models.BooleanField(default=False)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_access_grants_made")
    granted_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["document", "user"], name="uniq_document_user_access"),
            models.CheckConstraint(
                condition=models.Q(can_download=False) | models.Q(can_view=True),
                name="document_download_requires_view",
            ),
        ]

    def clean(self):
        if self.can_download and not self.can_view:
            raise ValidationError({"can_download": "Download access requires view access."})


class AuditEvent(models.Model):
    class Action(models.TextChoices):
        CREATED = "CREATED", "Created"
        DRAFT_SAVED = "DRAFT_SAVED", "Draft saved"
        SUBMITTED = "SUBMITTED", "Submitted"
        UNDER_REVIEW = "UNDER_REVIEW", "Review started"
        RETURNED = "RETURNED", "Returned for revision"
        REVISION_SAVED = "REVISION_SAVED", "Revision saved"
        RESUBMITTED = "RESUBMITTED", "Resubmitted"
        APPROVED = "APPROVED", "Approved"
        FINALIZED = "FINALIZED", "Finalized"
        ARCHIVED = "ARCHIVED", "Archived"
        VIEWED = "VIEWED", "Viewed"
        PDF_VIEWED = "PDF_VIEWED", "PDF viewed"
        DOWNLOADED = "DOWNLOADED", "Downloaded"
        ATTACHMENT_VIEWED = "ATTACHMENT_VIEWED", "Attachment viewed"
        REASSIGNED = "REASSIGNED", "Workflow task reassigned"

    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="audit_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_audit_events")
    actor_assignment = models.ForeignKey(
        "organization.EmployeeAssignment", null=True, blank=True, on_delete=models.PROTECT,
        related_name="document_audit_events",
    )
    action = models.CharField(max_length=24, choices=Action.choices)
    version = models.ForeignKey(DocumentVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="audit_events")
    context = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        default_permissions = ("view",)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Audit events are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit events cannot be deleted.")

    def __str__(self):
        return f"{self.get_action_display()} — {self.document.reference}"


class WorkflowTaskReassignment(models.Model):
    task = models.ForeignKey(WorkflowTask, on_delete=models.PROTECT, related_name="reassignments")
    previous_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_tasks_reassigned_from")
    previous_assignment = models.ForeignKey(
        "organization.EmployeeAssignment", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reassignments_from",
    )
    new_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_tasks_reassigned_to")
    new_assignment = models.ForeignKey(
        "organization.EmployeeAssignment", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reassignments_to",
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_task_reassignments_made")
    reason = models.CharField(max_length=500)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["created_at", "pk"]
        default_permissions = ("view",)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Workflow reassignment records are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Workflow reassignment records cannot be deleted.")
