# Internal Memo recipient UX recommendations

Current screen analysis and future recommendations only. No form, model, route, or UI behavior was changed.

## Current behavior

The Internal Memo form has free-text single-line fields:

- `To`: required string, up to 500 characters.
- `Through`: optional string, up to 500 characters.
- `CC`: optional string, up to 500 characters.

The form does not resolve recipients against EmployeeProfile, Department, Position, or email. The Internal Memo handler trims these strings and stores them as part of the version payload. CC is not a structured multi-recipient list. A separate `Reviewer` selector uses active eligible employee users; that selected reviewer creates the workflow assignment. To/Through/CC values do not themselves trigger workflow routing.

## Recommended recipient interaction

For each recipient field, provide **Search → Select → Confirm**:

1. Search by employee name, staff ID, department, or position.
2. Show a disambiguating result with name, position, department, and email where policy permits.
3. Select a person or a department recipient only if department distribution behavior has been defined.
4. Confirm the selected recipient before adding it to the memo.
5. Display recipients as removable chips with a clear label and accessible keyboard interaction.

Support multiple CC recipients. To and Through cardinality and whether either accepts a department are business decisions that should be settled before implementation. Avoid turning a department selection into silent access for every member: define whether it expands to a distribution list, creates explicit recipients, or represents a textual addressee only.

## Validation and routing rules to decide

- Validate selected employees against the authoritative employee directory and show inactive/separated status rather than accepting stale names silently.
- Confirm whether recipients must have active DMS accounts. A memo addressee and a workflow reviewer may have different eligibility rules.
- Prevent accidental duplicate CCs and conflicting duplicates across To/Through/CC; define whether the same person may occupy more than one field.
- Confirm whether external recipients are allowed. If so, create an explicit separate external-recipient path and policy rather than allowing arbitrary text to masquerade as a directory user.
- Treat reviewer selection as a workflow action distinct from recipient selection. The current selected reviewer is a single task assignee; do not route based on To, Through, Position, or DMS role unless a later approved workflow rule says so.
- Preserve the recipient representation used when the Memo version was submitted so later directory changes do not rewrite historical PDFs. The stored version payload should retain a display snapshot (name and organizational label) alongside stable selected identity where design permits.
- Apply classification and explicit access policy independently of being listed as a recipient. Being named as To/CC must not silently grant document access unless an approved policy expressly says so.

## Suggested confirmation panel

Before saving or submitting, show:

| Field | Display |
|---|---|
| To | Selected person/department name; position and department; email if permitted |
| Through | Selected person(s) and organizational context, if cardinality is approved |
| CC | Every selected recipient, removable before submission |
| Reviewer | Separate workflow assignee and review responsibility |

Prompt the user to confirm the recipients and reviewer before Submit. For a returned Memo, show that the next submission starts a new review cycle while preserving earlier version/decision history.

## Current/future boundary

Currently implemented: free-text To/Through/CC; separate selected-user reviewer; one-step Memo review. Future: directory-backed recipient search, multiple structured CC selections, recipient identity/snapshot behavior, department distribution rules, and recipient confirmation UX. See `backend/apps/documents/forms.py`, `handlers.py`, and `templates/documents/memo_form.html`.
