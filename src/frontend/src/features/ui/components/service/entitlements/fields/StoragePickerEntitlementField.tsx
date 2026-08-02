import {
  Button,
  Input,
  Modal,
  ModalProps,
  ModalSize,
  Select,
  useModal,
} from "@openfun/cunningham-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ServiceAttribute } from "@/features/ui/components/service/ServiceAttribute";
import { Spinner } from "@gouvfr-lasuite/ui-kit";
import { Entitlement } from "@/features/api/Repository";
import { errorToString } from "@/features/api/APIError";
import { ServiceBlockEntitlementFieldProps } from "@/features/ui/components/service/entitlements/ServiceBlockEntitlements";

// Order matters ! The biggest unit should be first.
const UNITS = {
  TB: 1000 * 1000 * 1000 * 1000,
  GB: 1000 * 1000 * 1000,
  MB: 1000 * 1000,
} as const;
type StorageUnit = keyof typeof UNITS;

const asStorageBytes = (value: unknown) =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;

/**
 * Gets the value and unit from a number of bytes.
 * The exact byte value is preserved when switching units.
 * The unit is the biggest unit possible if the number is greater than 1, otherwise the smallest unit.
 */
const fromBytes = (bytes: number): [number, StorageUnit] => {
  let out: [number, StorageUnit] | undefined = undefined;
  for (const unit in UNITS) {
    const multiplier = UNITS[unit as keyof typeof UNITS];
    if (bytes / multiplier >= 1) {
      out = [bytes / multiplier, unit as StorageUnit];
      break;
    }
  }
  if (!out) {
    // If no unit is found, use the last unit.
    const lastUnit = Object.keys(UNITS)[Object.keys(UNITS).length - 1];
    const lastUnitMultiplier = UNITS[lastUnit as keyof typeof UNITS];
    out = [bytes / lastUnitMultiplier, lastUnit as StorageUnit];
  }
  return out;
};

/**
 * Gets the number of bytes from a value and unit.
 */
const toBytes = (value: string, unit: StorageUnit) => {
  const bytes = Number(value) * UNITS[unit];
  return value.trim() && Number(value) >= 0 && Number.isSafeInteger(bytes)
    ? bytes
    : null;
};

/**
 * Gets the translation prefix for an entitlement field.
 */
const getTranslationPrefix = (
  serviceType: string,
  entitlement: Entitlement,
  fieldName: string
) => {
  return `organizations.services.types.${serviceType}.entitlements.${entitlement.type}.${fieldName}.${entitlement.account_type}`;
};

export const StoragePickerEntitlementField = (
  props: ServiceBlockEntitlementFieldProps
) => {
  const { t } = useTranslation();
  const bytes = asStorageBytes(props.entitlement.config.max_storage);
  const converted = useMemo(
    () => (bytes === null ? null : fromBytes(bytes)),
    [bytes]
  );

  const modal = useModal({
    isOpenDefault: false,
  });
  const translationPrefix = getTranslationPrefix(
    props.service.type,
    props.entitlement,
    props.fieldName
  );
  return (
    <>
      {/* The goal is reset the modal state when the component is unmounted */}
      {modal.isOpen && (
        <StoragePickerEntitlementFieldModal {...modal} {...props} />
      )}
      <ServiceAttribute
        name={t(`${translationPrefix}.label`)}
        value={
          converted === null
            ? t(
                "organizations.services.entitlements.fields.storage_picker.invalid_persisted_value"
              )
            : converted[0]
              ? `${converted[0]} ${converted[1]}`
              : t(`${translationPrefix}.zero_value`)
        }
        onClick={() => modal.open()}
        interactive={true}
      />
    </>
  );
};

const StoragePickerEntitlementFieldModal = (
  props: ServiceBlockEntitlementFieldProps &
    Pick<ModalProps, "isOpen" | "onClose">
) => {
  const translationPrefix = getTranslationPrefix(
    props.service.type,
    props.entitlement,
    props.fieldName
  );
  const { t } = useTranslation();

  const bytes = asStorageBytes(props.entitlement.config.max_storage);
  const [initialValue, initialUnit] =
    bytes === null ? ["", "MB" as StorageUnit] : fromBytes(bytes);

  const [value, setValue] = useState(String(initialValue));
  const [unit, setUnit] = useState(initialUnit);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const newStorageValue = toBytes(value, unit);

  const submit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (newStorageValue === null) return;
    setIsLoading(true);
    setError(null);

    // Use the subscription API with entitlements - works for both new and existing subscriptions
    props.onChangeSubscription(
      {
        is_active: props.service.subscription?.is_active ?? false,
        entitlements: [
          {
            type: props.entitlement.type,
            account_type: props.entitlement.account_type,
            config: { max_storage: newStorageValue },
          },
        ],
      },
      {
        onSuccess: () => {
          setIsLoading(false);
          props.onClose();
        },
        onError: (error) => {
          setIsLoading(false);
          setError(errorToString(error));
        },
      }
    );
  };

  return (
    <Modal
      size={ModalSize.SMALL}
      title={t(`${translationPrefix}.modal.title`)}
      closeOnEsc={true}
      closeOnClickOutside={true}
      rightActions={
        <>
          <Button
            type="button"
            onClick={props.onClose}
            color="secondary"
            disabled={isLoading}
          >
            {t("common.cancel")}
          </Button>
          <Button
            type="submit"
            form="storage-picker-form"
            disabled={isLoading || newStorageValue === null}
            icon={isLoading ? <Spinner /> : undefined}
            iconPosition="right"
          >
            {t("common.save")}
          </Button>
        </>
      }
      {...props}
    >
      <div className="dc__service__attribute__modal__content">
        <p className="dc__service__attribute__modal__content__help">
          {t(`${translationPrefix}.modal.description`)}
        </p>

        <form
          id="storage-picker-form"
          onSubmit={submit}
          className="dc__service__attribute__modal__content__storage-picker__inputs"
        >
          <Input
            label={t(
              `organizations.services.entitlements.fields.storage_picker.input_placeholder`
            )}
            type="number"
            min="0"
            step="any"
            required
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              setError(null);
            }}
          />
          <Select
            label={t(
              `organizations.services.entitlements.fields.storage_picker.unit_placeholder`
            )}
            value={unit}
            clearable={false}
            onChange={(e) => {
              setUnit(e.target.value as StorageUnit);
              setError(null);
            }}
            options={Object.keys(UNITS).map((unit) => ({
              label: unit,
              value: unit,
            }))}
          />
        </form>
        {value && newStorageValue === null && (
          <p className="dc__service__attribute__modal__content__storage-picker__error">
            {t(
              "organizations.services.entitlements.fields.storage_picker.invalid_value"
            )}
          </p>
        )}
        {error && (
          <p
            className="dc__service__attribute__modal__content__storage-picker__error"
            role="alert"
          >
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
};
