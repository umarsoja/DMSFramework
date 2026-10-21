from pathlib import Path
from datetime import datetime, time

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .handlers import (
    INCOMING_CORRESPONDENCE_TYPE_CODE,
    INTERNAL_MEMO_TYPE_CODE,
    get_document_type_handler,
)
from .models import (
    Attachment,
    AuditEvent,
    Document,
    DocumentAccess,
    DocumentType,
    DocumentVersion,
    ManagementDirective,
    WorkflowDecision,
    WorkflowDefinition,
    WorkflowActionType,
    WorkflowInstance,
    WorkflowStep,
    RelatedDocument,
    WorkflowTask,
)
from .numbering import allocate_document_reference


def _record(document, actor, action, *, version=None, context=None):
    from apps.organization.services import current_assignment

    return AuditEvent.objects.create(
        document=document,
        actor=actor,
        actor_assignment=current_assignment(actor),
        action=action,
        version=version,
        context=context or {},
    )


def _store_attachments(version, actor, uploads):
    attachments = []
    for uploaded_file in uploads:
        filename = Path(uploaded_file.name).name[:255]
        media_type = getattr(uploaded_file, "content_type", "") or "application/octet-stream"
        attachment = Attachment(
            version=version,
            original_filename=filename,
            media_type=media_type[:120],
            size=uploaded_file.size,
            uploaded_by=actor,
        )
        attachment.file.save(filename, uploaded_file, save=True)
        attachments.append(attachment)
    return attachments


def _carry_forward_attachments(version, attachments):
    """Copy version attachment rows while reusing their existing storage objects."""
    source_ids = []
    for source in attachments:
        Attachment.objects.create(
            version=version,
            file=source.file.name,
            original_filename=source.original_filename,
            media_type=source.media_type,
            size=source.size,
            uploaded_by=source.uploaded_by,
            uploaded_at=source.uploaded_at,
        )
        source_ids.append(source.pk)
    return source_ids


def _new_version(document, actor, content, revision_note="", uploads=(), carry_forward_attachments=()):
    version_number = document.current_version_number + 1 if document.pk and document.versions.exists() else 1
    version = DocumentVersion.objects.create(
        document=document,
        number=version_number,
        content=content,
        revision_note=revision_note[:500],
        created_by=actor,
    )
    carried_attachment_ids = _carry_forward_attachments(version, carry_forward_attachments)
    _store_attachments(version, actor, uploads)
    document.title = content["subject"]
    document.classification = content["classification"]
    document.current_version_number = version_number
    document.save(update_fields=["title", "classification", "current_version_number", "modified_at"])
    return version, carried_attachment_ids


def _submit_locked(document, actor, reviewer, *, submitted_version, resubmitted=False):
    if document.status not in (Document.Status.DRAFT, Document.Status.RETURNED):
        raise ValidationError("Only a draft or returned document can be submitted.")

    if document.status == Document.Status.RETURNED and not resubmitted:
        raise ValidationError("Save a revision before resubmitting a returned document.")
    if not submitted_version or submitted_version.document_id != document.pk:
        raise ValidationError("The submitted version must belong to this document.")

    workflow = WorkflowInstance.objects.filter(document=document).first()
    if workflow:
        definition = workflow.definition
    else:
        definition = WorkflowDefinition.objects.filter(
            document_type=document.document_type,
            is_active=True,
        ).prefetch_related("steps").first()
        if not definition or not definition.steps.filter(is_review=True).exists():
            raise ValidationError("No active review workflow is configured for this document type.")
        workflow = WorkflowInstance.objects.create(document=document, definition=definition)

    step = definition.steps.filter(is_review=True).order_by("sequence").first()
    from apps.organization.actors import resolve_workflow_actor

    previous_task = None
    if resubmitted and step.actor_type == step.ActorType.SELECTED_USER:
        previous_task = workflow.tasks.filter(step=step).order_by("-cycle").first()
        if not previous_task:
            raise ValidationError("The original review assignment could not be established.")
        if reviewer is not None and reviewer.pk != previous_task.assigned_to_id:
            raise ValidationError("A returned memo must be resubmitted to its original reviewer.")
        # Resolve the reviewer from workflow history, never from a mutable form value.
        reviewer = previous_task.assigned_to
    reviewer, reviewer_assignment = resolve_workflow_actor(step=step, document=document, selected_user=reviewer)
    if resubmitted and step.actor_type == step.ActorType.SELECTED_USER:
        if not previous_task:
            raise ValidationError("The original review assignment could not be established.")
        if previous_task.assigned_to_id != reviewer.pk:
            raise ValidationError("A returned memo must be resubmitted to its original reviewer.")
    if reviewer.pk == actor.pk:
        raise ValidationError("A document creator cannot review their own memo.")
    cycle = workflow.tasks.aggregate(models_max=Max("cycle"))["models_max"] or 0
    cycle += 1
    review_action = step.action_type or WorkflowActionType.objects.get(code="review")
    task = WorkflowTask.objects.create(instance=workflow, submitted_version=submitted_version, step=step,
                                       action_type=review_action,
                                       assigned_to=reviewer, assigned_assignment=reviewer_assignment, cycle=cycle)
    workflow.status = WorkflowInstance.Status.IN_PROGRESS
    workflow.completed_at = None
    workflow.save(update_fields=["status", "completed_at"])
    document.status = Document.Status.UNDER_REVIEW
    document.save(update_fields=["status", "modified_at"])
    _record(document, actor, AuditEvent.Action.RESUBMITTED if resubmitted else AuditEvent.Action.SUBMITTED,
            version=submitted_version,
            context={"reviewer_id": reviewer.pk, "cycle": cycle, "workflow_id": workflow.pk})
    event = _record(document, actor, AuditEvent.Action.UNDER_REVIEW,
                    version=submitted_version,
                    context={"reviewer_id": reviewer.pk, "cycle": cycle, "workflow_id": workflow.pk,
                             "task_id": task.pk})
    from .notifications import record_notification
    record_notification(event, reviewer, workflow_task=task)
    return workflow


