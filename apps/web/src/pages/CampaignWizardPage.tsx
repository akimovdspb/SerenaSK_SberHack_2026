import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, FlaskConical, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import { apiGet, apiMutation } from "../api";
import { ErrorState, LoadingState } from "../components";
import type {
  AuthoringCatalog,
  AuthoringProduct,
  Campaign,
  EditorialReference,
} from "../types";

type ProductMode = "catalog" | "custom";
type Channel = "sms" | "email";
type FactDraft = {
  label: string;
  canonicalText: string;
  kind: string;
  sourceLabel: string;
  exactValue: string;
  unit: string;
  surfaceForms: string[];
};

const FACT_KINDS = [
  ["text", "Текстовый факт"],
  ["number", "Число"],
  ["percentage", "Процент"],
  ["money", "Денежное значение"],
  ["date", "Дата"],
  ["duration", "Период"],
  ["condition", "Условие"],
  ["concept", "Понятие"],
] as const;

function blankFact(): FactDraft {
  return {
    label: "",
    canonicalText: "",
    kind: "text",
    sourceLabel: "Синтетическая карточка продукта",
    exactValue: "",
    unit: "",
    surfaceForms: [],
  };
}

function referenceFact(
  value: NonNullable<EditorialReference["custom_product"]>["facts"][number],
): FactDraft {
  const normalized = value.normalized_value;
  const measure =
    normalized && typeof normalized === "object" && "value" in normalized
      ? (normalized as { value: number; unit: string })
      : null;
  return {
    label: value.label,
    canonicalText: value.canonical_text,
    kind: value.kind,
    sourceLabel: value.source_label,
    exactValue: measure ? String(measure.value) : normalized == null ? "" : String(normalized),
    unit: measure ? measure.unit : "",
    surfaceForms: value.allowed_surface_forms,
  };
}

function normalizedValue(fact: FactDraft): unknown {
  if (fact.kind === "money" || fact.kind === "duration") {
    return { value: Number(fact.exactValue), unit: fact.unit.trim() };
  }
  if (fact.kind === "number" || fact.kind === "percentage") {
    return Number(fact.exactValue);
  }
  return fact.exactValue.trim() || null;
}

export function CampaignWizardPage({ navigate }: { navigate: (path: string) => void }) {
  const catalog = useQuery({
    queryKey: ["authoring-catalog"],
    queryFn: () => apiGet<AuthoringCatalog>("/api/v1/authoring/catalog"),
  });

  if (catalog.isPending) return <LoadingState label="Загружаем безопасный каталог" />;
  if (catalog.isError)
    return <ErrorState error={catalog.error} retry={() => void catalog.refetch()} />;

  const referenceId = new URLSearchParams(window.location.search).get("reference");
  const reference = catalog.data.references.find((item) => item.reference_id === referenceId);
  return (
    <CampaignWizardForm
      catalog={catalog.data}
      key={reference?.reference_id ?? "blank"}
      navigate={navigate}
      reference={reference}
    />
  );
}

