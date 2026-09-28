"""Database-free integration checks for public routing and the template shell."""
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
        self.assertContains(response, "Customer Management")
        self.assertContains(response, 'id="sign-in"')
        self.assertNotContains(response, "TradeFlow is running successfully.")
        self.assertEqual(self.client.get(reverse("health")).status_code, 200)

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
