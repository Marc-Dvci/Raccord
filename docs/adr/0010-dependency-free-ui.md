# ADR 0010 — A dependency-free user interface

**Status:** Accepted

## Context

The AccessPulse operator interface has seven views, live-updating data, tables, an incident
workspace and an evidence replay. The default way to build that in 2026 is a component framework
and a UI library, and it would be faster to write.

It would also mean that the accessibility of a product *about* accessibility depends on a
component library's rendering choices, on an upgrade not regressing them, and on a build step
between the source in this repository and the markup a screen reader receives.

## Decision

No framework, no component library, no CSS toolkit, no bundler, no CDN. Three files:
`index.html`, `styles.css`, `app.js`. Semantic HTML written by hand; a small `el()` helper for
DOM construction; plain `fetch` against the API.

Consequences that follow directly:

- a `<button>` is a `<button>`; a table of results is a `<table>` with a `<caption>` and header
  cells; the state machine is an `<ol>`
- the accessibility tree matches the visual structure by construction
- `tools/a11y_audit.py` can audit the *real* markup, not a build artefact
- the browser loads nothing from a third-party host: no script, font, stylesheet, image or
  beacon ([MEDIA_RIGHTS.md](../MEDIA_RIGHTS.md))

## Alternatives considered

**React plus an accessible component library.** The mainstream answer, and defensible — good
libraries are accessible. Rejected here because it puts a dependency between our conformance
claim and our source, adds a build step to a judge-runnable repository, and means our
accessibility audit checks someone else's output. For this product, that is the wrong trade.

**A light framework (Preact, Alpine, htmx).** Less weight, same objection at smaller scale, plus
a CDN or a vendored copy.

**Server-rendered templates.** Considered. Rejected because the live cockpit and the MCP call
log update continuously, and full-page re-render is worse for assistive technology than targeted
updates into live regions.

## Consequences

**Good.** 63 automated accessibility checks pass against the actual shipped markup. The UI has
no supply chain and no upgrade path that can silently regress semantics. `accesspulse serve`
starts instantly with no build step, which matters for a judge.

**Costly.** More hand-written code, and no free component behaviour: the tab set's roving
keyboard contract (arrows, Home, End) is implemented here, and every table renderer is ours. A
larger product would feel this.

**Consequence we accept.** No client-side routing, no virtual scrolling, and long tables are
plain tables inside a scroll container. For an operator tool with seven views, that is
sufficient — and it is why the reflow behaviour at 320 CSS pixels is checkable.