@transaction.atomic
def create_memo(*, owner, values, uploads=(), reviewer=None):
    from apps.organization.services import current_assignment

    content = get_document_type_handler(INTERNAL_MEMO_TYPE_CODE).prepare_payload(values)
    document_type = DocumentType.objects.get(code=INTERNAL_MEMO_TYPE_CODE, is_active=True)
    originating_assignment = current_assignment(owner)
    document = Document.objects.create(
        document_type=document_type,
        title=content["subject"],
        classification=content["classification"],
        created_by=owner,
        originating_assignment=originating_assignment,
        issuing_office=originating_assignment.department if originating_assignment else None,
        issuing_office_code=(originating_assignment.department_code_snapshot if originating_assignment else ""),
    )
    version_result = _new_version(document, owner, content, uploads=uploads)
    version = version_result[0] if isinstance(version_result, tuple) else version_result
    DocumentAccess.objects.create(
        document=document,
        user=owner,
        can_view=True,
        can_download=True,
        granted_by=owner,
    )
    _record(document, owner, AuditEvent.Action.CREATED, version=version)
    if reviewer:
        _submit_locked(document, owner, reviewer, submitted_version=version)
    else:
        _record(document, owner, AuditEvent.Action.DRAFT_SAVED, version=version)
    return document


@transaction.atomic
def register_incoming_correspondence(*, actor, values):
    from apps.organization.services import current_assignment
    from apps.organization.models import Department, EmployeeProfile

    if not actor.has_perm("documents.register_incoming_correspondence"):
        raise PermissionDenied("You are not authorized to register incoming correspondence.")
    selected_office = values["receiving_office"]
    receiving_office = Department.objects.select_for_update().get(pk=selected_office.pk)
    if not receiving_office.is_active:
        raise ValidationError({"receiving_office": "Choose an active receiving office."})
    if not receiving_office.head_id:
        raise ValidationError({
            "receiving_office": "The receiving office must have an active designated manager before correspondence can be registered.",
        })
    manager = EmployeeProfile.objects.select_for_update().select_related("user").get(
        pk=receiving_office.head_id,
    )
    manager_assignment = manager.assignments.select_for_update().filter(
        end_date__isnull=True,
        start_date__lte=timezone.localdate(),
    ).select_related("department", "position").first()
    if (
        manager.account_status != EmployeeProfile.AccountStatus.ACTIVE
        or not manager.user.is_active
        or manager.employment_status != EmployeeProfile.EmploymentStatus.EMPLOYED
        or manager_assignment is None
        or manager_assignment.department_id != receiving_office.pk
        or not manager_assignment.department.is_active
        or not manager_assignment.position.is_active
    ):
        raise ValidationError({
            "receiving_office": "The receiving office must have an active designated manager with a current assignment in that office.",
        })

    values = {**values, "receiving_office": receiving_office}
    handler = get_document_type_handler(INCOMING_CORRESPONDENCE_TYPE_CODE)
    content = handler.prepare_payload(values)
    document_type = DocumentType.objects.get(code=INCOMING_CORRESPONDENCE_TYPE_CODE, is_active=True)
    receiving_office = values["receiving_office"]
    originating_assignment = current_assignment(actor)
    document = Document.objects.create(
        document_type=document_type,
        title=content["subject"],
        classification=content["classification"],
        status=Document.Status.REGISTERED,
        created_by=actor,
        originating_assignment=originating_assignment,
        issuing_office=receiving_office,
        issuing_office_code=receiving_office.code,
    )
    uploads = [values["original_document"], *values.get("supporting_documents", ())]
    version, _ = _new_version(document, actor, content, uploads=uploads)
    DocumentAccess.objects.create(
        document=document, user=actor, can_view=True, can_download=True, granted_by=actor,
    )
    manager_grant_created = manager.user_id != actor.pk
    if manager_grant_created:
        DocumentAccess.objects.create(
            document=document, user=manager.user, can_view=True, can_download=False, granted_by=actor,
        )
    cc_profiles = list(values.get("cc", ()))
    # Keep the service boundary authoritative when callers bypass the form.
    from apps.organization.directory import current_directory_profiles
    eligible_cc_ids = set(current_directory_profiles(include_viewers=False).values_list("pk", flat=True))
    if any(profile.pk not in eligible_cc_ids for profile in cc_profiles):
        raise ValidationError({"cc": "Choose only active employees with a current DMS assignment."})
    cc_users = []
    for cc_profile in cc_profiles:
        cc_user = cc_profile.user
        if cc_user.pk in {actor.pk, manager.user_id} or cc_user.pk in {user.pk for user in cc_users}:
            continue
        DocumentAccess.objects.create(
            document=document, user=cc_user, can_view=True, can_download=False, granted_by=actor,
        )
        cc_users.append(cc_user)
    allocate_document_reference(document)
    document.save(update_fields=[
        "reference", "numbering_year", "number_sequence", "modified_at",
    ])
    event = _record(document, actor, AuditEvent.Action.INCOMING_REGISTERED, version=version, context={
        "document_type": INCOMING_CORRESPONDENCE_TYPE_CODE,
        "receiving_office_id": receiving_office.pk,
        "external_reference": content["external_reference"],
        "visibility_grants": [
            {
                "user_id": actor.pk,
                "reason": "document_creator",
                "can_view": True,
                "can_download": True,
            },
            {
                "user_id": manager.user_id,
                "employee_profile_id": manager.pk,
                "assignment_id": manager_assignment.pk,
                "reason": "receiving_office_designated_head",
                "grant_created": manager_grant_created,
                "can_view": True,
                "can_download": False,
            },
        ],
    })
    if manager_grant_created:
        from .notifications import record_notification
        record_notification(event, manager.user)
    from .notifications import record_notification
    for cc_user in cc_users:
        record_notification(event, cc_user)
    return document


