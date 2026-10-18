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


class WorkflowActionType(models.Model):
    """Governed, reportable action vocabulary used by workflow steps/tasks."""

    code = models.SlugField(max_length=32, unique=True)
    label = models.CharField(max_length=80)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["label", "code"]

    def __str__(self):
        return self.label


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
    action_type = models.ForeignKey(
        WorkflowActionType, null=True,
        on_delete=models.PROTECT, related_name="workflow_steps",
    )
    actor_type = models.CharField(max_length=32, choices=ActorType.choices, default=ActorType.SELECTED_USER)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["definition", "sequence"], name="uniq_workflow_step_order")]
        ordering = ["sequence"]

    def __str__(self):
        return self.name


class Document(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        REGISTERED = "REGISTERED", "Registered"
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
    issuing_office = models.ForeignKey(
        "organization.Department", null=True, blank=True, on_delete=models.PROTECT,
        related_name="issued_documents",
    )
    issuing_office_code = models.CharField(max_length=32, blank=True, editable=False)
    numbering_year = models.PositiveSmallIntegerField(null=True, blank=True, editable=False)
    number_sequence = models.PositiveIntegerField(null=True, blank=True, editable=False)
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
        constraints = [models.UniqueConstraint(
            fields=["issuing_office_code", "numbering_year", "number_sequence"],
            condition=models.Q(number_sequence__isnull=False),
            name="uniq_document_scoped_number",
        )]
        permissions = [
            ("download_document", "Can download permitted DMS documents"),
            ("review_document", "Can review assigned DMS documents"),
            ("reassign_workflow_tasks", "Can reassign pending DMS workflow tasks"),
            ("register_incoming_correspondence", "Can register incoming correspondence"),
            ("issue_management_directives", "Can issue management directives against DMS documents"),
            ("assign_workflow_tasks", "Can assign DMS workflow tasks"),
        ]

    def __str__(self):
        return f"{self.reference or 'Unnumbered'} — {self.title}"

    @property
    def current_version(self):
        return self.versions.get(number=self.current_version_number)

    @property
    def memo_date(self):
        return timezone.localtime(self.created_at).date()


class DocumentNumberSequence(models.Model):
    """Transactional counter for an issuing office and calendar year."""

    office_code = models.CharField(max_length=32)
    year = models.PositiveSmallIntegerField()
    last_sequence = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["office_code", "year"], name="uniq_document_number_office_year",
        )]
        ordering = ["office_code", "year"]

    def __str__(self):
        return f"{self.office_code}/{self.year}: {self.last_sequence}"


