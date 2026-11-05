/**
 * Coach settings page for the Hermes Desktop plugin host.
 *
 * Plain ESM, no build step. The host supplies `jsx`, `useValue`, `rest`,
 * `host.request`, and `registerSettingsPage` on the plugin context.
 */

const MODEL_ROWS = [
  [
    "coach_model",
    "Shared model",
    "Every mode uses this unless it has its own. Nothing is configured until it is set.",
  ],
  [
    "active_model",
    "Active",
    "Runs every reviewed turn, so a cheap fast model here saves the most.",
  ],
  [
    "review_model",
    "Review",
    "Gates completion, so this is where the strongest model earns its cost.",
  ],
  [
    "consult_model",
    "Consult",
    "Used by `coach_ask` when the agent asks for advice.",
  ],
];

const NOOP_EFFECT = () => {};

function messageOf(error) {
  if (error && error.message) return String(error.message);
  return String(error == null ? "Unknown error" : error);
}

function unwrap(response) {
  if (response && typeof response === "object" && "data" in response) {
    return response.data;
  }
  return response;
}

function formatModel(value) {
  if (!value) return "Inherit shared model";
  return value.provider ? `${value.provider} / ${value.model}` : value.model;
}

function formatUsd(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `$${number.toFixed(2)}` : "$0.00";
}

function modelKey(value) {
  if (!value || !value.model) return "";
  return `${value.provider || ""}/${value.model}`;
}

function splitModel(raw) {
  const slash = String(raw).indexOf("/");
  if (slash < 0) return { provider: "", model: String(raw) };
  return {
    provider: String(raw).slice(0, slash),
    model: String(raw).slice(slash + 1),
  };
}

function flattenModels(input, provider) {
  const out = [];
  const parent = provider || "";
  if (input == null) return out;
  if (Array.isArray(input)) {
    input.forEach((item) => out.push(...flattenModels(item, parent)));
    return out;
  }
  if (typeof input === "string") {
    const choice = splitModel(input);
    out.push({
      provider: choice.provider || parent,
      model: choice.model,
      label: input,
    });
    return out;
  }
  if (typeof input !== "object") return out;
  const ownProvider = String(
    input.provider || input.provider_id || input.providerId || parent,
  );
  const children = input.models || input.children || input.options;
  if (Array.isArray(children)) {
    children.forEach((child) => out.push(...flattenModels(child, ownProvider)));
    return out;
  }
  const model = String(
    input.model || input.id || input.value || input.name || "",
  ).trim();
  if (!model) return out;
  out.push({
    provider: ownProvider,
    model,
    label: String(input.label || input.name || model),
  });
  return out;
}

