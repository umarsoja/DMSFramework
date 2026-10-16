# APGC DMS role definitions — current and intended

Snapshot date: 4 October 2026. These are the seven official DMS application role categories. Their names are used exactly as approved. The role descriptions distinguish implemented behavior from target responsibility; they do not create grants or change current data.

## Identity and responsibility are separate dimensions

### Organizational structure

An employee has an account and EmployeeProfile, with dated EmployeeAssignment records connecting the employee to a **Department**, **Position**, and optional **Supervisor**. The supervisor is assigned on the dated assignment, not inferred from position seniority. Organizational examples include Managing Director/CEO, Executive Director/COO, Head of IT, and Information Technology Expert.

Current data establishes:

- MD/CEO: Department “Managing Director CEO”; Position “Managing Director / Chief Executive Officer”; no supervisor. A null supervisor is valid.
- Head of IT: Information Technology; Head of Information Technology; supervisor is MD/CEO.
- Iliyas Nasir: Information Technology; Information Technology Expert; supervisor is Head of IT.

The actual MD/CEO department label differs from the example “Office of MD/CEO”; the current assignment was not altered. A future workflow step explicitly configured as `CREATOR_SUPERVISOR` will fail to resolve for a CEO-originated document with no supervisor. The workflow actor resolver reports the missing actor; it does not invent one.

### DMS application role

A DMS role describes the user's enduring responsibility in the application. Current seeded groups are permissionless, and some existing employee memberships are labels only. In the database snapshot, DMS Department Manager/HOD is assigned to the MD/CEO, COO, and Head of IT; DMS Standard User is assigned to Iliyas. All seven group permission sets are empty.

There is one naming mismatch: the approved role is **DMS Documents Officer**, while the seeded group is **DMS Document Officer**. This document keeps the approved role name and records the seed mismatch without changing the group.

### Workflow responsibility

Document Owner, Reviewer, Approver, and Decision Maker are contextual responsibilities for a document or task, not permanent DMS roles. The owner is the document creator under current service logic. The reviewer is the selected assignee of a WorkflowTask. In the current single-step Memo workflow, that assigned reviewer can approve or return; “approver” is not a separate role or separate configured step. Reassignment changes the current task assignee with an audit trail and requires `documents.reassign_workflow_tasks`.

## Official roles

### DMS Administrator

**Purpose:** Enterprise/system-level DMS application administration.

**Can (intended):** Manage approved DMS-wide configuration, role policy, supported document types, system settings, and administration visibility.

**Cannot (by implication):** Automatically access every Confidential or Restricted document, or bypass business ownership and workflow integrity checks. Elevated access should be explicitly defined and audited.

**Scope:** Enterprise-wide, subject to approved administrative policy.

**Current implementation:** **Partial.** Django Admin exposes user/group, document configuration, and selected model administration subject to Django permissions. Migration `organization.0007` grants `organization.manage_roles` and migration `organization.0008` grants `organization.manage_employees` to the DMS Administrator group. These do not make the group equivalent to a Django superuser or grant department/position management. Workspace access checks still apply to the account.

### DMS Department Manager/HOD

**Purpose:** Manage authorized document and workflow responsibility within the manager's assigned department.

**Can (intended):** See eligible department work, review assigned/escalated actions, monitor workload, and take department-scoped actions permitted by classification and explicit authorization.

**Cannot (intended):** Browse all records merely because they share a department, download without the required grant, or override restricted classification and workflow separation.

**Scope:** Current department assignment and formally delegated department scope.

**Current implementation:** **Partial.** Employee assignments and a Department.head relationship exist. Workflow actor resolution supports a department-head actor type. Current Internal Memo is configured as a selected-user step, and `visible_documents()` does not grant department-based visibility. The group has no permissions.

### DMS Documents Officer

**Purpose:** Operational document handling distinct from policy administration and records custody.

**Can (intended):** Perform approved document intake, quality checks, registration, metadata correction, and operational handoffs within assigned scope.

**Cannot (intended):** Change immutable approved versions, decide a review task unless assigned, or administer retention/disposal policy solely by being a Documents Officer.

