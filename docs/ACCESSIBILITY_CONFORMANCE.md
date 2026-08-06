# Accessibility conformance report — the AccessPulse product itself

A product that monitors accessibility and is not itself accessible is self-refuting. This is the
conformance report for the AccessPulse operator interface (`accesspulse serve`,
`src/accesspulse/web/`).

- **Standard:** WCAG 2.2 Level AA (also the normative reference for EN 301 549 §9 and §11)
- **Scope:** the whole operator product — Overview, Readiness studio, Live cockpit, Incident
  workspace, Evidence replay, Agent & MCP observability, Benchmark laboratory
- **Result:** conforms to WCAG 2.2 AA, with the known limitations in §6 stated rather than
  omitted
- **Re-checked automatically:** `python tools/a11y_audit.py` — **63/63 checks pass**
  (`docs/accessibility_audit.json`; CI fails the build if any check regresses)

---

## 1. How the interface is built, and why that matters

The UI is **dependency-free**: no framework, no bundler, no component library, no CDN. That is
an accessibility decision before it is a build decision. Every element that appears on screen is
one this repository chose: a `<button>` is a button, a table of results is a `<table>` with a
`<caption>` and header cells, the incident stages are `<section>`s with headings, the state
machine is an ordered list. There is no wrapper library rendering a `<div role="button">` on
our behalf and no upgrade that can silently regress the semantics.

Consequences that show up throughout this report: full keyboard operation comes mostly for free,
the accessibility tree matches the visual structure, and the audit in `tools/a11y_audit.py` can
check the real markup rather than a build artefact.

---

## 2. Conformance by principle

### 1. Perceivable

| Criterion | Level | How it is met |
|---|---|---|
| 1.1.1 Non-text content | A | The single decorative brand mark is `aria-hidden="true"`. Every other graphic — status glyphs, budget bars, the state machine — carries a text equivalent in the DOM. There are no images. |
| 1.3.1 Info and relationships | A | Semantic landmarks (`header`, `nav`, `main`, `footer`), one `h1` per panel with a correct heading order, `<dl>` for key/value data, `<table>` with `<caption>` and `<th>` for every data view, `<ol>` for the state machine. Verified per-panel by the audit. |
| 1.3.2 Meaningful sequence | A | DOM order is reading order; layout is grid/flex without positional reordering. |
| 1.3.4 Orientation | AA | No orientation lock; layout is fluid. |
| 1.3.5 Identify input purpose | AA | The only inputs are domain selectors (fault, event) with explicit `<label for>`; no personal-data fields exist. |
| 1.4.1 Use of colour | A | **No status is conveyed by colour alone.** All status rendering goes through one helper, `statusSpan(kind, text)`, which always emits a text label, and each `.status-*` class adds a glyph (`✓ ✕ ! ·`) via `::before`. The audit asserts both: 24 call sites all supply a label, 4 coloured classes all set a glyph. |
| 1.4.3 Contrast (minimum) | AA | Every text/background pair measured in both palettes — see §3. Lowest text ratio: **6.39:1** (light), **6.95:1** (dark), against a 4.5:1 requirement. |
| 1.4.4 Resize text | AA | Root font size is `100%`; all type, spacing and component sizing is in `rem`/`em`. Nothing is pinned in `px`. |
| 1.4.10 Reflow | AA | At 320 CSS px (1280px at 400% zoom) every multi-column grid collapses to one column and fixed key/value columns become fluid — `@media (max-width: 48rem)` and `(max-width: 30rem)`. Wide data tables scroll horizontally inside their own `.table-wrap`, which the tabular-data exception permits. |
| 1.4.11 Non-text contrast | AA | Control boundaries use a dedicated `--control-line` token at **4.53:1** (light) / **5.83:1** (dark); focus indicator **5.96:1** / **11.0:1**; stage rules ≥ 6.39:1. Decorative separators are exempt and are the only use of the lighter `--line`. |
| 1.4.12 Text spacing | AA | No fixed line heights or letter-spacing that would clip; `line-height: 1.55` with fluid containers. |
| 1.4.13 Content on hover or focus | AA | No hover-only or focus-only overlays exist. |

### 2. Operable

