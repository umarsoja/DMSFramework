# APGC DMS role-aware workspace UX specification

This specifies a future user-facing workspace based on the approved role categories. It does not describe a completed UI or define new authorization grants.

## Current workspace

The authenticated navigation label **My workspace** links to the Internal Memo list (`/documents/memos/`). That screen lists documents visible to the user under the current object-level rule (owner, explicit view grant, or assigned workflow task). It is not a personal dashboard, and it is not limited to documents owned by the current user. The page supports a basic search over reference/title and status filtering. Create Memo is available from workspace navigation.

Current screens expose owner edit/submit, assigned review, finalize/archive, detail history, and finalized PDF/attachment actions according to object rules and state. There is no separate task inbox, pending action queue, recent-document panel, department queue, role-based landing page, or role-filtered action catalogue.

## Target My Workspace information architecture

| Workspace area | Expected behavior | Current status |
|---|---|---|
| My Documents | Documents owned by the user, with status and recent activity | Future; current memo list means “documents visible to me,” not strictly owned |
| My Memos | Memo drafts, returned items, submitted/approved/finalized Memos the user owns or can access | Partial; Internal Memo list exists, but not a distinct personal-owner dashboard |
| My Tasks | Assigned pending/recent workflow tasks, with originating document and due/age context | Future; users reach tasks through accessible Memo detail, no inbox route |
| Pending Actions | Action cards for assigned review, owner revision, finalization, or permitted management tasks | Future; detail page conditionally exposes buttons, no consolidated queue |
| Recent Documents | Recently viewed/updated documents the user is authorized to see | Future; no recent activity panel in DMS workspace |
| Search | Search only within authorized documents; filters for type, status, date, department/classification only when authorized | Partial; current memo list searches reference/title and filters status |
| Role-aware actions | Show only actions the user can take and enforce the same policy server-side | Future; present action visibility is mostly ownership/status/task-based rather than role-based |

The employee should see plain business terms, not `DocumentVersion`, `WorkflowInstance`, or `WorkflowTask`. These remain internal implementation concepts. Use “version”, “review”, “assigned to”, and “activity/history” only where useful to explain a user action.

## Role-aware workspace expectations

- **DMS Standard User:** My Memos, Create Memo, drafts needing submission, returned items needing revision, and submitted items awaiting decision.
- **DMS Department Manager/HOD:** authorized department queue and escalated/assigned actions only after department, classification, and delegation policy is approved. Do not show all department records by default.
- **DMS Documents Officer:** assigned intake/quality/registration queue after operational permissions and scope are specified.
- **DMS Records/Registry Officer:** finalized records awaiting registration, retention/archival actions, and retrieval queue after records policy is specified.
- **DMS Workflow Administrator:** workflow health and configuration status; keep task decisions tied to actual task assignment, not administrator role.
- **DMS Administrator:** application administration and role/configuration governance, distinct from Django superuser and subject to audited privileged access.
- **DMS Viewer:** read-only list/detail/preview for explicitly authorized items. Hide every mutating control and reject mutation requests server-side. Download and history remain separately governed.

One user can hold multiple roles only if that policy is approved. The workspace should combine visible capabilities without widening data scope by accident. Each action still requires server authorization, not merely a hidden button.

## Authorization and accessibility rules

1. Build each page from the user's authorized document queryset; never load broad results and rely on the UI to hide rows.
2. Treat view, download, edit, submit, review, finalize, archive, and administration as distinct actions.
3. Role, current organizational assignment, document ownership, workflow task assignment, classification, and explicit grants must be evaluated by policy together.
4. Keep a visible explanation when access is denied or an item requires another actor; do not reveal unauthorized document metadata.
5. Provide accessible status labels and consistent “next action” guidance for keyboard and screen-reader users.

## Not implemented now

Separate My Documents/My Tasks/Pending Actions/Recent Documents views, role-specific navigation, HOD department scope, Documents Officer queue, Records/Registry queue, Viewer group enforcement, and Workflow Administrator dashboard are future requirements. The current DMS group memberships do not drive the workspace.