function CampaignWizardForm({
  catalog,
  navigate,
  reference,
}: {
  catalog: AuthoringCatalog;
  navigate: (path: string) => void;
  reference: EditorialReference | undefined;
}) {
  const initialProduct = catalog.products.find(
    (item) => item.product_id === reference?.brief.product_id,
  );
  const [step, setStep] = useState(1);
  const [mode, setMode] = useState<ProductMode>(
    reference?.custom_product ? "custom" : "catalog",
  );
  const [productId, setProductId] = useState(initialProduct?.product_id ?? "");
  const [campaignName, setCampaignName] = useState(reference?.brief.name ?? "");
  const [objective, setObjective] = useState(reference?.brief.objective ?? "");
  const [segmentId, setSegmentId] = useState(reference?.brief.segment_id ?? "");
  const [triggerId, setTriggerId] = useState(reference?.brief.trigger_id ?? "");
  const [channels, setChannels] = useState<Channel[]>(
    reference?.brief.channels ?? ["sms", "email"],
  );
  const [tone, setTone] = useState(reference?.brief.tone ?? "");
  const [ctaLabel, setCtaLabel] = useState(
    reference?.brief.cta_label ?? initialProduct?.cta_label ?? "",
  );
  const [ctaUrl, setCtaUrl] = useState(
    reference?.brief.cta_url ?? initialProduct?.cta_url ?? "",
  );
  const [periodStart, setPeriodStart] = useState(
    reference?.brief.offer_period?.start ?? "",
  );
  const [periodEnd, setPeriodEnd] = useState(reference?.brief.offer_period?.end ?? "");
  const [notes, setNotes] = useState(reference?.brief.notes ?? "");
  const [customName, setCustomName] = useState(
    reference?.custom_product?.exact_name ?? "",
  );
  const [facts, setFacts] = useState<FactDraft[]>(
    reference?.custom_product?.facts.map(referenceFact) ?? [blankFact()],
  );
  const [syntheticConfirmed, setSyntheticConfirmed] = useState(false);
  const [noPiiConfirmed, setNoPiiConfirmed] = useState(false);

  const products = catalog.products;
  const personas = catalog.personas;
  const selectedProduct = products.find((item) => item.product_id === productId);
  const selectedPersona = personas.find((item) => item.segment_id === segmentId);

  const applyProduct = (product: AuthoringProduct) => {
    setProductId(product.product_id);
    setCtaLabel(product.cta_label);
    setCtaUrl(product.cta_url);
  };

  const productStepReady = useMemo(() => {
    if (mode === "catalog") return Boolean(selectedProduct);
    return Boolean(
      customName.trim() &&
        ctaLabel.trim() &&
        ctaUrl.startsWith("https://") &&
        facts.length &&
        facts.every((fact) => {
          const needsExact = ["number", "percentage", "money", "duration", "date"].includes(
            fact.kind,
          );
          const needsUnit = fact.kind === "money" || fact.kind === "duration";
          return (
            fact.label.trim() &&
            fact.canonicalText.trim() &&
            fact.sourceLabel.trim() &&
            (!needsExact || fact.exactValue.trim()) &&
            (!needsUnit || fact.unit.trim())
          );
        }) &&
        syntheticConfirmed &&
        noPiiConfirmed,
    );
  }, [
    ctaLabel,
    ctaUrl,
    customName,
    facts,
    mode,
    noPiiConfirmed,
    selectedProduct,
    syntheticConfirmed,
  ]);
  const briefStepReady = Boolean(
    campaignName.trim() &&
      objective.trim() &&
      selectedPersona &&
      triggerId &&
      channels.length &&
      tone.trim(),
  );

  const create = useMutation({
    mutationFn: async () => {
      let effectiveProduct = selectedProduct;
      if (mode === "custom") {
        effectiveProduct = await apiMutation<AuthoringProduct>("/api/v1/authoring/products", {
          exact_name: customName,
          cta_label: ctaLabel,
          cta_url: ctaUrl,
          facts: facts.map((fact) => ({
            label: fact.label,
            canonical_text: fact.canonicalText,
            kind: fact.kind,
            source_label: fact.sourceLabel,
            normalized_value: normalizedValue(fact),
            allowed_surface_forms: fact.surfaceForms,
          })),
          synthetic_confirmed: syntheticConfirmed,
          no_pii_confirmed: noPiiConfirmed,
        });
      }
      if (!effectiveProduct) throw new Error("Выберите продукт");
      const campaign = await apiMutation<Campaign>("/api/v1/campaigns", {
        brief: {
          name: campaignName,
          objective,
          product_id: effectiveProduct.product_id,
          segment_id: segmentId,
          trigger_id: triggerId,
          channels,
          cta_label: effectiveProduct.cta_label,
          cta_url: effectiveProduct.cta_url,
          tone,
          offer_period:
            periodStart || periodEnd
              ? { start: periodStart || null, end: periodEnd || null }
              : null,
          notes: notes || null,
          synthetic: true,
        },
      });
      await apiMutation<Campaign>(`/api/v1/campaigns/${campaign.campaign_id}/validate`);
      return campaign.campaign_id;
    },
    onSuccess: (campaignId) => navigate(`/campaigns/${campaignId}`),
  });

  return (
    <div className="page page-wizard">
      <button className="back-link" onClick={() => navigate("/")} type="button">
        <ArrowLeft size={16} /> К кампаниям
      </button>
      <section className="page-heading wizard-heading">
        <div>
          <p className="eyebrow">Новая коммуникация</p>
          <h1>Соберите бриф кампании</h1>
          <p className="page-lede">
            Три шага: проверенный продукт, аудитория и параметры коммуникации. Все данные
            синтетические, внешняя отправка отключена.
          </p>
        </div>
        <div className="wizard-safety" aria-label="Границы данных">
          <span><FlaskConical size={15} /> Синтетические данные</span>
          <span><ShieldCheck size={15} /> Без отправки</span>
        </div>
      </section>

      {reference ? (
        <div className="reference-prefill-note">
          Начато с редакционного примера «{reference.title}». Бриф можно полностью изменить;
          готовый текст примера не переносится.
        </div>
      ) : null}

      <ol className="wizard-progress" aria-label="Шаги создания">
        {["Продукт", "Бриф", "Проверка"].map((label, index) => {
          const number = index + 1;
          return (
            <li aria-current={step === number ? "step" : undefined} className={step >= number ? "is-active" : ""} key={label}>
              <span>{step > number ? <Check size={15} /> : number}</span>{label}
            </li>
          );
        })}
      </ol>

      <section className="content-card wizard-card">
        {step === 1 ? (
          <ProductStep
            mode={mode}
            setMode={setMode}
            products={products}
            productId={productId}
            applyProduct={applyProduct}
            customName={customName}
            setCustomName={setCustomName}
            ctaLabel={ctaLabel}
            setCtaLabel={setCtaLabel}
            ctaUrl={ctaUrl}
            setCtaUrl={setCtaUrl}
            facts={facts}
            setFacts={setFacts}
            syntheticConfirmed={syntheticConfirmed}
            setSyntheticConfirmed={setSyntheticConfirmed}
            noPiiConfirmed={noPiiConfirmed}
            setNoPiiConfirmed={setNoPiiConfirmed}
          />
        ) : step === 2 ? (
          <BriefStep
            campaignName={campaignName}
            setCampaignName={setCampaignName}
            objective={objective}
            setObjective={setObjective}
            segmentId={segmentId}
            onPersona={(value) => {
              setSegmentId(value);
              const persona = personas.find((item) => item.segment_id === value);
              if (persona) {
                setTriggerId(persona.trigger_id);
                if (!tone) setTone(persona.tone_hint);
                setChannels((current) =>
                  current.filter((channel) => persona.available_channels.includes(channel)),
                );
              }
            }}
            personas={personas}
            selectedPersona={selectedPersona}
            channels={channels}
            setChannels={setChannels}
            tone={tone}
            setTone={setTone}
          />
        ) : (
          <ReviewStep
            productName={mode === "custom" ? customName : selectedProduct?.exact_name ?? ""}
            campaignName={campaignName}
            objective={objective}
            personaLabel={selectedPersona?.label ?? ""}
            channels={channels}
            tone={tone}
            ctaLabel={ctaLabel}
            ctaUrl={ctaUrl}
            periodStart={periodStart}
            setPeriodStart={setPeriodStart}
            periodEnd={periodEnd}
            setPeriodEnd={setPeriodEnd}
            notes={notes}
            setNotes={setNotes}
          />
        )}

        {create.isError ? <div className="inline-error wizard-error" role="alert">{create.error.message}</div> : null}
        <div className="wizard-actions">
          {step > 1 ? (
            <button className="button button-secondary" onClick={() => setStep(step - 1)} type="button">
              <ArrowLeft size={16} /> Назад
            </button>
          ) : <span />}
          {step < 3 ? (
            <button
              className="button button-primary"
              disabled={step === 1 ? !productStepReady : !briefStepReady}
              onClick={() => setStep(step + 1)}
              type="button"
            >
              Продолжить <ArrowRight size={16} />
            </button>
          ) : (
            <button
              className="button button-primary button-prominent"
              disabled={create.isPending}
              onClick={() => create.mutate()}
              type="button"
            >
              {create.isPending ? "Создаём…" : "Создать кампанию"}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}

function ProductStep({
  mode,
  setMode,
  products,
  productId,
  applyProduct,
  customName,
  setCustomName,
  ctaLabel,
  setCtaLabel,
  ctaUrl,
  setCtaUrl,
  facts,
  setFacts,
  syntheticConfirmed,
  setSyntheticConfirmed,
  noPiiConfirmed,
  setNoPiiConfirmed,
}: {
  mode: ProductMode;
  setMode: (value: ProductMode) => void;
  products: AuthoringProduct[];
  productId: string;
  applyProduct: (value: AuthoringProduct) => void;
  customName: string;
  setCustomName: (value: string) => void;
  ctaLabel: string;
  setCtaLabel: (value: string) => void;
  ctaUrl: string;
  setCtaUrl: (value: string) => void;
  facts: FactDraft[];
  setFacts: (value: FactDraft[]) => void;
  syntheticConfirmed: boolean;
  setSyntheticConfirmed: (value: boolean) => void;
  noPiiConfirmed: boolean;
  setNoPiiConfirmed: (value: boolean) => void;
}) {
  const updateFact = (index: number, patch: Partial<FactDraft>) => {
    setFacts(facts.map((fact, current) => current === index ? { ...fact, ...patch } : fact));
  };
  return (
    <div>
      <div className="wizard-section-heading"><span>1</span><div><h2>Выберите продукт</h2><p>Каталог безопасен для генерации; новый продукт проходит строгую серверную проверку.</p></div></div>
      <div className="choice-grid" role="radiogroup" aria-label="Источник продукта">
        <label className={mode === "catalog" ? "choice-card is-selected" : "choice-card"}><input checked={mode === "catalog"} name="product-mode" onChange={() => setMode("catalog")} type="radio" /><strong>Из каталога</strong><span>Готовая синтетическая факт-карточка</span></label>
        <label className={mode === "custom" ? "choice-card is-selected" : "choice-card"}><input checked={mode === "custom"} name="product-mode" onChange={() => setMode("custom")} type="radio" /><strong>Новый продукт</strong><span>Структурированные факты без загрузки файлов</span></label>
      </div>
      {mode === "catalog" ? (
        <label className="field"><span>Продукт</span><select aria-label="Продукт" value={productId} onChange={(event) => { const product = products.find((item) => item.product_id === event.target.value); if (product) applyProduct(product); }}><option value="">Выберите продукт</option>{products.map((product) => <option key={product.product_id} value={product.product_id}>{product.exact_name}</option>)}</select></label>
      ) : (
        <div className="custom-product-form">
          <div className="form-grid two-columns"><label className="field"><span>Точное название продукта</span><input value={customName} onChange={(event) => setCustomName(event.target.value)} /></label><label className="field"><span>Подпись действия</span><input value={ctaLabel} onChange={(event) => setCtaLabel(event.target.value)} /></label></div>
          <label className="field"><span>Синтетическая HTTPS-ссылка (.test или .invalid)</span><input placeholder="https://product.example.test/start" type="url" value={ctaUrl} onChange={(event) => setCtaUrl(event.target.value)} /></label>
          <div className="custom-fact-list"><div className="fact-list-heading"><div><h3>Факты продукта</h3><p>Цель кампании и заметки сюда не попадают.</p></div><button className="button button-secondary button-small" onClick={() => setFacts([...facts, blankFact()])} type="button"><Plus size={15} /> Добавить факт</button></div>
            {facts.map((fact, index) => <div className="fact-editor" key={`${index}-${fact.label}`}><div className="fact-editor-title"><strong>Факт {index + 1}</strong>{facts.length > 1 ? <button aria-label={`Удалить факт ${index + 1}`} className="icon-button" onClick={() => setFacts(facts.filter((_, current) => current !== index))} type="button"><Trash2 size={15} /></button> : null}</div><div className="form-grid two-columns"><label className="field"><span>Название факта</span><input value={fact.label} onChange={(event) => updateFact(index, { label: event.target.value })} /></label><label className="field"><span>Тип</span><select value={fact.kind} onChange={(event) => updateFact(index, { kind: event.target.value, exactValue: "", unit: "" })}>{FACT_KINDS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div><label className="field"><span>Точная каноническая формулировка</span><textarea rows={2} value={fact.canonicalText} onChange={(event) => updateFact(index, { canonicalText: event.target.value })} /></label><div className="form-grid two-columns"><label className="field"><span>Безопасное название источника</span><input value={fact.sourceLabel} onChange={(event) => updateFact(index, { sourceLabel: event.target.value })} /></label>{["number", "percentage", "money", "duration", "date"].includes(fact.kind) ? <label className="field"><span>Точное значение</span><input type={fact.kind === "date" ? "date" : "number"} step="any" value={fact.exactValue} onChange={(event) => updateFact(index, { exactValue: event.target.value })} /></label> : <label className="field"><span>Нормализованное значение (необязательно)</span><input value={fact.exactValue} onChange={(event) => updateFact(index, { exactValue: event.target.value })} /></label>}</div>{fact.kind === "money" || fact.kind === "duration" ? <label className="field"><span>Единица измерения</span><input placeholder={fact.kind === "money" ? "RUB/month" : "day"} value={fact.unit} onChange={(event) => updateFact(index, { unit: event.target.value })} /></label> : null}</div>)}
          </div>
          <div className="confirmation-list"><label><input checked={syntheticConfirmed} onChange={(event) => setSyntheticConfirmed(event.target.checked)} type="checkbox" />Подтверждаю: продукт и факты полностью синтетические.</label><label><input checked={noPiiConfirmed} onChange={(event) => setNoPiiConfirmed(event.target.checked)} type="checkbox" />Подтверждаю: в данных нет имён, телефонов, e-mail и другой PII.</label></div>
        </div>
      )}
    </div>
  );
}

function BriefStep({ campaignName, setCampaignName, objective, setObjective, segmentId, onPersona, personas, selectedPersona, channels, setChannels, tone, setTone }: { campaignName: string; setCampaignName: (value: string) => void; objective: string; setObjective: (value: string) => void; segmentId: string; onPersona: (value: string) => void; personas: AuthoringCatalog["personas"]; selectedPersona: AuthoringCatalog["personas"][number] | undefined; channels: Channel[]; setChannels: (value: Channel[]) => void; tone: string; setTone: (value: string) => void }) {
  const toggle = (channel: Channel) => setChannels(channels.includes(channel) ? channels.filter((item) => item !== channel) : [...channels, channel]);
  return <div><div className="wizard-section-heading"><span>2</span><div><h2>Опишите кампанию</h2><p>Бриф задаёт сценарий и тон, но не становится источником продуктовых фактов.</p></div></div><label className="field"><span>Название кампании</span><input value={campaignName} onChange={(event) => setCampaignName(event.target.value)} /></label><label className="field"><span>Цель коммуникации</span><textarea rows={3} value={objective} onChange={(event) => setObjective(event.target.value)} /></label><div className="form-grid two-columns"><label className="field"><span>Аудитория и событие</span><select value={segmentId} onChange={(event) => onPersona(event.target.value)}><option value="">Выберите синтетическую аудиторию</option>{personas.map((persona) => <option key={persona.segment_id} value={persona.segment_id}>{persona.label}</option>)}</select></label><label className="field"><span>Тон</span><input value={tone} onChange={(event) => setTone(event.target.value)} /></label></div><fieldset className="channel-field"><legend>Каналы</legend>{(["sms", "email"] as Channel[]).map((channel) => { const available = selectedPersona?.available_channels.includes(channel) ?? true; return <label key={channel}><input checked={channels.includes(channel)} disabled={!available} onChange={() => toggle(channel)} type="checkbox" /><strong>{channel === "sms" ? "SMS" : "E-mail"}</strong><span>{available ? "Разрешён для выбранной аудитории" : "Нет согласия в синтетическом профиле"}</span></label>; })}</fieldset></div>;
}

function ReviewStep({ productName, campaignName, objective, personaLabel, channels, tone, ctaLabel, ctaUrl, periodStart, setPeriodStart, periodEnd, setPeriodEnd, notes, setNotes }: { productName: string; campaignName: string; objective: string; personaLabel: string; channels: Channel[]; tone: string; ctaLabel: string; ctaUrl: string; periodStart: string; setPeriodStart: (value: string) => void; periodEnd: string; setPeriodEnd: (value: string) => void; notes: string; setNotes: (value: string) => void }) {
  return <div><div className="wizard-section-heading"><span>3</span><div><h2>Проверьте бриф</h2><p>После создания сервер ещё раз проверит факты, CTA, канал и согласие.</p></div></div><dl className="review-grid"><div><dt>Кампания</dt><dd>{campaignName}</dd></div><div><dt>Продукт</dt><dd>{productName}</dd></div><div><dt>Цель</dt><dd>{objective}</dd></div><div><dt>Аудитория</dt><dd>{personaLabel}</dd></div><div><dt>Каналы</dt><dd>{channels.map((item) => item === "sms" ? "SMS" : "E-mail").join(" + ")}</dd></div><div><dt>Тон</dt><dd>{tone}</dd></div><div className="review-wide"><dt>Действие</dt><dd>{ctaLabel} · <code>{ctaUrl}</code></dd></div></dl><div className="form-grid two-columns"><label className="field"><span>Период с (необязательно)</span><input type="date" value={periodStart} onChange={(event) => setPeriodStart(event.target.value)} /></label><label className="field"><span>Период до (необязательно)</span><input type="date" value={periodEnd} onChange={(event) => setPeriodEnd(event.target.value)} /></label></div><label className="field"><span>Заметки (необязательно)</span><textarea placeholder="Контекст для сценария и тона — не продуктовые факты" rows={4} value={notes} onChange={(event) => setNotes(event.target.value)} /></label></div>;
}
