# APGC DMS authorization architecture

Current-state and target-role documentation snapshot: 4 October 2026. This is a documentation-only model; role groups, users, permissions, application behavior, and database records have not been changed.

## Decision and reference documents

- [Employee onboarding and account activation](employee-onboarding-and-activation.md)
- [Official DMS role definitions](dms-role-definitions.md)
- [Role-permission matrix](role-permission-matrix.md)
- [Workflow responsibility model](workflow-responsibility-model.md)
- [Test-user recommendations](test-user-matrix.md)
- [Role-aware workspace UX](role-based-workspace-ux.md)
- [Internal Memo recipient UX](internal-memo-recipient-ux.md)
- [Current-state use cases and authorization gaps](current-state-use-cases.md)

## Use-case diagrams

The PlantUML source shows current behavior and labels future role use cases explicitly:

- [Overall DMS use cases](diagrams/overall-dms-use-cases.puml)
- [DMS Administrator](diagrams/role-use-cases/administrator.puml)
- [DMS Department Manager/HOD](diagrams/role-use-cases/department-manager-hod.puml)
- [DMS Documents Officer](diagrams/role-use-cases/documents-officer.puml)
- [DMS Records/Registry Officer](diagrams/role-use-cases/records-registry-officer.puml)
- [DMS Standard User](diagrams/role-use-cases/standard-user.puml)
- [DMS Viewer](diagrams/role-use-cases/viewer.puml)
- [DMS Workflow Administrator](diagrams/role-use-cases/workflow-admin.puml)

Earlier foundation diagrams for the general DMS user, workflow, and administration remain in `diagrams/`.

## Current implementation snapshot

The live development database has all seven DMS groups but no permissions assigned to those groups. DMS Department Manager/HOD is currently assigned to the MD/CEO, COO, and Head of IT; DMS Standard User is assigned to Iliyas Nasir. The remaining roles have no members in this snapshot. Existing employee accounts are not Django staff, while the configured `/admin/login/` uses staff-only Admin authentication. Read [test-user-matrix.md](test-user-matrix.md) before planning interactive tests.

Implementation update, 7 October 2026: role grants and application onboarding have since changed. The 4 October database and behavior findings above remain historical observations; see [employee onboarding and account activation](employee-onboarding-and-activation.md) and the update in [role-permission-matrix.md](role-permission-matrix.md) for the implemented policy.
