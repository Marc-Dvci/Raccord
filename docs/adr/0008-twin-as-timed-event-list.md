# ADR 0008 — The twin models the programme as a timed event list

**Status:** Accepted

## Context

AccessPulse needs a delivery chain to break: encoders, packagers, a CDN, players, caption
sources, a described-audio track, an interpreter feed. It also needs a *programme* going through
that chain, because the probes measure the rendered experience against what was actually spoken
and shown.

The obvious approach is real media: encode a video, produce real subtitle files, render real
described audio, run a real player. It is also the approach that makes the project impossible
to run — gigabytes of assets, a media toolchain, licensing for anything not shot ourselves, and
a demonstration that cannot start without a download.

## Decision

Model the programme as a **timed event list**: timed dialogue tokens, timed caption cues,
described-audio windows with importance weights, and interpreter-feed frame statistics
(`media.py`). The simulator applies fault effects to those structures — dropping tokens, shifting
cue times, silencing description windows, freezing sign frames — and the probes consume exactly
the structures a real decoder would hand them.

The delivery chain itself is a **versioned topology graph** (`twin.py`) with health state and
blast-radius traversal, so "which promises ride on this encoder pool" is a query, not a guess.

## Alternatives considered

**Real encoded media.** Rejected for this repository: it makes the demo un-runnable without
downloads, forces a licensing story for every asset, and adds a media toolchain to CI — all to
exercise decode paths that are not what this project is about. The measurement logic is
identical either way, which is the point.

**Record real player sessions and replay them.** Rejected: replay is fixed. The benchmark needs
to inject 45 different faults at controlled intensities across a slice matrix, which requires
generation, not playback.

**A pure statistical model of metrics with no programme at all.** Rejected: then the caption
probe has nothing to align against, and drift measurement — the core competence — becomes
`abs(a - b)` on two synthetic numbers. The programme is what makes the measurement real.

## Consequences

**Good.** The entire demonstration runs on a laptop with no downloads, no credentials and no
media toolchain. Any scenario is exactly reproducible from a seed. The probes' inputs are the
real interfaces, so the same probe code would run against a real decoder.

**Costly, and stated in the dataset card.** A twin is not the world. The benchmark establishes
that the system reasons correctly about *these* symptoms; it does not establish accuracy against
real broadcast incidents. The calibration study measures the estimator's recovery of a known
offset, not forced alignment against real speech with accents, overlap and music — against real
audio the dominant error term would be an ASR front end that is not part of this repository.

**Consequence we accept.** The 85-second looping programme cannot represent hour-long phenomena:
slow drift over a feature film, ad-break boundaries, mid-programme language switching.
