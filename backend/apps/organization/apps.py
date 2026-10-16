from django.apps import AppConfig
import logging

from .logging import ActivationTokenRedactionFilter


class OrganizationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.organization"
    label = "organization"
    verbose_name = "APGC identity and organisation"

    def ready(self):
        token_filter = ActivationTokenRedactionFilter()
        for name in ("django.server", "django.request"):
            logger = logging.getLogger(name)
            logger.addFilter(token_filter)
            for handler in logger.handlers:
                handler.addFilter(token_filter)
        for handler in logging.getLogger().handlers:
            handler.addFilter(token_filter)
