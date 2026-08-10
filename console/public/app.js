/* Knowledge Console.
 *
 * 03-FRONTEND-KNOWLEDGE-CONSOLE.md §1: "One job: make the memory system
 * inspectable and curatable by a human in under ten minutes a week per project."
 * Build order from §2: the Review Inbox first, the Retrieval Debugger second.
 *
 * TWO DECISIONS WORTH STATING UP FRONT.
 *
 * 1. NO innerHTML FOR ANYTHING THAT CAME FROM THE DATABASE. This console renders
 *    memory titles and content, and this system DELIBERATELY STORES PROMPT
 *    INJECTION PAYLOADS — that is what the quarantine queue is for. A reviewer
 *    reading the inbox is reading attacker-controlled text by design. So every
 *    value from the API goes through `el()`, which assigns textContent. The one
 *    exception is `raw()`, used only for markup this file wrote itself.
 *
 * 2. THE CONSOLE NEVER TOUCHES THE DATABASE (§1 principle 2). Every write goes
 *    through the same API endpoints, and therefore the same policy engine and
 *    audit trail, as an MCP write. There is no privileged path here.
 */
'use strict';

const $ = (s) => document.querySelector(s);

/* DOM helper. `text` is assigned as textContent — never parsed as markup. */
function el(tag, attrs, text) {
  const n = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function raw(tag, cls, html) {           // ONLY for markup written in this file
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  n.innerHTML = html;
  return n;
}
const frag = (...kids) => kids.reduce((f, k) => (k && f.appendChild(k), f),
                                      document.createDocumentFragment());

/* -------------------------------------------------------------- API layer */
const state = { scope: null, projects: [], route: 'inbox', busy: false };

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    // §1 copy rules: errors say what happened and what to do.
    const detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || body);
    throw new Error(detail || `${r.status} ${r.statusText}`);
  }
  return body;
}
const q = (extra) => new URLSearchParams(Object.assign({
  tenant_id: state.scope.tenant_id,
  project_id: state.scope.project_id,
  principal_id: state.scope.principal_id,
}, extra || {})).toString();

/* ----------------------------------------------------------------- toasts */
let undoTimer = null, pendingUndo = null;

function toast(msg, undo) {
  const t = $('#toast');
  t.textContent = '';
  t.appendChild(el('span', null, msg));
  clearTimeout(undoTimer);
  pendingUndo = undo || null;
  if (undo) {
    // §1 principle 3: undo with a 10-second window. A reviewer moving at three
    // seconds an item WILL mis-key, and without undo the only recovery is to
    // find the memory again and reason about what the wrong action did.
    t.appendChild(el('span', { class: 'undo', onclick: runUndo }, 'undo (u)'));
  }
  t.hidden = false;
  undoTimer = setTimeout(() => { t.hidden = true; pendingUndo = null; }, undo ? 10000 : 3000);
}
async function runUndo() {
  if (!pendingUndo) return;
  const fn = pendingUndo;
  pendingUndo = null;
  $('#toast').hidden = true;
  try { await fn(); toast('Undone'); } catch (e) { toast('Could not undo — ' + e.message); }
}

/* ------------------------------------------------------------------ chrome */
const ROUTES = [
  ['inbox', 'Inbox', 'i'],
  ['debug', 'Debugger', 'd'],
  ['conflicts', 'Conflicts', 'c'],
  ['health', 'Health', 'h'],
];

function renderNav() {
  const nav = $('#nav');
  nav.textContent = '';
  for (const [id, label] of ROUTES) {
    nav.appendChild(el('a', {
      href: '#/' + id, class: state.route === id ? 'on' : '',
    }, label));
  }
}

function renderSwitcher() {
  const sel = $('#project-switch');
  sel.textContent = '';
  for (const p of state.projects) {
    const bits = [p.slug];
    if (p.quarantined) bits.push(`(${p.quarantined} awaiting review)`);
    const o = el('option', { value: p.id }, bits.join(' '));
    if (p.id === state.scope.project_id) o.selected = true;
    sel.appendChild(o);
  }
  sel.onchange = () => {
    state.scope.project_id = sel.value;
    localStorage.setItem('scope', JSON.stringify(state.scope));
    route();
  };
}

