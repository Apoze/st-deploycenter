"""
Client and helpers for the ProConnect "api-partenaires" API.

Pushes the full list of authorized fully-qualified domain names (fqdns) for a
given OIDC provider (identified by its ``idp_id`` / provider uid) to
https://github.com/proconnect-gouv/api-partenaires

Authentication is a shared HMAC-SHA256 secret that is *global* to all
``/api/oidc_providers/*`` routes (per-provider access is enforced by the
api-partenaires allowlist on their side, not by the secret). The signed
message is::

    {timestamp}:{METHOD}:{path}?{query}[:{body}]

and is sent in the ``X-Timestamp`` / ``X-Signature`` headers. The body, when
present, must be signed byte-for-byte as it is sent on the wire.
"""

import hashlib
import hmac
import json
import logging
import re
import time
from collections import defaultdict
from typing import Optional
from urllib.parse import urljoin

from django.conf import settings
from django.core.cache import caches
from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify

import requests

from core.models import (
    Operator,
    OperatorServiceConfig,
    Organization,
    Service,
    ServiceSubscription,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10

# Matches URL userinfo (``scheme://user[:pass]@host``) for redaction.
_CREDENTIALS_RE = re.compile(r"://[^/\s:@]+(?::[^/\s@]+)?@")

# Hostname charset — everything stored in a domain bucket must match this.
_HOSTNAME_RE = re.compile(r"[a-z0-9.-]+")


def _redact_credentials(text: str) -> str:
    """Strip ``user[:pass]@`` credentials from any URL in a string (e.g. proxy URLs).

    Underlying ``requests``/PySocks exceptions can embed the full proxy URL —
    including its password — in their message; scrub it before logging.
    """
    return _CREDENTIALS_RE.sub("://***@", text)


class ProConnectPartnersError(Exception):
    """Raised when a call to the api-partenaires API fails.

    Carries the structured details of the api-partenaires error response when
    available (``error_code``/``fqdns``), so callers can surface an actionable
    message — notably ``fqdn_not_allowed`` with the offending domains.
    """

    def __init__(self, message, status_code=None, error_code=None, fqdns=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.fqdns = fqdns or []


def sign_request(
    secret: str, method: str, path: str, query: str, body: Optional[str]
) -> tuple[str, str]:
    """Return an ``(timestamp, signature)`` pair for the given request.

    The message format mirrors the api-partenaires signature middleware:
    ``{timestamp}:{METHOD}:{path}?{query}`` optionally followed by
    ``:{body}`` when a body is present.
    """
    timestamp = str(int(time.time()))
    message = f"{timestamp}:{method}:{path}?{query}"
    if body:
        message += f":{body}"
    signature = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return timestamp, signature


class ProConnectPartnersClient:
    """Minimal signed client for the api-partenaires OIDC providers API."""

    def __init__(
        self, base_url=None, secret=None, timeout=DEFAULT_TIMEOUT, proxy_url=None
    ):
        base_url = base_url if base_url is not None else (
            settings.PROCONNECT_API_PARTENAIRES_URL or ""
        )
        self.base_url = base_url.rstrip("/")
        self.secret = (
            secret if secret is not None else settings.PROCONNECT_API_PARTENAIRES_SECRET
        )
        # Optional SOCKS5 proxy (e.g. "socks5://user:pass@host:1080"); requires
        # the PySocks-backed "socks" extra of requests.
        self.proxy_url = (
            proxy_url
            if proxy_url is not None
            else settings.PROCONNECT_API_PARTENAIRES_PROXY_URL
        )
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        """Whether both a base URL and a secret are available."""
        return bool(self.base_url and self.secret)

    def _request(self, method: str, path: str, body: Optional[str] = None) -> dict:
        if not self.is_configured:
            raise ProConnectPartnersError(
                "api-partenaires client is not configured "
                "(PROCONNECT_API_PARTENAIRES_URL / PROCONNECT_API_PARTENAIRES_SECRET)."
            )

        timestamp, signature = sign_request(self.secret, method, path, "", body)
        headers = {
            "X-Timestamp": timestamp,
            "X-Signature": signature,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        url = urljoin(self.base_url + "/", path.lstrip("/"))
        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url
            else None
        )
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                data=body,
                timeout=self.timeout,
                proxies=proxies,
            )
        except requests.exceptions.RequestException as e:
            raise ProConnectPartnersError(
                _redact_credentials(f"{method} {path} failed: {e}")
            ) from e

        if response.status_code >= 400:
            error_code = None
            fqdns = None
            try:
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                error_code = data.get("error")
                fqdns = data.get("fqdns")
            raise ProConnectPartnersError(
                f"{method} {path} failed with status {response.status_code}: "
                f"{response.text[:500]}",
                status_code=response.status_code,
                error_code=error_code,
                fqdns=fqdns if isinstance(fqdns, list) else None,
            )

        try:
            return response.json()
        except ValueError:
            return {}

    def get_configuration(self, idp_id: str) -> dict:
        """Read the current provider configuration (uid, name, fqdns, ...)."""
        path = f"/api/oidc_providers/{idp_id}/configuration"
        return self._request("GET", path)

    def set_fqdns(self, idp_id: str, fqdns: list[str]) -> dict:
        """Replace the provider's fqdns with the given list."""
        path = f"/api/oidc_providers/{idp_id}/configuration"
        # Serialize once and sign/send the exact same bytes.
        body = json.dumps({"fqdns": fqdns}, separators=(",", ":"))
        return self._request("PATCH", path, body=body)


# ---------------------------------------------------------------------------
# Per-organization ProConnect domain state.
#
# The Organization model only stores the raw ``proconnect_domains`` JSON dict;
# every read/derivation lives here as a simple ``fn(organization)`` API.
# ---------------------------------------------------------------------------

# Buckets stored in ``Organization.proconnect_domains``.
PROCONNECT_DOMAIN_SOURCES = (
    "requested",
    "manual",
    "dpnt",
    "candidates",
    "discarded",
)

# Full RPNT compliance (no candidate domain is generated when all are satisfied).
RPNT_COMPLETE_CRITERIA = frozenset({"1.1", "1.2", "2.1", "2.2", "2.3"})


def _clean_domains(domains) -> list[str]:
    """Normalize a domain bucket: lowercase, stripped, non-empty, **deduped, sorted**.

    Buckets are semantically sets — order is never meaningful — so we store them
    canonically. This keeps equality checks (change detection, spurious-write
    avoidance) order- and duplicate-insensitive everywhere they are compared.

    Only hostname-charset values (``[a-z0-9.-]``) are kept: this drops anything with
    whitespace, ``#`` or ``/`` before it can reach the public allowlist YAML (built
    by string concatenation), preventing YAML injection via a superuser-set domain.
    """
    cleaned = set()
    for domain in domains or []:
        if not isinstance(domain, str):
            continue
        domain = domain.strip().lower()
        if domain and _HOSTNAME_RE.fullmatch(domain):
            cleaned.add(domain)
    return sorted(cleaned)


def domain_bucket(organization: Organization, key: str) -> list[str]:
    """Return one normalized ``proconnect_domains`` bucket for an org."""
    value = organization.proconnect_domains
    return _clean_domains(value.get(key)) if isinstance(value, dict) else []


def proconnect_domains(organization: Organization) -> dict:
    """Return all normalized buckets: ``{requested, manual, dpnt, candidates, discarded}``."""
    return {key: domain_bucket(organization, key) for key in PROCONNECT_DOMAIN_SOURCES}


def update_proconnect_domains(organization: Organization, **overrides):
    """Atomically merge bucket overrides into an org's ``proconnect_domains``.

    Read-modify-write on the JSON field would let a cron writing one bucket clobber
    a concurrent edit of another. We lock the row (``SELECT FOR UPDATE``) and re-read
    inside the transaction so concurrent writers serialize instead of losing updates.
    Example: ``update_proconnect_domains(org, candidates=["x.fr"])`` replaces only candidates.

    Invariant: a DILA (``dpnt``) domain is authoritative — once it's declared on
    service-public.gouv.fr it must live in ``dpnt`` ONLY, so any copy in
    ``manual``/``requested``/``candidates`` is stripped on every write. That's the
    end state the dpnt import drives toward (a domain "graduating" to ``dpnt``).

    Returns ``(previous, new)`` bucket dicts and syncs the passed instance.
    """
    with transaction.atomic():
        locked = Organization.objects.select_for_update().get(pk=organization.pk)
        previous = proconnect_domains(locked)
        new_value = dict(previous)
        for key, value in overrides.items():
            new_value[key] = _clean_domains(value)
        dpnt_set = set(new_value["dpnt"])
        if dpnt_set:
            for bucket in ("manual", "requested", "candidates"):
                new_value[bucket] = [d for d in new_value[bucket] if d not in dpnt_set]
        if new_value != previous:
            locked.proconnect_domains = new_value
            locked.save(update_fields=["proconnect_domains", "updated_at"])
    organization.proconnect_domains = new_value
    return previous, new_value


def is_rpnt_complete(organization: Organization) -> bool:
    """Whether the org satisfies the full RPNT criteria set (1.1/1.2/2.1/2.2/2.3)."""
    return RPNT_COMPLETE_CRITERIA.issubset(set(organization.rpnt or []))


def routed_domains(organization: Organization, idp_id: Optional[str] = None) -> set[str]:
    """Domains currently routed by the org's active ProConnect subscriptions.

    When ``idp_id`` is given, only subscriptions resolving to that provider are
    counted — so the allowlist's "routed" (live) set for an idp is exactly what
    :func:`compute_idp_fqdns` would push there, never another provider's domains.
    """
    domains: set[str] = set()
    for subscription in organization.service_subscriptions.all():
        if subscription.service.type != "proconnect" or not subscription.is_active:
            continue
        if idp_id is not None and subscription_idp_id(subscription) != idp_id:
            continue
        domains |= set(_clean_domains((subscription.metadata or {}).get("domains")))
    return domains


def _effective_discarded(buckets: dict) -> set[str]:
    """Discarded domains that actually take effect.

    DILA (``dpnt``) domains are authoritative — the rule of law — and can never be
    discarded, so they are removed from the discard set.
    """
    return set(buckets["discarded"]) - set(buckets["dpnt"])


def authorized_domains(organization: Organization) -> list[str]:
    """Domains authorized for ProConnect: (manual + dpnt + candidates) minus discarded.

    Feeds the allowlist. "requested" (pending) is excluded; "dpnt" is always kept
    (discards cannot remove a DILA domain). The routable pool and per-domain source
    view are derived on the frontend from the raw buckets.
    """
    buckets = proconnect_domains(organization)
    return sorted(
        (set(buckets["manual"]) | set(buckets["dpnt"]) | set(buckets["candidates"]))
        - _effective_discarded(buckets)
    )


def subscription_idp_id(subscription: ServiceSubscription) -> Optional[str]:
    """Return the effective ``idp_id`` for a subscription (with operator overrides)."""
    effective_config = OperatorServiceConfig.get_effective_service_config(
        subscription.service, subscription.operator
    )
    return (effective_config or {}).get("idp_id")


def compute_idp_fqdns(idp_id: str) -> list[str]:
    """Return the sorted, normalized union of ``domains`` across all active
    ProConnect subscriptions that resolve to the given ``idp_id``.
    """
    fqdns: set[str] = set()
    subscriptions = ServiceSubscription.objects.filter(
        service__type="proconnect", is_active=True
    ).select_related("service", "operator")

    for subscription in subscriptions:
        if subscription_idp_id(subscription) != idp_id:
            continue
        fqdns.update(_clean_domains((subscription.metadata or {}).get("domains")))

    return sorted(fqdns)


def sync_proconnect_provider(
    idp_id: str, client=None, raise_on_error: bool = False
) -> dict:
    """Compute the full fqdn list for ``idp_id`` and PATCH it to api-partenaires.

    Returns a result dict. By default failures are logged and never raised. With
    ``raise_on_error=True``, a failed PATCH raises ``ProConnectPartnersError`` (so
    the caller can roll back its transaction). A not-configured client is always a
    silent skip, never an error.
    """
    client = client or ProConnectPartnersClient()
    if not client.is_configured:
        logger.info(
            "Skipping ProConnect fqdns push for idp %s: api-partenaires not configured",
            idp_id,
        )
        return {"idp_id": idp_id, "success": False, "skipped": True}

    fqdns = compute_idp_fqdns(idp_id)
    try:
        result = client.set_fqdns(idp_id, fqdns)
    except ProConnectPartnersError as e:
        logger.error("Failed to push ProConnect fqdns for idp %s: %s", idp_id, e)
        if raise_on_error:
            raise
        return {"idp_id": idp_id, "success": False, "error": str(e), "fqdns": fqdns}

    logger.info("Pushed ProConnect fqdns for idp %s: %s", idp_id, fqdns)
    return {"idp_id": idp_id, "success": True, "fqdns": fqdns, "result": result}


def sync_proconnect_provider_for_subscription(
    subscription: ServiceSubscription, raise_on_error: bool = False
) -> Optional[dict]:
    """Resolve a subscription's ``idp_id`` and push its provider's full fqdn list."""
    idp_id = subscription_idp_id(subscription)
    if not idp_id:
        logger.warning(
            "ProConnect subscription %s has no idp_id; skipping fqdns push",
            subscription.pk,
        )
        return None
    return sync_proconnect_provider(idp_id, raise_on_error=raise_on_error)


# ---------------------------------------------------------------------------
# Deployed-allowlist pre-validation cache.
#
# The *deployed* api-partenaires allowlist (a file in their repo, updated by PR)
# lags our generated one, and their PATCH rejects any fqdn not yet in it. We fetch
# it (``proconnect_fetch_prevalidated``) and cache the allowed fqdns per idp so the
# UI can flag which of an org's domains are already routable vs pending the deploy.
# Stored as a list value (NOT a native redis SET, which can't represent an
# empty-but-defined allowlist) keyed ``proconnect_idps_allowed_fqdns:{uid}``.
# ---------------------------------------------------------------------------

PREVALIDATED_KEY_PREFIX = "proconnect_idps_allowed_fqdns:"


def _allowlist_cache():
    """The cache holding the fetched deployed allowlist.

    Reuse ``SESSION_CACHE_ALIAS`` — in every environment it already points at the
    real shared cache (redis in dev/prod, locmem in tests), never the no-op
    DummyCache that some envs use as their ``default``. The fetch command and the
    web process must share it, so a per-process cache won't do.
    """
    return caches[settings.SESSION_CACHE_ALIAS]


def store_prevalidated_fqdns(idp_id: str, fqdns) -> list[str]:
    """Cache an idp's deployed allowlist (normalized list; TTL from settings)."""
    cleaned = _clean_domains(fqdns)
    _allowlist_cache().set(
        f"{PREVALIDATED_KEY_PREFIX}{idp_id}",
        cleaned,
        timeout=settings.PROCONNECT_DOMAIN_ALLOWLIST_CACHE_TTL,
    )
    return cleaned


def get_prevalidated_fqdns(idp_id: str) -> Optional[list]:
    """An idp's cached deployed allowlist, or ``None`` if not defined (unknown)."""
    return _allowlist_cache().get(f"{PREVALIDATED_KEY_PREFIX}{idp_id}")


def operator_prevalidated_sets(operator_id) -> dict:
    """Map each of the operator's ProConnect idps that has a cached deployed
    allowlist to its fqdn set (``{idp_id: frozenset}``).

    idps without a cached allowlist are omitted (→ "pre-validation unknown" for
    that idp). Computed once per request (one query + a few cache reads) so the org
    serializer stays N+1-free. Pre-validation is per-idp: the same domain can be
    deployed on one provider and pending on another.
    """
    result = {}
    if not operator_id:
        return result
    idps = set()
    for config in OperatorServiceConfig.objects.filter(
        operator_id=operator_id, service__type="proconnect"
    ).select_related("service", "operator"):
        effective = OperatorServiceConfig.get_effective_service_config(
            config.service, config.operator
        )
        idp = (effective or {}).get("idp_id")
        if idp:
            idps.add(idp)

    for idp in idps:
        cached = get_prevalidated_fqdns(idp)
        if cached is not None:
            result[idp] = frozenset(cached)
    return result


def prevalidated_org_domains(organization, buckets, sets_by_idp) -> Optional[dict]:
    """Per-idp intersection of the org's domains with each idp's deployed allowlist.

    ``sets_by_idp`` is ``operator_prevalidated_sets(...)``. Returns
    ``{idp_id: sorted(org_domains ∩ set)}`` for each idp with a known allowlist, or
    ``None`` when none is known (→ the ``_prevalidated`` key is omitted →
    "unknown"). An idp mapping to ``[]`` means "defined, but nothing pre-validated".
    """
    if not sets_by_idp:
        return None
    domains = set(routed_domains(organization))
    for names in buckets.values():
        domains.update(names)
    return {
        idp: sorted(domains & allowed) for idp, allowed in sets_by_idp.items()
    }


# ---------------------------------------------------------------------------
# Allowlist (oidc_providers.*.yaml) generation
#
# The api-partenaires allowlist YAML declares, per provider (uid = idp_id), the
# ``allowed_fqdns`` a partner is permitted to route. We regenerate it from DB
# data so it stays a superset of everything we may push.
# ---------------------------------------------------------------------------


# RPNT-valid domain extensions (référentiel criteria 1.2 / 2.3) mapped to the
# départements where each applies. The value is the set of INSEE département codes,
# or ``None`` for a nationwide extension (a candidate for every collectivité).
# Source: RPNT référentiel + suitenumerique/st-home DOMAIN_EXTENSIONS_ALLOWED.
# Note: ".eu" (supranational) and ".tf" (no communes) are intentionally omitted
# from candidates to avoid proposing them for every organization.
PROCONNECT_DOMAIN_EXTENSIONS: dict[str, Optional[frozenset]] = {
    # National — always a candidate.
    "fr": None,
    # Régional.
    "bzh": frozenset({"22", "29", "35", "44", "56"}),  # Bretagne
    "alsace": frozenset({"67", "68"}),  # Alsace
    "corsica": frozenset({"2A", "2B"}),  # Corse
    "paris": frozenset({"75"}),  # Paris
    # Outre-mer.
    "gp": frozenset({"971"}),  # Guadeloupe
    "mq": frozenset({"972"}),  # Martinique
    "gf": frozenset({"973"}),  # Guyane
    "re": frozenset({"974"}),  # Réunion
    "pm": frozenset({"975"}),  # Saint-Pierre-et-Miquelon
    "yt": frozenset({"976"}),  # Mayotte
    "wf": frozenset({"986"}),  # Wallis-et-Futuna
    "pf": frozenset({"987"}),  # Polynésie française
    "nc": frozenset({"988"}),  # Nouvelle-Calédonie
}


def _org_slug(name: str) -> Optional[str]:
    """Return the bare slug for a collectivité name, or ``None`` if empty.

    Slug rules follow :func:`django.utils.text.slugify` (accents stripped,
    lowercased, non-word runs collapsed to hyphens).
    """
    return slugify(name or "") or None


def slugify_org_domain(name: str) -> Optional[str]:
    """Return the candidate ``{slug}.fr`` domain derived from a collectivité name."""
    slug = _org_slug(name)
    return f"{slug}.fr" if slug else None


def candidate_domains_for_organization(organization: Organization) -> list[str]:
    """Candidate domains for an org.

    - ``{slug}.<ext>`` for every RPNT-valid extension applicable to the org's
      département (always ``.fr``).
    - extra ``.fr`` forms — ``mairie-{slug}.fr``, ``ville-{slug}.fr`` and
      ``{slug}{dept}.fr`` — but only when ``{slug}.fr`` is not already one of the
      org's DILA domains (i.e. not already its official domain).

    Only communes get candidate domains (EPCIs and other types are skipped).
    Returns a sorted list (empty if not a commune or the name yields no slug).
    """
    if organization.type != "commune":
        return []
    slug = _org_slug(organization.name)
    if not slug:
        return []
    dept = organization.departement_code_insee or ""
    dpnt = set(domain_bucket(organization, "dpnt"))

    domains = {
        f"{slug}.{ext}"
        for ext, depts in PROCONNECT_DOMAIN_EXTENSIONS.items()
        if depts is None or dept in depts
    }
    # Alternative .fr forms, unless the plain {slug}.fr is already a DILA domain.
    if f"{slug}.fr" not in dpnt:
        domains.add(f"mairie-{slug}.fr")
        domains.add(f"ville-{slug}.fr")
        if dept:
            domains.add(f"{slug}{dept.lower()}.fr")

    # Never propose a domain that is already an authoritative DILA domain.
    return sorted(domains - dpnt)


def org_rpnt_valid_domains(organization: Organization) -> set[str]:
    """Return the RPNT-valid candidate domains for an organization.

    - criterion ``1.1`` (website): ``site_internet_domain``
    - criteria ``2.1`` + ``2.2`` (email): ``adresse_messagerie_domain``
    """
    domains = set()
    rpnt_set = set(organization.rpnt or [])
    if "1.1" in rpnt_set and organization.site_internet_domain:
        domains.add(organization.site_internet_domain)
    if {"2.1", "2.2"}.issubset(rpnt_set) and organization.adresse_messagerie_domain:
        domains.add(organization.adresse_messagerie_domain)
    return {d.strip().lower() for d in domains if d and d.strip()}


def _proconnect_idp_scopes() -> dict[str, dict]:
    """Map each **effective** ``idp_id`` to the operators and services that route to it.

    Mirrors the push path (:func:`subscription_idp_id`), which honors per-operator
    ``idp_id`` overrides — so the allowlist is keyed by the very idp we push to,
    not the service's base config idp. Each value is
    ``{"operator_ids": set, "service_ids": set}``.
    """
    services = list(Service.objects.filter(type="proconnect"))
    services_by_id = {service.id: service for service in services}
    scopes: dict[str, dict] = defaultdict(
        lambda: {"operator_ids": set(), "service_ids": set()}
    )

    def _add(idp_id, operator_id, service_id):
        if idp_id:
            scopes[idp_id]["operator_ids"].add(operator_id)
            scopes[idp_id]["service_ids"].add(service_id)

    # Every operator that has a proconnect service configured (override or base).
    for config in OperatorServiceConfig.objects.filter(
        service__in=services
    ).select_related("operator"):
        service = services_by_id.get(config.service_id)
        effective = OperatorServiceConfig.get_effective_service_config(
            service, config.operator
        )
        _add((effective or {}).get("idp_id"), config.operator_id, config.service_id)

    # Operators routing via an active subscription (covers those with no config row).
    for subscription in ServiceSubscription.objects.filter(
        service__in=services, is_active=True
    ).select_related("service", "operator"):
        _add(
            subscription_idp_id(subscription),
            subscription.operator_id,
            subscription.service_id,
        )

    return scopes


def _covered_departement_codes(operator_ids) -> set[str]:
    """Départements covered by the given operators, from their ``config["departements"]``.

    Coverage is the operator's declared reference scope, NOT the départements of
    the organizations it currently manages.
    """
    codes: set[str] = set()
    for config in Operator.objects.filter(id__in=list(operator_ids)).values_list(
        "config", flat=True
    ):
        for code in (config or {}).get("departements") or []:
            if isinstance(code, str) and code.strip():
                codes.add(code.strip())
    return codes


# Domain sources that feed the allowlist, low → high display priority. The
# highest-priority source is shown in the YAML comment.
_ALLOWLIST_SOURCES = [
    ("routed", "routed"),
    ("candidates", "candidates"),
    ("manual", "manual"),
    ("dpnt", "DILA"),
]


def _scoped_organizations(operator_ids, service_ids):
    """Organizations whose domains feed a provider's allowlist.

    For the operators/services resolving to a given idp, this is the union of:
    - orgs in one of the operators' declared ``config["departements"]``,
    - orgs the operators currently manage (OperatorOrganizationRole),
    - any org with an active subscription to one of the services under one of
      the operators.
    """
    operator_ids = list(operator_ids)
    covered = _covered_departement_codes(operator_ids)

    query = Q(
        service_subscriptions__service_id__in=service_ids,
        service_subscriptions__is_active=True,
        service_subscriptions__operator_id__in=operator_ids,
    )
    if covered:
        query |= Q(departement_code_insee__in=covered)
    if operator_ids:
        query |= Q(operators__in=operator_ids)
    return Organization.objects.filter(query).distinct()


def build_proconnect_allowlist() -> list[dict]:
    """Build the allowlist entries for every ProConnect provider.

    Each provider's ``allowed_fqdns`` is the union of the authorized domains
    (manual + dpnt + candidates + routed) of every organization in scope. For each
    fqdn we keep the highest-priority source and the contributing organization's
    Service-Public URL, for an explanatory YAML comment.
    """
    entries = []
    for idp_id, scope in _proconnect_idp_scopes().items():
        # fqdn -> (priority, source_label, service_public_url, org_name)
        fqdn_info: dict[str, tuple[int, str, Optional[str], str]] = {}
        organizations = _scoped_organizations(
            scope["operator_ids"], scope["service_ids"]
        )
        for organization in organizations.iterator():
            sp_url = organization.service_public_url or None
            org_name = organization.name or ""
            buckets = proconnect_domains(organization)
            discarded = _effective_discarded(buckets)  # dpnt is never discardable
            source_domains = {
                # Per-idp: only domains this org routes to THIS provider (never
                # another idp's live domains — see routed_domains docstring).
                "routed": routed_domains(organization, idp_id),
                "candidates": set(buckets["candidates"]),
                "manual": set(buckets["manual"]),
                "dpnt": set(buckets["dpnt"]),
            }
            for priority, (src, label) in enumerate(_ALLOWLIST_SOURCES):
                # "routed" domains are what is CURRENTLY LIVE on the provider, so
                # they are never subtracted by a discard (that would drop a domain
                # the provider is actively using). Discard only hides candidates/
                # manual proposals; dpnt is already exempt via _effective_discarded.
                fqdns = source_domains[src]
                if src != "routed":
                    fqdns = fqdns - discarded
                for fqdn in fqdns:
                    current = fqdn_info.get(fqdn)
                    if current is None or priority > current[0]:
                        fqdn_info[fqdn] = (priority, label, sp_url, org_name)

        # Ordered by organization name ASC, then domain ASC.
        allowed = [
            {"domain": fqdn, "source": info[1], "service_public_url": info[2]}
            for fqdn, info in sorted(
                fqdn_info.items(), key=lambda kv: (kv[1][3].casefold(), kv[0])
            )
        ]
        entries.append({"uid": idp_id, "allowed_fqdns": allowed})

    entries.sort(key=lambda entry: entry["uid"])
    return entries


def render_proconnect_allowlist_yaml(entries: list[dict]) -> str:
    """Render allowlist entries as YAML matching the api-partenaires format.

    Each fqdn is followed by a ``# Source: <src> | <Service-Public URL>`` comment
    (the URL part is omitted when unknown).
    """
    lines = ["oidc_providers:"]
    for entry in entries:
        lines.append(f'  - uid: "{entry["uid"]}"')
        fqdns = entry["allowed_fqdns"]
        if not fqdns:
            lines.append("    allowed_fqdns: []")
            continue
        lines.append("    allowed_fqdns:")
        for item in fqdns:
            comment = f"Source: {item['source']}"
            if item.get("service_public_url"):
                comment += f" | {item['service_public_url']}"
            lines.append(f"      - {item['domain']}  # {comment}")
    return "\n".join(lines) + "\n"
