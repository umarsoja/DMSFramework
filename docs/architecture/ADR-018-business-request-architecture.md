# ADR-018 — Business Request Architecture, Cash Advance, and Cash Retirement

- **Status:** Proposed for architecture review
- **Date:** 5 October 2026
- **Decision owners:** APGC business, Finance, Internal Control, and DMS architecture owners
- **Scope:** Business Request domain boundary, workflow targets, Cash Advance, Cash Retirement, authorization, audit, and ERP/Finance integration

## Context

The DMS currently models a `Document` as the aggregate root for a typed document, its immutable `DocumentVersion` records, version-owned attachments, classification and access grants. A `WorkflowInstance` currently targets one `Document`; a new `WorkflowTask` is bound to the exact `DocumentVersion` submitted for review; an immutable `WorkflowDecision` belongs to a task. DMS `AuditEvent` records document events and may identify the relevant version.

Document types use a controlled `DocumentType` table and an explicit handler registry (`DocumentTypeHandler`). This is useful precedent for controlled type registration, but its JSON version content is document payload. It is not an appropriate general data store for financial requests.

The existing organization domain supplies `EmployeeProfile`, current and historical `EmployeeAssignment`, departments, positions, and supervisors. Assignment rows retain historical organizational snapshots. ADR-017 defines document classification and recipient access; that policy remains authoritative for every linked supporting document.

Document-level correspondence decisions are recorded separately in ADR-019 and ADR-020. This ADR does not absorb memo purpose, document numbering, incoming correspondence, or general related-document workflow into the Business Request domain. A Business Request may link existing DMS Documents as evidence; the link is not a substitute for their classification/access rules and does not make a linked document a Business Request.

The current workflow schema has ordered steps, but the current document submission/decision services select the first review step and complete the workflow after that decision. Thus, the current runtime does not yet execute the multi-step Supervisor → Internal Control → ED → Finance processes described here. Supporting those processes requires a separately scoped workflow-runtime change.

There is no existing Business Request model or ERP/Finance integration boundary in the inspected application. This ADR proposes a distinct business domain and records assumptions that need business approval before implementation.

## Decision

### 1. Introduce Business Request as a separate aggregate

Business Requests are structured, actionable processes alongside Documents. A request is not a document type and does not replace the documents that evidence or originate it.

The Business Request domain owns request identity, requester and organizational context, request lifecycle, typed request data and rules, links to originating/supporting documents, request-specific audit events, and links to subsequent business processes. The DMS remains the authority for documents, versions, attachments, classification, document access, document audit, workflow evidence associated with documents, and document retrieval.

An implementation should use one Business Request domain/app and a common `BusinessRequest` envelope. It should not create a separate Django app for each small request type and must not put unrelated request fields into one universal JSON payload.

### 2. Controlled request types with typed domain data

Add a controlled `BusinessRequestType` catalog (stable code, display name, active/configuration state) and an explicit request-type handler registry for type-specific validation and application behavior. The registry controls which request types the application accepts; it is not a substitute for relational domain fields.

Cash Advance and Cash Retirement must have dedicated typed domain records and fields. Use one-to-one type-specific tables related to the common request envelope, or equivalent explicit typed models. Important amounts, dates, outcomes, departments, originating references, and status facts must be strongly typed and queryable. Do not use EAV, `GenericForeignKey`, or JSON-only storage for authoritative business fields.

A request type warrants its own typed domain model/fields when it has one or more of these characteristics:

1. Required fields with type-specific validation or cross-field invariants.
2. Values used in filtering, reporting, reconciliation, authorization, integration, or uniqueness constraints.
3. A distinct lifecycle, approval consequence, financial/operational effect, or relationship to another domain object.
4. A need for stable, auditable snapshots of what was submitted or approved.

A handler may provide normalization, validation orchestration, and type-specific operations. It must not hide query-critical facts inside opaque payloads. Shared envelope fields should be added only when they have the same meaning for all request types.

### 3. Common Business Request envelope

The common envelope should contain:

| Concept | Decision |
|---|---|
| Request identity | Required unique `request_number`; generation/format and numbering authority need business approval. Enforce uniqueness and generate safely under concurrent creation. |
| Request type | Required FK to the controlled `BusinessRequestType`; immutable once a request is created. |
| Requester | Required FK to the authenticated user/employee that owns the request. |
| Organizational context | Required reference to the requester's `EmployeeAssignment` at creation/submission, protected from deletion; department/position context should be derived from that immutable assignment snapshot unless a specific type requires its own separately justified snapshot. |
| Lifecycle | A common lifecycle state for broad request progress, not a combined list of every type's finance and approval statuses. Suggested vocabulary: Draft, Submitted, Returned, In progress, Completed, Rejected, Cancelled. Exact transitions and cancellation authority require approval. |
| Timestamps | `created_at`, `updated_at`, and lifecycle timestamps such as `submitted_at` and `completed_at` where the transition occurred. Record event history rather than overwriting the only evidence of prior transitions. |
| Priority | Optional only if APGC defines a common priority policy and authorized actors may set it; otherwise omit. |
| Workflow reference | Expose the active/current workflow relation through the target relationship; avoid a second independently mutable `current_task` pointer. Current task is derivable from pending workflow tasks. |