@transaction.atomic
def register_incoming_revision(*, document, actor, values):
    from .access import can_view_document

    if not actor.has_perm("documents.register_incoming_correspondence"):
        raise PermissionDenied("You are not authorized to register incoming correspondence revisions.")
    document = Document.objects.select_for_update().get(pk=document.pk)
    if not can_view_document(actor, document):
        raise PermissionDenied("You are not authorized to access this incoming correspondence.")
    if document.document_type.code != INCOMING_CORRESPONDENCE_TYPE_CODE:
        raise ValidationError("Only incoming correspondence can receive an external-document revision.")
    if document.status != Document.Status.REGISTERED:
        raise ValidationError("Only registered incoming correspondence can receive a revision.")

    current_version = document.current_version
    current_content = current_version.content
    supporting_names = set(current_content.get("supporting_filenames", ()))
    new_names = {
        Path(upload.name).name[:255]
        for upload in [values["original_document"], *values.get("supporting_documents", ())]
    }
    if supporting_names.intersection(new_names):
        raise ValidationError({
            "supporting_documents": "New files must use filenames distinct from carried-forward supporting files.",
        })
    carry_forward = [
        item for item in current_version.attachments.all()
        if item.original_filename in supporting_names
    ]
    handler = get_document_type_handler(INCOMING_CORRESPONDENCE_TYPE_CODE)
    content = handler.prepare_revision_payload(current_content, values)
    uploads = [values["original_document"], *values.get("supporting_documents", ())]
    version, carried_ids = _new_version(
        document,
        actor,
        content,
        revision_note=values["revision_note"],
        uploads=uploads,
        carry_forward_attachments=carry_forward,
    )
    _record(document, actor, AuditEvent.Action.INCOMING_REVISION_REGISTERED, version=version, context={
        "revision_note": values["revision_note"].strip(),
        "carried_forward_attachment_ids": carried_ids,
        "previous_version": current_version.number,
    })
    return document