function dedupeModels(models) {
  const seen = new Set();
  return models.filter((item) => {
    if (!item || !item.model) return false;
    const key = `${item.provider}/${item.model}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function useConfig(ctx) {
  const [config, setConfig] = ctx.useValue(null);
  const [status, setStatus] = ctx.useValue(null);
  const [error, setError] = ctx.useValue("");
  const [busy, setBusy] = ctx.useValue(false);
  const effect = typeof ctx.useEffect === "function" ? ctx.useEffect : NOOP_EFFECT;

  const reload = async () => {
    try {
      const [nextConfig, nextStatus] = await Promise.all([
        ctx.rest.get("/config"),
        ctx.rest.get("/status"),
      ]);
      setConfig(unwrap(nextConfig));
      setStatus(unwrap(nextStatus));
      setError("");
    } catch (err) {
      setError(messageOf(err));
    }
  };

  const save = async (key, value) => {
    setBusy(true);
    setError("");
    try {
      if (typeof ctx.setConfig === "function") {
        await ctx.setConfig(key, value);
      } else if (ctx.config && typeof ctx.config.set === "function") {
        await ctx.config.set(key, value);
      } else {
        throw new Error("This host does not expose a config writer.");
      }
      await reload();
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setBusy(false);
    }
  };

  effect(() => {
    reload();
  }, []);

  return { config, status, error, busy, reload, save };
}

function PageFrame(props) {
  const { h, state, title, children } = props;
  return h(
    "div",
    { className: "coach-page", style: { display: "grid", gap: "12px" } },
    h("h3", { style: { margin: 0 } }, title),
    state.error
      ? h("p", { className: "coach-error", role: "alert" }, state.error)
      : null,
    ...children,
  );
}

function Loading(props) {
  const { h, state } = props;
  return h(
    "div",
    { className: "coach-loading" },
    h("p", { className: "coach-muted" }, "Loading settings..."),
    h(
      "button",
      { type: "button", onClick: () => state.reload() },
      "Reload",
    ),
  );
}

function ToggleField(props) {
  const { ctx, label, checked, onChange, hint } = props;
  const h = ctx.jsx;
  return h(
    "label",
    { className: "coach-toggle", style: { display: "grid", gap: "4px" } },
    h(
      "span",
      { style: { display: "flex", gap: "8px", alignItems: "center" } },
      h("input", {
        type: "checkbox",
        checked: Boolean(checked),
        onChange: (event) => onChange(event.target.checked),
      }),
      h("span", null, label),
    ),
    hint ? h("span", { className: "coach-hint" }, hint) : null,
  );
}

function TextInput(props) {
  const { ctx, value, onChange, placeholder } = props;
  const h = ctx.jsx;
  return h("input", {
    type: "text",
    className: "coach-input",
    value: value == null ? "" : String(value),
    placeholder: placeholder || "",
    onChange: (event) => onChange(event.target.value),
  });
}

function NumberInput(props) {
  const { ctx, value, min, max, step, onCommit } = props;
  const h = ctx.jsx;
  const [draft, setDraft] = ctx.useValue(value == null ? "" : String(value));
  const effect = typeof ctx.useEffect === "function" ? ctx.useEffect : NOOP_EFFECT;

  effect(() => {
    setDraft(value == null ? "" : String(value));
  }, [value]);

  const commit = () => {
    const parsed = Number(draft);
    if (draft !== "" && Number.isFinite(parsed)) {
      onCommit(parsed);
    } else {
      setDraft(value == null ? "" : String(value));
    }
  };

  return h("input", {
    type: "number",
    className: "coach-input",
    value: draft,
    min,
    max,
    step,
    onChange: (event) => setDraft(event.target.value),
    onBlur: commit,
    onKeyDown: (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        commit();
      }
    },
  });
}

function ChoiceInput(props) {
  const { ctx, value, choices, onChange } = props;
  const h = ctx.jsx;
  const current = value == null ? "" : String(value);
  return h(
    "select",
    {
      className: "coach-select",
      value: current,
      onChange: (event) => onChange(event.target.value),
    },
    choices.map((choice) =>
      h("option", { key: choice, value: choice }, choice),
    ),
  );
}

function ModelInput(props) {
  const { ctx, value, models, inherit, onChange } = props;
  const h = ctx.jsx;
  const current = modelKey(value);
  const known = new Set(models.map((item) => modelKey(item)));
  const options = [
    h(
      "option",
      { key: "__empty", value: "" },
      inherit ? "Inherit shared model" : "Choose a model...",
    ),
  ];
  if (current && !known.has(current)) {
    options.push(
      h("option", { key: "__current", value: current }, current),
    );
  }
  models.forEach((item, index) => {
    const key = modelKey(item);
    const label = item.provider
      ? `${item.provider} / ${item.model}`
      : item.model;
    options.push(h("option", { key: `${key}-${index}`, value: key }, label));
  });
  return h(
    "select",
    {
      className: "coach-select",
      value: current,
      onChange: (event) => {
        const raw = event.target.value;
        onChange(raw ? splitModel(raw) : null);
      },
    },
    options,
  );
}

function StatusSummary(props) {
  const { ctx, status } = props;
  const h = ctx.jsx;
  if (!status) {
    return h("p", { className: "coach-muted" }, "Loading status...");
  }
  const rows = [
    ["Configured", status.configured ? "Yes" : "Unconfigured"],
    ["Shared model", formatModel(status.coach_model)],
    [
      "Spend",
      `${formatUsd(status.spend_usd)} of ${formatUsd(status.budget_usd)}`,
    ],
    ["Pending advice", String(status.pending_notes || 0)],
    [
      "Model overrides",
      status.permissions && status.permissions.model_overrides
        ? "Granted"
        : "Not granted",
    ],
  ];
  return h(
    "dl",
    {
      className: "coach-status",
      style: { display: "grid", gridTemplateColumns: "auto 1fr", gap: "6px 16px" },
    },
    rows.map(([label, value]) =>
      h(
        "div",
        {
          key: label,
          style: { display: "contents" },
        },
        h("dt", { style: { opacity: 0.7 } }, label),
        h("dd", { style: { margin: 0 } }, value),
      ),
    ),
  );
}

function OverviewPage(props) {
  const { ctx } = props;
  const h = ctx.jsx;
  const state = useConfig(ctx);
  const status = state.status;
  return h(
    "div",
    { className: "coach-page", style: { display: "grid", gap: "12px" } },
    h(
      "p",
      { className: "coach-lede", style: { margin: 0 } },
      "A second model for advice, per-turn review, completion gating, and typed decisions.",
    ),
    state.error
      ? h("p", { className: "coach-error", role: "alert" }, state.error)
      : null,
    status && !status.configured
      ? h(
          "div",
          {
            className: "coach-notice",
            style: {
              border: "1px solid currentColor",
              borderRadius: "8px",
              padding: "12px",
            },
          },
          h("strong", null, "Unconfigured"),
          h(
            "p",
            { style: { margin: "6px 0 0" } },
            "Choose a coach model on the Models page. Until then the tools say no model is set and Review stays quiet.",
          ),
        )
      : null,
    h(StatusSummary, { ctx, status }),
    h(
      "button",
      {
        type: "button",
        onClick: () => state.reload(),
        disabled: state.busy,
      },
      state.busy ? "Working..." : "Refresh",
    ),
  );
}

function ModelsPage(props) {
  const { ctx } = props;
  const h = ctx.jsx;
  const state = useConfig(ctx);
  const [catalog, setCatalog] = ctx.useValue([]);
  const [catalogError, setCatalogError] = ctx.useValue("");
  const effect = typeof ctx.useEffect === "function" ? ctx.useEffect : NOOP_EFFECT;

  effect(() => {
    let alive = true;
    const host = ctx.host;
    if (!host || typeof host.request !== "function") {
      setCatalogError("This host does not expose the model catalog.");
      return undefined;
    }
    Promise.resolve(host.request("model.options"))
      .then((result) => {
        if (alive) setCatalog(dedupeModels(flattenModels(unwrap(result))));
      })
      .catch((err) => {
        if (alive) setCatalogError(messageOf(err));
      });
    return () => {
      alive = false;
    };
  }, []);

  if (!state.config) {
    return h(Loading, { h, state });
  }
  const values = state.config.values || {};

  return h(
    PageFrame,
    { h, state, title: "Models" },
    h(
      "p",
      { className: "coach-hint", style: { margin: 0 } },
      "Per-mode models need the override grant on the Status page. Inheriting the shared model needs no permission.",
    ),
    catalogError
      ? h("p", { className: "coach-error" }, catalogError)
      : null,
    catalog.length === 0
      ? h(
          "p",
          { className: "coach-hint" },
          "No catalog models were returned; current selections still render.",
        )
      : null,
    ...MODEL_ROWS.map(([key, label, hint]) =>
      h(
        "div",
        { className: "coach-field", key, style: { display: "grid", gap: "4px" } },
        h("span", { className: "coach-field-label" }, label),
        h(ModelInput, {
          ctx,
          value: values[key],
          models: catalog,
          inherit: key !== "coach_model",
          onChange: (next) => state.save(key, next),
        }),
        h("span", { className: "coach-hint" }, hint),
      ),
    ),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Max reply tokens"),
      h(NumberInput, {
        ctx,
        value: values.max_reply_tokens,
        min: 50,
        max: 4000,
        step: 50,
        onCommit: (next) => state.save("max_reply_tokens", next),
      }),
      h(
        "span",
        { className: "coach-hint" },
        "Hard cap on the coach's answer; output is the expensive half of a review.",
      ),
    ),
    state.busy ? h("p", { className: "coach-muted" }, "Saving...") : null,
  );
}

function ModesPage(props) {
  const { ctx } = props;
  const h = ctx.jsx;
  const state = useConfig(ctx);
  if (!state.config) {
    return h(Loading, { h, state });
  }
  const values = state.config.values || {};
  const choices = state.config.choices || {};
  const save = state.save;

  return h(
    PageFrame,
    { h, state, title: "Modes" },
    h(ToggleField, {
      ctx,
      label: "Active",
      checked: values.active_enabled,
      onChange: (next) => save("active_enabled", next),
      hint: "Reviews each completed turn. Off by default because it spends on every turn.",
    }),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Active cadence"),
      h(ChoiceInput, {
        ctx,
        value: values.active_cadence,
        choices: choices.active_cadence || [],
        onChange: (next) => save("active_cadence", next),
      }),
      h(
        "span",
        { className: "coach-hint" },
        "`tool_turns_only` skips turns where the agent only talked.",
      ),
    ),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Turns per review"),
      h(NumberInput, {
        ctx,
        value: values.active_every_n,
        min: 1,
        max: 50,
        step: 1,
        onCommit: (next) => save("active_every_n", next),
      }),
      h("span", { className: "coach-hint" }, "Used when the cadence is `every_n`."),
    ),
    h(ToggleField, {
      ctx,
      label: "Review",
      checked: values.review_enabled,
      onChange: (next) => save("review_enabled", next),
      hint: "Reviews the diff when the agent is about to claim it is done.",
    }),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Review authority"),
      h(ChoiceInput, {
        ctx,
        value: values.review_authority,
        choices: choices.review_authority || [],
        onChange: (next) => save("review_authority", next),
      }),
      h(
        "span",
        { className: "coach-hint" },
        "`hold_once` may send the agent back to work one time per request.",
      ),
    ),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Holds per request"),
      h(NumberInput, {
        ctx,
        value: values.review_max_holds,
        min: 0,
        max: 5,
        step: 1,
        onCommit: (next) => save("review_max_holds", next),
      }),
      h("span", { className: "coach-hint" }, "Above one, the gate turns into an argument."),
    ),
    h(ToggleField, {
      ctx,
      label: "Decide",
      checked: values.decide_enabled,
      onChange: (next) => save("decide_enabled", next),
      hint: "Exposes `coach_decide` for typed fork resolution.",
    }),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Decide backend"),
      h(ChoiceInput, {
        ctx,
        value: values.decide_backend,
        choices: choices.decide_backend || [],
        onChange: (next) => save("decide_backend", next),
      }),
      h(
        "span",
        { className: "coach-hint" },
        "`jev-openrouter` uses your OpenRouter key; `jev-direct` uses a TypeSafe key.",
      ),
    ),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Decide model"),
      h(TextInput, {
        ctx,
        value: values.decide_model,
        placeholder: "typesafe/jev-1.14",
        onChange: (next) => save("decide_model", next),
      }),
      h("span", { className: "coach-hint" }, "The decision model returns verdicts, not prose."),
    ),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Minimum confidence"),
      h(NumberInput, {
        ctx,
        value: values.decide_min_confidence,
        min: 0,
        max: 1,
        step: 0.05,
        onCommit: (next) => save("decide_min_confidence", next),
      }),
      h(
        "span",
        { className: "coach-hint" },
        "Below this the tool answers `escalate` instead of picking.",
      ),
    ),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Decide calls per turn"),
      h(NumberInput, {
        ctx,
        value: values.decide_max_calls_per_turn,
        min: 1,
        max: 20,
        step: 1,
        onCommit: (next) => save("decide_max_calls_per_turn", next),
      }),
    ),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Consult calls per turn"),
      h(NumberInput, {
        ctx,
        value: values.consult_max_calls_per_turn,
        min: 1,
        max: 20,
        step: 1,
        onCommit: (next) => save("consult_max_calls_per_turn", next),
      }),
    ),
    state.busy ? h("p", { className: "coach-muted" }, "Saving...") : null,
  );
}

function DeliveryPage(props) {
  const { ctx } = props;
  const h = ctx.jsx;
  const state = useConfig(ctx);
  if (!state.config) {
    return h(Loading, { h, state });
  }
  const values = state.config.values || {};
  const choices = state.config.choices || {};
  const save = state.save;

  return h(
    PageFrame,
    { h, state, title: "Delivery" },
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Policy"),
      h(ChoiceInput, {
        ctx,
        value: values.delivery_policy,
        choices: choices.delivery_policy || [],
        onChange: (next) => save("delivery_policy", next),
      }),
      h(
        "span",
        { className: "coach-hint" },
        "`ambient` holds advice for the next turn; `corrective` starts a new turn now; `severity` sends blockers corrective and the rest ambient.",
      ),
    ),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Corrective per request"),
      h(NumberInput, {
        ctx,
        value: values.delivery_max_corrective_per_request,
        min: 0,
        max: 10,
        step: 1,
        onCommit: (next) => save("delivery_max_corrective_per_request", next),
      }),
      h(
        "span",
        { className: "coach-hint" },
        "Stops advice from turning into a conversation of its own.",
      ),
    ),
    h(ToggleField, {
      ctx,
      label: "Show ambient cards",
      checked: values.delivery_show_cards,
      onChange: (next) => save("delivery_show_cards", next),
      hint: "Off means advice reaches the model but you never see it.",
    }),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Note lifetime (turns)"),
      h(NumberInput, {
        ctx,
        value: values.delivery_note_ttl_turns,
        min: 0,
        max: 50,
        step: 1,
        onCommit: (next) => save("delivery_note_ttl_turns", next),
      }),
      h(
        "span",
        { className: "coach-hint" },
        "Undelivered notes are dropped after this many turns.",
      ),
    ),
    state.busy ? h("p", { className: "coach-muted" }, "Saving...") : null,
  );
}

function BudgetPage(props) {
  const { ctx } = props;
  const h = ctx.jsx;
  const state = useConfig(ctx);
  if (!state.config) {
    return h(Loading, { h, state });
  }
  const values = state.config.values || {};
  const choices = state.config.choices || {};
  const save = state.save;

  return h(
    PageFrame,
    { h, state, title: "Budget" },
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Session budget (USD)"),
      h(NumberInput, {
        ctx,
        value: values.budget_session_usd,
        min: 0,
        max: 1000,
        step: 0.05,
        onCommit: (next) => save("budget_session_usd", next),
      }),
      h(
        "span",
        { className: "coach-hint" },
        "Computed from real provider usage, not estimates of calls.",
      ),
    ),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "When exceeded"),
      h(ChoiceInput, {
        ctx,
        value: values.budget_on_exceed,
        choices: choices.budget_on_exceed || [],
        onChange: (next) => save("budget_on_exceed", next),
      }),
      h(
        "span",
        { className: "coach-hint" },
        "`warn` keeps going; `soft` stops background reviews but leaves explicit consults; `hard` stops everything.",
      ),
    ),
    state.status
      ? h(
          "p",
          { className: "coach-muted" },
          `Recorded spend: ${formatUsd(state.status.spend_usd)} across ${
            state.status.sessions || 0
          } session(s).`,
        )
      : null,
    state.busy ? h("p", { className: "coach-muted" }, "Saving...") : null,
  );
}

function GuidancePage(props) {
  const { ctx } = props;
  const h = ctx.jsx;
  const state = useConfig(ctx);
  const [scope, setScope] = ctx.useValue("global");
  const [cwd, setCwd] = ctx.useValue("");
  const [text, setText] = ctx.useValue("");
  const [notice, setNotice] = ctx.useValue("");
  const [saving, setSaving] = ctx.useValue(false);
  const effect = typeof ctx.useEffect === "function" ? ctx.useEffect : NOOP_EFFECT;

  const load = async (nextScope, nextCwd) => {
    setNotice("");
    try {
      const query =
        "/guidance?scope=" +
        encodeURIComponent(nextScope) +
        "&cwd=" +
        encodeURIComponent(nextCwd || "");
      const data = unwrap(await ctx.rest.get(query));
      setText(data && typeof data.text === "string" ? data.text : "");
    } catch (err) {
      setNotice(messageOf(err));
    }
  };

  effect(() => {
    load("global", "");
  }, []);

  if (!state.config) {
    return h(Loading, { h, state });
  }
  const values = state.config.values || {};

  const save = async () => {
    setSaving(true);
    setNotice("");
    try {
      const result = unwrap(
        await ctx.rest.put("/guidance", {
          scope,
          cwd,
          text,
        }),
      );
      setNotice(result && result.message ? result.message : "Saved.");
    } catch (err) {
      setNotice(messageOf(err));
    } finally {
      setSaving(false);
    }
  };

  const maxChars = 8000;

  return h(
    PageFrame,
    { h, state, title: "Guidance" },
    h(ToggleField, {
      ctx,
      label: "Send guidance to the coach",
      checked: values.guidance_enabled,
      onChange: (next) => state.save("guidance_enabled", next),
      hint: "Off ignores both files without deleting them.",
    }),
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Scope"),
      h(ChoiceInput, {
        ctx,
        value: scope,
        choices: ["global", "project"],
        onChange: (next) => {
          setScope(next);
          load(next, cwd);
        },
      }),
      h(
        "span",
        { className: "coach-hint" },
        scope === "global"
          ? "Global guidance lives in $HERMES_HOME/coach/guidance.md."
          : "Project guidance is written to COACH.md in a directory recorded from a real session.",
      ),
    ),
    scope === "project"
      ? h(
          "div",
          { className: "coach-field", style: { display: "grid", gap: "4px" } },
          h("span", { className: "coach-field-label" }, "Project directory"),
          h(TextInput, {
            ctx,
            value: cwd,
            placeholder: "/path/to/focused/project",
            onChange: setCwd,
          }),
          h(
            "button",
            { type: "button", onClick: () => load("project", cwd) },
            "Load",
          ),
        )
      : null,
    h(
      "div",
      { className: "coach-field", style: { display: "grid", gap: "4px" } },
      h("span", { className: "coach-field-label" }, "Guidance text"),
      h("textarea", {
        className: "coach-textarea",
        value: text,
        rows: 12,
        maxLength: maxChars,
        style: { fontFamily: "monospace", width: "100%" },
        onChange: (event) => setText(event.target.value),
      }),
      h(
        "span",
        { className: "coach-hint" },
        `${text.length} / ${maxChars} chars - about ${Math.ceil(
          text.length / 4,
        )} tokens. Only the first 4000 characters reach the coach prompt.`,
      ),
    ),
    h(
      "div",
      { style: { display: "flex", gap: "8px", alignItems: "center" } },
      h(
        "button",
        { type: "button", onClick: save, disabled: saving || text.length > maxChars },
        saving ? "Saving..." : "Save guidance",
      ),
      notice ? h("span", { className: "coach-hint" }, notice) : null,
    ),
  );
}

function StatusPage(props) {
  const { ctx } = props;
  const h = ctx.jsx;
  const state = useConfig(ctx);
  const [granting, setGranting] = ctx.useValue(false);
  const [notice, setNotice] = ctx.useValue("");

  if (!state.config) {
    return h(Loading, { h, state });
  }
  const status = state.status;
  const granted =
    status && status.permissions && status.permissions.model_overrides;

  const grant = async () => {
    setGranting(true);
    setNotice("");
    try {
      unwrap(await ctx.rest.post("/permissions/grant", {}));
      setNotice("Model overrides granted.");
      await state.reload();
    } catch (err) {
      setNotice(messageOf(err));
    } finally {
      setGranting(false);
    }
  };

  return h(
    PageFrame,
    { h, state, title: "Status" },
    h(StatusSummary, { ctx, status }),
    h(
      "div",
      {
        className: "coach-permission",
        style: {
          display: "grid",
          gap: "6px",
          borderTop: "1px solid rgba(127,127,127,0.35)",
          paddingTop: "10px",
        },
      },
      h("span", { className: "coach-field-label" }, "Model-override permission"),
      h(
        "p",
        { className: "coach-hint", style: { margin: 0 } },
        granted
          ? "Granted. Per-mode models can call providers other than the primary."
          : "Not granted. Per-mode models fail at call time until both trust flags are written; inheriting needs no permission.",
      ),
      granted
        ? null
        : h(
            "button",
            { type: "button", onClick: grant, disabled: granting },
            granting ? "Granting..." : "Grant",
          ),
      notice ? h("span", { className: "coach-hint" }, notice) : null,
    ),
    status && status.last_error
      ? h(
          "p",
          { className: "coach-hint" },
          `Last coach error: ${status.last_error}`,
        )
      : null,
    h(
      "p",
      { className: "coach-hint", style: { margin: 0 } },
      "Pending advice: deliver or dismiss it from the CLI with /coach pending and /coach send.",
    ),
    h(
      "button",
      { type: "button", onClick: () => state.reload(), disabled: state.busy },
      state.busy ? "Refreshing..." : "Refresh",
    ),
  );
}

export function register(ctx) {
  if (!ctx || typeof ctx.registerSettingsPage !== "function") {
    throw new Error("Coach settings need the Hermes Desktop plugin host.");
  }
  const h = ctx.jsx;
  const withContext = (Component) => (props) =>
    h(Component, { ...(props || {}), ctx });

  ctx.registerSettingsPage({
    id: "coach",
    title: "Coach",
    icon: "sparkle",
    render: withContext(OverviewPage),
    children: [
      { id: "models", title: "Models", render: withContext(ModelsPage) },
      { id: "modes", title: "Modes", render: withContext(ModesPage) },
      { id: "delivery", title: "Delivery", render: withContext(DeliveryPage) },
      { id: "budget", title: "Budget", render: withContext(BudgetPage) },
      { id: "guidance", title: "Guidance", render: withContext(GuidancePage) },
      { id: "status", title: "Status", render: withContext(StatusPage) },
    ],
  });
}

export default register;