Type-specific process stages (for example, Paid or Refund Required) belong to the typed request process and its authoritative events, not to a giant shared status enum. Workflow task status is not a substitute for request lifecycle or payment/reconciliation state.

### 4. Explicit Workflow targets

The workflow engine must target exactly one of `Document` or `BusinessRequest`. Use explicit relational fields, never a `GenericForeignKey`, `ContentType`, untyped universal object, or JSON target reference.

The proposed relational design is:

- `WorkflowDefinition` identifies its target kind (`DOCUMENT` or `BUSINESS_REQUEST`) and is bound to exactly one configured `DocumentType` or `BusinessRequestType`.
- `WorkflowInstance` has nullable explicit `document` and `business_request` FKs with a database check requiring exactly one to be non-null. Add uniqueness for the selected target so a target has one workflow instance under the current one-instance-per-object lifecycle rule.
- `WorkflowTask` remains related to its `WorkflowInstance`; decisions remain related to tasks. A task therefore has an explicit target through its instance.
- A document task has an immutable FK to exactly one submitted `DocumentVersion`, preserving the existing version-binding invariant.
- A Business Request task has an immutable FK to exactly one typed `BusinessRequestSubmission` (an immutable submitted-state revision). The revision identifies its BusinessRequest and cycle. Type-specific immutable submission details live in typed child snapshot tables, not a generic payload. A task's request revision must belong to its instance's BusinessRequest.
- A task has exactly one of `submitted_version` or `submitted_request_submission`; the binding must match the target type of its workflow instance. Enforce local exactly-one constraints in the database and cross-record consistency in transactional workflow services/model validation.

This keeps a decision bound to the exact data the actor reviewed. It also prevents a later edit from silently changing the data represented by an earlier approval. For a returned request, a new immutable submission revision is created for the next cycle; earlier tasks, decisions, and revisions remain unchanged.

The workflow definition and task services must validate target/type compatibility and allowed transitions. The current document-specific `WorkflowDefinition.document_type` and `WorkflowInstance.document` relationships would require an additive migration path; this ADR does not authorize a migration or schema implementation.

### 5. Cash Advance

Cash Advance is a Business Request with a typed `CashAdvance` detail model. Its common envelope owns request number, requester, organizational assignment context, broad lifecycle, and timestamps. The type-specific model should include:

- unique Cash Advance number if distinct from the common request number is operationally required; avoid maintaining two identifiers with identical purpose;
- required originating approved Internal Memo reference, and the exact approved `DocumentVersion` where available, with a consistency rule that the version belongs to the memo;
- purpose/justification;
- requested amount as fixed-precision decimal and explicit currency code;
- typed supporting-evidence links;
- DMS-owned approval evidence through the Business Request workflow and immutable submission revision;
- Finance-originated payment reference(s), a minimal payment-status projection, and source/provenance timestamps only to the extent needed by DMS processing and reconciliation.

The originating Memo remains a `Document`, follows ADR-017 and its own workflow, and is linked rather than copied into the Cash Advance. Approval/instruction of the Memo does not itself create or approve a Cash Advance. Staff explicitly create a Cash Advance that references the approved Memo/version.

The Cash Advance workflow target is the Business Request, not its originating Memo. The proposed process is: staff creates/submits → Supervisor review → Internal Control vetting → ED approval → Finance payment → Paid. The workflow stages are proposals; the runtime must support multiple sequential tasks before this process can be implemented. `Paid` must be driven by a Finance-authoritative payment confirmation, not inferred from ED approval.

Do not duplicate Finance's authoritative ledger/payment record. The DMS may retain stable external transaction identifiers, a necessary status projection, source timestamps, and evidence links. Amount actually paid and payment granularity remain Finance-authoritative; whether the DMS needs a minimal amount snapshot for reconciliation must be confirmed with Finance.

### 6. Cash Retirement

Cash Retirement is a separate typed Business Request, not a document status. It references one specific Cash Advance. The proposed relationship is:

```text
CashAdvance 1 ───── 0..1 CashRetirement
```