function view(...kids) {
  const v = $('#view');
  v.textContent = '';
  kids.forEach((k) => k && v.appendChild(k));
  v.focus();
}
function header(title, sub) {
  return frag(el('h1', null, title), sub ? el('p', { class: 'sub' }, sub) : null);
}
function empty(strong, rest) {
  const d = el('div', { class: 'empty' });
  d.appendChild(el('strong', null, strong));
  d.appendChild(el('span', null, rest));
  return d;
}
const fail = (e) => el('p', { class: 'err' }, e.message);

/* ================================================================== INBOX */
/* §3.1 — "the most important screen in the product". Ordered by consequence,
 * single-key actions, reject-with-reason, undo. Target: 30 items in 3 minutes. */

const REJECT_REASONS = ['noise', 'wrong', 'already known', 'too specific', 'unsafe'];
let inbox = { items: [], cursor: 0, meta: null };

async function renderInbox() {
  let d;
  try { d = await api('/v1/inbox?' + q({ limit: 100 })); }
  catch (e) { return view(header('Inbox'), fail(e)); }

  inbox.items = d.items;
  inbox.meta = d;
  if (inbox.cursor >= d.items.length) inbox.cursor = Math.max(0, d.items.length - 1);

  const sub = `${d.backlog} awaiting review · oldest ${d.oldest_days}d · ${d.health}`;
  if (!d.items.length) {
    return view(header('Inbox', sub),
      empty('Nothing waiting.',
            'Agent-written memories and detected conflicts land here. ' +
            'Extraction runs at session end.'));
  }

  const list = el('div');
  d.items.forEach((it, i) => list.appendChild(inboxRow(it, i)));
  view(header('Inbox', sub), list,
       el('p', { class: 'muted' },
          'j/k move · a accept · v accept as verified · r reject · s skip · u undo'));
  scrollCursor();
}

function inboxRow(it, i) {
  const tier = it.tier || 'observed';
  const row = el('div', {
    class: `item trust trust-${tier}` + (i === inbox.cursor ? ' cursor' : ''),
    'data-i': i, onclick: () => { inbox.cursor = i; paintCursor(); },
  });

  const meta = el('div', { class: 'meta' });
  meta.appendChild(el('span', { class: `tier tier-${tier}` }, it.kind));
  meta.appendChild(el('span', null, it.type || ''));
  meta.appendChild(el('span', null, `${it.age_days || 0}d old`));
  if (it.source) meta.appendChild(el('span', null, it.source));
  meta.appendChild(el('span', null, it.ref.slice(0, 8)));
  // §3.1: candidates expire in 14 days; show it.
  const left = 14 - (it.age_days || 0);
  if (it.kind !== 'conflict') {
    meta.appendChild(el('span', { class: left <= 3 ? 'tier tier-untrusted' : '' },
                        left > 0 ? `expires in ${left}d` : 'expired'));
  }
  row.appendChild(meta);

  row.appendChild(el('div', { class: 'title' }, it.title || '(untitled)'));
  if (it.digest) row.appendChild(el('div', { class: 'digest' }, it.digest));

  if (it.why && it.why.length) {
    // The injection heuristic's reason, verbatim. A reviewer deciding on a
    // flagged item needs to see WHAT was flagged, not just that something was.
    row.appendChild(el('div', { class: 'flag' }, '⚠ ' + [].concat(it.why).join(' · ')));
  }
  if (it.kind === 'inferred' || it.kind === 'untrusted') {
    row.appendChild(el('div', { class: 'muted' },
      'Accepting records this as observed (tier 2). `authoritative` is earned by ' +
      'writing the file in git, not from this screen.'));
  }

  const acts = el('div', { class: 'actions' });
  if (it.kind === 'conflict') {
    acts.appendChild(el('button', { onclick: (e) => { e.stopPropagation(); resolveAt(i); } },
                        'resolve…'));
  } else {
    acts.appendChild(el('button', { onclick: (e) => { e.stopPropagation(); decide(i, 'observed'); } },
                        'accept (a)'));
    acts.appendChild(el('button', { onclick: (e) => { e.stopPropagation(); decide(i, 'verified'); } },
                        'verified (v)'));
    acts.appendChild(el('button', { onclick: (e) => { e.stopPropagation(); rejectAt(i); } },
                        'reject (r)'));
  }
  row.appendChild(acts);
  return row;
}

function paintCursor() {
  document.querySelectorAll('#view .item').forEach((n, i) =>
    n.classList.toggle('cursor', i === inbox.cursor));
  scrollCursor();
}
function scrollCursor() {
  const n = document.querySelector('#view .item.cursor');
  if (n) n.scrollIntoView({ block: 'nearest' });
}

