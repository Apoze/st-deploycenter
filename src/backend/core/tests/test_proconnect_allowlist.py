"""
Tests for the ProConnect api-partenaires allowlist generation:

- domain-building logic in core/services/proconnect.py
- the proconnect_regen_candidate_domains command (fills the "candidates" bucket)
- the public allowlist API route (serves the YAML as text/plain, with comments)
"""

from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

import pytest
import responses
from rest_framework.test import APIClient

from core import factories
from core.services.proconnect import (
    authorized_domains,
    build_proconnect_allowlist,
    domain_bucket,
    get_prevalidated_fqdns,
    org_rpnt_valid_domains,
    proconnect_domains,
    render_proconnect_allowlist_yaml,
    slugify_org_domain,
    update_proconnect_domains,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_allowlist_cache():
    """Keep the 60s allowlist cache from leaking between tests."""
    cache.clear()
    yield
    cache.clear()


def _proconnect_service(idp_id):
    return factories.ServiceFactory(type="proconnect", config={"idp_id": idp_id})


def _active_subscription(service, operator, domains, departement="01"):
    org = factories.OrganizationFactory(departement_code_insee=departement)
    factories.OperatorOrganizationRoleFactory(operator=operator, organization=org)
    factories.ServiceSubscriptionFactory(
        organization=org,
        service=service,
        operator=operator,
        metadata={"domains": domains},
        is_active=True,
    )
    return org


def _domains(entry):
    return [item["domain"] for item in entry["allowed_fqdns"]]


# --- slugify -----------------------------------------------------------------


def test_slugify_org_domain_basic():
    assert slugify_org_domain("Castellet-en-Luberon") == "castellet-en-luberon.fr"


def test_slugify_org_domain_strips_accents_and_spaces():
    assert slugify_org_domain("Saint-Étienne du Rouvray") == "saint-etienne-du-rouvray.fr"


def test_slugify_org_domain_empty():
    assert slugify_org_domain("") is None
    assert slugify_org_domain("   ") is None


# --- RPNT-valid domains (used to compute the dpnt cache) ---------------------


def test_org_rpnt_valid_domains_website_only():
    org = factories.OrganizationFactory(
        rpnt=["1.1"], site_internet="https://www.mairie-a.fr", adresse_messagerie=None
    )
    assert org_rpnt_valid_domains(org) == {"mairie-a.fr"}


def test_org_rpnt_valid_domains_email_needs_both_criteria():
    org_ok = factories.OrganizationFactory(
        rpnt=["2.1", "2.2"], adresse_messagerie="contact@b.fr", site_internet=None
    )
    assert org_rpnt_valid_domains(org_ok) == {"b.fr"}

    org_partial = factories.OrganizationFactory(
        rpnt=["2.1"], adresse_messagerie="contact@c.fr", site_internet=None
    )
    assert org_rpnt_valid_domains(org_partial) == set()


# --- allowlist building ------------------------------------------------------


def test_build_allowlist_includes_routed_domains():
    operator = factories.OperatorFactory()
    service = _proconnect_service("idp-x")
    _active_subscription(service, operator, ["a.fr", "b.fr"])

    entries = build_proconnect_allowlist()
    assert len(entries) == 1
    assert entries[0]["uid"] == "idp-x"
    assert _domains(entries[0]) == ["a.fr", "b.fr"]
    assert all(i["source"] == "routed" for i in entries[0]["allowed_fqdns"])


def test_build_allowlist_covers_whole_departement_with_sources():
    # The operator's declared config["departements"] is the reference scope.
    operator = factories.OperatorFactory(config={"departements": ["42"]})
    service = _proconnect_service("idp-x")
    factories.OperatorServiceConfigFactory(operator=operator, service=service)

    # Org A in the covered département contributes via "dpnt".
    factories.OrganizationFactory(
        departement_code_insee="42", proconnect_domains={"dpnt": ["a.fr"]}
    )
    # Org B in the same département contributes via "candidates".
    factories.OrganizationFactory(
        departement_code_insee="42", proconnect_domains={"candidates": ["b.fr"]}
    )
    # Org C in another département -> excluded.
    factories.OrganizationFactory(
        departement_code_insee="99", proconnect_domains={"dpnt": ["c.fr"]}
    )

    # A managed org outside the declared départements is also in scope.
    managed = factories.OrganizationFactory(
        departement_code_insee="99", proconnect_domains={"dpnt": ["managed.fr"]}
    )
    factories.OperatorOrganizationRoleFactory(operator=operator, organization=managed)

    entries = build_proconnect_allowlist()
    by_domain = {i["domain"]: i["source"] for i in entries[0]["allowed_fqdns"]}
    assert by_domain.get("a.fr") == "DILA"
    assert by_domain.get("b.fr") == "candidates"
    assert by_domain.get("managed.fr") == "DILA"  # managed org, dept not declared
    assert "c.fr" not in by_domain


def test_build_allowlist_source_priority_prefers_dila():
    operator = factories.OperatorFactory(config={"departements": ["01"]})
    service = _proconnect_service("idp-x")
    factories.OperatorServiceConfigFactory(operator=operator, service=service)
    factories.OrganizationFactory(
        departement_code_insee="01",
        proconnect_domains={"dpnt": ["x.fr"], "candidates": ["x.fr"], "manual": ["x.fr"]},
    )

    entries = build_proconnect_allowlist()
    by_domain = {i["domain"]: i["source"] for i in entries[0]["allowed_fqdns"]}
    assert by_domain["x.fr"] == "DILA"


def test_build_allowlist_discard_excludes_candidates_but_not_dila():
    """A discarded candidates domain is excluded; a discarded DILA (dpnt) domain stays."""
    operator = factories.OperatorFactory(config={"departements": ["01"]})
    service = _proconnect_service("idp-x")
    factories.OperatorServiceConfigFactory(operator=operator, service=service)
    factories.OrganizationFactory(
        departement_code_insee="01",
        proconnect_domains={
            "dpnt": ["dila.fr"],
            "candidates": ["extra.fr"],
            "discarded": ["dila.fr", "extra.fr"],
        },
    )

    entries = build_proconnect_allowlist()
    domains = {i["domain"] for i in entries[0]["allowed_fqdns"]}
    assert "dila.fr" in domains  # DILA is authoritative, discard has no effect
    assert "extra.fr" not in domains  # candidates is discardable


def test_build_allowlist_discard_excludes_manual():
    """A discarded manual domain is excluded from the allowlist too."""
    operator = factories.OperatorFactory(config={"departements": ["01"]})
    service = _proconnect_service("idp-x")
    factories.OperatorServiceConfigFactory(operator=operator, service=service)
    factories.OrganizationFactory(
        departement_code_insee="01",
        proconnect_domains={"manual": ["m.fr", "keep.fr"], "discarded": ["m.fr"]},
    )

    entries = build_proconnect_allowlist()
    domains = {i["domain"] for i in entries[0]["allowed_fqdns"]}
    assert "keep.fr" in domains
    assert "m.fr" not in domains


def test_build_allowlist_keeps_routed_domain_even_when_discarded():
    """A currently-routed (live) domain stays in the allowlist even if discarded —
    routing is what the provider is actually using, stronger than any discard."""
    operator = factories.OperatorFactory(config={"departements": ["01"]})
    service = _proconnect_service("idp-x")
    factories.OperatorServiceConfigFactory(operator=operator, service=service)
    org = _active_subscription(service, operator, ["live.fr"], departement="01")
    # A superuser discards the very domain that is currently routed/live.
    org.proconnect_domains = {"discarded": ["live.fr"]}
    org.save(update_fields=["proconnect_domains"])

    entries = build_proconnect_allowlist()
    domains = {item["domain"] for entry in entries for item in entry["allowed_fqdns"]}
    assert "live.fr" in domains


def test_build_allowlist_routed_domain_does_not_leak_across_idps():
    """A domain routed to idp-a must not appear in idp-b's allowlist, even when the
    same operator manages the org for both providers."""
    operator = factories.OperatorFactory(config={"departements": []})
    svc_a = _proconnect_service("idp-a")
    svc_b = _proconnect_service("idp-b")
    factories.OperatorServiceConfigFactory(operator=operator, service=svc_a)
    factories.OperatorServiceConfigFactory(operator=operator, service=svc_b)
    org = factories.OrganizationFactory()
    factories.OperatorOrganizationRoleFactory(operator=operator, organization=org)
    # org routes onlya.fr to idp-a only.
    factories.ServiceSubscriptionFactory(
        organization=org,
        service=svc_a,
        operator=operator,
        metadata={"domains": ["onlya.fr"]},
        is_active=True,
    )

    by_uid = {
        entry["uid"]: {item["domain"] for item in entry["allowed_fqdns"]}
        for entry in build_proconnect_allowlist()
    }
    assert "onlya.fr" in by_uid.get("idp-a", set())
    assert "onlya.fr" not in by_uid.get("idp-b", set())


def test_domain_bucket_rejects_non_hostname_values():
    """Junk (spaces, newlines, YAML metachars) is dropped from buckets so it can
    never be injected into the allowlist YAML."""
    org = factories.OrganizationFactory(
        proconnect_domains={
            "manual": ["ok.fr", "UP.FR", "bad domain.fr", "evil.fr\n      - x.fr"]
        }
    )
    assert domain_bucket(org, "manual") == ["ok.fr", "up.fr"]


def test_update_proconnect_domains_promotes_domain_to_dpnt_only():
    """A domain entering dpnt is stripped from manual/requested/candidates — the
    end state after the dpnt import declares it on service-public.gouv.fr."""
    org = factories.OrganizationFactory(
        proconnect_domains={
            "manual": ["x.fr", "keep-manual.fr"],
            "requested": ["x.fr"],
            "candidates": ["x.fr", "keep-cand.fr"],
        }
    )
    update_proconnect_domains(org, dpnt=["x.fr"])

    buckets = proconnect_domains(org)
    assert buckets["dpnt"] == ["x.fr"]
    assert buckets["manual"] == ["keep-manual.fr"]
    assert buckets["requested"] == []
    assert buckets["candidates"] == ["keep-cand.fr"]


def test_build_allowlist_uses_effective_idp_override():
    """The allowlist is keyed by the *effective* idp (operator override wins),
    matching what the push path (subscription_idp_id) actually targets."""
    operator = factories.OperatorFactory(config={"departements": ["45"]})
    service = _proconnect_service("base-idp")
    factories.OperatorServiceConfigFactory(
        operator=operator, service=service, config_override={"idp_id": "override-idp"}
    )
    factories.OrganizationFactory(
        departement_code_insee="45", proconnect_domains={"manual": ["x.fr"]}
    )

    entries = build_proconnect_allowlist()
    uids = {entry["uid"] for entry in entries}
    assert "override-idp" in uids
    assert "base-idp" not in uids
    entry = next(e for e in entries if e["uid"] == "override-idp")
    assert "x.fr" in {item["domain"] for item in entry["allowed_fqdns"]}


def test_authorized_domains_keeps_dila_ignores_effective_discard():
    """authorized_domains = (manual ∪ dpnt ∪ candidates) − (discarded − dpnt)."""
    org = factories.OrganizationFactory(
        proconnect_domains={
            "manual": ["m.fr"],
            "candidates": ["c.fr"],
            "dpnt": ["d.fr"],
            # d.fr is DILA -> its discard is ignored; c.fr is genuinely discarded.
            "discarded": ["c.fr", "d.fr"],
        }
    )
    assert authorized_domains(org) == ["d.fr", "m.fr"]


# --- YAML rendering ----------------------------------------------------------


def test_render_yaml_with_comments():
    entries = [
        {
            "uid": "x",
            "allowed_fqdns": [
                {"domain": "a.fr", "source": "DILA", "service_public_url": "https://sp.fr/a"},
                {"domain": "b.fr", "source": "candidates", "service_public_url": None},
            ],
        },
        {"uid": "y", "allowed_fqdns": []},
    ]
    expected = (
        "oidc_providers:\n"
        '  - uid: "x"\n'
        "    allowed_fqdns:\n"
        "      - a.fr  # Source: DILA | https://sp.fr/a\n"
        "      - b.fr  # Source: candidates\n"
        '  - uid: "y"\n'
        "    allowed_fqdns: []\n"
    )
    assert render_proconnect_allowlist_yaml(entries) == expected


# --- proconnect_regen_candidate_domains command --------------------------------------


def test_suggest_domains_command_populates_candidates():
    org = factories.OrganizationFactory(name="Ville A", departement_code_insee="45")
    call_command("proconnect_regen_candidate_domains")
    org.refresh_from_db()
    assert set(domain_bucket(org, "candidates")) == {
        "ville-a.fr",
        "ville-a45.fr",
        "mairie-ville-a.fr",
        "ville-ville-a.fr",
    }


def test_suggest_domains_command_adds_fr_variants():
    """When {slug}.fr is not the org's DILA domain, add mairie-/ville-/{slug}{dept}."""
    org = factories.OrganizationFactory(name="Aiglun", departement_code_insee="06")
    call_command("proconnect_regen_candidate_domains")
    org.refresh_from_db()
    assert set(domain_bucket(org, "candidates")) == {
        "aiglun.fr",
        "aiglun06.fr",
        "mairie-aiglun.fr",
        "ville-aiglun.fr",
    }


def test_suggest_domains_command_no_candidate_when_dila_has_slug():
    """When {slug}.fr is already the org's DILA domain: no .fr variants, and
    {slug}.fr itself is not re-proposed as a candidate (it is already authoritative)."""
    org = factories.OrganizationFactory(
        name="Aiglun",
        departement_code_insee="06",
        proconnect_domains={"dpnt": ["aiglun.fr"]},
    )
    call_command("proconnect_regen_candidate_domains")
    org.refresh_from_db()
    assert domain_bucket(org, "candidates") == []


def test_suggest_domains_command_adds_bzh_for_brittany():
    """Breton départements (22/29/35/44/56) also get a {slug}.bzh suggestion."""
    org = factories.OrganizationFactory(
        name="Brest",
        departement_code_insee="29",
        # brest.fr is the DILA domain -> dropped + no .fr variants; .bzh still stands.
        proconnect_domains={"dpnt": ["brest.fr"]},
    )
    call_command("proconnect_regen_candidate_domains")
    org.refresh_from_db()
    assert domain_bucket(org, "candidates") == ["brest.bzh"]


def test_suggest_domains_command_adds_regional_and_om_extensions():
    """Réunion (974) gets .re, Corsica (2A) gets .corsica, in addition to .fr."""
    # {slug}.fr is each org's DILA domain -> dropped + no .fr variants; the
    # regional/OM extension still stands.
    reunion = factories.OrganizationFactory(
        name="Saint-Denis",
        departement_code_insee="974",
        proconnect_domains={"dpnt": ["saint-denis.fr"]},
    )
    corsica = factories.OrganizationFactory(
        name="Ajaccio",
        departement_code_insee="2A",
        proconnect_domains={"dpnt": ["ajaccio.fr"]},
    )
    call_command("proconnect_regen_candidate_domains")
    reunion.refresh_from_db()
    corsica.refresh_from_db()
    assert domain_bucket(reunion, "candidates") == ["saint-denis.re"]
    assert domain_bucket(corsica, "candidates") == ["ajaccio.corsica"]


def test_suggest_domains_command_only_communes():
    """EPCIs (and other non-commune types) get no candidates suggestion."""
    epci = factories.OrganizationFactory(
        name="CC Test", type="epci", departement_code_insee="45"
    )
    call_command("proconnect_regen_candidate_domains")
    epci.refresh_from_db()
    assert domain_bucket(epci, "candidates") == []


def test_suggest_domains_command_preserves_other_buckets():
    org = factories.OrganizationFactory(
        name="Aiglun",
        departement_code_insee="06",
        proconnect_domains={"manual": ["manual.fr"]},
    )
    call_command("proconnect_regen_candidate_domains")
    org.refresh_from_db()
    expected = {"aiglun.fr", "aiglun06.fr", "mairie-aiglun.fr", "ville-aiglun.fr"}
    assert set(domain_bucket(org, "candidates")) == expected
    assert domain_bucket(org, "manual") == ["manual.fr"]

    # Idempotent.
    call_command("proconnect_regen_candidate_domains")
    org.refresh_from_db()
    assert set(domain_bucket(org, "candidates")) == expected
    assert domain_bucket(org, "manual") == ["manual.fr"]


def test_suggest_domains_command_skips_when_rpnt_complete():
    """No candidates suggestion for an org that already satisfies the full RPNT set."""
    org = factories.OrganizationFactory(
        name="Ville A",
        departement_code_insee="45",
        rpnt=["1.1", "1.2", "2.1", "2.2", "2.3"],
    )
    call_command("proconnect_regen_candidate_domains")
    org.refresh_from_db()
    assert domain_bucket(org, "candidates") == []


def test_suggest_domains_command_skips_discarded():
    """A discarded slug is never re-suggested into candidates (per TLD)."""
    org = factories.OrganizationFactory(
        name="Brest",
        departement_code_insee="29",
        # dpnt has brest.fr -> no .fr variants; brest.fr also discarded.
        proconnect_domains={"discarded": ["brest.fr"], "dpnt": ["brest.fr"]},
    )
    call_command("proconnect_regen_candidate_domains")
    org.refresh_from_db()
    # .fr discarded, but the Breton .bzh is still suggested.
    assert domain_bucket(org, "candidates") == ["brest.bzh"]
    assert domain_bucket(org, "discarded") == ["brest.fr"]


def test_suggest_domains_command_dry_run_does_not_write():
    org = factories.OrganizationFactory(name="Ville A", departement_code_insee="45")
    out = StringIO()
    call_command("proconnect_regen_candidate_domains", "--dry-run", stdout=out)
    org.refresh_from_db()
    assert domain_bucket(org, "candidates") == []
    assert "ville-a.fr" in out.getvalue()


def test_suggest_domains_command_filters_by_operator():
    op1 = factories.OperatorFactory()
    op2 = factories.OperatorFactory()
    org1 = factories.OrganizationFactory(name="Ville One", departement_code_insee="45")
    org2 = factories.OrganizationFactory(name="Ville Two", departement_code_insee="45")
    factories.OperatorOrganizationRoleFactory(operator=op1, organization=org1)
    factories.OperatorOrganizationRoleFactory(operator=op2, organization=org2)

    call_command("proconnect_regen_candidate_domains", "--operator", str(op1.id))

    org1.refresh_from_db()
    org2.refresh_from_db()
    assert set(domain_bucket(org1, "candidates")) == {
        "ville-one.fr",
        "ville-one45.fr",
        "mairie-ville-one.fr",
        "ville-ville-one.fr",
    }
    assert domain_bucket(org2, "candidates") == []


# --- public allowlist API route ----------------------------------------------


def test_allowlist_api_route_serves_yaml_text_plain():
    operator = factories.OperatorFactory()
    service = _proconnect_service("idp-x")
    factories.OperatorServiceConfigFactory(operator=operator, service=service)
    org = _active_subscription(service, operator, ["sub.fr"], departement="42")
    org.service_public_url = "https://service-public.fr/org"
    org.proconnect_domains = {"dpnt": ["dila.fr"], "manual": ["manual.fr"]}
    org.save()

    response = APIClient().get(reverse("api-proconnect-allowlist"))
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    assert body.startswith("oidc_providers:")
    assert 'uid: "idp-x"' in body
    assert "- sub.fr" in body  # routed
    assert "- dila.fr" in body  # dpnt (DILA) cache
    assert "- manual.fr" in body  # manual
    assert "# Source: DILA | https://service-public.fr/org" in body


def test_allowlist_api_route_is_public():
    """No authentication required."""
    _proconnect_service("idp-x")
    response = APIClient().get(reverse("api-proconnect-allowlist"))
    assert response.status_code == 200


def test_allowlist_api_route_is_cached():
    """The expensive build runs once and is served from cache for 60s."""
    client = APIClient()
    with mock.patch(
        "core.api.viewsets.proconnect.build_proconnect_allowlist",
        return_value=[],
    ) as build_mock:
        client.get(reverse("api-proconnect-allowlist"))
        client.get(reverse("api-proconnect-allowlist"))
    assert build_mock.call_count == 1


# --- proconnect_fetch_prevalidated command -----------------------------------


@responses.activate
def test_fetch_prevalidated_caches_per_idp_allowlist():
    """The command caches each provider's allowed fqdns (empty set stays defined)."""
    yaml_text = (
        "oidc_providers:\n"
        '  - uid: "idp-x"\n'
        "    allowed_fqdns:\n"
        "      - b.fr\n"
        "      - a.fr\n"  # stored normalized + sorted
        '  - uid: "idp-y"\n'
        "    allowed_fqdns: []\n"
    )
    responses.add(
        responses.GET, "https://allowlist.test/x.yaml", body=yaml_text, status=200
    )

    out = StringIO()
    call_command(
        "proconnect_fetch_prevalidated",
        "--url",
        "https://allowlist.test/x.yaml",
        stdout=out,
    )

    assert get_prevalidated_fqdns("idp-x") == ["a.fr", "b.fr"]
    assert get_prevalidated_fqdns("idp-y") == []  # empty but DEFINED
    assert get_prevalidated_fqdns("idp-z") is None  # never seen → unknown


@responses.activate
def test_fetch_prevalidated_raises_on_fetch_error():
    responses.add(responses.GET, "https://allowlist.test/x.yaml", status=500)
    with pytest.raises(CommandError):
        call_command(
            "proconnect_fetch_prevalidated", "--url", "https://allowlist.test/x.yaml"
        )
