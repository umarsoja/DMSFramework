# ADR-019 — Document Workflow, Correspondence, and Related Records

- **Status:** Proposed for business and architecture review
- **Date:** 7 October 2026
- **Decision owners:** APGC business process owners, Records/Registry, DMS architecture owners
- **Scope:** Memo purpose, task/action vocabulary, authority-aware routing, correspondence recipients, related documents/business matters, incoming correspondence, notification boundary, and workflow scope control

## Context

The current DMS has `Document`, immutable `DocumentVersion` and version-owned `Attachment`, `WorkflowDefinition` scoped to `DocumentType`, ordered `WorkflowStep` with an `is_review` flag, `WorkflowInstance` targeting one `Document`, `WorkflowTask` assigned to a user and bound to a submitted version, immutable approve/return `WorkflowDecision`, `DocumentAccess`, classification, document audit, and organization `EmployeeAssignment` snapshots. `DocumentTypeHandler` is an explicit registry. Internal Memo To/Through/CC currently live as version content, while a separate selected reviewer creates a task.

Current submission selects the first configured review step; the current service handles one review decision and owner-driven finalization. It does not yet implement arbitrary action types, configured multi-step authority routing, explicit issuance/execution/response, related-document relationships, incoming correspondence registration, or a notification subsystem. ADR-017 remains the access authority and ADR-018 remains a separate proposed Business Request architecture.

## Decision

### 1. Memo purpose and workflow responsibility

A memo is a formal corporate correspondence/document record, not an approval process by definition. The communication purpose and the explicitly assigned responsibility determine the workflow. Supported purpose values should be controlled and configurable; the initial business vocabulary is For Information, For Review, For Advice, For Action, For Response, For Acknowledgement, For Approval, and For Filing/Record. Whether these belong in a controlled catalog or a constrained code set is an implementation detail, but labels/codes must be stable and reportable.

Workflow actions must likewise use a controlled vocabulary with stable codes, display labels, and active/configuration state. Candidate initial actions are REVIEW, APPROVE, ACKNOWLEDGE, ASSIGN, EXECUTE, RESPOND, ENDORSE, ISSUE, and CLOSE. This list is a proposal for business validation, not a final exhaustive enum. Add actions through configuration or a governed extension mechanism; avoid hard-coding positions or creating a workflow engine per document type.

Keep distinct evidence and semantics for authorship, review, approval, issuance, execution, acknowledgement, response, and closure. A decision is the recorded outcome of a task; it is not a synonym for every action. Existing immutable version-bound decision evidence remains applicable to review/approval tasks. Task/action outcomes and allowed transitions must be defined per action and workflow definition.

### 2. Authority-aware configurable routing

Workflow definitions express business routing using configured actor-resolution rules and task/action types, with assignment to a user or organizational unit resolved to an accountable user/assignment snapshot when a task is created. Organization hierarchy and `EmployeeAssignment` can inform actor resolution, but workflow code must not hard-code APGC positions such as ED, MD, Head of Sales, or department head.

Examples validating the model include Staff → Supervisor → Department Head → ED where required, and MD directive → ISSUE to recipient → EXECUTE → RESPOND/COMPLETE. An MD-originated document does not receive a review/approval task merely because it is a memo. If issuance requires review, the workflow explicitly configures (for example) MD Office → Legal REVIEW → MD ISSUE.

The workflow definition must state which actions are required, who resolves each assignee, sequencing/parallelism, completion conditions, and which statuses result. A routing rule may be selected by document type, purpose, issuing assignment, or other approved metadata; final selectors and delegation rules require business approval. Current `is_review` and actor-type mechanics are insufficient for this target and would need a separately reviewed runtime change.

### 3. Correspondence recipients are not task assignments

To, Through, and CC are correspondence relationships and remain distinct from action responsibility. A formal recipient may have no task. A CC recipient must not receive approval/execution authority by default. These relationships do not automatically create workflow tasks. An actionable responsibility must be represented by a `WorkflowTask`, created only from an explicitly configured or explicitly selected responsibility. Preserve To/Through/CC for display, correspondence access as permitted by ADR-017, and reporting. Do not infer a task from recipient presence or infer recipient roles from task assignment.

Each actionable `WorkflowTask` is the authoritative accountability record and identifies, directly or through an immutable task definition/snapshot:

- the accountable assignee (a user, or an organizational-unit assignment resolved to accountable user(s) under the approved routing policy);
- the controlled action type;
- the instruction/context for the assignee;
- current task status;
- assigned timestamp;
- due date when applicable;
- completion timestamp when completed; and
- response, decision, or evidence where applicable.

