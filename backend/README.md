# TradeFlow

Modular Django with PostgreSQL, Django templates, Tabler, HTMX, Alpine.js,
and ApexCharts. The public presentation layer is available at `/`; the existing
health response is available at `/health/` (URL name `health`).

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
- `partials/brand.html` contains the temporary typographic logo.
- `static/css/app.css` owns TradeFlow tokens and responsive overrides.
- `static/js/app.js` configures same-origin HTMX CSRF headers using Django's
  masked token. Mutating HTMX endpoints must retain Django CSRF protection.
  Future HTMX endpoints should return partial templates, not a new app shell.
- Alpine is available for small local interactions; the landing page needs no
  Alpine state or HTMX requests. Tabler handles navigation and alert dismissal.
- ApexCharts is prepared by the build but not loaded globally. Load it via
  `static 'vendor/apexcharts/apexcharts.min.js'` in `extra_js` only on chart pages.

The Sign In CTA leads to an explicit availability notice. Authentication and
operational modules are future work; the landing page does not simulate them.

## Validation

```sh
python manage.py check
python manage.py test tests --settings=config.settings.testing
python manage.py collectstatic --noinput
```

Presentation checks use `SimpleTestCase` and do not create or query a database.
Tabler integration follows its [official installation guide](https://docs.tabler.io/ui/getting-started/installation/).

## Homepage composition

The public page composes `partials/home/` sections for the hero, capabilities,
benefits, and audiences. Reusable content cards live in `components/`; the original
feature-card partial remains a compatibility include. The trade visual contains
a decorative inline SVG concept and a visible placeholder caption. Replace this
figure with the approved illustration without changing the surrounding grid.

Footer resources use native HTML disclosures until approved destinations and
contact details are available. No legal policy or contact address is fabricated.
The homepage describes intended platform capabilities; operational modules are
not implemented by this presentation work. No additional JavaScript is needed.

### Final public homepage components

- `workflow_step.html` renders the ordered, illustrative trade lifecycle.
- `icon.html` renders an external SVG symbol from the locally built Tabler sprite.
  `scripts/build-assets.cjs` contains the small allowlist of official icons; add
  new names there before referencing them in a component. The npm version is
  pinned and its MIT license is copied alongside the generated sprite.
- Capability details and footer notices use native `details`/`summary` controls.
- `preview.html` is a decorative screenshot slot, not a working dashboard.
- `trust.html` labels availability as a design target and future features as
  planned. Replace these only with validated release claims.
- Sticky navigation uses CSS; the only new JavaScript closes the mobile menu
  when a navigation link is selected. Reduced-motion preferences are respected.
