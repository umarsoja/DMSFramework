# APGC Document Management System

The existing Django application uses PostgreSQL, Django templates, Tabler,
HTMX, Alpine.js, and ApexCharts. The APGC-branded public presentation layer is
available at `/`; the health response is available at `/health/` (URL name
`health`). The existing Django admin login is the configured sign-in route.
The Internal Memo workspace is available at `/documents/memos/` to signed-in
users. The User & Organisation workspace is at `/organisation/` for accounts
with the relevant Django permissions.

## Frontend setup

From `backend`, with Node.js and the project's Python environment available:

```sh
npm ci
npm run build
python manage.py runserver
```

The build copies the lockfile-resolved package distributions into
`static/vendor/`. Run it after dependency changes and on every deployment before
`python manage.py collectstatic --noinput`. Generated assets are ignored by Git.
No CDN, separate Bootstrap dependency, or JavaScript bundler is required.
Production must serve `STATIC_ROOT` at `/static/` through the deployment's static
server. Django's development server serves these assets when DEBUG is enabled.

## Presentation conventions

- Extend `templates/layouts/base.html`. Override `title`, `meta_description`,
  `content`, `extra_css`, and `extra_js` as needed.
- Navbar, sidebar, footer, breadcrumbs, and messages are separate partials.
  Override the `navbar`, `sidebar`, or `footer` block for a different shell.
- Sidebar is optional: pass `sidebar_items`, a list of `label`, `url`, and
  `active` dictionaries. For a workspace shell, override `navbar` to avoid
  displaying the public navigation alongside the vertical sidebar.
- Breadcrumbs accept `breadcrumbs`, a list of `label` and optional `url`
  dictionaries. The final item is the current page.
- Django messages are escaped, mapped to Tabler alert variants, and dismissible.
- `partials/feature_card.html` accepts `number`, `title`, and `description`.
- `partials/brand.html` renders the shared APGC logo from `static/apgc/brand/`.
- `static/apgc/css/apgc-theme.css` owns APGC colour tokens, shared component
  styles, and responsive presentation rules; `static/css/app.css` retains the
  existing application styles.
- `static/apgc/css/apgc-admin.css` brands the existing Django admin login.
- `static/js/app.js` configures same-origin HTMX CSRF headers using Django's
  masked token. Mutating HTMX endpoints must retain Django CSRF protection.
  Future HTMX endpoints should return partial templates, not a new app shell.
- Alpine is available for small local interactions; the landing page needs no
  Alpine state or HTMX requests. Tabler handles navigation and alert dismissal.
- ApexCharts is prepared by the build but not loaded globally. Load it via
  `static 'vendor/apexcharts/apexcharts.min.js'` in `extra_js` only on chart pages.

The landing page links to the existing Django admin sign-in. The repository
contains an initial Internal Memo vertical slice; other DMS document types and
operational modules remain to be implemented.

## Internal Memo workflow

- Create and edit drafts; submission assigns one active staff reviewer.
- Reviewers can open assigned memos, approve them, or return them with a reason.
- Owners save a revision with a revision note and resubmit returned memos.
- Owners finalize approved memos and may archive finalized memos.
- Finalized memos have an APGC PDF view and a separate controlled download.
- Attachments use private storage paths and authorized view/download routes.
- Document access grants distinguish view from download permission.
- Versions and audit events are append-only through the application model
  boundary; audit records capture workflow changes and relevant access events.

The reviewer selector uses Django's active `is_staff` users because this
repository had no employee directory. It now prefers active employee profiles
and temporarily accepts active staff accounts without a profile so existing
accounts and tests continue working during onboarding. Access grants can be
managed by authorized administrators in Django admin. Do not expose the private
attachment storage directory through a static or media web-server route.

## User and Organisation module

- Django's standard `User` remains the identity and login authority; one
  `EmployeeProfile` stores staff ID, employment status, and DMS account status.
- Departments support parent departments and a department head; positions stay
  separate from authorization.
- Each placement is a dated `EmployeeAssignment`. Transfers end the prior row
  and create a new one. Department, position, and supervisor labels are
  snapshotted for historical displays and workflow records.
- New accounts are created with an unusable password. No self-registration,
  generated password, or password email is provided. Credential setup remains
  a separate controlled operation.
- Onboarding and employee changes require `organization.manage_employees`;
  department and position changes require `organization.manage_departments` and
  `organization.manage_positions`; DMS Group assignment requires
  `organization.manage_roles`. Pending task reassignment requires
  `documents.reassign_workflow_tasks`.
- The migrations create the foundation DMS Groups only. They have no
  permissions assigned by default; an authorized administrator configures
  Groups with Django permissions. No APGC employee, department, position, or
  reporting data is seeded.
- Workflow steps support selected reviewer, document creator, creator
  supervisor, and department head actor types. The current Internal Memo flow
  still uses an explicitly selected reviewer. Current MVP workflow capability
  supports a single configured review step. Multi-step workflow progression is
  not yet implemented and must not be configured until implemented and
  verified. Existing tasks keep their assignee and assignment context after
  employee transfers; authorized reassignment is recorded separately.

Organization and document migrations are additive. New links to existing memo,
task, decision, and audit records are nullable, so existing records remain
available with their organization snapshot fields empty where the historical
assignment cannot be determined. Migration `organization.0005` deliberately
leaves snapshot fields blank for assignments that predate the snapshot columns:
current department, position, and supervisor labels cannot establish historical
values after a rename. New assignments capture these labels when created.

Before applying `organization.0005`, verify the target and migration plan, take
the normal database backup, and confirm the deployed application tolerates blank
snapshots on pre-existing assignments. Do not populate those rows from current
organisation labels. If historical labels must be recovered, first obtain and
validate an authoritative historical source, then handle that as a separately
reviewed data correction. Apply the migration set before deploying code that
depends on the new snapshot fields. Reversing `0005` drops the snapshot columns;
it does not reconstruct or preserve values written to those columns.

PostgreSQL verification status: the complete test suite passed on PostgreSQL
(`54 tests`, 0 failures/errors). The test suite uses its isolated `test_apgcdms`
database; the configured development database is not used by this command. The
streamed attachment tests consume the response iterator so Django's test client
performs response cleanup without closing the `TestCase` transaction connection.

To apply the new `documents` migrations to the configured development database,
run `python manage.py migrate` from `backend` after confirming its
`DATABASE_URL` points to the intended development database. Follow the project's
deployment procedure before applying migrations to production.

## Validation

```sh
python manage.py check
python manage.py test tests --settings=config.settings.testing
python manage.py collectstatic --noinput
```

The testing settings use an isolated in-memory SQLite database, not the
configured PostgreSQL `DATABASE_URL`. The suite includes memo workflow,
authorization, attachment controls, PDF generation, audit/version
immutability, and presentation checks.
Tabler integration follows its [official installation guide](https://docs.tabler.io/ui/getting-started/installation/).

## APGC presentation layer

The public page extends `layouts/base.html` and reuses the existing navbar,
brand, capability card, message, breadcrumb, and footer partials. APGC logos
and the favicon live in `static/apgc/brand/`. Keep shared UI styling in the
APGC theme and reuse its tokens and component classes as DMS pages are added.
The established Django templates, Tabler, HTMX, and Alpine.js remain in place.
