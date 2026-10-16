# APGC DMS workflow responsibility model

## Permanent roles versus per-document responsibility

The approved DMS roles describe enduring application responsibilities. Workflow responsibility is assigned for a document cycle and must not be treated as a permanent role:

| Responsibility | Current source | Current action |
|---|---|---|
| Document Owner | `Document.created_by` | Create/save own draft, submit/resubmit, finalize after approval, archive finalized document |
| Reviewer | `WorkflowTask.assigned_to` | Decide the pending assigned task while the document is under review |
| Approver | Current decision by the assigned reviewer | “Approve” is an outcome of the single current review step; not a separate permanent DMS role |
| Decision Maker | Actor recorded on `WorkflowDecision` | Actor must be the assigned task user; decision binds to the task's submitted `DocumentVersion` |
| Reassignment operator | User invoking task reassignment | Requires `documents.reassign_workflow_tasks`, pending task, under-review document, eligible target, and reason |

Membership in DMS Department Manager/HOD or DMS Workflow Administrator does not currently assign a task or confer permission to decide one. The task is the authority for the current decision.

## Current Internal Memo process

1. Owner creates or edits a Memo version.
2. On submission, the owner selects one eligible reviewer; the workflow task binds to the exact submitted `DocumentVersion`.
3. The assigned reviewer can approve or return that version. Return requires a reason.
4. On return, the owner revises with a note and resubmits a new version/cycle.
5. On approval, the owner can finalize; the final PDF and related events use the approved version.
6. The owner can archive after finalization.

The current Memo `WorkflowStep` is one selected-user review step. The model supports actor types `SELECTED_USER`, `DOCUMENT_CREATOR`, `CREATOR_SUPERVISOR`, and `DEPARTMENT_HEAD`, but a configurable actor type is not the same as a role mapping. Current documents are not routed automatically based on DMS group membership.

## Organizational source for actor resolution

EmployeeAssignment records Department, Position, optional Supervisor, and effective dates. A profile-based workflow actor must have an active account, employed status, current assignment, and active department/position. A department-head actor resolves through `Department.head` for the document's originating department. The current supervisor chain is:

```text
MD/CEO (no supervisor)
└── Head of IT
    └── Iliyas Nasir (Information Technology Expert)
```

The MD/CEO having no supervisor is valid. If a process selects `CREATOR_SUPERVISOR` for an MD/CEO-originated document, actor resolution cannot succeed and reports that no active supervisor exists. No fallback to CEO, a group, or another executive is implemented.

## Future process decisions

- Decide whether DMS Department Manager/HOD receives a department-head task automatically, sees an escalated queue, or only reviews explicit assignments.
- Keep the workflow actor/decision separate from document recipients in the To/Through/CC fields.
- Decide if approver and reviewer become different sequential responsibilities in a future multi-step workflow. The current flow has no separate approver step.
- Define whether DMS Workflow Administrator can edit active definitions, publish a new workflow version, pause routing, or reassign tasks; every configuration change should be audited.
- Define handling when no supervisor or department head is configured. Fail closed and require explicit alternate routing; do not infer an actor from job title.
- Preserve version-specific workflow binding and immutable decision history as authorization invariants.

See [workflow-use-cases.puml](diagrams/workflow-use-cases.puml) and the role-specific diagrams under `diagrams/role-use-cases/`.