| Criterion | Level | How it is met |
|---|---|---|
| 2.1.1 Keyboard | A | Every control is a native interactive element. The one composite widget, the tab set, implements the APG keyboard contract — `ArrowLeft`/`ArrowRight` to move between tabs, `Home`/`End` to jump to the first or last — and the audit asserts it. There are no drag, gesture or pointer-only interactions anywhere in the product. |
| 2.1.2 No keyboard trap | A | No modal dialogs, no focus-capturing widgets, nothing that manages focus away from the user. |
| 2.1.4 Character key shortcuts | A | None are implemented. |
| 2.2.1 Timing adjustable | A | Live data refreshes but no interaction is time-limited; nothing expires under the user. |
| 2.2.2 Pause, stop, hide | A | The only animation is a 240ms budget-bar width transition, and it is inside `prefers-reduced-motion: no-preference`. |
| 2.4.1 Bypass blocks | A | A skip link targets `#main`, which is `tabindex="-1"` so it accepts programmatic focus. Both facts are asserted by the audit. |
| 2.4.2 Page titled | A | Descriptive `<title>`. |
| 2.4.3 Focus order | A | Follows DOM order; no positive `tabindex` anywhere. |
| 2.4.4 Link purpose (in context) | A | Link text is self-describing; no "click here". |
| 2.4.5 Multiple ways | AA | Tabs plus in-page navigation plus deep links to the same views. |
| 2.4.6 Headings and labels | AA | Every panel and card is headed; every control is labelled. |
| 2.4.7 Focus visible | AA | `:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }` — never removed anywhere in the stylesheet. |
| 2.4.11 Focus not obscured (min) | AA (2.2) | No sticky overlays or floating panels; the 2px outline offset keeps the ring clear of adjacent borders. |
| 2.5.1 Pointer gestures | A | No path-based or multipoint gestures. |
| 2.5.2 Pointer cancellation | A | Native `click` activation only — up-event, cancellable. |
| 2.5.3 Label in name | A | Visible label text is the accessible name for every control. |
| 2.5.4 Motion actuation | A | No motion-actuated functionality. |
| 2.5.7 Dragging movements | AA (2.2) | Nothing is draggable. |
| 2.5.8 Target size (minimum) | AA (2.2) | Buttons are `0.5rem 0.9rem` padding on 0.9rem text (≥ 24×24 px); tabs are larger. |

### 3. Understandable

| Criterion | Level | How it is met |
|---|---|---|
| 3.1.1 Language of page | A | `<html lang="en">`. |
| 3.1.2 Language of parts | AA | The evidence replay renders spoken and captioned programme text in the language of the affected slice inside an `lang="en"` document, so each excerpt carries its own `lang` attribute and a screen reader switches voice for it. Asserted by the audit. |
| 3.2.1 On focus | A | Focus never changes context. |
| 3.2.2 On input | A | Selecting a fault populates a description; it does not navigate or submit. |
| 3.2.3 Consistent navigation | AA | One tab bar, same order everywhere. |
| 3.2.4 Consistent identification | AA | The same status vocabulary and the same glyphs everywhere. |
| 3.2.6 Consistent help | A (2.2) | The same help links — public accessibility status page, API reference, metrics — sit in the same footer on every panel. |
| 3.3.1 Error identification | A | Failures are announced in a `role="status"` region in text, with the reason. |
| 3.3.2 Labels or instructions | A | Every control has a `<label for>`; the audit asserts this. |
| 3.3.7 Redundant entry | A (2.2) | No multi-step forms; nothing is asked twice. |

### 4. Robust

| Criterion | Level | How it is met |
|---|---|---|
| 4.1.2 Name, role, value | A | Native elements throughout. The one composite widget is the tab set, implemented to the APG pattern: `role="tablist"` → `role="tab"` with `aria-controls` and `aria-selected` → `role="tabpanel"` with `aria-labelledby`. The audit asserts the wiring is complete, reciprocal and that exactly one tab is selected on load. |
| 4.1.3 Status messages | AA | Five live regions: event strip, certification summary, fault detail, incident status (`role="status"`), verification result. Asynchronous results are announced without moving focus. |

---

## 3. Measured contrast

Produced by `tools/a11y_audit.py`, which parses the two `:root` palettes out of `styles.css` and
computes WCAG relative-luminance ratios. Requirement is 4.5:1 for text, 3:1 for control
boundaries and focus indicators.

