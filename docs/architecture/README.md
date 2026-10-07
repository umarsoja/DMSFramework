# APGC DMS Architecture Decisions

Architecture decision records (ADRs) are numbered sequentially. Status indicates whether the decision is accepted or remains for review; a proposed ADR does not authorize implementation.

## Decision records

- [ADR-017 — Document Classification & Recipient Access Policy](authorization/ADR-017-document-classification-recipient-access.md) — Accepted 4 October 2026; clarified by amendment 7 October 2026.
- [ADR-018 — Business Request Architecture, Cash Advance, and Cash Retirement](ADR-018-business-request-architecture.md) — Proposed for architecture review, dated 5 October 2026.
- [ADR-019 — Document Workflow, Correspondence, and Related Records](ADR-019-document-workflow-and-correspondence.md) — Proposed for business and architecture review, dated 7 October 2026.
- [ADR-020 — Document Business Numbering and Enterprise Reporting](ADR-020-document-numbering-and-reporting.md) — Proposed for business and architecture review, dated 7 October 2026.

ADR-017 governs classification and access for every DMS document. ADR-018 concerns the separate Business Request domain. ADR-019 concerns document workflow, correspondence, and document-level related-document relationships. Related Documents link separate DMS Documents; files remain version-owned Attachments, and relationships do not grant access or create a Business Matter aggregate. ADR-020 concerns document numbering and reporting. Proposed decisions require the listed business approvals before implementation.