class RelatedDocument(models.Model):
    class RelationshipType(models.TextChoices):
        RESPONSE_TO = "RESPONSE_TO", "Response to"
        REVISION_OF = "REVISION_OF", "Revision of"
        SUPPORTS = "SUPPORTS", "Supports"
        REFERENCES = "REFERENCES", "References"
        RELATED_TO = "RELATED_TO", "Related to"

    source_document = models.ForeignKey(
        Document, on_delete=models.PROTECT, related_name="outgoing_relationships",
    )
    target_document = models.ForeignKey(
        Document, on_delete=models.PROTECT, related_name="incoming_relationships",
    )
    relationship_type = models.CharField(max_length=20, choices=RelationshipType.choices)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_document_relationships",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_document", "target_document", "relationship_type"],
                name="uniq_related_document_typed_pair",
            ),
            models.CheckConstraint(
                condition=~models.Q(source_document=models.F("target_document")),
                name="related_document_not_self_referential",
            ),
        ]
        indexes = [models.Index(fields=["source_document", "is_active"])]

    def clean(self):
        if self.source_document_id and self.source_document_id == self.target_document_id:
            raise ValidationError({"target_document": "A document cannot be related to itself."})
        if self.relationship_type not in self.RelationshipType.values:
            raise ValidationError({"relationship_type": "Choose a supported relationship type."})

    def __str__(self):
        return f"{self.source_document} {self.get_relationship_type_display()} {self.target_document}"


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
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        RETURNED = "RETURNED", "Returned"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    instance = models.ForeignKey(WorkflowInstance, on_delete=models.CASCADE, related_name="tasks")
    submitted_version = models.ForeignKey(
        DocumentVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="workflow_tasks",
    )
    step = models.ForeignKey(WorkflowStep, on_delete=models.PROTECT, related_name="tasks")
    action_type = models.ForeignKey(
        WorkflowActionType, null=True, on_delete=models.PROTECT, related_name="tasks",
    )
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_workflow_tasks")
    assigned_assignment = models.ForeignKey(
        "organization.EmployeeAssignment", null=True, blank=True, on_delete=models.PROTECT,
        related_name="workflow_tasks",
    )
    parent_task = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="supporting_tasks",
    )
    cycle = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    instruction = models.TextField(blank=True)
    assigned_at = models.DateTimeField(default=timezone.now, editable=False)
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    response = models.TextField(blank=True)
    evidence = models.ManyToManyField(Attachment, blank=True, related_name="workflow_tasks")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["instance", "step", "cycle"], name="uniq_workflow_task_cycle")]
        ordering = ["cycle", "step__sequence"]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.only("submitted_version_id", "parent_task_id").get(pk=self.pk)
            original_version_id = original.submitted_version_id
            if original_version_id != self.submitted_version_id:
                raise ValidationError("A workflow task's submitted document version cannot be changed.")
            if original.parent_task_id != self.parent_task_id:
                raise ValidationError("A workflow task's parent cannot be changed.")
        elif not self.submitted_version_id:
            raise ValidationError("A new workflow task must identify its submitted document version.")

        if self.submitted_version_id:
            if self.submitted_version.document_id != self.instance.document_id:
                raise ValidationError({
                    "submitted_version": "The submitted version must belong to the workflow's document."
                })
        if self.parent_task_id:
            if self.pk and self.parent_task_id == self.pk:
                raise ValidationError({"parent_task": "A task cannot be its own parent."})
            if self.parent_task.instance.document_id != self.instance.document_id:
                raise ValidationError({"parent_task": "A supporting task must belong to its parent's document."})
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.instance.document.reference}: {self.step} ({self.get_status_display()})"


class ManagementDirective(models.Model):
    """Immutable management instruction targeting a DMS Document."""

    target_document = models.ForeignKey(
        Document, on_delete=models.PROTECT, related_name="management_directives",
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="issued_management_directives",
    )
    action_type = models.ForeignKey(
        WorkflowActionType, on_delete=models.PROTECT, related_name="management_directives",
    )
    instruction = models.TextField()
    issued_at = models.DateTimeField(default=timezone.now, editable=False)
    workflow_task = models.OneToOneField(
        WorkflowTask, null=True, blank=True, on_delete=models.PROTECT, related_name="management_directive",
    )

    class Meta:
        ordering = ["-issued_at", "-pk"]
        indexes = [models.Index(fields=["target_document", "issued_at"])]
        default_permissions = ("view",)

    def clean(self):
        if not self.instruction.strip():
            raise ValidationError({"instruction": "Provide the management instruction."})
        if self.workflow_task_id:
            task = self.workflow_task
            if task.instance.document_id != self.target_document_id:
                raise ValidationError({"workflow_task": "The task must target the directive's document."})
            if task.action_type_id != self.action_type_id:
                raise ValidationError({"workflow_task": "The task action must match the directive action."})

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Management directives are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Management directives cannot be deleted.")

    def __str__(self):
        return f"{self.action_type}: {self.target_document.reference or self.target_document.title}"


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

    @property
    def submitted_version(self):
        """The version reviewed by this decision's immutable workflow task."""
        return self.task.submitted_version

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
        TASK_COMPLETED = "TASK_COMPLETED", "Workflow task completed"
        TASK_ASSIGNED = "TASK_ASSIGNED", "Supporting workflow task assigned"
        RELATED_DOCUMENT_ADDED = "RELATED_DOCUMENT_ADDED", "Related document added"
        RELATED_DOCUMENT_REMOVED = "RELATED_DOCUMENT_REMOVED", "Related document removed"
        INCOMING_REGISTERED = "INCOMING_REGISTERED", "Incoming correspondence registered"
        INCOMING_REVISION_REGISTERED = "INCOMING_REVISION_REGISTERED", "Incoming correspondence revision registered"
        DIRECTIVE_ISSUED = "DIRECTIVE_ISSUED", "Management directive issued"

    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="audit_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_audit_events")
    actor_assignment = models.ForeignKey(
        "organization.EmployeeAssignment", null=True, blank=True, on_delete=models.PROTECT,
        related_name="document_audit_events",
    )
    action = models.CharField(max_length=32, choices=Action.choices)
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


