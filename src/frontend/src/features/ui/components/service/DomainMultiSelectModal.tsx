import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Icon, Spinner } from "@gouvfr-lasuite/ui-kit";
import { Button, Input, Modal, ModalSize } from "@openfun/cunningham-react";
import { MutateOptions } from "@tanstack/react-query";
import { Organization } from "@/features/api/Repository";
import { errorToString } from "@/features/api/APIError";
import { useAuth } from "@/features/auth/Auth";
import { useOperatorContext } from "@/features/layouts/components/GlobalLayout";
import { useMutationUpdateOrganizationProconnectDomains } from "@/hooks/useQueries";

/**
 * The ProConnect domains manager.
 *
 * Combines routing selection (which of the organization's available domains are
 * routed to this FI) with domain management:
 * - each row shows the domain and its source(s) (routed / DILA / candidat / manuel / demandé)
 * - superusers can add a manual domain, validate/reject requested ones, delete manual ones
 * - non-superusers can request ("ask") a new domain for a superuser to validate
 *
 * Routing edits are saved via `onSave`; domain-management edits are persisted
 * immediately (they mutate the organization's proconnect_domains buckets).
 */
export type DomainMultiSelectModalProps = {
  isOpen: boolean;
  onClose: () => void;
  organization: Organization;
  instanceName: string;
  idpId?: string;
  // Whether the subscription is active — a "routed" domain is only actually live
  // (en ligne) when it is; otherwise it's configured but offline.
  isActive: boolean;
  selected: string[];
  onSave: (
    domains: string[],
    options?: MutateOptions<unknown, unknown, unknown, unknown>
  ) => void;
};

const SOURCE_LABELS: Record<string, string> = {
  // "routed" is rendered specially (en ligne / hors ligne per is_active).
  routed: "Routé",
  dpnt: "DILA (service-public.gouv.fr)",
  candidates: "Candidat",
  manual: "Manuel",
  requested: "Demandé (à valider)",
  discarded: "Écarté",
};

const badgeStyle: React.CSSProperties = {
  padding: "2px 8px",
  borderRadius: "4px",
  fontSize: "11px",
  background: "var(--c--theme--colors--greyscale-100)",
  border: "1px solid var(--c--theme--colors--greyscale-200)",
  whiteSpace: "nowrap",
};

// Collectivité types that are in scope of the RPNT service-public.gouv.fr declaration.
const COLLECTIVITE_TYPES = ["commune", "epci", "departement", "region"];

type Buckets = Organization["proconnect_domains"];
const BUCKET_ORDER = [
  "dpnt",
  "candidates",
  "manual",
  "requested",
  "discarded",
] as const;

/**
 * Whether a domain is already in the *deployed* ProConnect allowlist.
 * - "unknown": we don't know the deployed allowlist (`_prevalidated` absent).
 * - "prevalidated": listed → routable now.
 * - "not-yet": allowlist known but this domain isn't in it yet (routing would be
 *   rejected until the next allowlist deploy).
 */
type Prevalidation = "unknown" | "prevalidated" | "not-yet";

// Pre-validation is per-idp: look up this modal's provider (idpId) in the map.
const prevalidationStatus = (
  pd: Buckets,
  idpId: string | undefined,
  domain: string
): Prevalidation => {
  const list = idpId ? pd._prevalidated?.[idpId] : undefined;
  if (!list) return "unknown";
  return list.includes(domain) ? "prevalidated" : "not-yet";
};

const PREVALIDATION_PILL: Record<
  Prevalidation,
  { label: string; bg: string; color: string }
> = {
  prevalidated: {
    label: "Pré-validé",
    bg: "var(--c--theme--colors--success-100)",
    color: "var(--c--theme--colors--success-700)",
  },
  "not-yet": {
    label: "Pas encore pré-validé (peut prendre jusqu'à une semaine)",
    bg: "var(--c--theme--colors--warning-100)",
    color: "var(--c--theme--colors--warning-700)",
  },
  unknown: {
    label: "Pré-validation inconnue",
    bg: "var(--c--theme--colors--greyscale-100)",
    color: "var(--c--theme--colors--greyscale-600)",
  },
};