Use a protected one-to-one relationship. Do not create repeat retirement cycles by default. Whether a formally rejected or cancelled retirement permits a replacement requires an explicit business rule before implementation.

Cash Retirement should include a unique request number through the common envelope, a protected reference to the Cash Advance, typed retirement/expenditure lines (description, date, amount, and any approved category fields), and DMS evidence links to receipts/supporting documents. Eligible expenditure totals should be derived from validated lines rather than duplicated as independently editable totals.

Its proposed process is: staff initiates and submits against a Paid Cash Advance → Supervisor review → Internal Control vetting → ED approval → Finance reconciliation → Fully Accounted, Refund Required, or Exception → Closed. Business and Finance must confirm whether the first three review stages apply identically to retirement and which role may resolve each exception.

The reconciliation equation is:

```text
Amount Advanced = Eligible Retired Expenditure + Refund + Approved Adjustment
```

Here, **Amount Advanced** means the Finance-confirmed amount disbursed; **Eligible Retired Expenditure** is calculated from typed retirement lines under Finance-approved eligibility rules; **Refund** is a Finance-confirmed amount/reference; and **Approved Adjustment** is included only if APGC policy explicitly authorizes it and identifies its approval authority. DMS may calculate a proposed balance, but Finance owns final reconciliation, refund settlement, and accounting records. No unapproved adjustment rule or expense category is implied by this ADR.

### 7. Lifecycle separation

Use the shared envelope only for broad request lifecycle states. Keep Cash Advance payment progression and Cash Retirement reconciliation outcomes type-specific and attributable to their authoritative source.

| Process | Proposed semantic progression (not a final persisted enum) |
|---|---|
| Cash Advance | Draft → Submitted → Supervisor review → Internal Control review → ED decision → Approved/Rejected/Returned → Pending Finance payment → Paid → Retirement pending → Retired/Closed |
| Cash Retirement | Draft → Submitted → Supervisor review → Internal Control review → ED decision → Pending Finance reconciliation → Fully Accounted / Refund Required / Exception → Closed |

Workflow task/decision events establish approval stages. Finance integration/processing events establish payment and reconciliation facts. A request must not be shown as Paid merely because its workflow is approved. Exact labels, transition ownership, and whether Returned resumes the same request or creates a new cycle require business approval; resubmission must create a new immutable submission revision either way.

### 8. Documents and evidence

Documents remain the single evidence/file layer. The originating Memo and every supporting document, receipt, approval record, or Finance evidence item are DMS `Document` records with `DocumentVersion` and version-owned `Attachment` rows. Link them to the request using an explicit association such as `BusinessRequestDocument` with a typed relationship purpose. Do not store a second physical copy on the Business Request and do not make attachments global to a request.

The association does not grant access by itself. Every linked document retains its own classification, recipient, workflow, and explicit-grant rules under ADR-017. A request viewer must not gain a bypass to Restricted evidence merely through the request-document link. Evidence may be linked to the request revision for submitted-state traceability; the exact evidence-link snapshot and replacement rules need to be enforced by the request submission service.

This request-to-document association is specific to Business Request evidence. For document-to-document business context (such as a memo, proposal, quotation, contract, or supporting correspondence), follow ADR-019's proposed reusable related-document mechanism. Do not introduce a Business Matter aggregate solely to group those records; it remains a conceptual relationship unless independently justified and approved.

Approval evidence is the workflow decision/task and associated submission revision. Payment/reference evidence is either a linked DMS document or a Finance external identifier, according to Finance's retention requirements; do not represent one artifact in both places as if both were authoritative.

### 9. Authorization boundary

Request access and action permissions are distinct:

| Actor | Request view | Request edits/submission | Review/approval | Finance processing/reconciliation | Close |
|---|---|---|---|---|---|
| Requester | Own requests, subject to policy | Own Draft/Returned request only | None from requester status | None from requester status | None unless a defined cancellation/closure rule grants it |
| Supervisor | Requests within explicitly defined organizational/workflow scope | No requester edits | Only when assigned the workflow task | None from supervisor status | None by default |
| Internal Control | Requests in assigned/control scope | No requester edits | Only when assigned the vetting task | No payment authority from review assignment | Only if a separately approved reconciliation/closure task is assigned |
| ED / authorized approver | Requests in assigned approval scope | No requester edits | Only when assigned an approval task | No Finance processing authority | None by default |
| Finance | Requests/evidence required for Finance duties | No requester edits | No workflow approval unless separately assigned | Explicitly granted payment/reconciliation actions | Close only under approved process rules |
| Explicit viewer | Request view where specifically granted | None | None | None | None |

