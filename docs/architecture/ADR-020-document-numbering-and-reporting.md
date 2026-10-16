# ADR-020 — Document Business Numbering and Enterprise Reporting

- **Status:** Proposed for business and architecture review
- **Date:** 7 October 2026
- **Decision owners:** APGC business, Records/Registry, Finance/department representatives, DMS architecture owners
- **Scope:** Official document identifiers, sequence allocation, issuing office, number allocation timing, and enterprise reporting

## Context

The current `Document.reference` is unique and generated as `APGC/IM/{year}/{document.pk:06d}` in `create_memo`. This is effectively one database-primary-key sequence for all memos and is assigned at creation, so drafts consume numbers. It is not scoped to an issuing office and does not provide a concurrency-safe business sequence independent of database row identity. Document primary keys and human-readable business identifiers serve different purposes.

## Decision

### Business identifier components

An official business identifier should be derived from explicit document metadata: APGC prefix/issuer, issuing organizational unit, numbering period (normally year), sequence, and optionally document type if approved. Candidate format: `APGC/{OFFICE}/{YEAR}/{SEQUENCE}`, for example `APGC/MD/2026/000001`, `APGC/ED/2026/000001`, `APGC/FIN/2026/000001`, and `APGC/IT/2026/000001`. Whether document type appears as an additional component (and its position/code) remains a business decision. The final format, width/padding, reissue/cancellation rules, and treatment of transfers or renamed offices must be approved before implementation.

The identifier is a human-readable business key, not a database primary key. Technical identity remains an independent stable key. Store issuing unit, type, period/year, and sequence as explicit structured metadata so filtering, uniqueness, audit, and reporting do not depend on parsing the rendered string. The formatted reference may be stored as a unique immutable snapshot for display and external correspondence.

### Atomic scoped allocation

Sequence allocation must be atomic under concurrent document creation. The allocation scope is at least issuing unit + numbering period/year, and also document type if the final approved format includes type. Use a database-backed sequence/allocation record or equivalent transactional allocator with a uniqueness constraint on the complete scope and sequence, plus locking/atomic increment or retry on uniqueness conflict. The allocator must guarantee that two simultaneous submissions never receive the same official number.

Do not implement `SELECT MAX(number) + 1`, application-level read-last-and-increment, or numbering from the document primary key. Allocation and the transition that makes a document officially numbered must be one transactionally safe operation. Failure to allocate must fail the official transition without leaving an issued document with a duplicate or partial identifier.

### Allocation timing

Drafts should not normally consume official issued numbers. Allocate the official number when the document reaches the approved business state for numbering, provisionally submission or issuance depending on document type. The policy may vary by document class: incoming correspondence may need a registry number at registration, while an outgoing memo may receive its official number at issuance. The exact state per type, returned/revised documents, abandoned drafts, cancelled issued records, and voided numbers require business approval. Do not reuse an issued number after cancellation; retain its audit/status evidence if that policy is approved.

### Reporting independence

Enterprise reporting aggregates explicit document metadata and relationships: issuing unit, document type, official-number period, lifecycle state, and task/action state. Reports must be able to count enterprise totals and break them down by office, period, and type, as well as outstanding/completed actions and correspondence volume. A human-readable reference string is for identification, not a reporting source. Do not derive office, year, or type by parsing the display number.

## Consequences

- Numbering changes from the current database-PK-derived global memo reference and requires a data migration/backfill policy for existing records.
- The allocator and official-state transition need a concurrency-safe transaction and a database uniqueness guarantee.
- Issuing-office identity must be explicit and historically stable for existing numbered records even if the organization directory later changes.
- Reporting remains enterprise-wide while numbering is scoped by office.
- ADR-018's proposed `request_number` allocator remains a separate Business Request decision and must not be implicitly replaced by document numbering.

## Questions requiring business approval

1. What is the final reference format, including whether it includes document type, sequence width, office aliases, and any separator rules?
2. What is the authoritative issuing unit, and can a document have only one issuing unit?
3. Which document types receive official numbers at registration, submission, approval, or issuance?
4. Do revisions/resubmissions keep the same identifier? How are cancelled/withdrawn/void numbers represented, and may any number ever be reused?
5. Which timezone/calendar defines the numbering year, and how are late registration or back-dated incoming records handled?
6. What are legacy reference backfill/display and external citation rules?

## Implementation boundary

This is a documentation proposal only. It authorizes no model, allocator, migration, service, UI, or reporting changes. After approval, define legacy data mapping, implement a transactionally safe scoped allocator with database constraints, transition number allocation at the approved state, and migrate/report without changing technical primary keys.