/** Per-domain source breakdown, derived from the raw buckets + routed domains. */
const domainRows = (pd: Buckets, routed: string[]) => {
  const routedSet = new Set(routed);
  const all = new Set<string>(routed);
  BUCKET_ORDER.forEach((key) => pd[key].forEach((d) => all.add(d)));
  return [...all].sort().map((domain) => {
    const sources = routedSet.has(domain) ? ["routed"] : [];
    BUCKET_ORDER.forEach((key) => {
      if (pd[key].includes(domain)) sources.push(key);
    });
    return { domain, sources };
  });
};

/** Routable pool: (manual ∪ dpnt ∪ candidates ∪ routed) minus discarded (dpnt & routed kept). */
const availableDomains = (pd: Buckets, routed: string[]) => {
  const dpnt = new Set(pd.dpnt);
  const routedSet = new Set(routed);
  // A currently-routed domain is live, so it stays routable even if discarded.
  const discarded = new Set(
    pd.discarded.filter((d) => !dpnt.has(d) && !routedSet.has(d))
  );
  const available = new Set<string>();
  [...pd.manual, ...pd.dpnt, ...pd.candidates, ...routed].forEach((d) => {
    if (!discarded.has(d)) available.add(d);
  });
  return available;
};

export const DomainMultiSelectModal = (props: DomainMultiSelectModalProps) => {
  const { t } = useTranslation();
  const { user } = useAuth();
  const isSuperUser = user?.is_superuser ?? false;
  const { operatorId } = useOperatorContext();
  const { mutate: updateProconnectDomains, isPending: isBucketPending } =
    useMutationUpdateOrganizationProconnectDomains();

  const [selected, setSelected] = useState<string[]>(props.selected);
  const [newDomain, setNewDomain] = useState("");
  const [isPending, setIsPending] = useState(false);
  const [showSpinner, setShowSpinner] = useState(false);
  const [saveErrorMessage, setSaveErrorMessage] = useState<string | null>(null);
  const [bucketError, setBucketError] = useState(false);
  const spinnerTimeout = useRef<ReturnType<typeof setTimeout>>(undefined);

  useEffect(() => {
    setSelected(props.selected);
  }, [props.selected]);

  useEffect(() => {
    return () => clearTimeout(spinnerTimeout.current);
  }, []);

  const pd = props.organization.proconnect_domains;
  const rows = domainRows(pd, props.selected);
  const available = availableDomains(pd, props.selected);
  const manual = pd.manual;
  const requested = pd.requested;
  const discarded = pd.discarded;

  const saveDomains = (payload: {
    manual?: string[];
    requested?: string[];
    discarded?: string[];
  }) => {
    setBucketError(false);
    updateProconnectDomains(
      {
        operatorId,
        organizationId: props.organization.id,
        payload,
      },
      { onError: () => setBucketError(true) }
    );
  };

  const handleDiscard = (domain: string) => {
    // Discarding removes the domain from the routable pool, so drop any pending
    // routing selection for it (otherwise Save would route a discarded domain).
    setSelected((prev) => prev.filter((d) => d !== domain));
    saveDomains({ discarded: [...discarded, domain] });
  };

  const handleRestore = (domain: string) =>
    saveDomains({ discarded: discarded.filter((d) => d !== domain) });

  const toggle = (domain: string) =>
    setSelected((prev) =>
      prev.includes(domain)
        ? prev.filter((d) => d !== domain)
        : [...prev, domain]
    );

  const handleAddOrAsk = () => {
    // Guard the Enter-key path too (the button is disabled, but the input isn't),
    // so a pending bucket mutation isn't clobbered by one built from a stale snapshot.
    if (isBucketPending) {
      return;
    }
    const domain = newDomain.trim().toLowerCase();
    if (!domain || !domain.includes(".")) {
      return;
    }
    if (isSuperUser) {
      if (!manual.includes(domain)) {
        saveDomains({ manual: [...manual, domain] });
      }
    } else if (!requested.includes(domain)) {
      saveDomains({ requested: [...requested, domain] });
    }
    setNewDomain("");
  };

  const handleValidateAsk = (domain: string) =>
    saveDomains({
      manual: [...manual, domain],
      requested: requested.filter((d) => d !== domain),
    });

  const handleRejectAsk = (domain: string) =>
    saveDomains({ requested: requested.filter((d) => d !== domain) });

  const handleRemoveManual = (domain: string) => {
    saveDomains({ manual: manual.filter((d) => d !== domain) });
    setSelected((prev) => prev.filter((d) => d !== domain));
  };

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setIsPending(true);
    setSaveErrorMessage(null);
    spinnerTimeout.current = setTimeout(() => setShowSpinner(true), 600);
    props.onSave([...selected].sort(), {
      onSuccess: () => {
        clearTimeout(spinnerTimeout.current);
        setIsPending(false);
        setShowSpinner(false);
        props.onClose();
      },
      onError: (error) => {
        clearTimeout(spinnerTimeout.current);
        setIsPending(false);
        setShowSpinner(false);
        // Surface the backend detail (e.g. ProConnect "fqdn_not_allowed" with the
        // offending domains) instead of a generic message.
        setSaveErrorMessage(errorToString(error));
      },
    });
  };

  const renderRowActions = (domain: string, sources: string[]) => {
    if (!isSuperUser) {
      return null;
    }
    // A currently-routed domain is live: it can't be deleted or discarded until it
    // is un-routed (uncheck it and Save first). This prevents dropping a domain
    // from the buckets while the subscription still routes it.
    const isRouted = sources.includes("routed");
    if (sources.includes("discarded")) {
      return (
        <Button
          type="button"
          size="small"
          color="secondary"
          className="dc__domain-selector__item__delete"
          icon={<Icon name="undo" />}
          title="Rétablir"
          disabled={isBucketPending}
          onClick={() => handleRestore(domain)}
        />
      );
    }
    if (sources.includes("requested")) {
      return (
        <div style={{ display: "flex", gap: "0.5rem", marginLeft: "auto" }}>
          <Button
            type="button"
            size="small"
            icon={<Icon name="check" />}
            title="Valider"
            disabled={isBucketPending}
            onClick={() => handleValidateAsk(domain)}
          />
          <Button
            type="button"
            size="small"
            color="secondary"
            icon={<Icon name="delete" />}
            title="Rejeter"
            disabled={isBucketPending}
            onClick={() => handleRejectAsk(domain)}
          />
        </div>
      );
    }
    if (sources.includes("manual")) {
      return (
        <Button
          type="button"
          size="small"
          color="secondary"
          className="dc__domain-selector__item__delete"
          icon={<Icon name="delete" />}
          title={
            isRouted
              ? "Retirez ce domaine du routage avant de le supprimer"
              : "Supprimer"
          }
          disabled={isBucketPending || isRouted}
          onClick={() => handleRemoveManual(domain)}
        />
      );
    }
    // DILA (dpnt) domains are authoritative and cannot be discarded.
    if (sources.includes("candidates") && !sources.includes("dpnt")) {
      return (
        <Button
          type="button"
          size="small"
          color="secondary"
          className="dc__domain-selector__item__delete"
          icon={<Icon name="close" />}
          title={
            isRouted
              ? "Retirez ce domaine du routage avant de l'écarter"
              : "Écarter ce candidat"
          }
          disabled={isBucketPending || isRouted}
          onClick={() => handleDiscard(domain)}
        />
      );
    }
    return null;
  };

  return (
    <Modal
      size={ModalSize.LARGE}
      title={`Domaines (${props.instanceName})`}
      closeOnEsc={!isPending}
      closeOnClickOutside={!isPending}
      isOpen={props.isOpen}
      onClose={isPending ? () => {} : props.onClose}
      rightActions={
        isSuperUser ? (
          <>
            <Button
              type="button"
              onClick={props.onClose}
              color="secondary"
              disabled={isPending}
            >
              {t("common.cancel")}
            </Button>
            <Button
              type="submit"
              form="domain-multiselect-form"
              disabled={isPending}
              icon={showSpinner ? <Spinner /> : undefined}
            >
              {t("common.save")}
            </Button>
          </>
        ) : (
          <Button type="button" onClick={props.onClose} color="secondary">
            {t("common.cancel")}
          </Button>
        )
      }
    >
      <div className="dc__service__attribute__modal__content">
        <p className="dc__service__attribute__modal__content__help">
          Cochez les domaines à router vers ce fournisseur d’identité. Ces
          domaines doivent d’abord être pré-validés pour une utilisation avec
          ProConnect.
        </p>
        <form id="domain-multiselect-form" onSubmit={handleSubmit}>
          <div className="dc__domain-selector">
            <div className="dc__domain-selector__list">
              {rows.map((row) => {
                const routable = available.has(row.domain);
                const prevalidation = prevalidationStatus(
                  pd,
                  props.idpId,
                  row.domain
                );
                const pill = PREVALIDATION_PILL[prevalidation];
                const isEnLigne =
                  row.sources.includes("routed") && props.isActive;
                // Discarded domains are excluded from the allowlist entirely, so
                // their status is moot; and a live (en ligne) domain doesn't need
                // an "unknown" pre-validation pill (it is already live).
                const showPill =
                  !row.sources.includes("discarded") &&
                  !(isEnLigne && prevalidation === "unknown");
                // A live domain not declared on service-public.gouv.fr (no DILA
                // source) should be declared — only for collectivité org types.
                const needsDilaDeclaration =
                  isEnLigne &&
                  !row.sources.includes("dpnt") &&
                  COLLECTIVITE_TYPES.includes(props.organization.type);
                // A "not yet pre-validated" domain can't be routed (it would be
                // rejected), so its checkbox is disabled — unless it's already
                // selected, so a live domain can still be un-routed.
                const notYetBlocked =
                  prevalidation === "not-yet" && !selected.includes(row.domain);
                return (
                  <div key={row.domain} className="dc__domain-selector__item">
                    <input
                      type="checkbox"
                      checked={selected.includes(row.domain)}
                      disabled={!routable || !isSuperUser || notYetBlocked}
                      onChange={() => toggle(row.domain)}
                    />
                    <span className="dc__domain-selector__item__name">
                      {row.domain}
                    </span>
                    <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                      {row.sources
                        .filter(
                          // A live (routed + active) domain is beyond "candidate"
                          // — don't show that origin badge alongside "en ligne".
                          (source) =>
                            !(
                              source === "candidates" &&
                              row.sources.includes("routed") &&
                              props.isActive
                            )
                        )
                        .map((source) => (
                          <span key={source} style={badgeStyle}>
                            {source === "routed"
                              ? props.isActive
                                ? "Routé (en ligne)"
                                : "Sera routé après activation"
                              : (SOURCE_LABELS[source] ?? source)}
                          </span>
                        ))}
                      {showPill && (
                        <span
                          style={{
                            ...badgeStyle,
                            background: pill.bg,
                            color: pill.color,
                            border: "none",
                          }}
                          title={
                            prevalidation === "not-yet"
                              ? "Pré-validation par ProConnect en cours"
                              : undefined
                          }
                        >
                          {pill.label}
                        </span>
                      )}
                      {needsDilaDeclaration && (
                        <span
                          style={{
                            ...badgeStyle,
                            background:
                              "var(--c--theme--colors--warning-100)",
                            color: "var(--c--theme--colors--warning-700)",
                            border: "none",
                          }}
                          title="Ce domaine routé n'est pas déclaré sur service-public.gouv.fr (conformité RPNT)."
                        >
                          Déclaration sur service-public.gouv.fr à faire
                        </span>
                      )}
                    </div>
                    {renderRowActions(row.domain, row.sources)}
                  </div>
                );
              })}
              {rows.length === 0 && (
                <p className="dc__domain-selector__empty">
                  Aucun domaine connu pour cette collectivité
                </p>
              )}
            </div>
            <div className="dc__domain-selector__add">
              <Input
                label=""
                placeholder={
                  isSuperUser
                    ? "Ajouter un domaine (ex. : exemple.fr)"
                    : "Demander un domaine (ex. : exemple.fr)"
                }
                value={newDomain}
                disabled={isBucketPending}
                onChange={(e) => setNewDomain(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    handleAddOrAsk();
                  }
                }}
              />
              <Button
                type="button"
                color="secondary"
                onClick={handleAddOrAsk}
                disabled={isBucketPending || !newDomain.trim().includes(".")}
              >
                {isSuperUser ? "Ajouter" : "Demander"}
              </Button>
            </div>
            {(saveErrorMessage || bucketError) && (
              <p className="dc__domain-selector__error">
                {saveErrorMessage ?? t("api.error.unexpected")}
              </p>
            )}
          </div>
        </form>
      </div>
    </Modal>
  );
};
