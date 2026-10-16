# APGC DMS role and permission matrix

Snapshot: 4 October 2026. The seven official DMS role labels below are distinct from organizational positions and workflow responsibilities. This matrix separates current application behavior from grants attributable to membership in a DMS group.

Implementation update, 7 October 2026: migrations `organization.0007` and `organization.0008` grant `organization.manage_roles` and `organization.manage_employees` to DMS Administrator, and `documents.reassign_workflow_tasks` to DMS Workflow Administrator. No department or position management permissions are granted by those migrations. Employee bootstrap and activation behavior is documented in [employee-onboarding-and-activation.md](employee-onboarding-and-activation.md). Database observations dated before these migrations are historical snapshots.

## Symbols and interpretation

- **✓** — granted to that DMS group by a migration and enforced by the application.
- **△** — the function exists, but access is conditional on ownership, explicit document access, task assignment, an account-level permission, or Django Admin permissions; the DMS role itself does not grant it.
- **—** — membership in the corresponding DMS role group does not grant this permission.
- **?** — the role-specific capability is a future requirement or needs a policy decision.

The migrations seed seven role groups and add only the grants shown below. The first matrix shows *effective grants attributable to group membership*; the second records application functions that exist independently of the role groups.

## Effective role grants today

| Capability | DMS Administrator | DMS Department Manager/HOD | DMS Documents Officer | DMS Records/Registry Officer | DMS Standard User | DMS Viewer | DMS Workflow Administrator |
|---|---:|---:|---:|---:|---:|---:|---:|
| Sign in | — | — | — | — | — | — | — |
| View authorized documents | — | — | — | — | — | — | — |
| Create a document | — | — | — | — | — | — | — |
| Edit own draft | — | — | — | — | — | — | — |
| Submit / resubmit | — | — | — | — | — | — | — |
| Review / approve / return assigned task | — | — | — | — | — | — | — |
| View department documents by role | — | — | — | — | — | — | — |
| Manage employees | ✓ | — | — | — | — | — | — |
| Manage DMS roles | ✓ | — | — | — | — | — | — |
| Reassign workflow tasks | — | — | — | — | — | — | ✓ |
| Operate/manage documents belonging to others | — | — | — | — | — | — | — |
| Registry, retention, archive records | — | — | — | — | — | — | — |
| View audit by role | — | — | — | — | — | — | — |
| Configure workflows by role | — | — | — | — | — | — | — |

The dashes mean **the DMS group alone grants no listed permission**, not that the application has no such feature. The migration grants above are limited to the listed operations. Login is controlled by Django account state and staff status; other document and workflow actions use the non-role controls below.

## Role responsibility fit and implementation status

This second matrix associates each official role with its likely responsibility while retaining the current/future distinction. **△** means a related function exists only through an independent scoped control and is not granted by the role group. **?** means the role-specific behavior is not implemented or policy remains to be decided. **—** means the capability is not part of that role's stated responsibility. The ✓ entries identify the three migration-backed group permissions above.

| Responsibility / capability | DMS Administrator | DMS Department Manager/HOD | DMS Documents Officer | DMS Records/Registry Officer | DMS Standard User | DMS Viewer | DMS Workflow Administrator |
|---|---:|---:|---:|---:|---:|---:|---:|
| Create/edit/submit own Memo | △ | △ | △ | — | △ | ? | — |
| Review a task explicitly assigned to the user | △ | △ | — | — | △ | ? | △ |
| View documents in an authorized department scope | ? | ? | ? | ? | — | — | — |
| Operational intake / cross-owner document handling | ? | — | ? | — | — | — | — |
| Registry / retention / records custody | ? | — | — | ? | — | — | — |
| Explicit read-only document view | △ | △ | △ | △ | △ | △ | △ |
| Enforced Viewer read-only boundary | ? | — | — | — | — | ? | — |
| Configure workflow definitions and steps | △ | — | — | — | — | — | △ |
| Workflow monitoring and routing governance | ? | — | — | — | — | — | ? |
| Enterprise DMS role/configuration governance | ? | — | — | — | — | — | — |
| Organization employee/department administration | △ | ? | — | — | — | — | — |

For example, △ under DMS Standard User for Memo creation means the creation view exists for logged-in users; it does **not** mean the Standard User group grants it. Likewise, current Admin workflow configuration is available via Django Admin permissions, not by the DMS Workflow Administrator group.

## Existing capabilities and their actual controls

