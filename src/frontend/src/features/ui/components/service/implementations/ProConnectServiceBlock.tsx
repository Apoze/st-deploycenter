import {
  MailDomainStatus,
  Organization,
  Service,
} from "@/features/api/Repository";
import { useAuth } from "@/features/auth/Auth";
import { useMemo } from "react";
import {
  ServiceBlock,
  useServiceBlock,
} from "@/features/ui/components/service/ServiceBlock";
import {
  Button,
  useModal,
} from "@openfun/cunningham-react";
import { ServiceAttribute } from "../ServiceAttribute";
import { Icon, IconSize } from "@gouvfr-lasuite/ui-kit";
import { DomainMultiSelectModal } from "../DomainMultiSelectModal";
import { MutateOptions } from "@tanstack/react-query";

/**
 * ProConnect status message. Same for every user; superusers just additionally
 * get the domain editor (an action button), not a different message.
 */

type ProConnectMessage = {
  text?: React.ReactNode;
  alert?: React.ReactNode;
  icon?: string;
  disabled?: boolean;
};

const RPNT_REFERENTIEL_URL =
  "https://suiteterritoriale.anct.gouv.fr/conformite/referentiel";

const getProConnectMessage = (
  organization: Organization,
  subscriptionDomains: string[] | null,
  isActive: boolean
): ProConnectMessage => {
  // Active subscription: the selected domains are routed to the FI.
  if (isActive && subscriptionDomains && subscriptionDomains.length > 0) {
    const plural = subscriptionDomains.length > 1;
    const message: ProConnectMessage = {
      text: (
        <span>
          {plural ? "Les domaines " : "Le domaine "}
          <b>{subscriptionDomains.join(", ")}</b>
          {plural ? " sont routés" : " est routé"} vers ce FI.
        </span>
      ),
    };
    const conformant =
      organization.type === "other" ||
      (organization.mail_domain_status === MailDomainStatus.VALID &&
        subscriptionDomains[0] === organization.mail_domain);
    if (!conformant) {
      message.alert = (
        <span>
          Ce domaine n&apos;est pas déclaré sur Service-Public.gouv.fr.{" "}
          <a href={`${RPNT_REFERENTIEL_URL}#2.1`} target="_blank" rel="noopener noreferrer">
            Mettez-le à jour
          </a>{" "}
          pour assurer la conformité au RPNT.
        </span>
      );
      message.icon = "warning";
    }
    return message;
  }

  // No usable domain: one must be declared before activation.
  if (organization.mail_domain_status === MailDomainStatus.INVALID) {
    return {
      alert: (
        <span>
          Aucun nom de domaine valide n&apos;est connu. Vous devez d&apos;abord en{" "}
          <a href={`${RPNT_REFERENTIEL_URL}#1.1`} target="_blank" rel="noopener noreferrer">
            déclarer un
          </a>
          .
        </span>
      ),
      icon: "warning",
      disabled: true,
    };
  }

  // A valid domain is available and will be routed on activation.
  return {
    text: (
      <span>
        Le domaine <b>{organization.mail_domain}</b> sera routé vers ce FI.
      </span>
    ),
    icon: "info",
  };
};

/**
 * Handles the ProConnect service block.
 *
 * IDP is now stored in service.config.idp_id (immutable per service)
 * and displayed as read-only.
 */
export const ProConnectServiceBlock = (props: {
  service: Service;
  organization: Organization;
}) => {
  const { user } = useAuth();
  const isSuperUser = user?.is_superuser ?? false;
  const blockProps = useServiceBlock(props.service, props.organization);
  const subscription = props.service.subscription;
  const domainModal = useModal();

  const idpId = props.service.config?.idp_id;

  // Get domains from subscription metadata if available
  const subscriptionDomains = useMemo(() => {
    const domains = subscription?.metadata?.domains;
    if (domains && Array.isArray(domains) && domains.length > 0) {
      return domains;
    }
    return null;
  }, [subscription?.metadata?.domains]);

  const domains = subscriptionDomains ?? [];

  const handleDomainsChange = (
    newDomains: string[],
    options?: MutateOptions<unknown, unknown, unknown, unknown>
  ) => {
    blockProps.onChangeSubscription(
      {
        metadata: {
          ...subscription?.metadata,
          domains: newDomains,
        },
      },
      options
    );
  };

  const message = getProConnectMessage(
    props.organization,
    subscriptionDomains,
    subscription?.is_active || false
  );

  // Activation requires an IDP to be configured on the service
  const canActivateSubscription = async () => {
    if (message.disabled && !isSuperUser) {
      return false;
    }
    if (!idpId) {
      return false;
    }
    return true;
  };

  return (
    <ServiceBlock
      {...blockProps}
      showGoto={false}
      confirmationText={<>
        <span>En activant ProConnect, vous garantissez que :</span>
        <ul>
          <li>l&apos;annuaire <b>complet</b> des utilisateurs de ce domaine est présent dans le FI sélectionné,</li>
          <li>les utilisateurs sont capables de se connecter à leur compte,</li>
          <li>des procédures sont en place pour maintenir cet annuaire à jour.</li>
        </ul>
      </>}
      canActivateSubscription={canActivateSubscription}
      content={
        <>
          <form>
            <div className="dc__service__attribute__container">

              {domainModal.isOpen && (
                <DomainMultiSelectModal
                  {...domainModal}
                  organization={props.organization}
                  instanceName={props.service.instance_name}
                  idpId={props.service.config?.idp_id}
                  isActive={subscription?.is_active ?? false}
                  selected={domains}
                  onSave={handleDomainsChange}
                />
              )}
              <ServiceAttribute
                name="Domaines"
                interactive={!blockProps.isManagedByOtherOperator}
                onClick={() => domainModal.open()}
                value={
                  domains.length > 0
                    ? <span className="dc__domains-list">
                        {domains.map((domain) => (
                          <span key={domain}>{domain}</span>
                        ))}
                      </span>
                    : "Aucun"
                }
              />

              {message.text && <ServiceAttribute>
                <div className="dc__service__attribute_text">{message.text}</div>
              </ServiceAttribute>}

              {message.alert && message.icon && <div className={message.icon == "warning" ? "dc__service__warning" : "dc__service__info"}>
                  <Icon name={message.icon} size={IconSize.SMALL} />
                  {message.alert}
              </div>}

            </div>
          </form>
          {props.service.config?.help_center_url && (
            <div className="dc__service__block__goto">
              <a target="_blank" href={props.service.config?.help_center_url}>
                Centre de ressources
              </a>
              <Button
                color="tertiary"
                size="nano"
                href={props.service.config?.help_center_url}
                target="_blank"
                icon={<Icon name="open_in_new" size={IconSize.X_SMALL} />}
              ></Button>
            </div>
          )}
        </>
      }
    />
  );
};