async function decide(i, tier) {
  const it = inbox.items[i];
  if (!it || it.kind === 'conflict') return;
  try {
    await api('/v1/inbox/review', {
      method: 'POST',
      body: JSON.stringify(Object.assign(scopeBody(), {
        ref: it.ref, action: 'promote', to_tier: tier, note: 'accepted in console',
      })),
    });
    // Undo returns the memory to the queue at the tier it held before review.
    // The first version undid a promotion by REJECTING the memory, which is not
    // an inverse at all — it archives something the reviewer merely mis-keyed,
    // and it recorded a rejection reason that never happened.
    toast(`Accepted as ${tier}`, () => undoReview(it.ref));
    await renderInbox();
  } catch (e) { toast('Could not accept — ' + e.message); }
}

async function undoReview(ref) {
  await api('/v1/inbox/review', {
    method: 'POST',
    body: JSON.stringify(Object.assign(scopeBody(), { ref, action: 'undo' })),
  });
  await renderInbox();
}

function rejectAt(i) {
  const it = inbox.items[i];
  if (!it || it.kind === 'conflict') return;
  // §3.1: "Reject requires a reason from a short list." The reasons feed the
  // extractor-quality metric, so free text would lose the signal.
  const d = el('dialog');
  d.appendChild(el('h2', null, 'Reject'));
  d.appendChild(el('p', { class: 'muted' }, it.title || ''));
  const wrap = el('div');
  REJECT_REASONS.forEach((r) => {
    wrap.appendChild(el('button', {
      onclick: async () => {
        d.close();
        try {
          await api('/v1/inbox/review', {
            method: 'POST',
            body: JSON.stringify(Object.assign(scopeBody(), {
              ref: it.ref, action: 'reject', note: r,
            })),
          });
          // A mis-keyed rejection is at least as likely as a mis-keyed accept,
          // so undo is offered for both.
          toast(`Rejected — ${r}`, () => undoReview(it.ref));
          await renderInbox();
        } catch (e) { toast('Could not reject — ' + e.message); }
      },
    }, r));
  });
  d.appendChild(wrap);
  d.appendChild(raw('p', 'muted', 'Archived with the reason, never deleted.'));
  const f = el('form', { method: 'dialog' });
  f.appendChild(el('button', null, 'Cancel'));
  d.appendChild(f);
  document.body.appendChild(d);
  d.addEventListener('close', () => d.remove());
  d.showModal();
}

function resolveAt(i) {
  const it = inbox.items[i];
  if (!it || it.kind !== 'conflict') return;
  const d = el('dialog');
  d.appendChild(el('h2', null, 'Resolve conflict'));
  d.appendChild(el('p', { class: 'muted' }, it.title || ''));
  d.appendChild(el('p', { class: 'muted' }, it.digest || ''));
  const input = el('input', { type: 'text', placeholder: 'what was decided, and why' });
  input.style.width = '100%';
  d.appendChild(input);
  const f = el('form', { method: 'dialog', class: 'row' });
  f.appendChild(el('button', {
    class: 'primary', type: 'button',
    onclick: async () => {
      const note = input.value.trim();
      if (!note) { input.focus(); return; }
      d.close();
      try {
        await api('/v1/inbox/review', {
          method: 'POST',
          body: JSON.stringify(Object.assign(scopeBody(), {
            ref: it.ref, action: 'resolve', note,
          })),
        });
        toast('Resolved');
        await renderInbox();
      } catch (e) { toast('Could not resolve — ' + e.message); }
    },
  }, 'Resolve'));
  f.appendChild(el('button', null, 'Cancel'));
  d.appendChild(f);
  document.body.appendChild(d);
  d.addEventListener('close', () => d.remove());
  d.showModal();
  input.focus();
}

const scopeBody = () => ({
  tenant_id: state.scope.tenant_id,
  project_id: state.scope.project_id,
  principal_id: state.scope.principal_id,
});

/* =============================================================== DEBUGGER */
/* §3.6 — build this second. Plan, per-arm results, fusion with the score
 * decomposition, and what was dropped and why. */

