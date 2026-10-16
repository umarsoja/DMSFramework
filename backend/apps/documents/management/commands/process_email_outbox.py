from django.core.management.base import BaseCommand

from apps.organization.activation_email import process_pending_email


class Command(BaseCommand):
    help = "Attempt delivery of queued or retryable DMS email outbox records."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1:
            self.stderr.write("--limit must be a positive integer.")
            return
        records = process_pending_email(limit=limit)
        counts = {"sent": 0, "failed": 0, "skipped": 0}
        for record in records:
            if record.status == record.Status.SENT:
                counts["sent"] += 1
            elif record.status == record.Status.FAILED:
                counts["failed"] += 1
            else:
                counts["skipped"] += 1
        self.stdout.write(
            f"Processed {len(records)} email outbox record(s): "
            f"{counts['sent']} sent, {counts['failed']} failed, {counts['skipped']} skipped."
        )
