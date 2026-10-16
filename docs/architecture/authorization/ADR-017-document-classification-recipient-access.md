# ADR-017 — APGC DMS Document Classification & Recipient Access Policy

- **Status:** Accepted
- **Decision date:** 4 October 2026
- **Scope:** Document visibility, downloads, finalized PDFs, attachments, and workflow authority

## Context

Document classification defines the audience boundary for a document. Recipient relationships (To, Through, and CC) identify named recipients, but their access depends on the classification. View access is separate from permission to download or perform lifecycle and workflow actions. Backend authorization must protect direct URLs as well as list and search results. A correspondence recipient relationship is distinct from a workflow responsibility: recipients do not become task assignees by implication. This amendment clarifies the accepted access decision; workflow semantics are specified in proposed ADR-019.

## Decision

### Classification policy

- **Internal:** Normal internal business information. The owner, To/Through/CC recipients, workflow participants, and explicitly authorized users may view, subject to applicable access rules. To/Through/CC confer view-only access.
- **Confidential:** Sensitive business information for a narrower authorized audience. The owner, explicitly identified To/Through/CC recipients, workflow participants, and explicitly authorized users may view. General organizational or departmental membership does not itself grant access. To/Through/CC confer view-only access.
- **Restricted:** Highly sensitive information requiring explicit authorization. The owner, explicitly authorized users, and appropriately assigned workflow participants may view. To/Through/CC membership alone does not grant access.

### Role / classification / access matrix

| Actor or relationship | Internal | Confidential | Restricted | Authority from relationship alone |
|---|---|---|---|---|
| Document owner | View, subject to applicable rules | View, subject to applicable rules | View, subject to applicable rules | Ownership; lifecycle actions remain separately constrained |
| To recipient | View, subject to applicable rules | View if explicitly identified | No access from recipient status alone | View only where classification permits |
| Through recipient | View, subject to applicable rules | View if explicitly identified | No access from recipient status alone | View only where classification permits |
| CC recipient | View, subject to applicable rules | View if explicitly identified | No access from recipient status alone | View only where classification permits |
| Workflow participant | View, subject to applicable rules | View, subject to applicable rules | View when appropriately assigned | Assigned workflow actions only, as configured for the task |
| Explicitly authorized user | View, subject to applicable rules | View, subject to applicable rules | View when explicitly authorized | Only the separately granted authority |
| General organizational / departmental member | No access from membership alone | No access from membership alone | No access from membership alone | None |
| Unrelated / unauthorized user | Denied | Denied | Denied | None |

“View” includes the document surfaces permitted by the applicable policy. A recipient relationship does not confer download permission. The matrix does not create role-based access merely from membership in one of the seven DMS role groups.

### General authorization principles

1. Classification is an authorization boundary; recipient relationships determine visibility only within that boundary.
2. To, Through, and CC provide view-only access where the classification permits it. They do not grant edit, submit, review, approve, finalize, archive, or administrative authority, and do not automatically create workflow tasks.
3. Download authorization is separate from view authorization.
4. PDF preview/download and attachment view/download must apply the document's authorization policy. Backend checks apply to direct URL requests.
5. Workflow assignment independently grants only the actions required by the explicitly assigned workflow task. Recipient status is not a workflow assignment. Email is a notification channel only; the DMS remains the system of record for documents, authoritative versions, tasks, decisions, classification, and audit history. Email attachments are not authoritative records.
6. General organizational or departmental membership does not by itself grant Confidential or Restricted access.
7. Unauthorized access must be denied consistently across document detail, list/search, PDF, attachment, and other document routes.

## Consequences

- Authorization must evaluate both classification and the actor's relationship or explicit grant; a broad recipient check must not override Restricted policy.
- View policy remains distinct from download, mutation, lifecycle, and workflow-action checks.
- List/search querysets and object-level route checks must enforce compatible authorization rules.
- Tests must cover each classification and access surface, including direct URLs and the separation between viewing and actions.

## Implementation status

This ADR records the approved policy. It does not itself change application behavior, schema, migrations, permissions, or UI. Implementation should centralize document visibility decisions and retain separate checks for downloads and workflow actions.

## Amendment — 7 October 2026

The accepted decision is clarified to separate correspondence relationships from task/action assignment and to establish the notification boundary: **Email notifies; DMS records and controls.** No classification or recipient visibility rule above is superseded. Implementation status remains documentation-only.