async function renderDebug(query) {
  const input = el('input', {
    type: 'search', placeholder: 'why did we choose pgvector?', value: query || '',
  });
  const run = el('button', { class: 'primary' }, 'Run');
  const out = el('div');

  const go = async () => {
    const text = input.value.trim();
    if (!text) return;
    location.hash = '#/debug?q=' + encodeURIComponent(text);
    out.textContent = '';
    out.appendChild(el('p', { class: 'muted' }, 'running…'));
    try {
      const pack = await api('/v1/context', {
        method: 'POST',
        body: JSON.stringify(Object.assign(scopeBody(), {
          task: text, token_budget: 4000,
        })),
      });
      // Per-arm counts are NOT on the pack — they live on the recorded
      // retrieval_event, which is the right source anyway: /v1/explain answers
      // from what was recorded rather than recomputing, so the arms shown are
      // the arms that actually ran for this pack.
      let event = null;
      if (pack.pack_id) {
        try { event = await api('/v1/explain?' + q({ pack_id: pack.pack_id })); }
        catch { /* the pack still renders without it */ }
      }
      out.textContent = '';
      out.appendChild(renderPack(pack, event));
    } catch (e) { out.textContent = ''; out.appendChild(fail(e)); }
  };
  run.onclick = go;
  input.onkeydown = (e) => { if (e.key === 'Enter') go(); };

  const bar = el('div', { class: 'row' });
  input.classList.add('grow');
  bar.appendChild(input);
  bar.appendChild(run);

  view(header('Retrieval Debugger',
              'Why did retrieval return — or drop — this? Answered from what was ' +
              'recorded, not recomputed.'),
       bar, out);
  if (query) { input.value = query; go(); }
  else input.focus();
}