@transaction.atomic
def issue_management_directive(*, target_document, issuer, action_type, instruction,
                                assignee=None, due_date=None):
    """Issue an immutable directive, optionally creating its accountable task."""
    from apps.organization.actors import is_workflow_actor_eligible
    from apps.organization.models import EmployeeProfile
    from apps.organization.services import current_assignment
    from .access import can_view_document

    if not issuer.has_perm("documents.issue_management_directives"):
        raise PermissionDenied("You are not authorized to issue management directives.")
    if not is_workflow_actor_eligible(getattr(issuer, "employee_profile", None)):
        raise PermissionDenied("An active, eligible DMS employee is required to issue management directives.")
    document = Document.objects.select_for_update().select_related("document_type").get(pk=target_document.pk)
    if not can_view_document(issuer, document):
        raise PermissionDenied("You are not authorized to access the target document.")
    if not instruction or not instruction.strip():
        raise ValidationError({"instruction": "Provide the management instruction."})

    action = WorkflowActionType.objects.filter(pk=getattr(action_type, "pk", action_type), is_active=True).first()
    if not action:
        raise ValidationError({"action_type": "Choose an active workflow action type."})
    if action.code in {"review", "approve"}:
        raise ValidationError({"action_type": "Review and approval use the existing decision workflow."})
    if due_date and not assignee:
        raise ValidationError({"due_date": "A due date requires an assigned workflow task."})

    task = None
    if assignee:
        if not issuer.has_perm("documents.assign_workflow_tasks"):
            raise PermissionDenied("You are not authorized to assign workflow tasks.")
        try:
            profile = assignee.employee_profile
        except (AttributeError, EmployeeProfile.DoesNotExist):
            profile = None
        if not is_workflow_actor_eligible(profile):
            raise ValidationError({"assignee": "Choose an active DMS employee with a current assignment."})
        issuer_profile = getattr(issuer, "employee_profile", None)
        assignee_assignment = profile.current_assignment
        from apps.organization.directory import is_executive_authority_user
        is_executive = is_executive_authority_user(issuer)
        is_department_head = bool(
            assignee_assignment
            and assignee_assignment.department.head_id == profile.pk
            and assignee_assignment.department.is_active
        )
        if profile.pk == getattr(issuer_profile, "pk", None):
            raise ValidationError({"assignee": "You cannot assign a directive to yourself."})
        if is_executive:
            if not is_department_head:
                raise ValidationError({"assignee": "Choose the active head of an active department."})
        elif (
            not issuer_profile
            or not assignee_assignment
            or assignee_assignment.supervisor_id != issuer_profile.pk
        ):
            raise ValidationError({
                "assignee": "Choose an active direct report from your current organizational assignment.",
            })
        if not document.versions.filter(number=document.current_version_number).exists():
            raise ValidationError("The target document has no current version to bind to the task.")

        definition = WorkflowDefinition.objects.filter(
            document_type=document.document_type,
            name="Management Directed Actions",
            is_active=True,
        ).first()
        step = WorkflowStep.objects.filter(
            definition=definition,
            name="Management directed action",
            sequence=1,
            is_review=False,
        ).first() if definition else None
        if not definition or not step:
            raise ValidationError("No generic management-action workflow step is configured for this document type.")
        workflow, created = WorkflowInstance.objects.get_or_create(
            document=document,
            defaults={"definition": definition},
        )
        if not created and workflow.definition_id != definition.pk:
            raise ValidationError("The document is already bound to a different workflow definition.")
        last_cycle = workflow.tasks.filter(step=step).aggregate(value=Max("cycle"))["value"] or 0
        due_at = timezone.make_aware(datetime.combine(due_date, time.max)) if due_date else None
        task = WorkflowTask.objects.create(
            instance=workflow,
            submitted_version=document.current_version,
            step=step,
            action_type=action,
            assigned_to=assignee,
            assigned_assignment=current_assignment(assignee),
            cycle=last_cycle + 1,
            instruction=instruction.strip(),
            due_at=due_at,
        )

    directive = ManagementDirective(
        target_document=document,
        issued_by=issuer,
        action_type=action,
        instruction=instruction.strip(),
        workflow_task=task,
    )
    directive.full_clean()
    directive.save()
    audit_version = task.submitted_version if task else document.versions.filter(
        number=document.current_version_number,
    ).first()
    event = _record(document, issuer, AuditEvent.Action.DIRECTIVE_ISSUED,
                    version=audit_version,
                    context={
                "directive_id": directive.pk,
                "action_type": action.code,
                "instruction": directive.instruction,
                "issued_at": directive.issued_at.isoformat(),
                "assignee_id": task.assigned_to_id if task else None,
                "due_at": task.due_at.isoformat() if task and task.due_at else None,
                "task_id": task.pk if task else None,
                    })
    if task:
        from .notifications import record_notification
        record_notification(event, task.assigned_to, workflow_task=task)
    return directive


