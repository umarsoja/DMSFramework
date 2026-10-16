# APGC DMS authorization and use cases — current state

This document describes the implementation as inspected on 4 October 2026. It is a current-state reference for controlled functional testing, not a proposed authorization design. A group name, model permission, route, or template control is not treated as an effective capability unless the application actually enforces it.

Historical snapshot: migrations applied on 7 October 2026 later granted
`organization.manage_roles` and `organization.manage_employees` to DMS
Administrator and `documents.reassign_workflow_tasks` to DMS Workflow
Administrator. For the post-migration permission state, use
[role-permission-matrix.md](role-permission-matrix.md).

## Scope and evidence

The review covered Django authentication and admin configuration; the Organization and Documents models, forms, views, services, and access/actor helpers; URL configuration; navigation and Memo templates; seeded groups and database permission assignments; and authorization-related tests. A read-only development database query on 4 October found seven seeded DMS groups, one active staff superuser, four active employee profiles and their current/historical assignments, and no direct user permissions. Every seeded DMS group had zero permissions. Current DMS membership includes the MD/CEO, COO, and Head of IT in DMS Department Manager/HOD and Iliyas Nasir in DMS Standard User. Employee accounts are not staff, so the configured Django Admin login currently rejects them.

The configured login is Django Admin login (`/admin/login/`). Django's admin authentication form requires `is_staff=True`. The current superuser can sign in; ordinary onboarded accounts are created inactive, with unusable passwords, and are not made staff by onboarding. A controlled credential setup and staff decision are therefore prerequisites for logging in as additional test users.

## Current identity and authorization model

- Django `User` is the account identity. `EmployeeProfile` is optional and one-to-one with a user. Its account status synchronizes `User.is_active`; an employed, active profile and a current active department/position assignment make a profiled user eligible for organizational workflow actor resolution.
- A profileless active staff user is temporarily eligible for selected-user workflow assignment. This compatibility path does not create an employee identity or assignment.
- `EmployeeAssignment` is historical organizational placement, with department/position snapshots. A current assignment has no end date and has started by the current date.
- Django Groups and permissions are available, but seeded DMS groups currently have no permissions. Group titles therefore do not grant the responsibilities suggested by their names.
- The Memo workspace applies owner, active document-access grant, and workflow-task assignment checks. Review additionally requires the task to be assigned to the acting user and pending. These business rules remain in force even when the actor is a superuser.
- `DocumentAccess` separates view from download. Owner access allows both; a grant must allow view and download for a grantee to download. Merely being able to view does not imply download.
- Django Admin is a distinct administration surface. A superuser has Django's all-permissions behavior there. Admin read-only model configurations and application service invariants still constrain particular operations.

## Current use cases

### Authenticated DMS user

1. Sign in through Django Admin login, if the account is active, staff, and has usable credentials.
2. Open the Memo workspace and list documents visible as owner, active access grantee, or task assignee.
3. Create a Memo draft. Creation assigns the current user as owner and creates an owner access grant.
4. Edit an owned draft or returned Memo; a returned Memo requires a revision note when saved.
5. Submit an owned draft/returned Memo by selecting an eligible reviewer. Submission binds the workflow task to the exact submitted `DocumentVersion`.
6. View Memo details, versions, workflow information, and the associated audit history when the document is visible to the user.
7. View a finalized/archived PDF when allowed by the document state and access policy; download requires owner status or an access grant with both view and download enabled.
8. View attachments when document access permits; download is also subject to download access.

These are not blanket capabilities for every authenticated account. The account must first pass login, and document actions are object- and state-scoped.

### Document owner and workflow participant

- The owner can create and maintain the draft, submit it, resubmit after return, finalize an approved Memo, and archive a finalized Memo. Owner and allowed lifecycle state are checked in the service layer.
- The assigned reviewer can view an assigned task/document and decide a pending task while the document is under review. The decision must be approve or return; returning requires a reason. The reviewer cannot decide another user's task.
- Reassignment is a separate capability requiring `documents.reassign_workflow_tasks`, a pending task, an under-review document, an eligible replacement actor, and a reason.
- The current Memo workflow is single-step and uses a selected reviewer. No multi-step process is represented as currently available.

### Organization and administration