The matrix defines no automatic authority from department membership or from being a document To/Through/CC recipient. The implementation must authorize each action server-side. Workflow assignment grants only the action represented by that assigned task. A review permission does not grant payment, reconciliation, edit, or close authority. Finance processing authority must be separately defined and auditable.

Linked documents are separately authorized by DMS policy. Request authorization must not weaken classification restrictions, document downloads, or direct-route authorization.

### 10. Audit and integration ownership

Retain existing DMS `AuditEvent` for document creation, version, access, PDF, attachment, and lifecycle evidence. Retain workflow tasks, decisions, and reassignments as workflow-owned evidence. Do not copy those events into a second request log.

Business Request needs a request-targeted immutable audit event model (for example, `BusinessRequestAuditEvent`) for request creation, material typed-data changes, submission/revision, request linkage changes, Finance status/reference receipt, refund/reconciliation facts, cancellation/closure, and integration delivery/failure. Each event should identify request, actor or integration identity, actor assignment where available, action, timestamp, relevant immutable submission revision, and stable external reference/context. Do not duplicate full document or workflow event bodies; link to their IDs/objects where needed.

Reassignment remains a workflow event. Rejection/return is represented by the workflow decision and corresponding request lifecycle event, with one authoritative decision record. Material post-approval changes require a new authorized process and audit trail; they must not mutate an approved submission snapshot.

ERP/Finance/HR remain authoritative for posted financial transactions, payments, refunds, ledgers, leave balances, inventory, purchase orders, and other enterprise records. Integration sends only approved request references/status, relevant typed request data, and authorized evidence references; it receives stable external IDs and status/confirmation needed for DMS visibility and reconciliation. It must not create a second authoritative ledger in DMS. Integration operations need idempotency, correlation IDs, retry/failure audit, and reconciliation controls before implementation.

## Consequences

- Documents and Business Requests remain separate aggregate roots with explicit relationships.
- Workflow can serve both target classes using controlled target types and explicit FKs without polymorphic content types.
- Document approvals remain bound to immutable `DocumentVersion`; Business Request approvals will be bound to immutable typed submission revisions.
- Cash Advance and Cash Retirement become queryable typed processes without turning every request into a separate application or hiding business fields in JSON.
- Workflow runtime expansion is required: the current implementation executes one review step, while these flows require multiple sequential reviewers and Finance handoffs.
- Authorization, request audit, request evidence linkage, and external Finance status projection require deliberate implementation; this ADR introduces none of them into the running application.
- A staged, additive migration strategy is required if this proposal is accepted. Existing Document workflows must remain valid throughout rollout; no destructive conversion or fabricated historical request target is acceptable.

## Questions requiring business approval before implementation

1. What is the authoritative request-number format, numbering scope, and allocator (including year rollover and concurrent submissions)?
2. What currency/currencies are permitted, and can one Cash Advance or retirement contain multiple currencies?
3. Can Finance disburse an advance in partial or multiple payments? What exact paid amount is the reconciliation basis?
4. Which expense classes are eligible, what evidence is mandatory, and who makes the final eligibility determination?
5. Is an adjustment permitted in the reconciliation equation? If so, what types, limits, and approval authority apply?
6. Who can record/confirm refunds, what evidence/reference is required, and what constitutes Finance reconciliation and closure?
7. Are Supervisor → Internal Control → ED required for both Cash Advance and Cash Retirement, and who may return/reject at each stage?
8. What happens to the one-to-one retirement relationship after a retirement is rejected, cancelled, or reopened? This ADR assumes no second retirement cycle until approved otherwise.
9. Which data/evidence is sent to ERP/Finance, which system creates the external transaction, and which identifiers/statuses are returned?
10. What request and linked-evidence visibility is allowed to supervisors, Internal Control, ED, Finance, delegates, and auditors beyond workflow assignment?
11. Confirm that the proposed explicit request-to-document association plus ADR-019's document-to-document relationship is sufficient without a shared Business Matter aggregate; any separate aggregate requires its own business identity, lifecycle, authorization, or reporting justification.

## Implementation boundary

This ADR is a proposed architecture decision only. It creates no models, migrations, views, forms, templates, workflow changes, permissions, UI, or integration configuration. Implementation should proceed only after business approval of the open policy points and should be split into reviewed increments: explicit multi-target workflow foundation and migration; request envelope/type registry/audit/evidence links; typed Cash Advance; typed Cash Retirement and Finance reconciliation integration. This sequence is a recommendation, not authorization to implement. Document workflow changes for correspondence remain within ADR-019 and must not make Business Requests part of the Document aggregate.