@transaction.atomic
def create_supporting_workflow_task(*, parent_task_id, actor, assignee, action_type,
                                    instruction, due_date=None):
    """Delegate a supporting task from an actor's own pending task."""
    from apps.organization.actors import is_workflow_actor_eligible
    from apps.organization.models import EmployeeProfile
    from apps.organization.services import current_assignment
    from .access import can_view_document

    if not actor.has_perm("documents.assign_workflow_tasks"):
        raise PermissionDenied("You are not authorized to assign workflow tasks.")
    parent = WorkflowTask.objects.select_for_update(of=("self",)).select_related(
        "instance__document__document_type", "submitted_version", "step", "assigned_to",
    ).get(pk=parent_task_id)
    document = Document.objects.select_for_update().get(pk=parent.instance.document_id)
    if parent.assigned_to_id != actor.pk or parent.status != WorkflowTask.Status.PENDING:
        raise PermissionDenied("Only the current assignee of a pending task may delegate supporting work.")
    if not can_view_document(actor, document):
        raise PermissionDenied("You are not authorized to access the target document.")
    if parent.action_type_id and parent.action_type.code in {"review", "approve"}:
        raise ValidationError("Review and approval tasks cannot be delegated as supporting work.")
    if not instruction or not instruction.strip():
        raise ValidationError({"instruction": "Provide the task instruction."})

    action = WorkflowActionType.objects.filter(
        pk=getattr(action_type, "pk", action_type), is_active=True,
    ).exclude(code__in=("review", "approve")).first()
    if not action:
        raise ValidationError({"action_type": "Choose an active actionable task type."})

    try:
        actor_profile = actor.employee_profile
        assignee_profile = assignee.employee_profile
    except EmployeeProfile.DoesNotExist:
        raise ValidationError({"assignee": "Choose an active employee with a current assignment."})
    if not is_workflow_actor_eligible(assignee_profile):
        raise ValidationError({"assignee": "Choose an active DMS employee with a current assignment."})
    assignment = assignee_profile.current_assignment
    if (
        assignee_profile.pk == actor_profile.pk
        or not actor_profile.current_assignment
        or assignment is None
        or assignment.supervisor_id != actor_profile.pk
    ):
        raise ValidationError({
            "assignee": "Choose an active direct report from your current organizational assignment.",
        })

    due_at = timezone.make_aware(datetime.combine(due_date, time.max)) if due_date else None
    last_cycle = parent.instance.tasks.filter(step=parent.step).aggregate(value=Max("cycle"))["value"] or 0
    task = WorkflowTask.objects.create(
        instance=parent.instance,
        submitted_version=parent.submitted_version,
        step=parent.step,
        action_type=action,
        assigned_to=assignee,
        assigned_assignment=current_assignment(assignee),
        cycle=last_cycle + 1,
        parent_task=parent,
        instruction=instruction.strip(),
        due_at=due_at,
    )
    event = _record(document, actor, AuditEvent.Action.TASK_ASSIGNED,
                    version=task.submitted_version,
                    context={
                        "task_id": task.pk,
                        "parent_task_id": parent.pk,
                        "assignee_id": assignee.pk,
                        "action_type": action.code,
                        "instruction": task.instruction,
                        "due_at": due_at.isoformat() if due_at else None,
                    })
    from .notifications import record_notification
    record_notification(event, assignee, workflow_task=task)
    return task


