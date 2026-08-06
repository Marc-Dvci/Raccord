/* AccessPulse operator UI.
 *
 * Deliberately dependency-free: no framework, no bundler, no CDN. Every view is
 * built from semantic HTML that already exists in index.html, so the page works
 * with a screen reader and with JavaScript partially failed, and the whole
 * product is auditable by reading two files.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  incidentId: null,
  lastIncident: null,
  faults: [],
};

/* ---------------------------------------------------------------- helpers */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* noop */ }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json();
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function statusSpan(kind, text) {
  const cls = { ok: 'status-ok', bad: 'status-bad', warn: 'status-warn', idle: 'status-idle' }[kind];
  return el('span', { class: `status ${cls}` }, text);
}

function stat(label, value, sub) {
  return el('dl', { class: 'stat' },
    el('dt', {}, label),
    el('dd', {}, value, sub ? el('div', { class: 'stat-sub' }, sub) : null));
}

function fillTable(sel, rows) {
  const tbody = $(sel).querySelector('tbody');
  tbody.replaceChildren();
  for (const row of rows) {
    tbody.append(el('tr', {}, ...row.map((c) => el('td', {}, c))));
  }
}

function say(message, kind = 'idle') {
  const node = $('#incident-status');
  node.replaceChildren(statusSpan(kind, message));
}

function fmt(n, digits = 2) {
  if (n === null || n === undefined) return '—';
  return Number(n).toFixed(digits);
}

function pct(n) { return `${(Number(n) * 100).toFixed(1)}%`; }

function busy(button, on) {
  if (!button) return;
  button.disabled = on;
  button.setAttribute('aria-busy', on ? 'true' : 'false');
}

/* ------------------------------------------------------------------ tabs */

function setupTabs() {
  const tabs = $$('[role="tab"]');
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', (ev) => {
      const map = { ArrowRight: 1, ArrowLeft: -1, Home: 'first', End: 'last' };
      if (!(ev.key in map)) return;
      ev.preventDefault();
      let next;
      if (map[ev.key] === 'first') next = tabs[0];
      else if (map[ev.key] === 'last') next = tabs[tabs.length - 1];
      else next = tabs[(index + map[ev.key] + tabs.length) % tabs.length];
      next.focus();
      selectTab(next);
    });
  });
}

function selectTab(tab) {
  $$('[role="tab"]').forEach((t) => {
    const selected = t === tab;
    t.setAttribute('aria-selected', selected ? 'true' : 'false');
    $(`#${t.getAttribute('aria-controls')}`).hidden = !selected;
  });
}

/* -------------------------------------------------------------- overview */

async function loadEvent() {
  const data = await api('/api/event');
  $('#event-name').textContent = `${data.media.title} — World Premiere`;
  fillTable('#promise-table', data.promises.map((p) => [
    p.promise_id,
    p.feature.replace(/_/g, ' '),
    p.language,
    p.territories.join(', '),
    p.slo_tier.replace('tier0_', 'T0 ').replace(/_/g, ' '),
    `v${p.version}`,
  ]));
}

async function loadFaults() {
  state.faults = await api('/api/faults');
  $('#fault-count').textContent = state.faults.length;
  const select = $('#fault-select');
  select.replaceChildren();
  const byFeature = {};
  for (const f of state.faults) (byFeature[f.feature] ||= []).push(f);
  for (const [feature, list] of Object.entries(byFeature)) {
    const group = el('optgroup', { label: feature.replace(/_/g, ' ') });
    for (const f of list) group.append(el('option', { value: f.fault_id }, f.name));
    select.append(group);
  }
  select.value = 'cap.progressive_drift';
  select.addEventListener('change', describeFault);
  describeFault();
}