class NotificationReceipt(models.Model):
    """Per-user seen state for a notification derived from an immutable DMS event."""

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="dms_notification_receipts",
    )
    event = models.ForeignKey(
        AuditEvent, on_delete=models.PROTECT, related_name="notification_receipts",
    )
    workflow_task = models.ForeignKey(
        WorkflowTask, null=True, blank=True, on_delete=models.PROTECT,
        related_name="notification_receipts",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-event__occurred_at", "-pk"]
        constraints = [models.UniqueConstraint(
            fields=["recipient", "event"], name="uniq_user_notification_event",
        )]
        indexes = [models.Index(fields=["recipient", "seen_at"])]

    @property
    def document(self):
        return self.event.document

    @property
    def title(self):
        if self.workflow_task_id:
            if self.event.document.document_type.code == "IM" and self.workflow_task.action_type_id:
                if self.workflow_task.action_type.code == "approve":
                    return "Memo requires your approval"
                if self.workflow_task.action_type.code == "review":
                    return "Memo requires your review"
            if self.event.action == AuditEvent.Action.TASK_COMPLETED:
                return "Supporting task response received" if self.workflow_task.parent_task_id else "Task response received"
            task = self.workflow_task
            return task.action_type.label if task.action_type_id else task.step.name
        if self.event.document.document_type.code == "IM":
            if self.event.action == AuditEvent.Action.APPROVED:
                return "Your memo has been approved."
            if self.event.action == AuditEvent.Action.RETURNED:
                return "Your memo has been returned for revision."
        if self.event.action == AuditEvent.Action.INCOMING_REGISTERED:
            return "Incoming correspondence awaits management action"
        return self.event.get_action_display()

    @property
    def message(self):
        if (
            self.event.document.document_type.code == "IM"
            and self.event.action == AuditEvent.Action.RETURNED
        ):
            reason = self.event.context.get("comment", "").strip()
            return reason
        return ""


class EmailOutbox(models.Model):
    """Delivery bookkeeping only; email content and secrets are never persisted."""

    class MessageType(models.TextChoices):
        ACCOUNT_ACTIVATION = "ACCOUNT_ACTIVATION", "Account activation"
        PASSWORD_RESET = "PASSWORD_RESET", "Password reset"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        PROCESSING = "PROCESSING", "Processing"
        SENT = "SENT", "Provider accepted"
        FAILED = "FAILED", "Failed"

    class DeliveryProvider(models.TextChoices):
        SMTP = "smtp", "SMTP"
        ZOHO_API = "zoho_api", "Zoho Mail API"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dms_email_deliveries",
    )
    message_type = models.CharField(max_length=32, choices=MessageType.choices)
    delivery_provider = models.CharField(max_length=16, choices=DeliveryProvider.choices, default=DeliveryProvider.SMTP)
    automatic_retry_enabled = models.BooleanField(default=False)
    source_reference = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    queued_at = models.DateTimeField(default=timezone.now, editable=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    latest_failure_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-queued_at", "-pk"]
        indexes = [models.Index(fields=["status", "queued_at"])]

    def __str__(self):
        return f"{self.get_message_type_display()} to user {self.recipient_id} ({self.status})"


class ZohoMailCredential(models.Model):
    """Encrypted OAuth material for the configured Zoho Mail account."""

    singleton = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    refresh_token = models.TextField()
    access_token = models.TextField(blank=True)
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()

    def save(self, *args, **kwargs):
        self.singleton = 1
        super().save(*args, **kwargs)