@transaction.atomic
def save_memo(*, document, actor, values, uploads=(), revision_note="", reviewer=None,
              remove_attachment_ids=()):
    document = Document.objects.select_for_update().get(pk=document.pk)
    if document.created_by_id != actor.pk:
        raise PermissionDenied("Only the document owner can edit this memo.")
    if document.status not in (Document.Status.DRAFT, Document.Status.RETURNED):
        raise ValidationError("Only a draft or returned memo can be edited.")
    if document.status == Document.Status.RETURNED and not revision_note.strip():
        raise ValidationError("Add a revision note before saving a returned memo.")

    content = get_document_type_handler(INTERNAL_MEMO_TYPE_CODE).prepare_payload(values)
    source_attachments = list(document.current_version.attachments.all())
    try:
        removed_ids = {int(attachment_id) for attachment_id in remove_attachment_ids}
    except (TypeError, ValueError):
        raise ValidationError({"remove_attachments": "Choose only attachments on the current version."})
    available_ids = {attachment.pk for attachment in source_attachments}
    if not removed_ids.issubset(available_ids):
        raise ValidationError({"remove_attachments": "Choose only attachments on the current version."})
    attachments_to_carry = [attachment for attachment in source_attachments if attachment.pk not in removed_ids]
    version, carried_attachment_ids = _new_version(
        document,
        actor,
        content,
        revision_note=revision_note,
        uploads=uploads,
        carry_forward_attachments=attachments_to_carry,
    )
    attachment_audit_context = {
        "carried_forward_attachment_ids": carried_attachment_ids,
        "removed_attachment_ids": sorted(removed_ids),
    }
    if document.status == Document.Status.RETURNED:
        _record(document, actor, AuditEvent.Action.REVISION_SAVED, version=version,
                context={"revision_note": revision_note.strip(), **attachment_audit_context})
    else:
        _record(document, actor, AuditEvent.Action.DRAFT_SAVED, version=version,
                context=attachment_audit_context)
    if reviewer:
        _submit_locked(document, actor, reviewer, submitted_version=version,
                       resubmitted=document.status == Document.Status.RETURNED)
    return document


@transaction.atomic
def decide_review(*, task_id, actor, outcome, comment=""):
    from apps.organization.services import current_assignment

    task = WorkflowTask.objects.select_for_update(of=("self",)).select_related(
        "instance__document", "instance__definition", "step", "submitted_version"
    ).get(pk=task_id)
    document = Document.objects.select_for_update().get(pk=task.instance.document_id)
    if task.assigned_to_id != actor.pk or task.status != WorkflowTask.Status.PENDING:
        raise PermissionDenied("You do not have an active review assignment for this memo.")
    if document.status != Document.Status.UNDER_REVIEW:
        raise ValidationError("This memo is no longer awaiting review.")
    if not task.submitted_version_id:
        raise ValidationError("This workflow task has no safely identified submitted version and cannot be decided.")
    if outcome not in (WorkflowDecision.Outcome.APPROVE, WorkflowDecision.Outcome.RETURN):
        raise ValidationError("Choose approve or return for revision.")
    if outcome == WorkflowDecision.Outcome.RETURN and not comment.strip():
        raise ValidationError({"comment": "A reason is required when returning a memo."})

    decision = WorkflowDecision.objects.create(
        task=task,
        actor=actor,
        actor_assignment=current_assignment(actor),
        outcome=outcome,
        comment=comment.strip(),
    )
    task.status = WorkflowTask.Status.APPROVED if outcome == WorkflowDecision.Outcome.APPROVE else WorkflowTask.Status.RETURNED
    task.completed_at = timezone.now()
    task.save(update_fields=["status", "completed_at"])
    action = AuditEvent.Action.APPROVED if outcome == WorkflowDecision.Outcome.APPROVE else AuditEvent.Action.RETURNED
    document.status = Document.Status.APPROVED if outcome == WorkflowDecision.Outcome.APPROVE else Document.Status.RETURNED
    document.save(update_fields=["status", "modified_at"])
    workflow = task.instance
    workflow.status = WorkflowInstance.Status.APPROVED if outcome == WorkflowDecision.Outcome.APPROVE else WorkflowInstance.Status.RETURNED
    workflow.save(update_fields=["status"])
    event = _record(document, actor, action, version=task.submitted_version,
                    context={"decision_id": decision.pk, "task_id": task.pk,
                             "workflow_id": workflow.pk, "comment": decision.comment})
    if document.document_type.code == INTERNAL_MEMO_TYPE_CODE:
        from .notifications import record_notification

        record_notification(event, document.created_by)
    return decision