function renderPack(pack, event) {
  const f = document.createDocumentFragment();

  // PLAN — what the planner decided to look for, before any retrieval ran.
  const plan = pack.plan;
  if (plan) {
    const s = el('div', { class: 'stage' });
    s.appendChild(el('h3', null, 'Plan'));
    const bits = [
      `intent=${plan.intent}`,
      `types=[${(plan.memory_types || []).join(',')}]`,
      plan.identifiers && plan.identifiers.length ? `ids=[${plan.identifiers.join(',')}]` : null,
      plan.temporal ? `window=${JSON.stringify(plan.temporal)}` : 'window=none',
      plan.needs_graph ? 'graph=yes' : 'graph=no',
      `stage=${plan.stage}`,
    ].filter(Boolean);
    s.appendChild(el('div', { class: 'mono' }, bits.join('  ')));
    if (plan.matched_on) {
      // The phrase the classifier keyed on. Without it, "why did it think this
      // was a rationale question?" is unanswerable.
      s.appendChild(el('div', { class: 'muted' }, `matched on “${plan.matched_on}”`));
    }
    f.appendChild(s);
  }

  // ARMS — from the recorded retrieval_event.
  const arms = event && event.arm_results;
  if (arms) {
    const s = el('div', { class: 'stage' });
    s.appendChild(el('h3', null, 'Arms'));
    const t = el('table');
    t.appendChild(raw('thead', null, '<tr><th>arm</th><th>hits</th><th></th></tr>'));
    const tb = el('tbody');
    const max = Math.max(1, ...Object.values(arms).map((v) => count(v)));
    for (const [name, v] of Object.entries(arms)) {
      const n = count(v);
      const tr = el('tr');
      tr.appendChild(el('td', { class: 'mono' }, name));
      tr.appendChild(el('td', { class: 'num' }, n));
      const cell = el('td');
      const b = el('span', { class: 'bar' });
      b.style.width = Math.round((n / max) * 220) + 'px';
      cell.appendChild(b);
      // An arm returning zero is a finding, not a blank: it is how you notice
      // the graph arm is contributing nothing because entities never extracted.
      if (n === 0) cell.appendChild(el('span', { class: 'muted' }, ' contributed nothing'));
      tr.appendChild(cell);
      tb.appendChild(tr);
    }
    t.appendChild(tb);
    s.appendChild(t);
    f.appendChild(s);
  }

  // FUSION — every returned item with its score decomposition.
  const items = [];
  for (const [section, list] of Object.entries(pack.sections || {})) {
    (list || []).forEach((it) => items.push([section, it]));
  }
  const s2 = el('div', { class: 'stage' });
  s2.appendChild(el('h3', null, `Pack — ${items.length} items`));
  items.forEach(([section, it]) => {
    if (!it) return;
    if (section === 'contested') {
      // Contested entries carry two sides rather than one memory. Rendering
      // them through the item path would silently drop them, and a debugger
      // that hides the contested section is hiding the one thing the pack
      // most needs a human to look at.
      const row = el('div', { class: 'item trust trust-untrusted' });
      row.appendChild(el('div', { class: 'meta' },));
      row.appendChild(el('div', { class: 'title' }, 'contested — ' + (it.kind || '')));
      (it.sides || []).forEach((sd) =>
        row.appendChild(el('div', { class: 'digest' },
          `${sd.trust}: ${sd.title || ''}`)));
      s2.appendChild(row);
      return;
    }
    if (!it.ref) return;
    const tier = it.trust || 'observed';
    const row = el('div', { class: `item trust trust-${tier}` });
    const m = el('div', { class: 'meta' });
    m.appendChild(el('span', { class: `tier tier-${tier}` }, tier));
    m.appendChild(el('span', null, section));
    m.appendChild(el('span', null, `${it.token_cost || 0} tok`));
    m.appendChild(el('span', null, `score ${it.score}`));
    row.appendChild(m);
    row.appendChild(el('div', { class: 'title' }, it.title || ''));
    if (it.digest) row.appendChild(el('div', { class: 'digest' }, it.digest));
    if (it.score_parts) row.appendChild(parts(it.score_parts));
    row.appendChild(el('button', {
      onclick: () => showMemory(it.ref),
    }, 'provenance'));
    s2.appendChild(row);
  });
  f.appendChild(s2);

  // DROPPED — the half of the story a pack alone never tells. This is the
  // section that answers "why isn't ADR-0001 in here", which is the question
  // that actually gets asked.
  // Rendered even when empty. An absent section is ambiguous — did nothing get
  // dropped, or did the debugger not tell me? "nothing dropped" is an answer;
  // a missing heading is not.
  const dropped = pack.dropped || [];
  {
    const s3 = el('div', { class: 'stage' });
    s3.appendChild(el('h3', null, `Dropped — ${dropped.length}`));
    if (!dropped.length) {
      s3.appendChild(el('div', { class: 'muted' },
        'Nothing was dropped — every candidate that scored above the floor fit ' +
        'in the budget.'));
    }
    dropped.forEach((d) => {
      const line = el('div', { class: 'dropped mono' });
      line.appendChild(el('span', null,
        String(d.title || d.id || '').slice(0, 58).padEnd(60, ' ')));
      if (typeof d.score === 'number') {
        line.appendChild(el('span', null, d.score.toFixed(3) + '  '));
      }
      line.appendChild(el('span', { class: 'why' }, d.reason || 'dropped'));
      s3.appendChild(line);
    });
    f.appendChild(s3);
  }

  // TIMINGS + BUDGET + rerank state.
  const t = pack.timings_ms || {};
  const foot = el('div', { class: 'stage' });
  foot.appendChild(el('h3', null, 'Cost'));
  const b = pack.budget || {};
  foot.appendChild(el('div', { class: 'mono' },
    `${b.used || 0} / ${b.effective || 0} tokens` +
    (b.requested && b.requested !== b.effective ? ` (asked ${b.requested})` : '')));
  if (b.reason) foot.appendChild(el('div', { class: 'muted' }, b.reason));
  foot.appendChild(el('div', { class: 'mono muted' },
    Object.entries(t).map(([k, v]) => `${k} ${v}ms`).join('  ')));
  if (pack.rerank) {
    foot.appendChild(el('div', { class: 'muted' },
      `cross-encoder ${pack.rerank.applied ? 'applied' : 'not applied'}` +
      (pack.rerank.reason ? ` — ${pack.rerank.reason}` : '')));
  }
  if (pack.degraded) {
    // Loud: a degraded pack came back without an arm, and a silently smaller
    // pack is exactly the failure ADR-0008 warns about.
    foot.appendChild(el('div', { class: 'flag' },
      '⚠ degraded — at least one retrieval arm did not run'));
  }
  foot.appendChild(el('div', { class: 'mono muted' },
    `profile ${pack.ranking_profile || '?'} · pack ${pack.pack_id || '?'}`));

  // §3.6 starred affordance: export as an eval case. This is how the golden set
  // stays representative instead of going stale — real production queries, with
  // what was actually returned marked as the expected answer.
  foot.appendChild(el('button', {
    onclick: () => exportEvalCase(pack),
  }, 'export as eval case'));
  f.appendChild(foot);
  return f;
}