The task is the authoritative source for who is accountable, response tracking, completion, and response-time measurement. The memo body may explain a requested action in natural language, but it must not be parsed or relied upon as the structured task definition. Task instructions and outcomes are recorded on or explicitly linked to the DMS task record. A response or evidence required to complete a task is retained in the DMS and linked to that task; email is notification only and is not the response/completion record. Preserve ADR-017: classification and access rules continue to govern each task-linked document and evidence, and a task or correspondence relationship does not bypass those rules.

Example: To ED, Through Head of Sales, CC MD and Internal Control may correspond to ED REVIEW+APPROVE, Head of Sales ENDORSE, and MD/Internal Control INFORMATION only. Task assignment independently controls action authority.

### 4. Related documents and business matters

Support multiple documents linked to one business context without requiring each linked document to have its own workflow. Introduce a reusable, explicit document-to-document relationship with a controlled relationship purpose (for example originating correspondence, substantive document, supporting evidence, quotation, contract, revision/supersedes, or decision record), stable relational FKs, and auditability. Do not use EAV, `GenericForeignKey`, or a universal JSON object reference.

Treat “business matter” as a conceptual grouping represented by related-document relationships at this stage; do not introduce a Business Matter aggregate until approved requirements establish independent identity, lifecycle, authorization, or query needs that cannot be served by document relationships. Each document retains its own type, versions, classification, authorization, and audit. A relationship grants no access by itself. A linked proposal, quotation, or contract does not get a workflow solely because it is linked; create an independent workflow only when an independent business process genuinely requires it.

For a proposal, the originating memo and proposal can be related, with V1 returned and V2 submitted as preserved versions of the substantive proposal document. Each task/decision identifies the exact submitted `DocumentVersion` acted upon. Finance quotations, Legal documents, comments, and final decisions may join the same related-document context without automatic workflows. Where the proposal is an attachment/version content rather than a separate document, that choice must preserve version-bound review evidence and searchable identity.

### 5. Incoming Correspondence and direct management directives

Incoming Correspondence is a controlled DMS `DocumentType` representing an external document received by APGC. It is not a `BusinessRequest`, a bespoke subsystem, or a separate workflow engine. Registration stands on its own and does not require creating a Memo. The receiving office or EA registers the external record in DMS, captures its metadata, and stores the original in its DMS version/attachment record. The incoming correspondence remains the primary official record.

A registered incoming correspondence record may have zero or more subsequent management directives/actions. A user with the applicable directive-issuance capability may issue a directive directly against the incoming correspondence. A directive does not necessarily require a new `Document`; a Memo is created and related through the existing RelatedDocument foundation only when formal internal correspondence is required. Do not make a Memo a mandatory intermediary between incoming correspondence and action.

Keep these concepts distinct; do not collapse them into one workflow status or object:

- **Registration:** recording that external correspondence was received, including its original record and required metadata.
- **Management Directive:** an auditable management instruction by an authorized issuer against a target DMS `Document`. It identifies issuer, target document, directive/action type, instruction, and timestamp, with optional assignee and due date. A Management Directive is not necessarily a `Document`, Memo, approval, or `WorkflowTask`; it may exist without an individual task.
- **WorkflowTask:** accountable execution of an assigned action. When a directive creates an actionable responsibility, it produces or is associated with a `WorkflowTask` that records assignee, action type, instruction/context, due date where applicable, status, completion, response, and evidence. The directive records the management instruction; the task records individual accountability.
- **Approval/Decision:** a formal decision by an authorized decision-maker. It is distinct from issuing a directive and from completing a task.
- **Memo:** formal internal correspondence, created only when required. It is optional when acting on incoming correspondence.

Registration is separate from directive issuance and task assignment. A directive may have no task, for example an auditable “Noted. Proceed with the engagement” instruction without an individually assigned responsibility. Where the directive creates actionable work, it produces or is associated with one or more `WorkflowTask` records. “Review the proposed contract and advise” creates a task for the assigned officer; “Approve the proposal” is a formal approval action. A management decision such as “Proceed with the contract” must retain its actual directive/decision meaning rather than being forced into APPROVE. Do not assume every directive is an approval or every directive creates a task.

To, Through, CC, and follow-up remain correspondence relationships. They do not create tasks by themselves. In particular, an EA copied for follow-up is not thereby accountable for the substantive task; task ownership belongs to the explicitly assigned `WorkflowTask`. Correspondence visibility does not imply substantive task ownership or decision authority. A directive must not grant access to the correspondence or linked evidence beyond ADR-017.

The type should support searchable metadata for sender/organization, sender reference, date on document, date received, subject, correspondence type, receiving office, priority, classification, original document, and supporting documents. The original and supporting files remain DMS document versions/attachments; relevant structured metadata must be queryable rather than hidden solely in opaque payload. The original reference is generated for the Incoming Correspondence document. Exact metadata requiredness, sender identity/reference strategy, priority and correspondence-type vocabularies, retention, and external organization catalog remain to be decided before implementation.

