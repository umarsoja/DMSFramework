# APGC DMS DigitalOcean pilot deployment

This runbook describes the prepared host: Ubuntu 24.04, Python 3.12.3,
PostgreSQL 16, database `apgcdms`, role `apgc_dms_user`, service account
`dmsapp`, application root `/opt/apgc-dms`, and source checkout
`/opt/apgc-dms/app`. PostgreSQL listens on localhost only. The public IP is
`138.68.70.57`; the production hostname is not confirmed. Do not infer a
hostname from the IP or issue an SSL certificate until the owner confirms DNS.
These instructions do not authorize server access, DNS changes, email sending,
or deployment. The DMS must use a separate database and credentials from ERP.

## Release prerequisites

- A reviewed clean Git checkout and an approved deployment operator.
- Ubuntu 24.04 with Python 3.12.3, PostgreSQL 16, Nginx, Git, build tools and
  `libpq-dev`. `dmsapp` is the runtime account; PostgreSQL is not public.
- Node.js and npm for the frontend build. `backend/static/vendor/` is generated
  from the committed `backend/package-lock.json`, ignored by Git, and must be
  built on the host from each fresh checkout before Django collects static
  files.
- A confirmed hostname and DNS record, approved TLS certificate, Zoho SMTP
  configuration, environment-managed unique secrets, and an encrypted off-host
  backup target. The Nginx `dms.example.invalid` name is a deliberately invalid
  placeholder. Replace it only after hostname and certificate approval.

## Host, source and environment preparation

Create `/opt/apgc-dms`, with the checkout at `/opt/apgc-dms/app`, and keep the
runtime virtual environment at `/opt/apgc-dms/venv`. The service account needs
read access to the checkout and write access only to the private media
directory. Create `/var/lib/apgc-dms/private-media` owned by `dmsapp:www-data`
with mode `0750`; it must remain outside the repository, static root and web
root. Create `/etc/apgc-dms/apgc-dms.env` from
`deploy/digitalocean/apgc-dms.env.example`, owned by `root:root` with mode
`0600`. Never commit a populated environment file.

The environment example intentionally leaves `SECRET_KEY`, hostname settings,
SMTP credentials and sender address empty. Generate a new, unique production
secret in the approved secret manager; production settings reject missing,
short, placeholder and `django-insecure` keys. Never copy a key from local
settings, an old checkout, or Git history. Set `DEBUG=False`, `ALLOWED_HOSTS`,
`CSRF_TRUSTED_ORIGINS`, and canonical HTTPS `DMS_BASE_URL` only after the
hostname is confirmed. Set the URL to `postgresql://apgc_dms_user:<URL-encoded-
password>@127.0.0.1:5432/apgcdms`; use the approved database secret and do not
put a real password into shell history or this repository. Configure
`PRIVATE_MEDIA_ROOT=/var/lib/apgc-dms/private-media` and the approved Zoho SMTP
values. The Nginx template overwrites `X-Forwarded-Proto`, so it pairs with
`TRUST_X_FORWARDED_PROTO=True`; trust this header only behind this controlled
proxy. Preserve HTTPS redirect, secure session/CSRF cookies, and initial
300-second HSTS. HSTS subdomain inclusion and preload remain disabled.

Install OS dependencies and prepare the application from the clean checkout:

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev build-essential \
  libpq-dev libffi-dev libjpeg-dev libopenjp2-7-dev \
  postgresql-16 postgresql-client-16 nginx certbot python3-certbot-nginx \
  nodejs npm git