function exportEvalCase(pack) {
  const expected = [];
  for (const list of Object.values(pack.sections || {})) {
    (list || []).forEach((it) => { if (it && it.ref) expected.push(it.title); });
  }
  const blob = new Blob([JSON.stringify({
    query: pack.task,
    intent: (pack.plan || {}).intent,
    expected_titles: expected,
    captured_from: pack.pack_id,
    ranking_profile: pack.ranking_profile,
    note: 'Review expected_titles before adding to the golden set.',
  }, null, 2)], { type: 'application/json' });
  const a = el('a', {
    href: URL.createObjectURL(blob),
    download: `eval-case-${(pack.pack_id || 'pack').replace(/[^\w-]/g, '')}.json`,
  });
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast('Exported — review the expected set before committing it');
}

const count = (v) => Array.isArray(v) ? v.length : (typeof v === 'number' ? v : (v && v.count) || 0);

function parts(p) {
  const d = el('div', { class: 'parts' });
  Object.entries(p).forEach(([k, v]) => {
    if (typeof v !== 'number') return;
    d.appendChild(el('span', { class: v >= 0 ? 'pos' : 'neg' },
                     ` ${k} ${v >= 0 ? '+' : ''}${v.toFixed(3)}`));
  });
  return d;
}

/* ========================================================= MEMORY DETAIL */
/* §3.3. Provenance is the part that "converts skeptics", so it leads. */

async function showMemory(ref) {
  const d = el('dialog');
  d.appendChild(el('p', { class: 'muted' }, 'loading…'));
  document.body.appendChild(d);
  d.addEventListener('close', () => d.remove());
  d.showModal();
  try {
    const x = await api('/v1/explain?' + q({ ref }));
    const m = x.memory;
    d.textContent = '';
    const head = el('div', { class: `trust trust-${m.tier}` });
    head.appendChild(el('div', { class: `tier tier-${m.tier}` },
                        `${m.tier} · ${m.type} · ${m.status}`));
    head.appendChild(el('h2', null, m.title || ''));
    d.appendChild(head);

    d.appendChild(el('h2', null, 'Provenance'));
    d.appendChild(el('div', { class: 'mono' }, x.provenance || m.source_type));
    d.appendChild(el('div', { class: 'muted' },
                     `recorded ${m.recorded_at} · valid ${m.valid_at}`));

    if (x.versions && x.versions.length) {
      d.appendChild(el('h2', null, 'History'));
      const t = el('table');
      const tb = el('tbody');
      x.versions.forEach((v) => {
        const tr = el('tr');
        tr.appendChild(el('td', { class: 'num' }, 'v' + v.version));
        tr.appendChild(el('td', null, v.operation));
        tr.appendChild(el('td', { class: 'mono' }, v.changed_at));
        tb.appendChild(tr);
      });
      t.appendChild(tb);
      d.appendChild(t);
    }
    if (x.supersessions && x.supersessions.length) {
      d.appendChild(el('h2', null, 'Supersessions'));
      x.supersessions.forEach((sp) =>
        d.appendChild(el('div', { class: 'mono' },
          `${sp.old_id.slice(0, 8)} → ${sp.new_id.slice(0, 8)}  ${sp.reason || ''}`)));
    }
    d.appendChild(el('h2', null, 'Digest'));
    d.appendChild(el('div', null, m.digest || ''));

    const f = el('form', { method: 'dialog' });
    f.appendChild(el('button', { class: 'primary' }, 'Close'));
    d.appendChild(f);
  } catch (e) {
    d.textContent = '';
    d.appendChild(fail(e));
    const f = el('form', { method: 'dialog' });
    f.appendChild(el('button', null, 'Close'));
    d.appendChild(f);
  }
}

/* =============================================================== CONFLICTS */
/* §3.7 — side by side, each side with trust, valid time and source. */

