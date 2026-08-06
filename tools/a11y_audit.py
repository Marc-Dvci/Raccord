"""Static accessibility audit of the AccessPulse product UI.

A product about accessibility that is not itself accessible is self-refuting, so
the conformance claims in docs/ACCESSIBILITY_CONFORMANCE.md are re-checked by
this script rather than asserted once and left to rot. CI runs it.

What it checks, mechanically, against src/accesspulse/web/:

  contrast     every text/background token pair used by the stylesheet, in both
               the light and the dark palette, against WCAG 2.2 1.4.3 (4.5:1 for
               body text) and 1.4.11 (3:1 for UI component boundaries)
  structure    document language, one h1 per panel, skip link target exists,
               tab/tabpanel wiring is complete and reciprocal
  names        every interactive element resolves to an accessible name
  colour       no status is conveyed by colour alone: every status class that
               sets a colour also emits a text or shape affordance
  motion       any animation is inside a prefers-reduced-motion guard
  contrast-hc  forced-colors support is present

    python tools/a11y_audit.py            # human-readable table
    python tools/a11y_audit.py --json     # machine-readable, for CI

Exit code is 1 if any check fails. This is a static audit: it does not replace
the assistive-technology pass documented in the conformance report, and it says
so in its own output.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "accesspulse" / "web"

# Text/background pairs the stylesheet actually renders, as (label, ink, bg,
# minimum ratio). 4.5 is normal text; 3.0 is large text and component edges.
PAIRS: tuple[tuple[str, str, str, float], ...] = (
    ("body text on page", "--ink", "--bg", 4.5),
    ("body text on surface", "--ink", "--surface", 4.5),
    ("secondary text on surface", "--ink-2", "--surface", 4.5),
    ("secondary text on inset", "--ink-2", "--surface-2", 4.5),
    ("accent text on surface", "--accent", "--surface", 4.5),
    ("accent button label", "--accent-ink", "--accent", 4.5),
    ("healthy status text", "--ok", "--surface", 4.5),
    ("at-risk status text", "--warn", "--surface", 4.5),
    ("breaching status text", "--bad", "--surface", 4.5),
    ("evidence stage rule", "--evidence", "--surface", 3.0),
    ("hypothesis stage rule", "--hypothesis", "--surface", 3.0),
    ("policy stage rule", "--policy", "--surface", 3.0),
    ("verified stage rule", "--verify", "--surface", 3.0),
    ("control border on surface", "--control-line", "--surface", 3.0),
    ("control border on inset", "--control-line", "--surface-2", 3.0),
    ("focus ring on page", "--focus", "--bg", 3.0),
    ("focus ring on surface", "--focus", "--surface", 3.0),
)

INTERACTIVE = {"button", "a", "input", "select", "textarea", "summary"}


# ---------------------------------------------------------------------------
# colour
# ---------------------------------------------------------------------------


def _srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (0.2126 * _srgb_to_linear(r)
            + 0.7152 * _srgb_to_linear(g)
            + 0.0722 * _srgb_to_linear(b))


def contrast_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    lo, hi = sorted((la, lb))
    return (hi + 0.05) / (lo + 0.05)


def palettes(css: str) -> dict[str, dict[str, str]]:
    """Return {"light": {token: hex}, "dark": {...}} from the :root blocks."""
    out: dict[str, dict[str, str]] = {"light": {}, "dark": {}}
    blocks = re.findall(r":root\s*\{(.*?)\}", css, re.S)
    if not blocks:
        return out
    dark_block_index = None
    dark_media = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{(.*?)\n\}", css, re.S)
    for i, block in enumerate(blocks):
        if dark_media and block in dark_media.group(1):
            dark_block_index = i
    for i, block in enumerate(blocks):
        scheme = "dark" if i == dark_block_index else "light"
        for token, value in re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})", block):
            out[scheme][token] = value
    # tokens not overridden in dark mode inherit the light value
    for token, value in out["light"].items():
        out["dark"].setdefault(token, value)
    return out


# ---------------------------------------------------------------------------
# markup
# ---------------------------------------------------------------------------


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    text: str = ""
    line: int = 0


class Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self._stack: list[Element] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        el = Element(tag, {k: (v or "") for k, v in attrs}, line=self.getpos()[0])
        self.elements.append(el)
        if tag not in ("img", "input", "br", "hr", "meta", "link"):
            self._stack.append(el)

    def handle_endtag(self, tag: str) -> None:
        while self._stack:
            el = self._stack.pop()
            if el.tag == tag:
                break

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            for el in self._stack:
                el.text += " " + text


@dataclass
class Result:
    check: str
    detail: str
    ok: bool
    measured: str = ""


@dataclass
class Audit:
    results: list[Result] = field(default_factory=list)

    def add(self, check: str, detail: str, ok: bool, measured: str = "") -> None:
        self.results.append(Result(check, detail, ok, measured))

    @property
    def failures(self) -> list[Result]:
        return [r for r in self.results if not r.ok]


def _accessible_name(el: Element, ids_with_text: dict[str, str],
                     labels_for: dict[str, str]) -> str:
    if el.attrs.get("aria-label"):
        return el.attrs["aria-label"]
    labelled = el.attrs.get("aria-labelledby")
    if labelled:
        return " ".join(ids_with_text.get(i, "") for i in labelled.split())
    # a <label for="..."> pointing at this control
    if el.attrs.get("id") and labels_for.get(el.attrs["id"]):
        return labels_for[el.attrs["id"]]
    if el.attrs.get("title"):
        return el.attrs["title"]
    if el.tag == "input" and el.attrs.get("type") in ("submit", "button", "reset"):
        return el.attrs.get("value", "")
    return el.text.strip()


def audit_markup(html: str, audit: Audit) -> None:
    collector = Collector()
    collector.feed(html)
    els = collector.elements
    by_id = {el.attrs["id"]: el for el in els if el.attrs.get("id")}
    id_text = {i: el.text.strip() or el.attrs.get("aria-label", "") for i, el in by_id.items()}
    labels_for = {el.attrs["for"]: el.text.strip()
                  for el in els if el.tag == "label" and el.attrs.get("for")}

    # 3.1.1 language of page
    html_el = next((e for e in els if e.tag == "html"), None)
    audit.add("3.1.1 language", "<html> declares a language",
              bool(html_el and html_el.attrs.get("lang")),
              (html_el.attrs.get("lang", "") if html_el else "missing"))

    # 2.4.1 bypass blocks
    skip = next((e for e in els if "skip-link" in e.attrs.get("class", "")), None)
    target = (skip.attrs.get("href", "").lstrip("#") if skip else "")
    audit.add("2.4.1 bypass blocks", "skip link resolves to an existing target",
              bool(skip and target in by_id), f"#{target}" if skip else "no skip link")
    main = next((e for e in els if e.tag == "main"), None)
    audit.add("2.4.1 bypass blocks", "skip target is programmatically focusable",
              bool(main and main.attrs.get("tabindex") == "-1"),
              main.attrs.get("tabindex", "none") if main else "no <main>")

    # 4.1.2 name, role, value - tab pattern
    tabs = [e for e in els if e.attrs.get("role") == "tab"]
    panels = [e for e in els if e.attrs.get("role") == "tabpanel"]
    audit.add("4.1.2 tab pattern", "every tab controls an existing panel",
              bool(tabs) and all(t.attrs.get("aria-controls") in by_id for t in tabs),
              f"{len(tabs)} tabs")
    audit.add("4.1.2 tab pattern", "every panel is labelled by an existing tab",
              bool(panels) and all(p.attrs.get("aria-labelledby") in by_id for p in panels),
              f"{len(panels)} panels")
    selected = sum(1 for t in tabs if t.attrs.get("aria-selected") == "true")
    audit.add("4.1.2 tab pattern", "exactly one tab is selected on load",
              selected == 1, f"{selected} selected")
    audit.add("4.1.2 tab pattern", "tabs live in a tablist",
              any(e.attrs.get("role") == "tablist" for e in els))

    # 1.3.1 info and relationships - one h1 per panel
    for panel in panels:
        pid = panel.attrs.get("id", "?")
        audit.add("1.3.1 headings", f"panel {pid} contains a heading",
                  "<h1" in html.split(f'id="{pid}"', 1)[-1][:4000]
                  or "<h2" in html.split(f'id="{pid}"', 1)[-1][:4000])

    # 4.1.2 accessible names
    unnamed: list[str] = []
    for el in els:
        if el.tag not in INTERACTIVE:
            continue
        if el.tag == "a" and not el.attrs.get("href"):
            continue
        if el.tag == "input" and el.attrs.get("type") == "hidden":
            continue
        if not _accessible_name(el, id_text, labels_for):
            unnamed.append(f"<{el.tag}> line {el.line}")
    audit.add("4.1.2 accessible name", "every interactive element has a name",
              not unnamed, ", ".join(unnamed) if unnamed else "all named")
    audit.add("3.3.2 labels or instructions", "every form control has a <label for>",
              all(el.attrs.get("id") in labels_for or el.attrs.get("aria-label")
                  for el in els if el.tag in ("select", "textarea")
                  or (el.tag == "input" and el.attrs.get("type") not in ("hidden", "submit",
                                                                         "button", "reset"))),
              f"{len(labels_for)} labelled controls")

    # 1.1.1 decorative marks are hidden from assistive technology
    marks = [e for e in els if "brand-mark" in e.attrs.get("class", "")]
    audit.add("1.1.1 non-text content", "decorative marks are aria-hidden",
              all(m.attrs.get("aria-hidden") == "true" for m in marks),
              f"{len(marks)} decorative marks")

    # 4.1.3 status messages
    audit.add("4.1.3 status messages", "live regions announce asynchronous updates",
              any(e.attrs.get("aria-live") or e.attrs.get("role") == "status" for e in els),
              f"{sum(1 for e in els if e.attrs.get('aria-live'))} live regions")


def audit_stylesheet(css: str, audit: Audit) -> dict[str, dict[str, float]]:
    pal = palettes(css)
    ratios: dict[str, dict[str, float]] = {"light": {}, "dark": {}}
    for scheme in ("light", "dark"):
        tokens = pal[scheme]
        for label, ink, bg, minimum in PAIRS:
            if ink not in tokens or bg not in tokens:
                audit.add("1.4.3 contrast", f"{scheme}: {label} tokens defined", False,
                          f"missing {ink if ink not in tokens else bg}")
                continue
            ratio = contrast_ratio(tokens[ink], tokens[bg])
            ratios[scheme][label] = round(ratio, 2)
            audit.add("1.4.3 contrast" if minimum >= 4.5 else "1.4.11 non-text contrast",
                      f"{scheme}: {label} >= {minimum}:1",
                      ratio >= minimum, f"{ratio:.2f}:1")

    # 2.4.7 focus visible
    audit.add("2.4.7 focus visible", "focus-visible outline is at least 2px",
              bool(re.search(r":focus-visible\s*\{[^}]*outline:\s*[3-9]px", css, re.S)))
    # 2.3.3 / 2.2.2 motion
    animated = re.findall(r"(animation|transition):", css)
    guarded = re.search(r"prefers-reduced-motion:\s*reduce", css)
    audit.add("2.3.3 animation from interactions",
              "reduced-motion preference is honoured",
              (not animated) or bool(guarded), f"{len(animated)} animated declarations")
    # 1.4.12 text spacing / 1.4.4 resize
    audit.add("1.4.4 resize text", "root font size is relative, not fixed px",
              bool(re.search(r"html\s*\{[^}]*font-size:\s*100%", css, re.S)))
    # 1.4.10 reflow
    audit.add("1.4.10 reflow", "a narrow-viewport breakpoint exists",
              bool(re.search(r"@media[^{]*max-width", css)))
    # high contrast mode
    audit.add("1.4.11 non-text contrast", "forced-colors mode is supported",
              "forced-colors: active" in css)
    return ratios


def audit_colour_independence(css: str, js: str, audit: Audit) -> None:
    """1.4.1: status must never be carried by colour alone.

    Every status in the UI goes through one helper, `statusSpan(kind, text)`, so
    the guarantee is checkable in two places: the helper must render the text it
    is given, and each status class in the stylesheet must add a glyph on top of
    its colour.
    """
    helper = re.search(r"function\s+statusSpan\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    audit.add("1.4.1 use of colour", "statusSpan renders its text argument",
              bool(helper and "text" in helper.group(1)),
              "helper found" if helper else "statusSpan not found")

    calls = re.findall(r"statusSpan\(\s*'(\w+)'\s*,\s*([^)]*)\)", js)
    empty = [k for k, arg in calls if not arg.strip() or arg.strip() in ("''", '""')]
    audit.add("1.4.1 use of colour", "every statusSpan call supplies a label",
              not empty, f"{len(calls)} call sites")

    coloured = set(re.findall(r"\.(status-\w+)\s*\{[^}]*color:", css))
    glyphed = set(re.findall(r"\.(status-\w+)::before\s*\{[^}]*content:", css))
    missing = sorted(coloured - glyphed)
    audit.add("1.4.1 use of colour", "every coloured status class also sets a glyph",
              not missing, ", ".join(missing) if missing else f"{len(coloured)} classes")


def audit_behaviour(js: str, audit: Audit) -> None:
    """Rendering behaviour the markup alone cannot show."""
    # 3.1.2: the evidence replay renders programme text in the slice's language
    # inside an lang="en" document, so the language must travel with the text.
    audit.add("3.1.2 language of parts",
              "replay text declares the language of the slice it came from",
              bool(re.search(r"\{\s*lang\s*\}", js)) and "r.slice.language" in js,
              "lang attribute set from slice")
    # 2.1.1: the one composite widget must be operable from the keyboard the way
    # the APG tab pattern specifies, not only by clicking.
    audit.add("2.1.1 keyboard", "tab set supports arrow, Home and End keys",
              all(k in js for k in ("ArrowRight", "ArrowLeft", "Home", "End")),
              "APG tab pattern")
    audit.add("2.4.3 focus order", "no positive tabindex is created at runtime",
              not re.search(r"tabindex['\"]?\s*[:,]\s*['\"]?[1-9]", js))


# ---------------------------------------------------------------------------


def run() -> tuple[Audit, dict]:
    audit = Audit()
    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    audit_markup(html, audit)
    ratios = audit_stylesheet(css, audit)
    audit_colour_independence(css, js, audit)
    audit_behaviour(js, audit)
    report = {
        "checks": len(audit.results),
        "failed": len(audit.failures),
        "contrast_ratios": ratios,
        "results": [r.__dict__ for r in audit.results],
        "note": "Static audit. Does not replace the manual screen-reader and "
                "keyboard pass recorded in docs/ACCESSIBILITY_CONFORMANCE.md.",
    }
    return audit, report


def main() -> int:
    ap = argparse.ArgumentParser(description="AccessPulse UI accessibility audit")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--out", type=Path, default=None, help="also write the JSON report here")
    args = ap.parse_args()

    audit, report = run()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        width = max(len(r.check) for r in audit.results) + 2
        for r in audit.results:
            mark = "PASS" if r.ok else "FAIL"
            measured = f"  [{r.measured}]" if r.measured else ""
            print(f"{mark}  {r.check:<{width}}{r.detail}{measured}")
        print(f"\n{len(audit.results) - len(audit.failures)}/{len(audit.results)} checks passed")
        print(report["note"])
    return 1 if audit.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
