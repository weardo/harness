'use strict';

(function () {
  const config = window.__HARNESS_PRODUCT_CONFIG__ || {
    apiBase: '/api',
    projectName: 'harness',
  };

  const appEl = document.getElementById('app');
  const projectNameEl = document.getElementById('project-name');
  const apiBaseEl = document.getElementById('api-base');

  if (projectNameEl) projectNameEl.textContent = config.projectName || 'harness';
  if (apiBaseEl) apiBaseEl.textContent = config.apiBase || '/api';

  async function api(path, options) {
    const response = await fetch((config.apiBase || '/api') + path, options);
    const text = await response.text();
    let body = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = text;
      }
    }
    if (!response.ok) {
      const message = body && body.error ? body.error : response.statusText;
      throw new Error(message || 'Request failed');
    }
    return body;
  }

  function escapeHtml(value) {
    return String(value || '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;');
  }

  function statusPill(status) {
    const normalized = String(status || 'unknown').toLowerCase();
    return `<span class="pill ${normalized}">${escapeHtml(normalized)}</span>`;
  }

  function linesToListItems(value) {
    return String(value || '')
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => `<div class="microcopy">${escapeHtml(line)}</div>`)
      .join('');
  }

  function launchErrorBanner(request) {
    const launchError = request && request.metadata && request.metadata.launch_error;
    if (!launchError) return '';
    return `
      <div class="banner error" style="margin-top:12px">
        ${escapeHtml(launchError)}
      </div>
    `;
  }

  function isRequestRunning(request) {
    if (!request) return false;
    if (String(request.status || '').toLowerCase() === 'running') return true;
    return Boolean(request.metadata && request.metadata.active_launch_id);
  }

  function requestActionLabel(request) {
    if (isRequestRunning(request)) return 'Running…';
    const status = String((request && request.status) || '').toLowerCase();
    if (status === 'completed') return 'Run Again';
    if (status === 'blocked') return 'Retry Run';
    return 'Start Run';
  }

  function renderNav(route) {
    document.querySelectorAll('.nav a').forEach((link) => {
      const isActive = link.dataset.route === route;
      link.classList.toggle('active', isActive);
    });
  }

  function setView(html) {
    if (appEl) appEl.innerHTML = html;
  }

  function setLoading(title, copy) {
    setView(`
      <div class="empty-state">
        <h2>${escapeHtml(title)}</h2>
        <p class="muted">${escapeHtml(copy || '')}</p>
      </div>
    `);
  }

  function setError(message) {
    setView(`
      <div class="banner error">
        ${escapeHtml(message)}
      </div>
    `);
  }

  function routeParts() {
    const hash = window.location.hash.replace(/^#/, '') || 'requests';
    const [name, param] = hash.split('/');
    return { name, param };
  }

  async function renderRequestsView() {
    renderNav('requests');
    setLoading('Loading requests…', 'Gathering product requests and recent runs.');

    const [requests, runs] = await Promise.all([
      api('/requests'),
      api('/runs?limit=8'),
    ]);

    const stats = {
      totalRequests: requests.length,
      newRequests: requests.filter((item) => item.status === 'new').length,
      runningRequests: requests.filter((item) => item.status === 'running').length,
      totalRuns: runs.length,
    };

    setView(`
      <div class="layout-grid">
        <section class="panel section-card">
          <div class="section-head">
            <div>
              <div class="kicker">Inbox</div>
              <h2>Product Requests</h2>
              <p class="section-copy">Capture product work, then launch Harness from an explicit request object.</p>
            </div>
          </div>
          <div class="stats-grid">
            <div class="stat"><span class="label">Total Requests</span><strong>${stats.totalRequests}</strong></div>
            <div class="stat"><span class="label">New</span><strong>${stats.newRequests}</strong></div>
            <div class="stat"><span class="label">Running</span><strong>${stats.runningRequests}</strong></div>
            <div class="stat"><span class="label">Recent Runs</span><strong>${stats.totalRuns}</strong></div>
          </div>
          <div class="request-list" style="margin-top:18px">
            ${
              requests.length
                ? requests.map((request) => `
                  <a class="request-item" href="#request/${escapeHtml(request.id)}">
                    <div class="item-row">
                      <div>
                        <h3 class="item-title">${escapeHtml(request.title || request.id)}</h3>
                        <div class="microcopy">${escapeHtml(request.description || '')}</div>
                      </div>
                      ${statusPill(request.status || 'new')}
                    </div>
                    <div class="item-row">
                      <div class="item-meta">Source: ${escapeHtml(request.source || 'manual')} • Priority: ${escapeHtml(request.priority || 'normal')}</div>
                      <div class="microcopy mono">${escapeHtml(request.id)}</div>
                    </div>
                    ${launchErrorBanner(request)}
                  </a>
                `).join('')
                : `
                  <div class="empty-state">
                    <h3>No requests yet</h3>
                    <p class="muted">Create the first product request on the right to start the loop.</p>
                  </div>
                `
            }
          </div>
        </section>

        <section class="panel section-card">
          <div class="section-head">
            <div>
              <div class="kicker">Create</div>
              <h2>New Request</h2>
              <p class="section-copy">Keep the first version explicit and inspectable.</p>
            </div>
          </div>
          <form id="request-form" class="form-grid">
            <div class="field">
              <label for="request-title">Title</label>
              <input id="request-title" name="title" required placeholder="Build request intake UI">
            </div>
            <div class="field">
              <label for="request-description">Description</label>
              <textarea id="request-description" name="description" required placeholder="Describe the work in a few focused sentences."></textarea>
            </div>
            <div class="field">
              <label for="request-criteria">Acceptance Criteria</label>
              <textarea id="request-criteria" name="criteria" placeholder="One line per acceptance criterion"></textarea>
            </div>
            <div class="field">
              <label for="request-links">Linked Paths</label>
              <textarea id="request-links" name="links" placeholder="One project-relative path per line"></textarea>
            </div>
            <div class="actions">
              <button class="btn primary" type="submit">Create Request</button>
              <span id="request-form-status" class="microcopy"></span>
            </div>
          </form>

          <div class="section-head" style="margin-top:26px">
            <div>
              <div class="kicker">Recent</div>
              <h3>Runs</h3>
            </div>
          </div>
          <div class="run-list">
            ${
              runs.length
                ? runs.map((run) => `
                  <a class="run-item" href="#run/${escapeHtml(run.run_id)}">
                    <div class="item-row">
                      <strong>${escapeHtml(run.run_id)}</strong>
                      ${statusPill(run.status)}
                    </div>
                    <div class="item-row">
                      <div class="item-meta">${escapeHtml(run.prompt || 'No prompt stored')}</div>
                      <div class="microcopy">${escapeHtml(String(run.features_passing || 0))}/${escapeHtml(String(run.features_total || 0))} passing</div>
                    </div>
                  </a>
                `).join('')
                : `<div class="empty-state"><h3>No runs yet</h3><p class="muted">Launch a request to populate this timeline.</p></div>`
            }
          </div>
        </section>
      </div>
    `);

    const form = document.getElementById('request-form');
    const statusEl = document.getElementById('request-form-status');
    if (!form) return;

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const submitButton = form.querySelector('button[type="submit"]');
      const title = document.getElementById('request-title').value.trim();
      const description = document.getElementById('request-description').value.trim();
      const criteria = document.getElementById('request-criteria').value;
      const links = document.getElementById('request-links').value;

      if (statusEl) statusEl.textContent = 'Creating request…';
      if (submitButton) submitButton.disabled = true;

      try {
        const request = await api('/requests', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title,
            description,
            acceptance_criteria: criteria.split('\n').map((line) => line.trim()).filter(Boolean),
            linked_paths: links.split('\n').map((line) => line.trim()).filter(Boolean),
          }),
        });
        window.location.hash = `#request/${request.id}`;
      } catch (error) {
        if (statusEl) statusEl.textContent = error.message;
      } finally {
        if (submitButton) submitButton.disabled = false;
      }
    });
  }

  async function renderRequestDetail(requestId) {
    renderNav('requests');
    setLoading('Loading request…', 'Fetching request details and assembled context.');

    const request = await api(`/requests/${encodeURIComponent(requestId)}`);
    const context = await api(`/requests/${encodeURIComponent(requestId)}/context`);
    const lastRunId = request.metadata && request.metadata.last_run_id;
    const requestRunning = isRequestRunning(request);

    setView(`
      <div class="detail-grid">
        <section class="panel section-card">
          <div class="section-head">
            <div>
              <div class="kicker">Request</div>
              <h2>${escapeHtml(request.title || request.id)}</h2>
              <p class="section-copy">${escapeHtml(request.description || '')}</p>
            </div>
            ${statusPill(request.status || 'new')}
          </div>
          <div class="actions">
            <button id="start-run-button" class="btn primary" ${requestRunning ? 'disabled' : ''}>${escapeHtml(requestActionLabel(request))}</button>
            <a class="btn secondary" href="#requests">Back to Requests</a>
            ${
              lastRunId
                ? `<a class="btn secondary" href="#run/${escapeHtml(lastRunId)}">Open Last Run</a>`
                : ''
            }
            <span id="run-action-status" class="microcopy"></span>
          </div>
          ${launchErrorBanner(request)}
        </section>

        <div class="layout-grid">
          <section class="panel section-card">
            <div class="section-head">
              <div>
                <div class="kicker">Assembled Context</div>
                <h3>Execution Input</h3>
              </div>
            </div>
            <div class="key-value">
              <div class="kv-row"><span>Objective</span><strong>${escapeHtml(context.objective || '')}</strong></div>
              <div class="kv-row"><span>Summary</span><strong>${escapeHtml(context.request_summary || '')}</strong></div>
              <div class="kv-row"><span>Source</span><strong>${escapeHtml(context.source || '')}</strong></div>
              <div class="kv-row"><span>Request ID</span><strong class="mono">${escapeHtml(context.request_id || '')}</strong></div>
            </div>

            <div style="margin-top:18px">
              <div class="panel-label">Acceptance Criteria</div>
              <div class="criteria-list">
                ${
                  (context.acceptance_criteria || []).length
                    ? context.acceptance_criteria.map((item) => `
                      <div class="artifact-item">${escapeHtml(item)}</div>
                    `).join('')
                    : `<div class="muted">No acceptance criteria yet.</div>`
                }
              </div>
            </div>

            <div style="margin-top:18px">
              <div class="panel-label">Linked Paths</div>
              <div class="context-list">
                ${
                  (context.linked_artifacts || []).length
                    ? context.linked_artifacts.map((artifact) => `
                      <div class="artifact-item">
                        <div><strong>${escapeHtml(artifact.name || '')}</strong></div>
                        <div class="microcopy mono">${escapeHtml(artifact.path || '')}</div>
                      </div>
                    `).join('')
                    : `<div class="muted">No linked files resolved on disk.</div>`
                }
              </div>
            </div>
          </section>

          <section class="panel section-card">
            <div class="section-head">
              <div>
                <div class="kicker">Raw Request</div>
                <h3>Reference</h3>
              </div>
            </div>
            <div class="key-value">
              <div class="kv-row"><span>ID</span><strong class="mono">${escapeHtml(request.id || '')}</strong></div>
              <div class="kv-row"><span>Project</span><strong>${escapeHtml(request.project_id || '')}</strong></div>
              <div class="kv-row"><span>Priority</span><strong>${escapeHtml(request.priority || '')}</strong></div>
              <div class="kv-row"><span>Source</span><strong>${escapeHtml(request.source || '')}</strong></div>
            </div>
            <div style="margin-top:18px">
              <div class="panel-label">Description Lines</div>
              ${linesToListItems(request.description || '') || '<div class="muted">No description.</div>'}
            </div>
          </section>
        </div>
      </div>
    `);

    const actionButton = document.getElementById('start-run-button');
    const statusEl = document.getElementById('run-action-status');
    if (!actionButton) return;
    if (requestRunning && statusEl) {
      statusEl.textContent = 'Run currently in progress.';
    }

    actionButton.addEventListener('click', async () => {
      if (isRequestRunning(request)) return;
      actionButton.disabled = true;
      if (statusEl) statusEl.textContent = 'Launching Harness…';
      try {
        const result = await api(`/requests/${encodeURIComponent(requestId)}/runs`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        if (result.run_id) {
          window.location.hash = `#run/${result.run_id}`;
          return;
        }
        if (statusEl) statusEl.textContent = result.summary || 'Run launched.';
        await renderRequestDetail(requestId);
      } catch (error) {
        if (statusEl) statusEl.textContent = error.message;
      } finally {
        if (!isRequestRunning(request)) {
          actionButton.disabled = false;
        }
      }
    });
  }

  async function renderRunsView() {
    renderNav('runs');
    setLoading('Loading runs…', 'Fetching local run summaries from the product read model.');

    const runs = await api('/runs');

    setView(`
      <section class="panel section-card">
        <div class="section-head">
          <div>
            <div class="kicker">Runs</div>
            <h2>Execution History</h2>
            <p class="section-copy">Stable run summaries and links into artifacts and normalized events.</p>
          </div>
        </div>
        <div class="run-list">
          ${
            runs.length
              ? runs.map((run) => `
                <a class="run-item" href="#run/${escapeHtml(run.run_id)}">
                  <div class="item-row">
                    <div>
                      <div class="item-title mono">${escapeHtml(run.run_id)}</div>
                      <div class="microcopy">${escapeHtml(run.prompt || 'No prompt stored')}</div>
                    </div>
                    ${statusPill(run.status)}
                  </div>
                  <div class="item-row">
                    <div class="item-meta">Started: ${escapeHtml(run.started_at || 'unknown')}</div>
                    <div class="microcopy">
                      ${escapeHtml(String(run.features_passing || 0))}/${escapeHtml(String(run.features_total || 0))} passing
                      • $${escapeHtml(String(run.total_cost_usd || 0))}
                    </div>
                  </div>
                </a>
              `).join('')
              : `<div class="empty-state"><h3>No runs yet</h3><p class="muted">Start a request to create the first run record.</p></div>`
          }
        </div>
      </section>
    `);
  }

  async function renderRunDetail(runId) {
    renderNav('runs');
    setLoading('Loading run…', 'Collecting run detail, surfaced artifacts, and normalized events.');

    const [run, events] = await Promise.all([
      api(`/runs/${encodeURIComponent(runId)}`),
      api(`/runs/${encodeURIComponent(runId)}/events`),
    ]);

    const artifacts = run.artifacts || [];
    const featureCounts = run.feature_counts || {};
    const state = run.state || {};

    setView(`
      <div class="detail-grid">
        <section class="panel section-card">
          <div class="section-head">
            <div>
              <div class="kicker">Run</div>
              <h2 class="mono">${escapeHtml(run.run_id || runId)}</h2>
              <p class="section-copy">${escapeHtml(run.prompt || 'No prompt stored for this run.')}</p>
            </div>
            ${statusPill(run.status)}
          </div>
          <div class="stats-grid">
            <div class="stat"><span class="label">Features</span><strong>${escapeHtml(String(featureCounts.passing || 0))}/${escapeHtml(String(featureCounts.total || 0))}</strong></div>
            <div class="stat"><span class="label">Blocked</span><strong>${escapeHtml(String(featureCounts.blocked || 0))}</strong></div>
            <div class="stat"><span class="label">Remaining</span><strong>${escapeHtml(String(featureCounts.remaining || 0))}</strong></div>
            <div class="stat"><span class="label">Cost</span><strong>$${escapeHtml(String(run.total_cost_usd || 0))}</strong></div>
          </div>
        </section>

        <div class="layout-grid">
          <section class="panel section-card">
            <div class="section-head">
              <div>
                <div class="kicker">Run State</div>
                <h3>Summary</h3>
              </div>
            </div>
            <div class="key-value">
              <div class="kv-row"><span>Status</span><strong>${escapeHtml(run.status || '')}</strong></div>
              <div class="kv-row"><span>Started</span><strong>${escapeHtml(run.started_at || 'unknown')}</strong></div>
              <div class="kv-row"><span>Completed</span><strong>${escapeHtml(run.completed_at || 'in progress')}</strong></div>
              <div class="kv-row"><span>Phase</span><strong>${escapeHtml(state.phase || 'unknown')}</strong></div>
              <div class="kv-row"><span>Current Feature</span><strong class="mono">${escapeHtml(state.current_feature_id || '—')}</strong></div>
              <div class="kv-row"><span>Iteration</span><strong>${escapeHtml(String(state.iteration || 0))}</strong></div>
            </div>
          </section>

          <section class="panel section-card">
            <div class="section-head">
              <div>
                <div class="kicker">Artifacts</div>
                <h3>Surfaced Files</h3>
              </div>
            </div>
            <div class="artifact-list">
              ${
                artifacts.length
                  ? artifacts.map((artifact) => `
                    <div class="artifact-item">
                      <div class="item-row">
                        <strong>${escapeHtml(artifact.kind || 'artifact')}</strong>
                        <span class="microcopy">${escapeHtml(String((artifact.metadata || {}).size_bytes || 0))} bytes</span>
                      </div>
                      <div class="microcopy mono">${escapeHtml(artifact.path || '')}</div>
                    </div>
                  `).join('')
                  : `<div class="muted">No surfaced artifacts found for this run.</div>`
              }
            </div>
          </section>
        </div>

        <section class="panel section-card">
          <div class="section-head">
            <div>
              <div class="kicker">Events</div>
              <h3>Normalized Timeline</h3>
              <p class="section-copy">Event data is normalized from current Harness logs, so it stays useful before full schema migration.</p>
            </div>
          </div>
          <div class="event-list">
            ${
              events.length
                ? events.map((event) => `
                  <div class="event-item">
                    <div class="item-row">
                      <strong>${escapeHtml(event.message || event.kind || 'event')}</strong>
                      ${statusPill(event.kind || 'event')}
                    </div>
                    <div class="item-row">
                      <div class="microcopy">Phase: ${escapeHtml(event.phase || 'execution')}</div>
                      <div class="microcopy mono">${escapeHtml(event.ts || '')}</div>
                    </div>
                    ${
                      event.task_id
                        ? `<div class="microcopy mono">Task: ${escapeHtml(event.task_id)}</div>`
                        : ''
                    }
                  </div>
                `).join('')
                : `<div class="empty-state"><h3>No events yet</h3><p class="muted">This run has no normalized log entries yet.</p></div>`
            }
          </div>
        </section>
      </div>
    `);
  }

  async function dispatch() {
    const { name, param } = routeParts();
    try {
      if (!name || name === 'requests') {
        await renderRequestsView();
        return;
      }
      if (name === 'request' && param) {
        await renderRequestDetail(param);
        return;
      }
      if (name === 'runs') {
        await renderRunsView();
        return;
      }
      if (name === 'run' && param) {
        await renderRunDetail(param);
        return;
      }
      window.location.hash = '#requests';
    } catch (error) {
      setError(error.message || 'Something went wrong loading the prototype.');
    }
  }

  window.addEventListener('hashchange', dispatch);
  window.addEventListener('DOMContentLoaded', () => {
    if (!window.location.hash) {
      window.location.hash = '#requests';
      return;
    }
    dispatch();
  });
})();
