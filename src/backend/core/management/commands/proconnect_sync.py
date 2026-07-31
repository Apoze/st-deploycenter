"""
Push the full list of authorized fqdns to the ProConnect api-partenaires API
for every active ProConnect provider (idp_id).

Useful for the initial backfill and for periodic reconciliation.
"""

from django.core.management.base import BaseCommand, CommandError

from core.models import ServiceSubscription
from core.services.proconnect import (
    ProConnectPartnersClient,
    compute_idp_fqdns,
    subscription_idp_id,
    sync_proconnect_provider,
)


class Command(BaseCommand):
    """Reconcile ProConnect provider fqdns with active subscriptions."""

    help = (
        "Push authorized fqdns to the ProConnect api-partenaires API for all "
        "active providers (or a single one with --idp-id)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--idp-id",
            dest="idp_id",
            default=None,
            help="Only sync this idp_id (OIDC provider uid).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the fqdns that would be pushed without calling the API.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        client = ProConnectPartnersClient()
        if not client.is_configured and not dry_run:
            raise CommandError(
                "api-partenaires is not configured. Set "
                "PROCONNECT_API_PARTENAIRES_URL and "
                "PROCONNECT_API_PARTENAIRES_SECRET."
            )

        idp_ids = self._resolve_idp_ids(options["idp_id"])
        if not idp_ids:
            self.stdout.write("No active ProConnect providers found.")
            return

        failures = []
        for idp_id in sorted(idp_ids):
            fqdns = compute_idp_fqdns(idp_id)
            if dry_run:
                self.stdout.write(f"[dry-run] {idp_id}: {fqdns}")
                continue

            result = sync_proconnect_provider(idp_id, client=client)
            if result.get("success"):
                self.stdout.write(
                    self.style.SUCCESS(f"{idp_id}: OK -> {result.get('fqdns')}")
                )
            else:
                failures.append(idp_id)
                self.stderr.write(
                    self.style.ERROR(
                        f"{idp_id}: FAILED ({result.get('error')}) -> "
                        f"{result.get('fqdns')}"
                    )
                )

        if failures:
            raise CommandError(
                f"Failed to push fqdns for {len(failures)} provider(s): "
                f"{', '.join(sorted(failures))}"
            )

    @staticmethod
    def _resolve_idp_ids(single_idp_id):
        """Return the set of idp_ids to sync."""
        if single_idp_id:
            return {single_idp_id}

        idp_ids = set()
        for subscription in ServiceSubscription.objects.filter(
            service__type="proconnect", is_active=True
        ).select_related("service", "operator"):
            idp_id = subscription_idp_id(subscription)
            if idp_id:
                idp_ids.add(idp_id)
        return idp_ids