async function renderConflicts() {
  let d;
  try { d = await api('/v1/conflicts?' + q()); }
  catch (e) { return view(header('Conflicts'), fail(e)); }

  const list = d.conflicts || d.unresolved || d.items || [];
  if (!list.length) {
    return view(header('Conflicts'),
      empty('No contested points.',
            'Detected contradictions appear here, and in every context pack ' +
            'until they are resolved.'));
  }
  const wrap = el('div');
  list.forEach((k) => {
    const card = el('div', { class: 'card' });
    card.appendChild(el('div', { class: 'mono muted' }, k.kind || ''));
    const grid = el('div', { class: 'row' });
    (k.sides || []).forEach((s) => {
      const side = el('div', { class: `trust trust-${s.trust} grow` });
      side.appendChild(el('div', { class: `tier tier-${s.trust}` }, s.trust));
      side.appendChild(el('div', { class: 'title' }, s.title || ''));
      side.appendChild(el('div', { class: 'digest' }, s.digest || ''));
      side.appendChild(el('div', { class: 'muted mono' }, s.recorded_at || ''));
      side.appendChild(el('button', { onclick: () => showMemory(s.ref) }, 'provenance'));
      grid.appendChild(side);
    });
    card.appendChild(grid);
    if (k.note) card.appendChild(el('p', { class: 'muted' }, k.note));
    card.appendChild(el('button', {
      onclick: () => {
        inbox.items = [{ ref: k.conflict_id || k.id, kind: 'conflict',
                         title: (k.sides || [{}])[0].title, digest: k.note }];
        resolveAt(0);
      },
    }, 'resolve…'));
    wrap.appendChild(card);
  });
  view(header('Conflicts', `${list.length} unresolved`), wrap);
}

/* ================================================================== HEALTH */
/* §3.8 — the score is a transparent composite; the formula ships with it. */

async function renderHealth() {
  let d;
  try { d = await api('/v1/health/project?' + q()); }
  catch (e) { return view(header('Health'), fail(e)); }

  const c = d.counts, cur = d.curation;
  const top = el('div', { class: 'card' });
  top.appendChild(el('div', { class: 'big' }, d.health + ' / 100'));

  const stats = el('div', { class: 'stats' });
  const stat = (k, v, cls) => {
    const s = el('div', { class: 'stat' });
    s.appendChild(el('div', { class: 'k' }, k));
    s.appendChild(el('div', { class: 'v ' + (cls || '') }, v));
    stats.appendChild(s);
  };
  stat('active', c.active);
  stat('awaiting review', c.quarantined, c.quarantined > 25 ? 'warn' : '');
  stat('contested', c.contested, c.contested ? 'warn' : '');
  stat('stale >90d', c.stale, c.stale ? 'warn' : '');
  stat('never retrieved', c.never_used, c.never_used > c.active / 2 ? 'warn' : '');
  top.appendChild(stats);

  // The formula, not just the number. An opaque health score is worse than none.
  const t = el('table');
  t.appendChild(raw('thead', null,
    '<tr><th>component</th><th>penalty</th><th>weight</th><th>cost</th></tr>'));
  const tb = el('tbody');
  d.formula.forEach((r) => {
    const tr = el('tr');
    tr.appendChild(el('td', null, r.component));
    tr.appendChild(el('td', { class: 'num' }, r.penalty));
    tr.appendChild(el('td', { class: 'num' }, r.weight));
    tr.appendChild(el('td', { class: 'num' }, '-' + r.cost));
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  top.appendChild(el('h2', null, 'How this is calculated'));
  top.appendChild(t);

  // Curation, including the ADR-0015 kill switch. An operator staring at a
  // silent extractor must be able to tell "disabled by policy" from "broken".
  const cc = el('div', { class: 'card' });
  cc.appendChild(el('h2', null, 'Curation'));
  cc.appendChild(el('div', {
    class: 'tier tier-' + (cur.extraction_allowed ? 'authoritative' : 'untrusted'),
  }, 'LLM extraction ' + (cur.extraction_allowed ? 'enabled' : 'DISABLED')));
  cc.appendChild(el('div', { class: 'muted' }, cur.extraction_reason));
  cc.appendChild(el('div', { class: 'muted' }, 'Acceptance: ' + cur.acceptance.band));
  (cur.alerts || []).forEach((a) =>
    cc.appendChild(el('div', { class: 'flag' }, '⚠ ' + a)));

  const kids = [header('Project health'), top, cc];
  if (d.top_retrieved && d.top_retrieved.length) {
    const tr = el('div', { class: 'card' });
    tr.appendChild(el('h2', null, 'Most retrieved'));
    d.top_retrieved.forEach((m) => {
      const line = el('div', { class: 'row' });
      line.appendChild(el('span', { class: 'grow' }, m.title || ''));
      line.appendChild(el('span', { class: 'mono' }, m.uses + '×'));
      tr.appendChild(line);
    });
    kids.push(tr);
  }
  view(...kids);
}

/* ================================================================ keyboard */
let gPending = false;

document.addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
  if (document.querySelector('dialog[open]') && e.key !== '?') return;

  if (gPending) {
    gPending = false;
    const hit = ROUTES.find(([, , key]) => key === e.key);
    if (hit) { location.hash = '#/' + hit[0]; return; }
  }
  switch (e.key) {
    case '?': $('#help').showModal(); return;
    case 'g': gPending = true; setTimeout(() => { gPending = false; }, 900); return;
    case 'u': runUndo(); return;
  }
  if (state.route !== 'inbox' || !inbox.items.length) return;
  switch (e.key) {
    case 'j': inbox.cursor = Math.min(inbox.cursor + 1, inbox.items.length - 1); paintCursor(); break;
    case 'k': inbox.cursor = Math.max(inbox.cursor - 1, 0); paintCursor(); break;
    case 's': inbox.cursor = Math.min(inbox.cursor + 1, inbox.items.length - 1); paintCursor(); break;
    case 'a': decide(inbox.cursor, 'observed'); break;
    case 'v': decide(inbox.cursor, 'verified'); break;
    case 'r':
      (inbox.items[inbox.cursor] || {}).kind === 'conflict'
        ? resolveAt(inbox.cursor) : rejectAt(inbox.cursor);
      break;
  }
});