- A user with `organization.manage_employees` can use employee onboarding, employee/profile management, and organizational assignment workflows, subject to service-level safeguards such as preventing self-management. Onboarding-created accounts have unusable passwords and are initialized inactive; the development database also contains previously created active employee accounts, but those accounts have `is_staff=False` and cannot use the configured Admin login.
- `organization.manage_departments` and `organization.manage_positions` gate their respective organization structure views and changes.
- `organization.manage_roles` gates role/group administration views and role assignment through the supported organization workflows. The roles page is a view of groups/permissions; actual group and permission changes are handled through Django Admin and audited.
- Django Admin authentication/authorization manages users, groups, model permissions, document configuration, and selected administrative data. Organization UserAdmin/GroupAdmin changes are audited. Sensitive workflow/document history models are configured read-only in admin.
- There is no operational DMS dashboard route at `/`; it is the public landing page. There is no standalone DMS audit-history route. Memo audit is shown on an accessible Memo detail page; organization audit is available through admin and related organization screens.

### Django superuser boundary

The superuser is an account-level Django Admin identity, not one of the DMS groups. It has Django's all-permissions behavior in admin, subject to the configured read-only `ModelAdmin` rules and service safeguards. In the Memo workspace, superuser status does not override visibility, ownership, task assignment, eligibility, or lifecycle-state checks. A superuser who is not the owner, an access grantee, or a workflow assignee does not gain Memo visibility through the current access helper. This distinction is shown in the administration diagram and permission matrix.

## Current-State Authorization Gaps

1. **Seeded roles have no effective grants.** All seven seeded groups have zero permissions in the current database. They do not currently function as authorization roles or provide workflow access.
2. **Declared document permissions are not the workspace gate.** `documents.review_document` and `documents.download_document` exist, but review is authorized by assignment/state checks, and download by owner or a `DocumentAccess` grant. The permission names alone do not enable either action.
3. **Several business actions have no dedicated Django permission.** Create/save/submit/finalize/archive and ordinary review decisions are enforced by ownership, assignment, status, and service invariants rather than dedicated permission codenames.
4. **Login provisioning is incomplete for new employees.** Onboarding makes accounts inactive, gives them unusable passwords, and does not set `is_staff`; the configured Django Admin login rejects non-staff users. A controlled credential and staff setup is needed for additional interactive users.
5. **No organizational records exist in the current development database.** Profile-based reviewer eligibility, department/position administration, employee assignment, and related organization audit flows cannot yet be exercised with existing data.
6. **The permissions UI can suggest broader capability than application behavior.** Django model permissions can be assigned, but many document actions do not consult those permissions. Conversely, workflow and object access restrictions still constrain superusers in the DMS workspace.
7. **Superuser is not an object-scope bypass in the Memo workspace.** Visibility and lifecycle helpers rely on owner, access grant, or task assignment, then ownership/status for mutations. Django's Admin superuser behavior should not be conflated with Memo workspace behavior.
8. **Audit access has no standalone application route.** Audit visibility follows the visible Memo detail or organization/admin surfaces; `view_auditevent` is a Django admin/model permission, not a substitute for document visibility.

## Readiness for controlled testing

The current employee directory contains enough profiles and assignments for a two-person workflow test using Iliyas as owner and Head of IT as reviewer, but the application is **not ready for interactive employee login** because those users are not staff and `/admin/login/` requires staff status. The role groups also have no permissions, so the established role labels do not yet change access. Use only disposable test content and do not use real APGC documents.

For broader authorization testing, include the existing superuser as a separate administration identity and a non-superuser organization manager with explicitly approved custom permissions. The MD/CEO's current assignment with no supervisor is valid; it should be tested as such. The database's seeded DMS groups cannot distinguish their nominal roles until permissions are deliberately assigned. No test users or records are created by this documentation task.

## Traceability

The related artifacts are [dms-role-definitions.md](dms-role-definitions.md), [role-permission-matrix.md](role-permission-matrix.md), [workflow-responsibility-model.md](workflow-responsibility-model.md), [test-user-matrix.md](test-user-matrix.md), [role-based-workspace-ux.md](role-based-workspace-ux.md), [internal-memo-recipient-ux.md](internal-memo-recipient-ux.md), and PlantUML diagrams in `diagrams/`.
