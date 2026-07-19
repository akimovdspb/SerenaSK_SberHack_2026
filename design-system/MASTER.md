# Communication Factory design system

## Product posture

The interface is a calm, light enterprise workspace for evidence-backed communication work. It
uses Russian product copy, restrained green accents and dense information only where it supports a
decision. Synthetic-data and no-send boundaries remain visible at every viewport width: in the
global header on wide screens, in a persistent safety strip below the header on narrow screens,
and at every approval surface.

## Foundations

- System sans-serif for UI and Georgia only for primary editorial headings.
- Base text `#17231e`; muted text `#5f6e66` (>=4.5:1 on white and soft surfaces); placeholder
  `#6d7a73`; primary green `#123d30`; background `#f3f5f1`.
- White surfaces use a `#d9dfda` boundary. Green, amber, red and blue state fills retain readable
  dark foregrounds at small sizes.
- Type scale is proof-first: page titles stay within 24–38 px, base copy is 13–15 px and no
  data/metadata text renders below 11 px; most metadata uses 12 px.
- Spacing follows a practical 4 px base, with common gaps of 8, 12, 16, 24 and 32 px.
- Corners are 8–15 px. Shadows communicate layering, never status.

## Components and states

- One primary action per workflow state; secondary actions are outlined and destructive actions
  use an explicit confirmation dialog with initial focus, a focus trap, Escape and focus restore.
- A disabled control is always accompanied by a visible reason text linked via
  `aria-describedby`; `title`-only reasons are not allowed.
- Global “Workspace” navigation is contextual: before a campaign exists it is visibly disabled
  with a short reason; after opening a campaign it returns to the last workspace in this browser
  session instead of silently redirecting to Cases.
- Status tone and label come from one typed exhaustive mapping of existing domain values
  (`presentation/status.ts`); an unknown value falls back to a neutral badge with the raw value.
- Mode badges show the Russian label first and always distinguish `live_ouroboros`,
  `deterministic_template`, `replay`, `validation_only` and `mock`; the raw identifier stays
  available as a secondary monospace label or title.
- Tab rows group related sections (channels, proofs, iteration) with separators and support
  arrow/Home/End keyboard navigation with `tablist/tab/tabpanel` semantics.
- Exact hashes are truncated visually and preserved in the title/confirmation surface.
- Tables are used for repeated evidence mappings and case comparisons. Cards are used for metrics,
  readiness and bounded artifacts.
- Every query has loading, error and empty states. Loading copy promises a terminal result or an
  error; no indefinite spinner is used.
- Degraded and stale states are stated in words, not only color.

## Screen layout

- Cases: summary metrics, business/chaos switch and the case outcome table.
- Workspace: brief/context at left, artifacts and decisions in the center, public-safe trace at
  right. At tablet width the trace moves below; at mobile all zones become a single column.
- Evaluation: measured metrics and case assertions at left, modes/review/report links at right.
- Diagnostics: component readiness, pinned hashes, exact tool list and latest terminal errors.
- Hosted login: a dedicated, labelled username/password form in the same light enterprise visual
  language; boundaries remain visible before authentication. Destructive hosted demo reset lives
  in Diagnostics and requires typing an exact Russian confirmation.

## Accessibility and responsive behavior

- Semantic landmarks, headings, tables, labels, dialogs and status regions are mandatory.
- All interactive controls have visible `:focus-visible` outlines and a minimum 40 px normal
  action height. The skip link moves directly to main content.
- Text and state colors target WCAG AA contrast. Color is never the sole status signal.
- Desktop demo target is 1920×1080 at 90–100% zoom. The focused narrow target is 390×844.
- Tables scroll inside their own region on narrow screens; the page itself must not overflow
  horizontally. A fixed four-item mobile navigation keeps primary screens reachable.
- Reduced-motion preference disables transitions and reduces animation duration.

## Safety-specific language

- Use “Все данные синтетические” and “Отправка отключена”.
- Approval copy must say “Утверждение не означает отправку”.
- Deterministic browser-test approvals display `test_only` / `E2E test actor`. Hosted decisions
  display the authenticated human session, while explicitly stating that they are neither a send
  action nor an independent final submission sign-off.
- Evaluation must label whether a value is measured, assumed or a hypothesis and whether the slice
  is frozen.