function describeFault() {
  const f = state.faults.find((x) => x.fault_id === $('#fault-select').value);
  if (!f) return;
  const scope = Object.entries(f.scope || {})
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join('/') : v}`).join(' · ');
  $('#fault-detail').textContent =
    `${f.description} Ground-truth class ${f.failure_class}, component ${f.component}, ` +
    `onset ${f.onset}. Expected to breach: ${f.expected_slos.join(', ')}. ` +
    (scope ? `Scope — ${scope}.` : 'Scope — all slices.');
}

async function refreshState() {
  const s = await api('/api/state');
  $('#program-time').textContent = `${Math.floor(s.program_seconds / 60)}m ${Math.round(s.program_seconds % 60)}s`;
  $('#mcp-chip').textContent = `${s.mcp.transport} · ${s.mcp.tools_available} tools · ${s.mcp.calls} calls`;

  const breached = s.breached_slos.length;
  $('#overview-stats').replaceChildren(
    stat('Promises registered', s.promises, 'versioned contracts'),
    stat('Accessibility SLOs breaching', breached, breached ? 'action required' : 'all inside objective'),
    stat('Active faults', s.active_faults.filter((f) => !f.neutralised).length, 'in the digital twin'),
    stat('Agent steps recorded', s.agent_steps, 'traced'),
    stat('Topology revision', s.topology_hash.slice(0, 8), 'digital twin hash'),
  );

  const budgets = $('#budget-list');
  budgets.replaceChildren();
  for (const [slo, consumed] of Object.entries(s.error_budget)) {
    const cls = consumed >= 0.5 ? 'bad' : consumed >= 0.2 ? 'warn' : '';
    budgets.append(el('div', { class: 'budget' },
      el('div', { class: 'budget-head' },
        el('span', {}, slo),
        el('span', {}, `${pct(consumed)} consumed`)),
      el('div', {
        class: 'budget-track', role: 'meter', 'aria-valuenow': (consumed * 100).toFixed(1),
        'aria-valuemin': '0', 'aria-valuemax': '100',
        'aria-label': `${slo} error budget consumed`,
      },
        el('div', { class: `budget-fill ${cls}`, style: `width:${Math.min(100, consumed * 100)}%` }))));
  }

  // cockpit
  $('#cockpit-stats').replaceChildren(
    stat('Programme time', `${Math.round(s.program_seconds)}s`, s.wall_clock.slice(11, 19) + ' UTC'),
    stat('Caption encoder pool', s.environment.caption_encoder_pool),
    stat('Timing reference', s.environment.clock_source),
    stat('Manifest generation', s.environment.manifest_generation),
    stat('Open incidents', s.open_incidents.length),
  );
  fillTable('#health-table', Object.entries(s.health).map(([feature, row]) => [
    feature.replace(/_/g, ' '),
    row.evaluated,
    row.breached ? statusSpan('bad', String(row.breached)) : statusSpan('ok', '0'),
    row.abstained,
    row.worst_slo || '—',
  ]));
  const env = $('#env-list');
  env.replaceChildren();
  for (const [k, v] of Object.entries(s.environment)) {
    env.append(el('dt', {}, k.replace(/_/g, ' ')),
      el('dd', {}, Array.isArray(v) ? (v.join(', ') || '—') : String(v)));
  }
  return s;
}

async function refreshLogs() {
  const lines = await api('/api/logs?limit=60');
  fillTable('#log-table', lines.reverse().map((l) => [
    l.ts.slice(11, 19),
    l.labels.service,
    l.labels.level === 'error' ? statusSpan('bad', 'error')
      : l.labels.level === 'warn' ? statusSpan('warn', 'warn') : statusSpan('idle', 'info'),
    el('span', { class: 'mono' }, l.line),
  ]));
}

/* ------------------------------------------------------------- readiness */

async function certify(button) {
  busy(button, true);
  try {
    const data = await api('/api/certify', { method: 'POST' });
    const s = data.summary;
    $('#cert-summary').replaceChildren(
      el('div', { class: 'stat-row' },
        stat('Certification', s.certified ? 'READY' : 'BLOCKED',
          s.certified ? 'all hard assertions pass' : `${s.blockers} blocker(s)`),
        stat('Assertions run', s.assertions),
        stat('Gates', Object.keys(s.by_gate).length),
        stat('Signature', s.signature, 'HMAC over the record')));

    const container = $('#cert-gates');
    container.replaceChildren();
    const byGate = {};
    for (const a of data.assertions) (byGate[a.gate] ||= []).push(a);
    for (const [gate, list] of Object.entries(byGate)) {
      const failing = list.filter((a) => a.status !== 'passing');
      const card = el('section', { class: 'card', 'aria-labelledby': `g-${gate}` },
        el('h2', { id: `g-${gate}` }, gate.replace(/_/g, ' '),
          ' — ',
          failing.length ? statusSpan('bad', `${failing.length} of ${list.length} not passing`)
            : statusSpan('ok', `${list.length} passing`)));
      const wrap = el('div', { class: 'table-wrap' });
      const table = el('table', {},
        el('caption', { class: 'visually-hidden' }, `${gate} assertions`),
        el('thead', {}, el('tr', {},
          el('th', { scope: 'col' }, 'Assertion'), el('th', { scope: 'col' }, 'Hard'),
          el('th', { scope: 'col' }, 'Result'), el('th', { scope: 'col' }, 'Detail'),
          el('th', { scope: 'col' }, 'Owner'))),
        el('tbody', {}, ...list.map((a) => el('tr', {},
          el('td', {}, a.name),
          el('td', {}, a.hard ? 'blocking' : 'advisory'),
          el('td', {}, a.status === 'passing' ? statusSpan('ok', 'pass')
            : a.status === 'failing' ? statusSpan('bad', 'fail')
              : statusSpan('warn', a.status)),
          el('td', { class: 'mono' }, a.detail),
          el('td', {}, a.owner)))));
      wrap.append(table);
      card.append(wrap);
      container.append(card);
    }
  } finally {
    busy(button, false);
  }
}

/* -------------------------------------------------------------- incident */

const STATES = ['DETECTED', 'QUALIFIED', 'SCOPED', 'EVIDENCE_COMPLETE', 'DIAGNOSED',
  'POLICY_EVALUATED', 'AWAITING_APPROVAL', 'ACTION_EXECUTING', 'VERIFYING',
  'RECOVERED', 'COMMUNICATED', 'REVIEWED'];

function renderStateMachine(current) {
  const list = $('#state-machine');
  list.replaceChildren();
  const index = STATES.indexOf(current);
  STATES.forEach((s, i) => {
    const cls = current === s ? 'current' : (index > i && index !== -1 ? 'done' : '');
    list.append(el('li', { class: cls }, s.replace(/_/g, ' ').toLowerCase()));
  });
}

function renderIncident(data) {
  if (!data || !data.incident) return;
  const inc = data.incident;
  state.incidentId = inc.incident_id;
  state.lastIncident = data;
  renderStateMachine(inc.state);

  $('#evidence-list').replaceChildren(...(inc.evidence || []).map((e) =>
    el('li', {},
      el('span', { class: 'tool' }, e.source_tool),
      e.summary,
      e.deep_link ? el('div', {}, el('a', { href: e.deep_link, rel: 'noreferrer' },
        'Open in Grafana for human review')) : null)));

  $('#hypothesis-list').replaceChildren(...(inc.hypotheses || []).map((h, i) =>
    el('li', { class: i === 0 ? 'top' : '' },
      el('span', { class: 'posterior' }, fmt(h.posterior)), ' ',
      el('strong', {}, h.failure_class), ' — ', h.statement,
      h.abstained ? el('div', {}, statusSpan('warn', 'abstained: evidence insufficient')) : null,
      el('span', { class: 'uncertainty' }, h.uncertainty_note))));

  $('#causal-list').replaceChildren(...(data.causal_candidates || []).map((c) =>
    el('li', {},
      el('strong', {}, fmt(c.score)), ' ', c.component, ' · ', c.kind, ' — ', c.description,
      el('span', { class: 'uncertainty' },
        `supports: ${c.supporting.join('; ') || 'none'} · counter: ${c.contradicting.join('; ') || 'none'}`))));

  renderApproval(data);

  fillTable('#assertion-table', (inc.assertions || []).map((a) => [
    el('span', {}, a.name, a.mandatory ? ' (mandatory)' : ' (advisory)'),
    a.scope_note,
    fmt(a.observed, 4),
    `${a.comparator === 'lte' ? '≤' : '≥'} ${fmt(a.threshold, 4)}`,
    a.status === 'passing' ? statusSpan('ok', 'passing')
      : a.status === 'failing' ? statusSpan('bad', 'failing')
        : statusSpan('warn', a.status),
  ]));

  $('#comms-list').replaceChildren(...(inc.communications || []).map((c) =>
    el('article', { class: `comm ${c.audience === 'public_status' ? 'public' : ''}` },
      el('p', { class: 'audience' }, c.audience.replace(/_/g, ' ')),
      el('h3', {}, c.subject),
      el('pre', {}, c.body),
      c.reading_level_note ? el('p', { class: 'hint' }, c.reading_level_note) : null)));

  const review = data.review;
  $('#review-body').replaceChildren(review
    ? el('div', { class: 'stat-row' },
      stat('Root cause', review.root_cause),
      stat('Diagnosis correct', review.diagnosis_correct ? 'yes' : 'no'),
      stat('Time to detect', `${fmt(review.time_to_detect_s, 1)}s`),
      stat('Time to recovery', `${fmt(review.time_to_recovery_s, 1)}s`),
      stat('Sessions affected', review.affected_sessions.toLocaleString()),
      stat('Sessions protected', review.protected_sessions.toLocaleString()))
    : el('p', { class: 'hint' }, 'Not yet reviewed.'));
  if (review) {
    $('#review-body').append(
      el('h3', {}, 'Missed signals'),
      el('ul', {}, ...(review.missed_signals.length ? review.missed_signals : ['none'])
        .map((m) => el('li', {}, m))),
      el('h3', {}, 'Proposed improvements (require human acceptance)'),
      el('ul', {}, ...review.proposed_improvements.map((m) => el('li', {}, m))));
  }

  $('#audit-chain').replaceChildren(
    data.audit_chain_valid ? statusSpan('ok', 'hash chain verified')
      : statusSpan('bad', 'hash chain broken'),
    ` · ${(inc.audit || []).length} events · legal next states: ${(data.legal_transitions || []).join(', ') || 'none'}`);
  fillTable('#audit-table', (inc.audit || []).map((a) => [
    a.seq, a.event, a.from_state || '—', a.to_state || '—', a.actor,
  ]));
}

function renderApproval(data) {
  const inc = data.incident;
  const body = $('#approval-body');
  body.replaceChildren();
  const action = inc.proposed_action;
  const decision = inc.policy_decision;
  if (!action || !decision) {
    body.append(el('p', { class: 'hint' }, 'No action proposed yet.'));
    return;
  }
  const kv = el('dl', { class: 'kv' },
    el('dt', {}, 'Exact action'), el('dd', {}, action.action_type),
    el('dt', {}, 'Target'), el('dd', {}, action.target),
    el('dt', {}, 'Scope'), el('dd', {}, action.scope_digest || '—'),
    el('dt', {}, 'Expected result'), el('dd', {},
      Object.entries(action.expected_metric_change).map(([k, v]) => `${k}: ${v}`).join(' · ') || '—'),
    el('dt', {}, 'Verification suite'), el('dd', {}, action.verification_suite),
    el('dt', {}, 'Rollback'), el('dd', {}, action.rollback_behaviour),
    el('dt', {}, 'Policy'), el('dd', {}, decision.classification),
    el('dt', {}, 'Policy version'), el('dd', {}, decision.policy_version),
    el('dt', {}, 'Approval authority'), el('dd', {},
      decision.required_roles.join(', ') || 'not required'));
  body.append(kv);
  body.append(el('h3', {}, 'Why policy decided this'),
    el('ul', {}, ...decision.rationale.map((r) => el('li', {}, r))));

  if (inc.approval) {
    body.append(el('p', {},
      statusSpan('ok', `approved by ${inc.approval.approver} (${inc.approval.approver_role})`),
      ` · token expires ${inc.approval.expires_at.slice(11, 19)} UTC · single use, bound to action hash ${inc.approval.action_hash.slice(0, 12)}…`));
  } else if (decision.classification === 'approval_required') {
    const approverInput = el('input', {
      type: 'text', id: 'approver', value: 't.duval@studio.example',
      'aria-label': 'Approver identity',
    });
    const btn = el('button', { type: 'button', class: 'btn btn-primary' },
      'Approve this exact action');
    btn.addEventListener('click', async () => {
      busy(btn, true);
      try {
        const role = decision.required_roles[0] || 'event_technical_director';
        const result = await api(`/api/incident/${inc.incident_id}/approve`, {
          method: 'POST',
          body: JSON.stringify({ approver: approverInput.value, role }),
        });
        renderIncident(result);
        say('Approval issued: signed, single-use, bound to this action and evidence.', 'ok');
      } catch (err) {
        say(err.message, 'bad');
      } finally {
        busy(btn, false);
      }
    });
    body.append(el('div', { class: 'field-row' },
      el('label', { for: 'approver' }, 'Approver'), approverInput, btn));
  } else if (decision.classification === 'prohibited') {
    body.append(el('p', {}, statusSpan('bad', 'policy prohibits this action')));
  } else {
    body.append(el('p', {}, statusSpan('ok', 'policy permits automatic execution')));
  }
}

async function step(path, button, message) {
  if (!state.incidentId && !path.includes('detect')) {
    say('No open incident. Run step 1 first.', 'warn');
    return;
  }
  busy(button, true);
  try {
    const url = path.includes('detect')
      ? '/api/incident/step/detect'
      : `/api/incident/${state.incidentId}${path}`;
    const data = await api(url, { method: 'POST' });
    if (data.incident_id && !data.incident) {
      state.incidentId = data.incident_id;
      const full = await api(`/api/incident/${data.incident_id}`);
      renderIncident(full);
    } else if (data.detected === false) {
      say('No alert is firing. Inject a fault from the Overview tab first.', 'warn');
      return;
    } else {
      renderIncident(data);
    }
    say(message, 'ok');
    await refreshState();
    await refreshObservability();
  } catch (err) {
    say(err.message, 'bad');
  } finally {
    busy(button, false);
  }
}

/* ---------------------------------------------------------------- replay */

async function loadReplay(button) {
  if (!state.incidentId) { say('Open an incident first.', 'warn'); return; }
  busy(button, true);
  try {
    const r = await api(`/api/incident/${state.incidentId}/replay`);
    const drift = r.drift.seconds;
    $('#replay-summary').replaceChildren(el('div', { class: 'stat-row' },
      stat('Measured caption drift', `${fmt(drift)} s`,
        `confidence ${pct(r.drift.confidence)}`),
      stat('Slice', `${r.slice.language}/${r.slice.territory}`,
        `${r.slice.player_version} · ${r.slice.cdn_region}`),
      stat('Matched tokens', r.drift.detail.matched_tokens ?? '—',
        `of ${r.drift.detail.reference_tokens ?? '—'} spoken`),
      stat('Encoder pool', r.environment.caption_encoder_pool,
        `clock ${r.environment.clock_source}`)));

    // WCAG 3.1.2 language of parts: the replay shows spoken and captioned text
    // in the language of the affected slice, inside an lang="en" document. A
    // screen reader has to switch voice for it, so the language is declared on
    // the text itself rather than assumed from the page.
    const lang = r.slice.language;
    $('#reference-list').replaceChildren(...r.reference.map((t) =>
      el('li', {}, el('span', { class: 't' }, `${fmt(t.t, 1)}s`),
        el('span', { lang }, t.token))));
    $('#cue-list').replaceChildren(...r.cues.map((c) =>
      el('li', {},
        el('span', { class: `t ${drift > 1.5 ? 'late' : ''}` }, `${fmt(c.start, 1)}s`),
        el('span', {},
          el('span', { lang }, `${c.speaker}: ${c.text}`),
          c.rendered ? null : el('div', {}, statusSpan('bad', 'not rendered on device'))))));
  } finally {
    busy(button, false);
  }
}

/* --------------------------------------------------------- observability */

async function refreshObservability() {
  const [mcp, agent] = await Promise.all([api('/api/mcp'), api('/api/agent-observability')]);
  $('#obs-stats').replaceChildren(
    stat('MCP tool calls', mcp.call_count, `${mcp.transport} transport`),
    stat('MCP total latency', `${fmt(agent.mcp_total_ms, 1)} ms`),
    stat('Agent steps', agent.total_steps, `${fmt(agent.total_duration_ms, 1)} ms total`),
    stat('Tools advertised', mcp.tools.length),
    stat('Reasoning mode', agent.reasoning_mode));
  $('#mcp-transport-note').textContent =
    `Transport: ${mcp.transport}. Every fact in an incident must arrive through one of these ` +
    `calls — the state machine will not accept EVIDENCE_COMPLETE otherwise.`;
  fillTable('#mcp-table', mcp.calls.map((c, i) => [
    i + 1,
    el('span', { class: 'mono' }, c.tool),
    `${fmt(c.duration_ms, 2)} ms`,
    c.result_bytes,
    c.ok ? statusSpan('ok', 'ok') : statusSpan('bad', 'error'),
  ]));
  fillTable('#cap-table', mcp.capabilities.map((c) => [
    el('span', {}, c.key, c.purpose ? el('div', { class: 'hint' }, c.purpose) : null),
    c.required ? 'required' : 'optional',
    c.resolved_to
      ? el('span', { class: 'mono' }, c.resolved_to)
      : (c.required ? statusSpan('bad', 'missing') : statusSpan('idle', 'not advertised')),
  ]));
  fillTable('#agent-table', Object.entries(agent.by_agent).map(([name, row]) => [
    name, row.calls, `${fmt(row.ms, 1)} ms`,
  ]));
}

/* ------------------------------------------------------------- benchmark */

async function loadBenchmark() {
  try {
    const b = await api('/api/benchmark');
    const body = $('#bench-body');
    body.replaceChildren(
      el('div', { class: 'stat-row' },
        stat('Scenarios', b.scenarios, `seed ${b.seed}`),
        stat('Detection rate', pct(b.detection.detection_rate)),
        stat('Top-1 root cause', pct(b.diagnosis.top1_accuracy)),
        stat('Top-3 root cause', pct(b.diagnosis.top3_accuracy)),
        stat('Recovery verified', pct(b.verification.recovered_rate)),
        stat('Unsafe actions', b.agent.unsafe_action_rate === 0 ? '0'
          : pct(b.agent.unsafe_action_rate))));

    const section = (title, obj) => {
      const rows = Object.entries(obj).map(([k, v]) => [
        k.replace(/_/g, ' '),
        typeof v === 'number' ? fmt(v, 4) : String(v),
      ]);
      const table = el('table', {},
        el('caption', { class: 'visually-hidden' }, title),
        el('thead', {}, el('tr', {}, el('th', { scope: 'col' }, 'Metric'),
          el('th', { scope: 'col' }, 'Value'))),
        el('tbody', {}, ...rows.map((r) => el('tr', {}, ...r.map((c) => el('td', {}, c))))));
      return el('section', { class: 'card' }, el('h2', {}, title),
        el('div', { class: 'table-wrap' }, table));
    };
    body.append(el('div', { class: 'grid-2' },
      section('Detection', b.detection),
      section('Diagnosis', b.diagnosis),
      section('Scope accuracy', b.scope),
      section('Agent behaviour', b.agent),
      section('Verification', b.verification),
      section('Performance', b.performance)));

    if (b.ablations) {
      const rows = Object.entries(b.ablations).map(([name, r]) => [
        name.replace(/_/g, ' '), pct(r.detection_rate), pct(r.top1_accuracy),
        pct(r.recovered_rate), fmt(r.mean_mcp_calls, 1),
      ]);
      body.append(el('section', { class: 'card' },
        el('h2', {}, 'Ablations'),
        el('p', { class: 'card-note' },
          'Each row removes one capability and re-runs the same seeded corpus.'),
        el('div', { class: 'table-wrap' },
          el('table', {},
            el('caption', { class: 'visually-hidden' }, 'Ablation results'),
            el('thead', {}, el('tr', {},
              el('th', { scope: 'col' }, 'Configuration'),
              el('th', { scope: 'col' }, 'Detection'),
              el('th', { scope: 'col' }, 'Top-1 root cause'),
              el('th', { scope: 'col' }, 'Recovered'),
              el('th', { scope: 'col' }, 'MCP calls'))),
            el('tbody', {}, ...rows.map((r) =>
              el('tr', {}, ...r.map((c) => el('td', {}, c)))))))));
    }
  } catch (err) {
    $('#bench-body').replaceChildren(el('p', { class: 'hint' },
      `No results yet (${err.message}). Run: accesspulse bench --scenarios 1000`));
  }
}

/* ------------------------------------------------------------------ wire */

function wire() {
  setupTabs();

  $('#btn-tick').addEventListener('click', async (ev) => {
    busy(ev.target, true);
    try {
      await api('/api/tick?seconds=20', { method: 'POST' });
      await refreshState();
      await refreshLogs();
    } finally { busy(ev.target, false); }
  });

  $('#btn-reset').addEventListener('click', async (ev) => {
    busy(ev.target, true);
    try {
      await api('/api/reset', { method: 'POST' });
      state.incidentId = null;
      renderStateMachine(null);
      await Promise.all([refreshState(), refreshLogs(), refreshObservability()]);
      say('Demo reset to the seeded baseline.', 'ok');
    } finally { busy(ev.target, false); }
  });

  $('#btn-inject').addEventListener('click', async (ev) => {
    busy(ev.target, true);
    try {
      await api('/api/inject', {
        method: 'POST',
        body: JSON.stringify({ fault_id: $('#fault-select').value, ticks: 9,
          seconds_per_tick: 20 }),
      });
      await refreshState();
      await refreshLogs();
      say('Fault injected and the event advanced. Go to the Incident tab.', 'ok');
      selectTab($('#tab-incident'));
    } finally { busy(ev.target, false); }
  });

  $('#btn-certify').addEventListener('click', (ev) => certify(ev.target));
  $('#btn-replay').addEventListener('click', (ev) => loadReplay(ev.target));

  $('#btn-run-loop').addEventListener('click', async (ev) => {
    busy(ev.target, true);
    say('Running detect → evidence → diagnose → policy → approve → remediate → verify …');
    try {
      const data = await api('/api/incident/run?auto_approve=true', { method: 'POST' });
      renderIncident(data);
      say(data.error
        ? `Loop stopped: ${data.error}`
        : `Diagnosed ${data.ground_truth} (${data.diagnosis_correct ? 'correct' : 'incorrect'}), ` +
          `action ${data.action_taken}, ${data.recovered ? 'recovered and verified' : 'rolled back'}, ` +
          `${data.assertions[0]}/${data.assertions[1]} assertions passing, ${data.mcp_calls} MCP calls.`,
        data.error ? 'warn' : 'ok');
      await Promise.all([refreshState(), refreshObservability()]);
    } catch (err) {
      say(err.message, 'bad');
    } finally { busy(ev.target, false); }
  });

  const steps = [
    ['#btn-step-detect', '/step/detect', 'Alert qualified and scope computed.'],
    ['#btn-step-investigate', '/step/investigate', 'Evidence collected through Grafana MCP.'],
    ['#btn-step-diagnose', '/step/diagnose', 'Hypotheses ranked against the failure taxonomy.'],
    ['#btn-step-policy', '/step/policy', 'Policy evaluated for the proposed action.'],
    ['#btn-step-remediate', '/step/remediate', 'Approved action executed.'],
    ['#btn-step-verify', '/step/verify', 'Experience re-measured after the action.'],
    ['#btn-step-communicate', '/step/communicate', 'Role-specific communications generated.'],
    ['#btn-step-review', '/step/review', 'Post-incident review complete.'],
  ];
  for (const [sel, path, message] of steps) {
    $(sel).addEventListener('click', (ev) => step(path, ev.target, message));
  }

  $('#tab-benchmark').addEventListener('click', loadBenchmark);
  $('#tab-observability').addEventListener('click', refreshObservability);
  $('#tab-cockpit').addEventListener('click', refreshLogs);
}

/* The incident the server already has open, if any.

   Without this the incident workspace is empty until *this browser session*
   runs the loop, so reloading the page mid-incident — or opening the product
   while an incident is in flight, which is the normal way an operator arrives —
   shows nothing. The incident lives on the server; the page should reflect it. */
async function adoptOpenIncident() {
  const open = await api('/api/incidents');
  if (!open.length) return false;
  const latest = open.reduce((a, b) => (a.opened_at >= b.opened_at ? a : b));
  state.incidentId = latest.incident_id;
  renderIncident(await api(`/api/incident/${latest.incident_id}`));
  say(`Resumed ${latest.incident_id} — ${latest.state.toLowerCase()}.`, 'ok');
  return true;
}

async function boot() {
  wire();
  renderStateMachine(null);
  await loadEvent();
  await loadFaults();
  await refreshState();
  await refreshLogs();
  await refreshObservability();
  // A failure to resume must not stop the rest of the product from working.
  let resumed = false;
  try {
    resumed = await adoptOpenIncident();
  } catch (err) {
    say(`Could not resume the open incident: ${err.message}`, 'warn');
  }
  if (!resumed) {
    say('Ready. Inject a fault from the Overview tab, then run the loop.', 'idle');
  }
}

boot().catch((err) => {
  document.querySelector('main').prepend(
    el('p', { class: 'card', role: 'alert' }, `Failed to start: ${err.message}`));
});