Use the existing configurable `WorkflowActionType` catalog; do not create a second correspondence-specific action catalog. Treat `ADVISE` conceptually as a first-class action distinct from `REVIEW`: REVIEW examines/evaluates a document or matter; ADVISE provides a professional or management recommendation based on review. If implementation confirms it can be added safely, ADVISE uses the existing WorkflowActionType mechanism. Do not implement that extension in this documentation phase.

Separate the authority and audit events for document authoring, correspondence registration, management directive issuance, task assignment, task completion, and business/document approval. Conceptual capabilities include `correspondence.register`, `correspondence.route`, `directive.issue`, `workflow.task.assign`, `workflow.task.complete`, and `document.approve`. These are capability examples, not an implementation mandate for specific permission names. Users/groups receive capabilities according to APGC governance; position titles (MD, ED, Head of Department, EA) are not authorization rules. An EA may register and route correspondence and follow up where authorized; management users may review and issue directives where authorized; department officers may execute assigned tasks and provide response/evidence; CC/follow-up participants may have visibility or notification under existing rules but do not thereby own the task.

Issuing a directive is a material event recorded through the existing DMS audit mechanism. Directive and task lifecycle audit evidence together must support reconstructing who registered the correspondence, who issued what instruction and when, against which document, who was assigned, what action and due date applied, task response/completion/evidence, and any subsequent decision. Email remains notification only and points assignees to the DMS record where correspondence, directive, task, due date, evidence, response, and decisions are controlled.

Neither a directive nor a task grants access to the target document. Users must have normal authorization to view the target document before viewing directive/task context, subject to the existing workflow access rules. ADR-017 classification and authorization remain authoritative.

Related documents may connect the incoming record to subsequent Memos, proposals, contracts, responses, recommendations, and other DMS documents. Each document retains its own identity, versions, classification, access, and audit. Relationships do not grant access. Do not introduce a Business Matter aggregate, `GenericForeignKey`, EAV, or a correspondence-specific workflow engine.

### 6. Notifications and authoritative record

Email notifies; DMS records and controls. An actionable email identifies that an item requires attention and links the user to the DMS. The document, classification, authoritative version, task (including assignment, instruction/context, status and applicable due/completion times), response/decision/evidence, and audit history are authoritative only in the DMS. Do not treat an email attachment, mailbox response, or mailbox copy as the authoritative document or task-response channel. Notification delivery and failures may be recorded operationally, but must not replace DMS task, response, completion, or audit evidence.

### 7. Scope control principle

**The DMS should capture and control the business work APGC already performs; it should not create a separate workflow for every document, attachment, departmental contribution, or business scenario.** Before adding a workflow or domain object, determine whether existing `Document`, `DocumentVersion`, related-document relationship, recipient, `Task/Action`, `Workflow`, `Classification`, authorization, audit, and notification primitives are sufficient. If they are sufficient, do not create a new subsystem. Do not collapse Business Requests into Documents: Business Requests remain a separate domain as proposed in ADR-018; Cash Advance and Cash Retirement remain future Business Request use cases and are outside this decision.

## Consequences

- Memo purpose, recipient, and task responsibility have separate semantics.
- Every actionable responsibility has a `WorkflowTask` that is authoritative for accountability, response tracking, completion, and response-time measurement; memo prose and email are not structured task/response records.
- Authority-aware routing is configured and action-oriented; organizational positions are data, not workflow-engine constants.
- ADR-017 classification and access rules continue to govern each related document; relationships and recipients do not grant action authority.
- Version-bound workflow evidence remains required for each document version reviewed or approved.
- Related-document relationships support a business matter without introducing a premature aggregate or per-attachment workflow.
- Incoming Correspondence is a controlled Document Type and primary external record; registration does not require a Memo.
- Authorized management can direct actionable work against incoming correspondence directly, using WorkflowTask for accountability; a Memo remains optional.
- A Management Directive records an authorized instruction and may exist without a WorkflowTask; a task is associated only when individual actionable responsibility is assigned.
- Registration, Management Directive, WorkflowTask, Approval/Decision, and Memo remain separate concepts with distinct lifecycles and authority.
- Directive issuance and task lifecycle events remain auditable through the existing DMS audit mechanism.
- CC/follow-up does not create task ownership, and directive/relationship links do not change ADR-017 access.
- The present one-review-step runtime conflicts with the proposed model. This ADR authorizes no code or schema changes; runtime expansion requires approval and a migration/compatibility plan.

## Questions requiring business approval

