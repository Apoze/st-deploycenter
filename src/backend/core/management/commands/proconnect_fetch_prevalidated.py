"""
Fetch the *deployed* ProConnect allowlist YAML (the file that actually gates
api-partenaires, updated in their repo by PR) and cache the allowed fqdns per
provider (idp uid). Meant to run on a cron.

The UI reads this cache to show which of an organization's domains are already
pre-validated (routable now) vs pending the next allowlist deploy. The URL and
the cache TTL are configurable via ``PROCONNECT_DOMAIN_ALLOWLIST_URL`` and
``PROCONNECT_DOMAIN_ALLOWLIST_CACHE_TTL``.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

import requests
import yaml

from core.services.proconnect import store_prevalidated_fqdns


class Command(BaseCommand):
    """Cache the deployed ProConnect allowlist (per-idp allowed fqdns)."""

    help = (
        "Fetch the deployed ProConnect allowlist YAML and cache its per-idp "
        "allowed fqdns for the pre-validation UI."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            dest="url",
            default=None,
            help="Override PROCONNECT_DOMAIN_ALLOWLIST_URL.",
        )

    def handle(self, *args, **options):
        url = options["url"] or settings.PROCONNECT_DOMAIN_ALLOWLIST_URL
        if not url:
            raise CommandError("PROCONNECT_DOMAIN_ALLOWLIST_URL is not configured.")

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise CommandError(f"Failed to fetch {url}: {exc}") from exc

        try:
            data = yaml.safe_load(response.text) or {}
        except yaml.YAMLError as exc:
            raise CommandError(f"Invalid YAML at {url}: {exc}") from exc

        providers = data.get("oidc_providers") or []
        count = 0
        for provider in providers:
            uid = provider.get("uid") if isinstance(provider, dict) else None
            if not uid:
                continue
            cached = store_prevalidated_fqdns(uid, provider.get("allowed_fqdns") or [])
            count += 1
            self.stdout.write(f"{uid}: cached {len(cached)} allowed fqdns")

        if not count:
            raise CommandError(f"No providers found in the allowlist at {url}.")

        self.stdout.write(
            self.style.SUCCESS(
                f"Cached the allowlist for {count} provider(s) "
                f"(TTL {settings.PROCONNECT_DOMAIN_ALLOWLIST_CACHE_TTL}s)."
            )
        )