$('#help-btn').onclick = () => $('#help').showModal();

/* ================================================================ routing */
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [path, qs] = h.split('?');
  return { path: path || 'inbox', params: new URLSearchParams(qs || '') };
}

async function route() {
  const { path, params } = parseHash();
  state.route = ROUTES.some(([id]) => id === path) ? path : 'inbox';
  renderNav();
  if (state.route === 'inbox') return renderInbox();
  if (state.route === 'debug') return renderDebug(params.get('q'));
  if (state.route === 'conflicts') return renderConflicts();
  if (state.route === 'health') return renderHealth();
}
window.addEventListener('hashchange', route);

/* ================================================================== boot */
async function pingLoop() {
  const set = (cls, txt) => { const c = $('#conn'); c.className = 'conn ' + cls; c.textContent = txt; };
  const tick = async () => {
    try {
      const r = await api('/readyz');
      set(r.ready ? 'ok' : 'down', r.ready ? 'ready' : 'degraded');
    } catch { set('down', 'api unreachable'); }
  };
  await tick();
  setInterval(tick, 15000);
}

async function askForScope(cfg) {
  const d = el('dialog');
  d.appendChild(el('h2', null, 'Choose a scope'));
  d.appendChild(el('p', { class: 'muted' },
    'No dev binding is configured, so the console will not guess a tenant — ' +
    'guessing one means showing you someone else’s queue.'));
  const t = el('input', { type: 'text', placeholder: 'tenant id (uuid)' });
  const p = el('input', { type: 'text', placeholder: 'principal id (uuid)' });
  [t, p].forEach((i) => { i.style.width = '100%'; i.style.marginBottom = '6px'; d.appendChild(i); });
  const go = el('button', { class: 'primary' }, 'Open');
  d.appendChild(go);
  document.body.appendChild(d);
  d.showModal();
  return new Promise((resolve) => {
    go.onclick = () => {
      if (!t.value.trim()) { t.focus(); return; }
      d.close(); d.remove();
      resolve({ tenant_id: t.value.trim(), principal_id: p.value.trim() || t.value.trim() });
    };
  });
}

(async function boot() {
  pingLoop();
  try {
    let scope = JSON.parse(localStorage.getItem('scope') || 'null');
    if (!scope) {
      const cfg = await api('/v1/console/config');
      scope = cfg.tenant_id
        ? { tenant_id: cfg.tenant_id, project_id: cfg.project_id,
            principal_id: cfg.principal_id || cfg.tenant_id }
        : await askForScope(cfg);
    }
    state.scope = scope;

    const { projects } = await api('/v1/projects?' + new URLSearchParams({
      tenant_id: scope.tenant_id, principal_id: scope.principal_id,
    }));
    state.projects = projects;
    if (!projects.length) {
      return view(header('No projects'),
        empty('This tenant has no projects yet.',
              'Run `memory init` in a repository to register one.'));
    }
    if (!scope.project_id || !projects.some((p) => p.id === scope.project_id)) {
      scope.project_id = projects[0].id;
    }
    localStorage.setItem('scope', JSON.stringify(scope));
    renderSwitcher();
    route();
  } catch (e) {
    view(header('Console cannot start'), fail(e),
         el('p', { class: 'muted' },
            'Check that the api service is healthy: docker compose ps api'));
  }
})();
