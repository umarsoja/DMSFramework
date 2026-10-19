"""Database-free integration checks for public routing and the template shell."""
from pathlib import Path

from django.contrib.messages import ERROR, SUCCESS
from django.contrib.messages.storage.base import Message
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import reverse


class PresentationTests(SimpleTestCase):
    def test_public_landing_and_health_are_separate(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "layouts/base.html")
        self.assertContains(response, "Document Management")
        self.assertContains(response, "Alliance Power Generation Company")
        self.assertContains(response, "/static/apgc/brand/alliance-power-logo.png")
        self.assertContains(response, "/static/apgc/brand/favicon.png")
        self.assertContains(response, reverse("dms-login"))
        self.assertContains(response, 'id="sign-in"')
        self.assertNotContains(response, "TradeFlow is running successfully.")
        self.assertEqual(self.client.get(reverse("health")).status_code, 200)

    def test_existing_admin_login_remains_separate(self):
        response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Document Management System")
        self.assertContains(response, "/static/apgc/css/apgc-admin.css")

    def test_dms_login_is_a_separate_employee_sign_in_page(self):
        response = self.client.get(reverse("dms-login"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/login.html")
        self.assertTemplateUsed(response, "layouts/authentication.html")
        self.assertContains(response, "APGC Digital Portal")
        self.assertContains(response, "Sign in with your APGC employee account")
        self.assertNotContains(response, 'id="public-navigation"')
        self.assertNotContains(response, "Quick Links")
        self.assertContains(response, "apgc-auth-footer")
        self.assertNotContains(response, "/static/apgc/css/apgc-admin.css")

    def test_notifications_escape_content_and_map_error_level(self):
        html = render_to_string("partials/messages.html", {"messages": [
            Message(ERROR, "<script>alert(1)</script>"),
            Message(SUCCESS, "Saved"),
        ]})
        self.assertIn("alert-danger", html)
        self.assertIn("alert-success", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_optional_navigation_and_current_breadcrumb(self):
        self.assertEqual(render_to_string("partials/sidebar.html").strip(), "")
        html = render_to_string("partials/breadcrumb.html", {"breadcrumbs": [
            {"label": "Home", "url": "/"}, {"label": "Customers"},
        ]})
        self.assertIn('href="/"', html)
        self.assertIn('aria-current="page"', html)

    def test_shared_action_confirmation_uses_viewport_centering(self):
        css_path = Path(__file__).resolve().parents[1] / "static" / "css" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertIn("html:has(.action-confirmation[open]) { scrollbar-gutter: stable; }", css)
        self.assertIn(".action-confirmation { position: fixed; top: 50%; left: 50%;", css)
        self.assertIn("transform: translate(-50%, -50%)", css)
        self.assertIn(".action-confirmation::backdrop { position: fixed; inset: 0;", css)
        self.assertIn("max-height: min(90vh, 48rem); overflow-y: auto", css)

    def test_status_badges_and_memo_table_do_not_truncate_labels(self):
        backend = Path(__file__).resolve().parents[1]
        theme_css = (backend / "static" / "apgc" / "css" / "apgc-theme.css").read_text(encoding="utf-8")
        status_template = (backend / "templates" / "components" / "document_status.html").read_text(encoding="utf-8")
        memo_list_template = (backend / "templates" / "documents" / "memo_list.html").read_text(encoding="utf-8")

        self.assertIn("position: static", theme_css)
        self.assertIn("width: max-content", theme_css)
        self.assertIn("height: auto", theme_css)
        self.assertIn("min-width: 0; min-height: 0", theme_css)
        self.assertIn("overflow: visible", theme_css)
        self.assertIn("padding: .3rem .65rem", theme_css)
        self.assertIn("white-space: nowrap", theme_css)
        self.assertIn(".memo-table-wrap .memo-status-column { min-width: 11rem; }", theme_css)
        self.assertIn("tbody td.memo-status-column { grid-column: 1 / -1; min-width: 0; }", theme_css)
        self.assertIn("{{ display }}", status_template)
        self.assertNotIn("truncatechars", status_template)
        self.assertNotIn("slice:", status_template)
        self.assertIn("display=document.get_status_display", memo_list_template)

    def test_organisation_chart_contains_scroll_containment_for_wide_branches(self):
        css_path = Path(__file__).resolve().parents[1] / "static" / "apgc" / "css" / "organisation-structure.css"
        css = css_path.read_text(encoding="utf-8")

        self.assertIn(
            ".org-chart-scroll { width: 100%; min-width: 0; max-width: 100%; overflow-x: auto;",
            css,
        )
        self.assertIn("overscroll-behavior-inline: contain", css)
        self.assertIn(".org-chart { display: flex; width: max-content; min-width: 100%;", css)
        self.assertIn("margin-inline: 0; }", css)
        self.assertIn("width: max-content; min-width: 100%; flex-direction: column", css)

    def test_classification_badge_uses_existing_display_value_and_distinct_styles(self):
        backend = Path(__file__).resolve().parents[1]
        template = (backend / "templates" / "components" / "document_classification.html").read_text(encoding="utf-8")
        css = (backend / "static" / "apgc" / "css" / "dms-portal.css").read_text(encoding="utf-8")

        self.assertIn("{{ display }}", template)
        self.assertIn("classification-{{ classification|lower }}", template)
        for classification in ("internal", "confidential", "restricted"):
            self.assertIn(f".classification-{classification}", css)