**Scope:** Operational queue/assigned business scope, to be decided.

**Current implementation:** **Future.** Memo create/edit/submit exists for the owner, but no officer-specific cross-owner handling or permissions exist. The current seeded group is named “DMS Document Officer”; it has no permissions or members in the current snapshot.

### DMS Records/Registry Officer

**Purpose:** Custody and controlled management of registered records over their lifecycle.

**Can (intended):** Register finalized records, manage archive placement and retention tasks, retrieve records, and execute authorized records disposition when that policy is defined.

**Cannot (intended):** Rewrite immutable document content or approve business workflow decisions solely by virtue of registry custody.

**Scope:** Registry/records functions and authorized records, not a blanket read of all documents.

**Current implementation:** **Future.** The document owner can archive a finalized Memo. No registry register, retention schedule, transfer, disposal, records queue, or role-gated archive operation is implemented. The DMS Records/Registry Officer group is empty of permissions and members.

### DMS Standard User

**Purpose:** Normal employee authoring and management of documents they own or are explicitly authorized to use.

**Can:** Create a Memo draft, edit own draft/returned version, submit/resubmit, track accessible items, finalize after approval, and archive a finalized owned Memo under current business rules.

**Cannot:** Edit another owner's draft, decide a task assigned to someone else, or see every department document merely by being an employee.

**Scope:** Own documents, explicit grants, and assigned workflow tasks.

**Current implementation:** **Partial.** These operations exist but are not gated by membership in DMS Standard User; any logged-in eligible user can reach Memo creation, and ownership/state govern later actions. Iliyas is currently a member of this group. The configured login currently rejects the non-staff employee account.

### DMS Viewer

**Purpose:** Read-only access to specifically authorized documents.

**Can (intended):** View permitted document content, versions/history, and supported previews within explicit scope. Download is separate and allowed only if policy/grant permits.

**Cannot (intended):** Create, edit, submit, decide workflow, finalize, archive, or reassign.

**Scope:** Explicitly authorized documents and classifications; not blanket department access.

**Current implementation:** **Partial.** `DocumentAccess` distinguishes view from download, the Memo list has reference/title search and filters, details show versions/audit to visible users, and PDF/attachments have state/access checks. The DMS Viewer group is not consulted. A user assigned a task can review regardless of this group, so “Viewer” is not an enforced read-only security boundary today.

### DMS Workflow Administrator

**Purpose:** Configure and monitor approved workflow definitions, steps, actor resolution, routing, and task controls.

**Can (intended):** Administer configuration and monitoring tools while being unable to approve a task merely because of this role.

**Cannot (intended):** Change a submitted task's bound version or make decisions on an unassigned task by virtue of configuration access.

**Scope:** Workflow configurations and operational workflow queues.

**Current implementation:** **Partial.** WorkflowDefinition and WorkflowStep are configurable through Django Admin; actor types include selected user, document creator, creator supervisor, and department head. Current Internal Memo uses one selected-user review step. Task reassignment exists behind `documents.reassign_workflow_tasks`. There is no role-specific configuration UI or monitoring dashboard, and the DMS Workflow Administrator group has no permission grants.

## Role assignment and organizational position

Do not infer a DMS role from a position title (including CEO, HOD, Head of IT, or IT Expert). In particular, executive seniority does not imply DMS Administrator. Assign application roles through the approved role policy, independently of the organizational assignment. Workflow responsibility continues to come from document ownership and the task's current assignment.

## Readiness and implementation status key

The existing application is a functional Memo lifecycle with partial organization integration, not a complete seven-role authorization system. The current groups are not active policy. The existing role pages can show groups and permissions, but the database has no group grants. “Partial” means a related underlying data path exists without the role-specific behavior described above; it does not mean the role is safe to use as an enforcement boundary.

See [role-permission-matrix.md](role-permission-matrix.md), [workflow-responsibility-model.md](workflow-responsibility-model.md), [role-based-workspace-ux.md](role-based-workspace-ux.md), [internal-memo-recipient-ux.md](internal-memo-recipient-ux.md), and [test-user-matrix.md](test-user-matrix.md).