cd /opt/apgc-dms/app/backend
python3.12 -m venv /opt/apgc-dms/venv
/opt/apgc-dms/venv/bin/python -m pip install --upgrade pip
/opt/apgc-dms/venv/bin/pip install -r requirements/production.txt
npm ci
npm run build
```

The frontend build copies locked Tabler, Alpine, HTMX and ApexCharts assets into
ignored `static/vendor/`. Do not add those generated distributions to Git.
Running `npm run build` on each clean deployment makes the assets available to
`collectstatic`; the Nginx static alias points to
`/opt/apgc-dms/app/backend/staticfiles/`.

## Database, first administrator and role grants

Provision only the dedicated local PostgreSQL database `apgcdms` and role
`apgc_dms_user`. Do not use, connect to, or restore an ERP database. Apply
migrations after reviewing the plan and taking the normal backup. The initial
organization migration seeds the seven DMS role groups. Migration
`organization.0007` grants `organization.manage_roles` to DMS Administrator
and `documents.reassign_workflow_tasks` to DMS Workflow Administrator;
`organization.0008` grants `organization.manage_employees` to DMS
Administrator. No migration grants department or position management to that
group. Do not infer or automatically add other permissions; assign additional
permissions only through the approved organization/Admin process.

The first DMS Administrator is provisioned through the one-time bootstrap flow
at `/accounts/bootstrap/`, using the separately provisioned Django superuser
and its existing credential. The bootstrap endpoint is restricted to a Django
superuser and closes after the first DMS Administrator profile exists. It
creates the first employee-backed DMS Administrator; it does not bypass normal
DMS eligibility. Complete initial credentials through the existing activation
process. Do not create a second administrator, change existing employee data,
or broaden bootstrap permissions as part of deployment.

## Migrations, checks and static collection

Export the protected environment values for the intended deployment, then run
from `backend`:

```bash
cd /opt/apgc-dms/app/backend
source /opt/apgc-dms/venv/bin/activate
python manage.py check
python manage.py check --deploy
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan
python manage.py migrate --noinput
python manage.py showmigrations
npm ci
npm run build
python manage.py collectstatic --noinput
```

Do not point validation at the ERP database. `check --deploy` may report the
intentional initial HSTS guidance because subdomain inclusion and preload are
disabled; review all warnings against the confirmed domain and security policy.

## Explicit full test suite

The tests live under `backend/tests/`, which Django does not discover when
`manage.py test` is run without a label. Run the checked wrapper from `backend`:

```bash
cd /opt/apgc-dms/app/backend
source /opt/apgc-dms/venv/bin/activate
python scripts/run_tests.py
```

The wrapper executes `python manage.py test tests --settings=config.settings.testing`
and fails if Django succeeds without reporting a nonzero `Ran N tests` count.
Include all onboarding wizard source and test files in the reviewed release
inventory before preparing a clean checkout. In the current working tree,
`backend/tests/test_employee_onboarding_wizard.py` is untracked; it is included
when running this suite here but will not be present in a clean clone until it
is included in the release.
For a disposable PostgreSQL release test, configure production settings and a
disposable database/role with `CREATEDB` permission, then run
`APGC_TEST_SETTINGS=config.settings.production python scripts/run_tests.py`.
The test runner creates its own PostgreSQL test database; confirm its name and
ensure that database is disposable. Keep email routed to a test sink. This
repository's `config.settings.testing` uses in-memory SQLite and is not the
PostgreSQL validation path.

## Nginx, systemd and request logging

The systemd units run as `dmsapp`, use `/opt/apgc-dms/venv`, and set working
directory `/opt/apgc-dms/app/backend`. They share the `/run/apgc-dms` socket
and private media root. Install the units only after replacing the Nginx
placeholder hostname with the confirmed hostname and installing its approved
certificate. The Nginx static alias is the collected static directory. Do not
add a `/media/` alias: private documents must remain permission-checked through
Django views.

The application enforces a maximum of 20 MiB per file and 100 MiB total file
payload in one submission. Nginx permits a conservative 110 MiB request body
to allow multipart overhead. Do not raise either limit without reviewing
application validation, storage capacity and request timeouts.

Nginx's `dms_safe` access log omits request URI, query string, request line and
referrer. Nginx does not provide a configurable redacted error-log format; the
site template sets `error_log ... crit` to reduce routine entries, but critical
errors may still include request context. Restrict error-log file access and
do not forward unredacted error logs to external logging. If forwarding is
required, apply and test URI/token redaction before forwarding. Gunicorn access
logs are disabled and Django recovery-token logging filters remain enabled.
These settings do not replace a production verification of Nginx, journald and
any log collector behavior.

On the host, validate before enabling the service:

```bash
sudo nginx -t
sudo systemd-analyze verify /etc/systemd/system/apgc-dms.service \
  /etc/systemd/system/apgc-dms-outbox.service \
  /etc/systemd/system/apgc-dms-outbox.timer
```

The outbox timer stays disabled until mail-sink validation and pilot approval.
Never use live Zoho delivery for smoke tests.

## PostgreSQL 16 release validation and smoke tests

Run this gate only against a newly created disposable PostgreSQL 16 database
and role on localhost. Supply production-like settings using temporary test
secrets, a test mail sink, a disposable `DATABASE_URL`, and a canonical test
HTTPS URL. Do not use the real production account email addresses or Zoho
credentials. Execute the migration/check/test/static commands above with
`APGC_TEST_SETTINGS=config.settings.production python scripts/run_tests.py` to
validate PostgreSQL. Confirm
the test runner creates and removes only its disposable test database.

Using disposable users and files, then verify:

1. Login and activation/password-reset requests queue mail to the sink; process
   outbox messages only against the sink and verify token links expire and are
   single-use.
2. No recovery token or full URL appears in Django, Gunicorn, Nginx or system
   journal logs.
3. An authorized employee can upload and retrieve a test document; an
   unauthorized employee is denied; no direct `/media/` URL exposes storage.
4. Representative memo, incoming correspondence, management directive,
   notification and assignment flows preserve authorization and audit events.
5. Collected static assets load in a browser and the site behaves over HTTPS.

Local PostgreSQL 16 and Linux Nginx/systemd validation are host-level gates and
must be recorded separately; SQLite tests do not satisfy the PostgreSQL gate.

## Backups and restore

Back up PostgreSQL and `/var/lib/apgc-dms/private-media` together to encrypted
off-host storage under approved recovery objectives. Keep backup credentials
and encryption keys outside the Droplet. Use a restricted backup role and
root-managed password file with mode `0600`; use `umask 077` for temporary
artifacts. Coordinate database and media snapshots so uploads are not omitted.
Test restores into an isolated DMS database and media directory, and verify
that restored attachment records resolve to restored files. Never restore over
production without a separately approved recovery procedure.

## Pilot activation checklist

Confirm the hostname and TLS certificate, firewall, PostgreSQL isolation,
private storage permissions, secret-file permissions, SMTP sink validation,
safe access/error logging, encrypted backups and restore test. Install and
enable the outbox timer only after pilot approval. Keep it disabled during
validation against a real SMTP account. No hostname, certificate, infrastructure
or production settings are confirmed by this repository template.
