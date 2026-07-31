"""
Detect drift between our intended ProConnect routing and what is actually live on
the api-partenaires provider(s).

For each provider (idp_id) we GET the live configuration and compare its fqdns to
the exact set we intend to route — the union of ``metadata["domains"]`` across
active subscriptions resolving to that idp (:func:`compute_idp_fqdns`). The two
lists must match EXACTLY; any difference is reported and the command exits
non-zero so a cron can alert.

Read-only: this command never writes to the DB or the provider.
"""

from django.core.management.base import BaseCommand, CommandError

from core.models import ServiceSubscription
from core.services.proconnect import (
    ProConnectPartnersClient,
    ProConnectPartnersError,
    compute_idp_fqdns,
    subscription_idp_id,
)


class Command(BaseCommand):
    """Warn when a provider's live fqdns diverge from our intended routing."""

    help = (
        "Compare each ProConnect provider's live fqdns (GET api-partenaires) with "
        "the exact set we intend to route; report any drift and exit non-zero."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--idp-id",
            dest="idp_id",
            default=None,
            help="Only check this provider uid.",
        )

    def handle(self, *args, **options):
        client = ProConnectPartnersClient()
        if not client.is_configured:
            raise CommandError("api-partenaires is not configured.")

        idp_ids = self._active_idp_ids(options["idp_id"])
        if not idp_ids:
            self.stdout.write("No active ProConnect providers found.")
            return

        drifted = []
        for idp_id in sorted(idp_ids):
            try:
                config = client.get_configuration(idp_id)
            except ProConnectPartnersError as exc:
                drifted.append(idp_id)
                self.stderr.write(self.style.ERROR(f"{idp_id}: GET failed: {exc}"))
                continue

            live = sorted(
                {
                    fqdn.strip().lower()
                    for fqdn in (config.get("fqdns") or [])
                    if isinstance(fqdn, str) and fqdn.strip()
                }
            )
            intended = compute_idp_fqdns(idp_id)  # already sorted + normalized
            if live == intended:
                self.stdout.write(
                    self.style.SUCCESS(f"{idp_id}: in sync ({len(live)} fqdns)")
                )
                continue

            drifted.append(idp_id)
            missing = sorted(set(intended) - set(live))  # we route it, provider lacks it
            unexpected = sorted(set(live) - set(intended))  # provider has it, we don't
            self.stderr.write(
                self.style.WARNING(
                    f"{idp_id}: DRIFT — missing on provider: {missing}; "
                    f"unexpected on provider: {unexpected}"
                )
            )

        if drifted:
            raise CommandError(
                f"{len(drifted)} provider(s) out of sync: "
                f"{', '.join(sorted(drifted))}"
            )

    @staticmethod
    def _active_idp_ids(single_idp_id):
        """Resolve the set of effective idp_ids from active proconnect subscriptions."""
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
