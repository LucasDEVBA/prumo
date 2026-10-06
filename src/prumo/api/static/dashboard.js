// Painel do Prumo: lê a API e desenha indicadores, gráfico, eventos e fila de revisão.
(() => {
  "use strict";

  const body = document.body;
  const SIMULATED = body.dataset.simulated === "true";
  const SLO = Number(body.dataset.slo);
  const NEEDS_TOKEN = body.dataset.needsToken === "true";
  const STATUS_LABEL = {
    coletando: "Coletando rótulos",
    saudavel: "Saudável",
    em_observacao: "Em observação",
    abaixo_da_meta: "Abaixo da meta",
  };
  const $ = (id) => document.getElementById(id);
  const pct = (v) => (v == null ? "–" : (v * 100).toLocaleString("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + "%");
  const when = (iso) => new Date(iso).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

  // ---------- HTTP ----------
  function token() {
    try { return sessionStorage.getItem("prumo-token") || ""; } catch { return ""; }
  }

  async function api(path, options = {}) {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    if (NEEDS_TOKEN && token()) headers["X-Prumo-Token"] = token();
    const response = await fetch(path, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.message || `Erro ${response.status}`);
      error.code = data.code;
      throw error;
    }
    return data;
  }

  function showError(error) {
    const toast = $("toast");
    toast.textContent = error.code === "unauthorized"
      ? "Informe o token de administrador para usar os controles."
      : error.message;
    toast.hidden = false;
    clearTimeout(showError.timer);
    showError.timer = setTimeout(() => { toast.hidden = true; }, 6000);
  }

  // ---------- Indicadores ----------
  function renderKpis(current) {
    $("k-version").textContent = current.prompt_version;
    const pill = $("k-status");
    pill.className = "pill " + current.status;
    pill.textContent = STATUS_LABEL[current.status] || current.status;
    $("k-judge").textContent = pct(current.judge_rate);
    $("k-est").textContent = pct(current.point);
    $("k-ci").textContent = current.lower == null ? "aguardando rótulos" : `faixa ${pct(current.lower)} a ${pct(current.upper)}`;
    if (current.burn_rate == null) {
      $("k-burn").textContent = "–";
      $("k-budget").textContent = "aguardando rótulos";
    } else {
      const burn = current.burn_rate;
      $("k-burn").textContent = burn.toLocaleString("pt-BR", { maximumFractionDigits: 1, minimumFractionDigits: 1 }) + "×";
      $("k-budget").textContent = burn > 1 ? `gasta o orçamento do mês em ${Math.round(30 / burn)} dias` : "dentro do orçamento do mês";
    }
    $("k-labels").textContent = current.n_labels.toLocaleString("pt-BR");
    const share = current.n_decisions ? current.n_labels / current.n_decisions : 0;
    $("k-labels-sub").textContent = `${pct(share)} das ${current.n_decisions.toLocaleString("pt-BR")} decisões`;
  }

  // ---------- Gráfico ----------
  const W = 760, H = 300, M = { l: 44, r: 16, t: 26, b: 30 }, YMIN = 0.6, YMAX = 1.0, SLOTS = 48;
  const xs = (i) => M.l + (i / (SLOTS - 1)) * (W - M.l - M.r);
  const ys = (v) => M.t + (1 - (Math.min(YMAX, Math.max(YMIN, v)) - YMIN) / (YMAX - YMIN)) * (H - M.t - M.b);
  const NS = "http://www.w3.org/2000/svg";

  function svg(tag, attrs, text) {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, String(v)));
    if (text != null) node.textContent = text;
    return node;
  }

  function renderChart(history) {
    const chart = $("chart");
    chart.replaceChildren();
    for (let g = 0.6; g <= 1.0001; g += 0.1) {
      chart.append(svg("line", { class: "grid-line", x1: M.l, x2: W - M.r, y1: ys(g), y2: ys(g) }));
      chart.append(svg("text", { class: "axis", x: M.l - 8, y: ys(g) + 4, "text-anchor": "end" }, `${Math.round(g * 100)}%`));
    }
    chart.append(svg("text", { class: "axis", x: M.l, y: H - 8 }, "mais antiga"));
    chart.append(svg("text", { class: "axis", x: W - M.r, y: H - 8, "text-anchor": "end" }, "agora"));
    chart.append(svg("line", { class: "slo", x1: M.l, x2: W - M.r, y1: ys(SLO), y2: ys(SLO) }));
    chart.append(svg("text", { class: "slo-label", x: W - M.r - 4, y: ys(SLO) + 15, "text-anchor": "end" }, `meta ${Math.round(SLO * 100)}%`));

    const offset = SLOTS - history.length;
    const segments = [];
    history.forEach((point, i) => {
      const last = segments[segments.length - 1];
      const item = { ...point, i: i + offset };
      if (!last || last[0].prompt_version !== point.prompt_version) segments.push([item]);
      else last.push(item);
    });
    segments.forEach((segment, s) => {
      if (s > 0) {
        const x = xs(segment[0].i - 0.5);
        const anchorEnd = x > W - 150;
        chart.append(svg("line", { class: "marker", x1: x, x2: x, y1: M.t - 6, y2: H - M.b }));
        chart.append(svg("text", { class: "marker-label", x: anchorEnd ? x - 4 : x + 4, y: M.t - 10, "text-anchor": anchorEnd ? "end" : "start" }, segment[0].prompt_version));
      }
      const withEst = segment.filter((p) => p.point != null);
      if (withEst.length > 1) {
        const top = withEst.map((p) => `${xs(p.i)},${ys(p.upper)}`);
        const bottom = withEst.slice().reverse().map((p) => `${xs(p.i)},${ys(p.lower)}`);
        chart.append(svg("polygon", { class: "band", points: top.concat(bottom).join(" ") }));
      }
      const line = (key, cls) => {
        const pts = segment.filter((p) => p[key] != null);
        if (pts.length > 1) chart.append(svg("polyline", { class: cls, points: pts.map((p) => `${xs(p.i)},${ys(p[key])}`).join(" ") }));
      };
      line("truth", "truth");
      line("judge_rate", "judge");
      line("point", "est");
    });
  }

  // ---------- Eventos e fila ----------
  function renderEvents(items) {
    const list = $("log");
    list.replaceChildren(...items.map((e) => {
      const li = document.createElement("li");
      const time = document.createElement("time");
      time.dateTime = e.at;
      time.textContent = when(e.at);
      const text = document.createElement("span");
      text.className = e.kind;
      text.textContent = e.message;
      li.append(time, text);
      return li;
    }));
  }

  function renderQueue(items) {
    const queue = $("queue");
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "sub";
      empty.textContent = "Nenhuma decisão esperando revisão agora.";
      queue.replaceChildren(empty);
      return;
    }
    queue.replaceChildren(...items.map(reviewCard));
  }

  function reviewCard(decision) {
    const card = document.createElement("article");
    card.className = "q-item";
    const quote = document.createElement("blockquote");
    quote.textContent = `"${decision.lead_text}"`;
    const meta = document.createElement("div");
    meta.className = "q-meta";
    meta.append(metaItem("Decisão da IA: ", decision.predicted), metaItem("Juiz LLM: ", decision.judge_approved ? "aprovou" : "reprovou"));
    const actions = document.createElement("div");
    actions.className = "q-actions";
    [["A IA acertou", true], ["A IA errou", false]].forEach(([label, correct]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn";
      button.textContent = label;
      button.addEventListener("click", () => submitReview(decision, correct, card));
      actions.append(button);
    });
    card.append(quote, meta, actions);
    return card;
  }

  function metaItem(label, value) {
    const span = document.createElement("span");
    const strong = document.createElement("b");
    strong.textContent = value;
    span.append(label, strong);
    return span;
  }

  async function submitReview(decision, correct, card) {
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      const labeled = await api(`/api/v1/review/${encodeURIComponent(decision.id)}`, {
        method: "POST",
        body: JSON.stringify({ correct, reviewer: "painel" }),
      });
      const result = document.createElement("p");
      if (labeled.expected) {
        const aiRight = labeled.expected === labeled.predicted;
        const judgeRight = labeled.judge_approved === aiRight;
        result.className = "q-result " + (aiRight === correct ? "ok" : "bad");
        result.textContent = `Gabarito: ${labeled.expected}. A IA ${aiRight ? "acertou" : "errou"}` +
          (judgeRight ? ", e o juiz concordou." : `, e o juiz ${labeled.judge_approved ? "tinha aprovado" : "tinha reprovado"} errado.`);
      } else {
        result.className = "q-result ok";
        result.textContent = "Rótulo registrado. Ele entra na próxima leitura.";
      }
      card.querySelector(".q-actions").replaceWith(result);
    } catch (error) {
      card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      showError(error);
    }
  }

  // ---------- Controles ----------
  function renderDeployButtons(prompts) {
    const group = $("deploy-buttons");
    group.replaceChildren(...prompts.catalog.filter((p) => p.id !== prompts.active.id).map((p) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn " + (p.id === "v2" ? "warn" : "good");
      button.textContent = `Publicar prompt ${p.id}`;
      button.title = p.title;
      button.addEventListener("click", () => act(() => api(`/api/v1/prompts/${p.id}/deploy`, { method: "POST" })));
      return button;
    }));
  }

  async function act(fn) {
    try { await fn(); await refresh(); } catch (error) { showError(error); }
  }

  async function refresh() {
    const [quality, events, queue, prompts] = await Promise.all([
      api("/api/v1/quality?limit=48"),
      api("/api/v1/events?limit=25"),
      api("/api/v1/review/queue?limit=3"),
      api("/api/v1/prompts"),
    ]);
    renderKpis(quality.current);
    renderChart(quality.history);
    renderEvents(events.items);
    renderQueue(queue);
    renderDeployButtons(prompts);
  }

  let timer = null;
  function setPlaying(on) {
    clearInterval(timer);
    timer = null;
    if (on) timer = setInterval(() => act(() => api("/api/v1/simulation/advance", { method: "POST", body: JSON.stringify({ hours: 1 }) })), 1500);
    const play = $("play");
    if (play) play.textContent = on ? "Pausar" : "Continuar";
  }

  function wireSimulation() {
    if (!SIMULATED) return;
    const labels = $("labels");
    labels.addEventListener("input", () => { $("labels-out").textContent = labels.value; });
    labels.addEventListener("change", () => act(() => api("/api/v1/simulation/settings", { method: "PUT", body: JSON.stringify({ labels_per_hour: Number(labels.value) }) })));
    $("auto-rb").addEventListener("change", (e) => act(() => api("/api/v1/simulation/settings", { method: "PUT", body: JSON.stringify({ auto_rollback: e.target.checked }) })));
    $("play").addEventListener("click", () => setPlaying(!timer));
    $("step").addEventListener("click", () => act(() => api("/api/v1/simulation/advance", { method: "POST", body: JSON.stringify({ hours: 1 }) })));
    $("reset").addEventListener("click", () => act(async () => {
      const state = await api("/api/v1/simulation/reset", { method: "POST" });
      labels.value = state.labels_per_hour;
      $("labels-out").textContent = state.labels_per_hour;
      $("auto-rb").checked = state.auto_rollback;
    }));
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setPlaying(!reduced && (!NEEDS_TOKEN || token()));
  }

  function wireForms() {
    const tokenForm = $("token-form");
    if (tokenForm) {
      tokenForm.addEventListener("submit", (e) => {
        e.preventDefault();
        try { sessionStorage.setItem("prumo-token", $("token").value); } catch { /* sem storage: o token vale só nesta página */ }
        $("token").value = "";
        refresh().catch(showError);
      });
    }
    $("try-form").addEventListener("submit", (e) => {
      e.preventDefault();
      act(async () => {
        const decision = await api("/api/v1/decisions", { method: "POST", body: JSON.stringify({ lead_text: $("lead-text").value }) });
        $("try-result").textContent = `A IA (${decision.prompt_version}) classificou como ${decision.predicted}. O juiz ${decision.judge_approved ? "aprovou" : "reprovou"}.` +
          (decision.sampled_for_review ? " Esta decisão foi sorteada para revisão humana." : "");
      });
    });
  }

  wireForms();
  wireSimulation();
  refresh().catch(showError);
})();