| Pair | Light | Dark | Required |
|---|---|---|---|
| Body text on page | 16.60 | 17.29 | 4.5 |
| Body text on surface | 17.79 | 15.82 | 4.5 |
| Secondary text on surface | 7.77 | 9.40 | 4.5 |
| Secondary text on inset | 6.86 | 8.48 | 4.5 |
| Accent text on surface | 6.65 | 6.95 | 4.5 |
| Accent button label | 6.65 | 7.66 | 4.5 |
| Healthy status text | 6.50 | 9.45 | 4.5 |
| At-risk status text | 6.39 | 10.07 | 4.5 |
| Breaching status text | 8.15 | 8.47 | 4.5 |
| Evidence stage rule | 6.65 | 6.95 | 3.0 |
| Hypothesis stage rule | 7.38 | 8.20 | 3.0 |
| Policy stage rule | 6.39 | 10.07 | 3.0 |
| Verified stage rule | 6.50 | 9.45 | 3.0 |
| Control border on surface | 4.53 | 5.83 | 3.0 |
| Control border on inset | 4.00 | 5.26 | 3.0 |
| Focus ring on page | 5.96 | 11.00 | 3.0 |
| Focus ring on surface | 6.39 | 10.06 | 3.0 |

Both palettes ship: `color-scheme: light dark` plus a `prefers-color-scheme: dark` override, so
the interface follows the operator's system setting rather than imposing one. A
`forced-colors: active` block keeps borders and fills visible in Windows High Contrast mode.

---

## 4. Design decisions that are accessibility decisions

**Evidence, hypothesis, policy and verified result are structurally separated.** In an incident
workspace the most dangerous confusion is mistaking a *hypothesis* for a *fact* or a *proposal*
for a *verified outcome*. Each stage is its own `<section>` with its own heading, its own
uppercase text tag (`EVIDENCE`, `HYPOTHESIS`, `POLICY`, `VERIFIED`) and a coloured left rule.
The colour is the redundant channel; the tag and the heading carry the meaning. A screen-reader
user hears the distinction; a user who cannot distinguish the four hues reads it.

**Numbers are text, not pictures.** Every metric in the product is available as marked-up text
in a table or definition list. There is no canvas chart whose content exists only as pixels.

**Uncertainty is stated, not implied by colour.** When a probe abstains, the interface says
*"abstained: evidence insufficient"* in words.

**The public status component is written for viewers, not operators.** Plain language, no
internal component names, no jargon — and a test asserts it (`test_public_status_contains_no_
internal_detail`).

---

## 5. How this was tested

| Method | Coverage |
|---|---|
| **Automated static audit** (`tools/a11y_audit.py`) | 63 checks: contrast in both palettes, document language, language of parts, skip-link resolution, tab/tabpanel wiring, tab keyboard contract, heading presence per panel, accessible names, `<label for>` association, live regions, focus visibility, focus order, reduced motion, reflow breakpoints, forced-colors support, colour-independence of status |
| **Keyboard-only pass** | Every panel reached, every control operated, focus ring visible at every step, no trap, no focus loss on asynchronous updates |
| **Screen-reader pass** | NVDA on Windows and VoiceOver on macOS: landmark and heading navigation, table navigation with header announcement, tab-set announcement, live-region announcement of incident state changes |
| **Zoom and reflow** | 400% zoom at 1280px, and a 320px viewport |
| **Reduced motion / forced colours** | `prefers-reduced-motion: reduce` and Windows High Contrast |
| **Continuous** | CI runs the audit on every change; the build fails on any regression |

---

## 6. Known limitations — stated, not hidden

1. **The manual passes are not an independent third-party audit.** They were performed by the
   project, on NVDA and VoiceOver. A production deployment claiming EN 301 549 conformance
   should commission an independent evaluation, ideally including testers with disabilities. The
   automated audit is reproducible by anyone; the manual passes are not, and this report does
   not pretend otherwise.
2. **The static audit cannot see runtime DOM.** It checks `index.html`, `styles.css` and the
   rendering helpers in `app.js`. Content rendered dynamically is checked by the manual passes
   and by the discipline of routing all status through `statusSpan`.
3. **Data tables scroll horizontally on narrow viewports.** Permitted by 1.4.10 for tabular
   data, but a small-screen operator will still scroll to read the widest tables (the MCP call
   log, the benchmark ablation matrix).
4. **No high-contrast theme of our own.** We honour the platform's forced-colors mode rather
   than shipping a third palette; the two palettes we do ship exceed AA by a wide margin.
5. **Not tested with speech input or switch access.** Both should work — every control is a
   native, labelled element with a visible name matching its accessible name (2.5.3) — but
   "should" is not "verified", and this report will not claim otherwise.

---

## 7. Feedback

Accessibility defects in AccessPulse are treated as functional defects, not enhancements. The
issue tracker is the route; a report that a screen reader cannot operate a control is a bug of
the same severity as an incident closing without verification.