| Capability | Current behavior | Current control | DMS role effect |
|---|---|---|---|
| Login | Configured login is Django Admin login | Active account, `is_staff`, usable credentials | Group membership does not allow login; current employee accounts are not staff |
| View/list documents | Memo list and detail exist; list search filters reference/title within visible documents | Owner, active view grant, or any task assigned to user | No role or department-wide scope check |
| Create Memo | Authenticated user can create a draft | Login required; creator becomes owner | No create permission or role check |
| Edit / submit / resubmit | Owner can edit DRAFT/RETURNED and submit valid versions | Owner and lifecycle state; reviewer required on submit | No role check |
| Review / approve / return | Assigned reviewer can decide a pending task | Assigned actor, PENDING task, UNDER_REVIEW document; return needs reason | `documents.review_document` is declared but not enforced; task assignment controls decisions |
| Finalize / archive | Owner can finalize APPROVED and archive FINALIZED document | Owner and lifecycle state; final output uses approved version | No records/registry role gate |
| View PDF / attachment | PDF is available after finalization; permitted attachments can be viewed by users who can access the document | Object visibility and document state | Viewer role does not independently grant view |
| Download | Owner or grant with both `can_view` and `can_download` | `DocumentAccess` object flags | `documents.download_document` exists but is not enforced by this path |
| Reassign task | Pending task can be reassigned to an eligible employee with reason | `documents.reassign_workflow_tasks` plus task/state/target validation | DMS Workflow Administrator receives this permission in `organization.0007` |
| Manage employees | Onboard/edit profiles and dated assignments | `organization.manage_employees`; self-management prohibited | DMS Administrator receives this permission in `organization.0008` |
| Manage departments / positions | Create and edit structure records | `organization.manage_departments` / `organization.manage_positions` in app views | No DMS group contains these permissions |
| Manage role membership | Supported role assignment and roles view | `organization.manage_roles`; changes through organization workflow or audited Admin | DMS Administrator receives this permission in `organization.0007` |
| Configure workflow | Django Admin has definition/step configuration models; app supports selected-user, creator, creator-supervisor, and department-head actor types | Django Admin model permissions and current workflow data | No group-to-permission mapping; current Internal Memo is one selected-user step |
| View audit/history | Memo audit appears on accessible detail; organization audit is available in organization/admin surfaces | Object visibility, Django model/admin permissions, relevant app permission | No audit role grant |

### Django permission inventory

Custom permissions currently declared:

| Permission | Declared purpose | Actual enforcement |
|---|---|---|
| `organization.manage_employees` | Onboard/manage employee records | Employee/structure app views; service safeguards |
| `organization.manage_departments` | Manage departments | Department app views |
| `organization.manage_positions` | Manage positions | Position app views |
| `organization.manage_roles` | Manage DMS role/group assignments | Roles view and organization services |
| `documents.reassign_workflow_tasks` | Reassign pending workflow tasks | Reassignment view/service, plus state/eligibility/reason |
| `documents.review_document` | Review assigned documents | Declared only; actual review uses task assignment and state |
| `documents.download_document` | Download documents | Declared only; actual download uses owner or access grant flags |

Standard Django model permissions also exist and can govern model pages in Admin. They are not automatically consulted by document workspace services. `EmployeeProfileAdmin` and `EmployeeAssignmentAdmin` explicitly disable add/change/delete. Workflow definition/step administration is separate from the custom DMS role groups.

## Role intent compared with implementation

| Official role | Current status | Current facts |
|---|---|---|
| DMS Administrator | **Partial** | Django superuser/Admin can configure many models. DMS Administrator group itself is unprivileged and is not equivalent to `is_superuser`. Workspace object checks remain. |
| DMS Department Manager/HOD | **Partial** | Department head and organizational assignments exist; actor resolver can target department head when a workflow step is configured that way. No role-based department document visibility or management is enforced. |
| DMS Documents Officer | **Future** | Generic Memo creation and owner lifecycle exist, but no officer-specific operational access, document correction, registration, or cross-owner handling is granted. Seeded group is named “DMS Document Officer” (singular), unlike the approved role label. |
| DMS Records/Registry Officer | **Future** | Owner archive exists; no role-based registry, retention, records transfer, disposal, or archive administration exists. |
| DMS Standard User | **Partial** | Core Memo authoring/submission is available to any logged-in user; the group does not enable or constrain it. |
| DMS Viewer | **Partial** | Object-level view-only grants exist, with separate optional download; the Viewer group does not enforce read-only behavior. Assigned workflow tasks can provide decision actions regardless of this group. |
| DMS Workflow Administrator | **Partial** | Workflow definitions/steps are configurable in Django Admin, and task reassignment has a custom permission. Neither is mapped to this role group; monitoring and configuration are not provided as role-aware workspace tools. |

## Department-scope decision

Do not implement `department = access to every document in the department`. A future authorization decision should compose:

1. **DMS role** — establishes an action family, not a universal data bypass.
2. **Organizational relationship** — current assignment and, for manager use, the department the user is authorized to manage.
3. **Ownership and workflow responsibility** — owner and assigned reviewer remain explicit relationships.
4. **Classification** — Internal, Confidential, and Restricted are current values, but no classification-based access policy is currently enforced.
5. **Explicit access** — document-level grants remain separately recorded and distinguish view from download.
6. **Action and state** — view, download, edit, decide, finalize, and archive are separate checks.

Policy decisions still needed: which classifications a department manager may see; whether a manager sees all permitted department records or only escalated/assigned items; whether cross-department executives receive explicit grants; and who may delegate access. These decisions must be enforced server-side, logged, and tested. Organizational position or department alone must not grant access.

## Source and snapshot notes

Source: `apps/documents/access.py`, `actors.py`, `services.py`, `views.py`, `forms.py`, `models.py`; `apps/organization/models.py`, `views.py`, `services.py`, `admin.py`; workflow/admin registrations; current database group/permission/user membership query. As of 4 October 2026, DMS group permission sets are empty. Current memberships include DMS Department Manager/HOD for the MD/CEO, COO, and Head of IT accounts, and DMS Standard User for Iliyas Nasir. No direct user permissions were found for these accounts.