def _latest_approved_task(document):
    workflow = WorkflowInstance.objects.filter(document=document).first()
    approved_task = workflow.tasks.filter(status=WorkflowTask.Status.APPROVED).select_related(
        "submitted_version"
    ).order_by("-cycle").first() if workflow else None
    if not approved_task or not approved_task.submitted_version_id:
        raise ValidationError("The approved workflow has no safely identified document version to finalize.")
    return approved_task


def approved_workflow_version(document):
    """Return the exact version bound to the latest approved workflow task."""
    return _latest_approved_task(document).submitted_version


@transaction.atomic
def finalize_document(*, document, actor):
    document = Document.objects.select_for_update().get(pk=document.pk)
    if document.created_by_id != actor.pk:
        raise PermissionDenied("Only the memo owner can finalize it.")
    if document.status != Document.Status.APPROVED:
        raise ValidationError("Only an approved memo can be finalized.")
    workflow = document.workflow
    approved_task = _latest_approved_task(document)
    finalized_at = timezone.now()
    allocate_document_reference(document, issued_at=finalized_at)
    document.status = Document.Status.FINALIZED
    document.finalized_by = actor
    document.finalized_at = finalized_at
    document.save(update_fields=[
        "reference", "numbering_year", "number_sequence", "status", "finalized_by", "finalized_at", "modified_at",
    ])
    workflow.status = WorkflowInstance.Status.COMPLETED
    workflow.completed_at = document.finalized_at
    workflow.save(update_fields=["status", "completed_at"])
    _record(document, actor, AuditEvent.Action.FINALIZED, version=approved_task.submitted_version,
            context={"workflow_id": workflow.pk, "task_id": approved_task.pk})
    return document


@transaction.atomic
def archive_document(*, document, actor):
    document = Document.objects.select_for_update().get(pk=document.pk)
    if document.created_by_id != actor.pk:
        raise PermissionDenied("Only the memo owner can archive it.")
    if document.status != Document.Status.FINALIZED:
        raise ValidationError("Only a finalized memo can be archived.")
    document.status = Document.Status.ARCHIVED
    document.archived_by = actor
    document.archived_at = timezone.now()
    document.save(update_fields=["status", "archived_by", "archived_at", "modified_at"])
    _record(document, actor, AuditEvent.Action.ARCHIVED, version=approved_workflow_version(document))
    return document


@transaction.atomic
def record_access(*, document, actor, action, context=None, version=None):
    if version is None:
        version = document.current_version
    return _record(document, actor, action, version=version, context=context)


@transaction.atomic
def create_related_document(*, source_document, target_document, relationship_type, actor):
    from .access import can_view_document

    source = Document.objects.select_for_update().get(pk=source_document.pk)
    target = Document.objects.get(pk=target_document.pk)
    if not can_view_document(actor, source):
        raise PermissionDenied("You are not authorized to access the source document.")
    if not can_view_document(actor, target):
        raise PermissionDenied("You are not authorized to access the target document.")
    if source.pk == target.pk:
        raise ValidationError({"target_document": "A document cannot be related to itself."})
    if relationship_type not in RelatedDocument.RelationshipType.values:
        raise ValidationError({"relationship_type": "Choose a supported relationship type."})

    relationship, created = RelatedDocument.objects.get_or_create(
        source_document=source,
        target_document=target,
        relationship_type=relationship_type,
        defaults={"created_by": actor},
    )
    if not created:
        if not relationship.is_active:
            relationship.is_active = True
            relationship.save(update_fields=["is_active"])
            action = AuditEvent.Action.RELATED_DOCUMENT_ADDED
        else:
            raise ValidationError("This document relationship already exists.")
    else:
        action = AuditEvent.Action.RELATED_DOCUMENT_ADDED
    _record(source, actor, action, context={
        "relationship_id": relationship.pk,
        "target_document_id": target.pk,
        "relationship_type": relationship.relationship_type,
    })
    return relationship


def related_documents(*, document, user):
    from .access import visible_documents

    visible = visible_documents(user)
    outgoing = RelatedDocument.objects.filter(
        source_document=document, target_document__in=visible, is_active=True,
    ).select_related("target_document", "created_by")
    incoming = RelatedDocument.objects.filter(
        target_document=document, source_document__in=visible, is_active=True,
    ).select_related("source_document", "created_by")
    return {"outgoing": outgoing, "incoming": incoming}