1. Approve the purpose vocabulary and whether it is one purpose per document or supports multiple purposes.
2. Approve the initial action vocabulary, action outcomes, and who may extend it.
3. Define routing selectors, escalation/delegation rules, parallel tasks, and workflow completion conditions.
4. Confirm whether task assignees may be organizational units, named users, or both, and how a unit task is accepted/resolved into user accountability.
5. Decide which users/units may issue, execute, close, or record responses and what evidence each action requires.
6. Confirm the canonical document-to-document relationship types and whether proposal content is its own Document or a memo version attachment in the initial use case.
7. Define the capability/permission matrix for correspondence registration/routing, directive issuance, task assignment/completion, and document approval; determine how APGC governance maps users/groups to those capabilities.
8. Confirm implementation semantics and allowed outcomes/evidence for each directive/task action, including how “Proceed with the contract” is recorded separately from approval.
9. Confirm incoming correspondence metadata requiredness, sender identity/catalog strategy, priority and correspondence-type vocabularies, retention, and receiving-office responsibilities.
10. Define how a Management Directive is represented and audited without requiring a new model in this architecture phase, including directives with no task and directives associated with multiple tasks.
11. Confirm notification content rules and delivery audit/retention needs.

## Implementation boundary

This is a documentation proposal only. The Phase 4A clarification records the intended architecture but does not authorize feature implementation. It authorizes no models, migrations, services, views, templates, workflow changes, email implementation, permissions, or tests. Before implementation, resolve capability assignment, Management Directive representation/audit, action outcomes/evidence, and incoming metadata/policy choices. Implement the approved Incoming Correspondence type/register and direct-action integration on the existing Document, RelatedDocument, WorkflowTask, classification, access, and audit foundations; do not require a Memo and do not create a separate correspondence workflow engine. Preserve exact-version task evidence and ADR-017 access, and validate existing workflows/records during migration planning.

## Architectural clarification amendment — 7 October 2026

**Approved clarification:** To, Through, and CC are correspondence relationships and never create workflow tasks by themselves. Every actionable responsibility is represented by a `WorkflowTask` carrying the assignee, action type, instruction/context, status, assigned timestamp, due date where applicable, completion timestamp, and response/decision/evidence where applicable. The task is authoritative for accountability, response tracking, completion, and response-time measurement. The memo body is not parsed or relied upon as structured task data. Email only notifies; responses and completion evidence remain in the DMS. ADR-017's classification and access decisions remain unchanged and apply to every document and linked evidence item.

### Related-document implementation clarification — 7 October 2026

Related Documents are separate DMS `Document` aggregate roots linked by an explicit, directed relationship record. They are not `Attachment` rows: attachments remain files owned by a specific immutable `DocumentVersion`. Relationships are document-level; exact-version evidence remains with workflow decisions and task evidence. A relationship does not grant access to either endpoint. Each document keeps its own classification, access grants, versions, workflow, and audit. Relationship traversal must filter each endpoint through ADR-017 access policy. This provides a lightweight business/document thread without a Business Matter aggregate, and does not change BusinessRequest as a separate aggregate/domain.

The initial directed vocabulary is RESPONSE_TO (source responds to target), REVISION_OF (source revises target), SUPPORTS (source supports target), REFERENCES (source cites target), and RELATED_TO (source is related to target). Duplicate same-direction typed pairs and self-links are rejected. This implementation clarification does not implement Incoming Correspondence or other domain-specific workflows.

### Incoming Correspondence architecture clarification — Phase 4A

**Architecture decision:** Incoming Correspondence is a controlled DMS Document Type for the external record received by APGC. Registration does not require creation of a Memo. A user with the applicable capability may issue a Management Directive directly against the registered correspondence. A Memo is optional and is created only when formal internal correspondence is required; it can then be linked using RelatedDocument. The incoming correspondence remains the primary record.

Registration, Management Directive, WorkflowTask, task completion, Approval/Decision, and Memo are separate concepts and authority-bearing events. A directive may be auditable without a task. When an individual responsibility is assigned, WorkflowTask is authoritative for its assignee, action, response, completion, due date, and evidence; the directive remains the record of the management instruction. To/Through/CC and EA follow-up do not automatically create tasks or confer substantive task ownership. Directive/task context and related-document links grant no access beyond ADR-017. Email only notifies and directs users to the DMS record. Capability-based authorization governs registration, routing, directive issuance, task assignment/completion, and approval; organizational position titles are not rules. No Business Matter aggregate, BusinessRequest, GenericForeignKey, EAV, second action catalog, or correspondence-specific workflow engine is introduced.

This clarification describes the intended architecture only; implementation remains blocked on capability assignment, directive representation/audit details, incoming metadata/policy, and action outcome/evidence decisions above. ADVISE is conceptually a first-class action distinct from REVIEW and should use the existing configurable WorkflowActionType mechanism if implementation confirms safe extension; do not force advisory directives into APPROVE.

This amendment clarifies sections 3 and 6 and does not create a new ADR or authorize implementation. Where the earlier phrase “recipient relationships determine visibility” in ADR-017 is applied, it continues to mean classification-limited view access only; it does not create tasks or action authority.
