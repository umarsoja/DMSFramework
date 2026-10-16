# APGC DMS test-user matrix

Snapshot date: 4 October 2026. This recommends test assignments; it does not change users or memberships. The live development DB was queried read-only. Four active employee profiles and one superuser account were present. All DMS role groups have zero permissions, so existing memberships currently label users but do not authorize role-specific actions.

## Existing APGC identities

| Person | Current organizational placement | Current DMS group(s) | Current account facts | Test recommendation |
|---|---|---|---|---|
| Iliyas Nasir | Information Technology · Information Technology Expert · reports to Head of IT | DMS Standard User | Employee profile active/employed; `is_staff=False` | Use as the Standard User and Memo owner/submitter after login configuration supports ordinary employees. |
| Head of IT — Umar Yakubu | Information Technology · Head of Information Technology · reports to MD/CEO | DMS Department Manager/HOD | Employee profile active/employed; `is_staff=False` | Use as a distinct selected reviewer for Iliyas's test Memo; separately evaluate intended Department Manager/HOD scope after policy is implemented. Workflow reviewer is a temporary task responsibility, not a permanent role. |
| MD/CEO — Muhammed Gidado Modibbo | Managing Director CEO · Managing Director / Chief Executive Officer · no supervisor | DMS Department Manager/HOD | Employee profile active/employed; current assignment has no supervisor; `is_staff=False` | Include as optional third participant to test a valid no-supervisor executive and future manager scope. Do not make DMS Administrator based on seniority. |
| COO — Abubakar Muhammed Alhaji | Executive Director / Chief Operating Officer department; current position label is “Executive Director / Chief Executive Officer”; reports to MD/CEO | DMS Department Manager/HOD | Employee profile active/employed; `is_staff=False` | Not required for the first owner/reviewer flow; current position/department naming should be confirmed by the organization before using as a role test. |
| Existing superuser | Account-level Django superuser; no employee profile was found | No DMS group | `is_staff=True`, active, superuser | Use only to test Django Admin/superuser behavior. Superuser is not the DMS Administrator role and does not bypass Memo workspace object checks. |

The actual MD/CEO department is named **“Managing Director CEO”**, not the requested example label “Office of MD/CEO.” The actual executive position label is “Managing Director / Chief Executive Officer.” This is a naming alignment item, not changed here. The MD/CEO’s lack of a supervisor is valid organizational data. A workflow configured specifically for CREATOR_SUPERVISOR would have no actor for a document originating from that assignment; the resolver currently raises a validation error rather than inventing a supervisor.

## Minimum first controlled multi-user test

Two existing employee identities are sufficient for the current single-step Memo workflow:

1. **Iliyas Nasir — DMS Standard User**: create/save a disposable Memo, submit it, respond to a return, finalize after approval, and archive as owner.
2. **Head of IT — DMS Department Manager/HOD**: serve as a distinct selected reviewer for that test cycle and approve or return the assigned pending task.

This tests owner-vs-assignee isolation and the existing workflow path. Group membership currently does not cause those actions; ownership and explicit WorkflowTask assignment do. The MD/CEO is a useful third test identity for the no-supervisor case and future department-scoped authorization but is not required for a basic owner/reviewer cycle.

### Login blocker

The employee accounts above, including all three recommended pilot users, have `is_staff=False`. The configured `/admin/login/` uses Django Admin authentication, which rejects non-staff users. They are active and have EmployeeProfiles, but **cannot currently sign in through the configured login**. Therefore the minimum *interactive UI* test is blocked until the application’s login/authentication design is changed or an approved non-staff login path exists. Do not set `is_staff` casually as a workaround: it grants access to Django Admin surfaces according to Django permissions and is a security-model decision. No credentials were read or included in this report.

## Additional role-specific test identities

To test the following roles distinctly once role grants and UI behavior exist, use dedicated non-superuser test accounts rather than bundling several high-privilege roles together:

| Additional identity | Why needed | Suggested scope |
|---|---|---|
| DMS Administrator | Test application-level system administration separately from Django superuser | Dedicated DMS administrator account; no assumption of Django superuser rights |
| DMS Workflow Administrator | Test workflow definition, step/routing configuration and monitoring | Separate account with only approved workflow administration authority |
| DMS Documents Officer | Test operational handling distinct from authoring and registry custody | Separate account assigned a test department/operational scope |
| DMS Records/Registry Officer | Test registry/archival custody and records lifecycle distinct from document operations | Separate account assigned approved registry scope |
| DMS Viewer | Test enforced read-only access and download/history policy | Separate account with explicit document scope; no workflow assignment |

These identities are not yet effective as role tests because no DMS group currently has permissions. Existing users should not be placed in additional roles until the role-to-permission policy is approved.

## Seeded role membership snapshot

| Official DMS role label | Seeded group status / current users |
|---|---|
| DMS Administrator | Group exists; no permission grants and no member found |
| DMS Department Manager/HOD | Group exists; MD/CEO, COO, and Head of IT are members; no permission grants |
| DMS Documents Officer | Approved label; current seeded group is named **DMS Document Officer** (singular); no permission grants and no member found |
| DMS Records/Registry Officer | Group exists; no permission grants and no member found |
| DMS Standard User | Group exists; Iliyas Nasir is a member; no permission grants |
| DMS Viewer | Group exists; no permission grants and no member found |
| DMS Workflow Administrator | Group exists; no permission grants and no member found |

No direct Django user permissions were found on the queried accounts. The group naming difference is recorded only; this task does not rename or modify groups.