@transaction.atomic
def deactivate_related_document(*, relationship_id, actor):
    from .access import can_view_document

    relationship = RelatedDocument.objects.select_for_update().select_related(
        "source_document", "target_document"
    ).get(pk=relationship_id, is_active=True)
    if not can_view_document(actor, relationship.source_document):
        raise PermissionDenied("You are not authorized to access the source document.")
    if not can_view_document(actor, relationship.target_document):
        raise PermissionDenied("You are not authorized to access the target document.")
    relationship.is_active = False
    relationship.save(update_fields=["is_active"])
    _record(relationship.source_document, actor, AuditEvent.Action.RELATED_DOCUMENT_REMOVED, context={
        "relationship_id": relationship.pk,
        "target_document_id": relationship.target_document_id,
        "relationship_type": relationship.relationship_type,
    })
    return relationship


@transaction.atomic
def reassign_task(*, task_id, actor, new_user, reason):
    from apps.organization.actors import is_workflow_actor_eligible
    from apps.organization.models import EmployeeProfile
    from apps.organization.services import current_assignment
    from .models import WorkflowTaskReassignment

    if not actor.has_perm("documents.reassign_workflow_tasks"):
        raise PermissionDenied("You are not authorized to reassign workflow tasks.")
    task = WorkflowTask.objects.select_for_update(of=("self",)).select_related(
        "instance__document", "step", "assigned_to", "assigned_assignment",
        "management_directive__issued_by", "parent_task__assigned_to",
    ).get(pk=task_id)
    document = Document.objects.select_for_update().get(pk=task.instance.document_id)
    is_directive_task = hasattr(task, "management_directive")
    if task.status != WorkflowTask.Status.PENDING or (
        not is_directive_task and document.status != Document.Status.UNDER_REVIEW
    ):
        raise ValidationError("Only an eligible pending workflow task can be reassigned.")
    if not reason.strip() or len(reason.strip()) > 500:
        raise ValidationError({"reason": "Provide a reassignment reason of up to 500 characters."})
    previous_user = task.assigned_to
    previous_assignment = task.assigned_assignment
    try:
        profile = new_user.employee_profile
    except EmployeeProfile.DoesNotExist:
        profile = None
    if profile:
        if not is_workflow_actor_eligible(profile):
            raise ValidationError("A new task must be assigned to an active employee account.")
        if is_directive_task or task.parent_task_id:
            if task.parent_task_id:
                from .notifications import supporting_task_delegator

                assignment_authority = supporting_task_delegator(task)
            else:
                assignment_authority = task.management_directive.issued_by
            try:
                issuer_profile = assignment_authority.employee_profile
            except EmployeeProfile.DoesNotExist:
                issuer_profile = None
            assignment = profile.current_assignment
            if (
                profile.pk == getattr(issuer_profile, "pk", None)
                or issuer_profile is None
                or assignment is None
                or assignment.supervisor_id != issuer_profile.pk
            ):
                raise ValidationError({
                    "new_user": "A delegated task may only be reassigned to a current direct report of its delegating manager.",
                })
    elif not new_user.is_active or not new_user.is_staff:
        raise ValidationError("A new task must be assigned to an active staff reviewer.")
    elif is_directive_task or task.parent_task_id:
        raise ValidationError({
            "new_user": "A delegated task must be assigned to an active employee with a current direct-report relationship.",
        })
    resolved_user = new_user
    resolved_assignment = current_assignment(new_user)
    if resolved_user.pk == previous_user.pk:
        raise ValidationError("Choose someone other than the current assignee.")
    WorkflowTaskReassignment.objects.create(
        task=task,
        previous_user=previous_user,
        previous_assignment=previous_assignment,
        new_user=resolved_user,
        new_assignment=resolved_assignment,
        actor=actor,
        reason=reason.strip(),
    )
    task.assigned_to = resolved_user
    task.assigned_assignment = resolved_assignment
    task.save(update_fields=["assigned_to", "assigned_assignment"])
    event = _record(document, actor, AuditEvent.Action.REASSIGNED, version=task.submitted_version,
                    context={"task_id": task.pk, "workflow_id": task.instance_id,
                             "from_user_id": previous_user.pk,
                             "to_user_id": resolved_user.pk, "reason": reason.strip()})
    from .notifications import record_notification
    record_notification(event, resolved_user, workflow_task=task)
    return task
