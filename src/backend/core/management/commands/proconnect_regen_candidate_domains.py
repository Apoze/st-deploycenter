"""
Generate "future allowed" ProConnect candidate domains from collectivité names
and store them in the ``candidates`` bucket of ``Organization.proconnect_domains``.

These pre-authorized domains are later exposed (per provider) by the public
allowlist API route. Run for all organizations, or narrow to a single operator.

Usage::

    python manage.py proconnect_regen_candidate_domains
    python manage.py proconnect_regen_candidate_domains --operator <operator_id>
    python manage.py proconnect_regen_candidate_domains --dry-run
"""

from django.core.management.base import BaseCommand

from core.models import Organization
from core.services.proconnect import (
    candidate_domains_for_organization,
    domain_bucket,
    is_rpnt_complete,
    update_proconnect_domains,
)


class Command(BaseCommand):
    """Populate the ``candidates`` bucket of proconnect_domains from collectivité names."""

    help = (
        "Generate candidate future ProConnect domains ('{slug}.fr') from "
        "collectivité names and store them in the proconnect_domains candidates bucket."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--operator",
            dest="operator",
            default=None,
            help="Only process organizations managed by this operator id (default: all).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        organizations = Organization.objects.all()
        if options["operator"]:
            organizations = organizations.filter(operators__id=options["operator"])

        changed = 0
        total = 0
        for organization in organizations.iterator():
            total += 1
            # No candidate when the org is already fully RPNT-valid; and never
            # (re)generate a domain a superuser discarded.
            if is_rpnt_complete(organization):
                new_candidates = []
            else:
                discarded = set(domain_bucket(organization, "discarded"))
                new_candidates = [
                    domain
                    for domain in candidate_domains_for_organization(organization)
                    if domain not in discarded
                ]

            # The command only owns the "candidates" bucket; other buckets are preserved.
            current_candidates = domain_bucket(organization, "candidates")
            if set(new_candidates) == set(current_candidates):
                continue

            changed += 1
            if dry_run:
                self.stdout.write(
                    f"{organization.name} ({organization.pk}): "
                    f"candidates {current_candidates} -> {new_candidates}"
                )
            else:
                update_proconnect_domains(organization, candidates=new_candidates)

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{changed} organization(s) would be updated "
                f"out of {total} processed."
                if dry_run
                else f"{changed} organization(s) updated out of {total} processed."
            )
        )
