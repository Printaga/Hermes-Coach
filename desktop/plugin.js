/**
 * Coach settings page for the Hermes Desktop plugin host.
 *
 * Plain ESM, no build step. The SDK supplies `host` (gateway RPC) and
 * `useValue`; React supplies `createElement`; everything else is local.
 * The Python half owns the values through this plugin's own API.
 */

import { host, useValue } from "@hermes/plugin-sdk"
import { createElement, useEffect, useRef, useState } from "react"

const h = createElement

const PANEL = {
  border: "1px solid color-mix(in srgb, currentColor 18%, transparent)",
  borderRadius: 10,
  padding: "14px 16px",
  display: "grid",
  gap: 10,
}

const INPUT = {
  width: "100%",
  boxSizing: "border-box",
  padding: "7px 9px",
  borderRadius: 8,
  border: "1px solid color-mix(in srgb, currentColor 24%, transparent)",
  background: "transparent",
  color: "inherit",
  font: "inherit",
}

const BUTTON = {
  padding: "7px 12px",
  borderRadius: 8,
  border: "1px solid color-mix(in srgb, currentColor 24%, transparent)",
  background: "transparent",
  color: "inherit",
  font: "inherit",
  cursor: "pointer",
  textAlign: "left",
}

const MUTED = { opacity: 0.68, fontSize: "0.8125rem", lineHeight: 1.45, margin: 0 }
const TITLE = { margin: 0, fontSize: "0.9375rem", fontWeight: 600 }
const SECTION_TITLE = {
  margin: 0,
  fontSize: "0.8125rem",
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  opacity: 0.62,
}

function findingPrompt(row) {
  const lines = [
    row.severity && row.severity !== "none"
      ? `Coach ${row.severity}: ${row.title || ""}`
      : `Coach note: ${row.title || "finding"}`,
    row.body ? String(row.body) : "",
    row.evidence ? `Evidence: ${row.evidence}` : "",
    row.fix ? `Suggested fix: ${row.fix}` : "",
    "Address this finding, or explain why it does not apply.",
  ]
  return lines.filter(Boolean).join("\n").slice(0, 4000)
}

function finiteOrNull(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function emptyRecentMessage(recentError, scope) {
  if (recentError) return `Could not load recent reviews: ${recentError}`
  if (scope === "chat") return "No findings for this chat yet."
  return "No reviews logged yet."
}

function messageOf(error) {
  if (error && error.message) return String(error.message)
  return String(error === null || error === undefined ? "Unknown error" : error)
}

function unwrap(response) {
  if (response && typeof response === "object" && "data" in response) return response.data
  return response
}

function formatUsd(value) {
  // Missing or malformed spend is unknown, not zero: "$0.00" implied a budget.
  if (value === null || value === undefined || value === "") return "—"
  const number = Number(value)
  return Number.isFinite(number) ? `$${number.toFixed(2)}` : "—"
}

function modelLabel(value) {
  if (!value || !value.model) return "Inherit shared model"
  return value.provider ? `${value.provider} / ${value.model}` : value.model
}

function sameModel(a, b) {
  if (!a || !b) return false;
  return (
    String(a.model || "").trim().toLowerCase() === String(b.model || "").trim().toLowerCase() &&
    String(a.provider || "").trim().toLowerCase() === String(b.provider || "").trim().toLowerCase()
  );
}

function flattenModels(result) {
  const data = unwrap(result) || {}
  const rows = Array.isArray(data.providers) ? data.providers : []
  const out = []
  for (const row of rows) {
    if (!row || typeof row !== "object") continue
    const provider = String(row.slug || row.provider || "")
    const providerName = String(row.name || provider || "Other")
    const models = Array.isArray(row.models) ? row.models : []
    for (const entry of models) {
      const model =
        typeof entry === "string" ? entry : String((entry && (entry.model || entry.id)) || "")
      if (model) out.push({ provider, providerName, model })
    }
  }
  return out
}

function groupModels(models, query) {
  const needle = query.trim().toLowerCase()
  const groups = new Map()
  for (const item of models) {
    const haystack = `${item.providerName} ${item.provider} ${item.model}`.toLowerCase()
    if (needle && !haystack.includes(needle)) continue
    const key = item.provider || ""
    if (!groups.has(key)) {
      groups.set(key, {
        provider: key,
        providerName: item.providerName || key || "Other",
        items: [],
      })
    }
    groups.get(key).items.push(item)
  }
  return [...groups.values()]
}

// ---------------------------------------------------------------- data layer

function useSettings(ctx) {
  const [config, setConfig] = useState(null)
  const [status, setStatus] = useState(null)
  // A failed save must not blank the page: it is a notice, not a load error.
  const [loadError, setLoadError] = useState("")
  const [saveError, setSaveError] = useState("")
  const [busy, setBusy] = useState(false)
  const alive = useRef(true)
  const chain = useRef(Promise.resolve())

  const reload = async () => {
    try {
      const [nextConfig, nextStatus] = await Promise.all([
        ctx.rest("/config"),
        ctx.rest("/status"),
      ])
      if (!alive.current) return
      setConfig(unwrap(nextConfig))
      setStatus(unwrap(nextStatus))
      setLoadError("")
    } catch (err) {
      if (alive.current) setLoadError(messageOf(err))
    }
  }

  useEffect(() => {
    alive.current = true
    chain.current = chain.current.then(() => reload()).catch(() => {})
    return () => {
      alive.current = false
    }
  }, [])

  const write = async (key, value) => {
    if (!alive.current) return false
    setBusy(true)
    setSaveError("")
    try {
      const next = await ctx.rest("/config", {
        method: "PUT",
        body: { values: { [key]: value } },
      })
      if (!alive.current) return true
      setConfig(unwrap(next))
      setStatus(unwrap(await ctx.rest("/status")))
      return true
    } catch (err) {
      if (alive.current) setSaveError(messageOf(err))
      return false
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  // One PUT at a time: overlapping saves could land out of order.
  // Reloads go through the same chain: a reload racing a save must not
  // overwrite fresh config with its own older fetch.
  const save = (key, value) => {
    // The chain resolves to the write's outcome; callers can await failure.
    chain.current = chain.current.then(() => write(key, value)).catch(() => false)
    return chain.current
  }
  const orderedReload = () => {
    chain.current = chain.current.then(() => reload()).catch(() => {})
    return chain.current
  }

  return {
    config,
    status,
    loadError,
    saveError,
    error: loadError,
    busy,
    reload: orderedReload,
    save,
  }
}

// ---------------------------------------------------------------- primitives

function Card(props) {
  return h(
    "section",
    { style: PANEL },
    props.title ? h("h3", { style: TITLE }, props.title) : null,
    props.description ? h("p", { style: MUTED }, props.description) : null,
    props.children,
  )
}

function Field(props) {
  return h(
    "div",
    { style: { display: "grid", gap: 5 } },
    h(
      "label",
      {
        style: { fontWeight: 600, fontSize: "0.875rem" },
        ...(props.controlId ? { htmlFor: props.controlId } : {}),
      },
      props.label
    ),
    props.children,
    props.hint ? h("p", { style: MUTED }, props.hint) : null,
  )
}

function Toggle(props) {
  return h(
    "label",
    {
      style: {
        display: "flex",
        alignItems: "flex-start",
        gap: 10,
        padding: "6px 0",
        cursor: "pointer",
      },
    },
    h("input", {
      type: "checkbox",
      checked: !!props.checked,
      onChange: (event) => props.onChange(event.target.checked),
      style: { marginTop: 3 },
    }),
    h(
      "span",
      { style: { display: "grid", gap: 3 } },
      h("span", { style: { fontWeight: 600, fontSize: "0.875rem" } }, props.label),
      props.hint ? h("span", { style: MUTED }, props.hint) : null,
    ),
  )
}

function Choice(props) {
  const options = Array.isArray(props.options) ? props.options : []
  const selected = options.find((option) => option.value === props.value)
  const known = Boolean(selected)
  const controlId =
    props.controlId ||
    `coach-choice-${String(props.label || "field").toLowerCase().replace(/[^a-z0-9]+/g, "-")}`
  return h(
    Field,
    { label: props.label, hint: selected && selected.description ? selected.description : props.hint, controlId },
    h(
      "select",
      {
        id: controlId,
        style: INPUT,
        value: known ? props.value : "",
        onChange: (event) => props.onChange(event.target.value),
      },
      known ? null : h("option", { value: "" }, "Choose..."),
      options.map((option) =>
        h("option", { key: option.value, value: option.value }, option.label),
      ),
    ),
  )
}

function TextField(props) {
  const [draft, setDraft] = useState(String(props.value ?? ""))
  const editing = useRef(false)
  useEffect(() => {
    // Another field's save round-trip must not clobber what is being typed.
    if (!editing.current) setDraft(String(props.value ?? ""))
  }, [props.value])
  const commit = () => {
    const next = String(draft).trim()
    // Sync the draft to the trimmed value so the input shows what was kept.
    if (next !== String(draft)) setDraft(next)
    if (next !== String(props.value ?? "")) props.onChange(next)
  }
  return h(
    Field,
    { label: props.label, hint: props.hint, controlId: props.controlId },
    h("input", {
      id: props.controlId,
      style: INPUT,
      value: draft,
      placeholder: props.placeholder,
      onFocus: () => {
        editing.current = true
      },
      onChange: (event) => setDraft(event.target.value),
      onBlur: () => {
        editing.current = false
        commit()
      },
      onKeyDown: (event) => {
        if (event.key === "Enter") commit()
      },
    }),
  )
}

function NumberField(props) {
  const [draft, setDraft] = useState(String(props.value ?? ""))
  const editing = useRef(false)
  useEffect(() => {
    if (!editing.current) setDraft(String(props.value ?? ""))
  }, [props.value])
  const commit = () => {
    if (!String(draft).trim()) {
      setDraft(String(props.value ?? ""))
      return
    }
    const parsed = Number(draft)
    if (!Number.isFinite(parsed)) {
      setDraft(String(props.value ?? ""))
      return
    }
    const min = props.min ?? Number.NEGATIVE_INFINITY
    const max = props.max ?? Number.POSITIVE_INFINITY
    const clamped = Math.min(max, Math.max(min, parsed))
    // Sync the draft to the clamped value so the input shows what was kept,
    // not the out-of-range number, before the config round-trip lands.
    setDraft(String(clamped))
    const current = Number(props.value)
    if (!Number.isFinite(current) || clamped !== current) props.onChange(clamped)
  }
  const controlId =
    props.controlId ||
    `coach-num-${String(props.label || "field").toLowerCase().replace(/[^a-z0-9]+/g, "-")}`
  return h(
    Field,
    { label: props.label, hint: props.hint, controlId },
    h("input", {
      id: controlId,
      type: "number",
      style: INPUT,
      value: draft,
      min: props.min,
      max: props.max,
      step: props.step ?? 1,
      onChange: (event) => setDraft(event.target.value),
      onBlur: commit,
      onKeyDown: (event) => {
        if (event.key === "Enter") commit()
      },
    }),
  )
}

// ---------------------------------------------------------------- model picker

function ModelPicker(props) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const groups = groupModels(props.models, query)
  const current = modelLabel(props.value)

  const choose = (value) => {
    props.onChange(value)
    setOpen(false)
    setQuery("")
  }

  return h(
    "div",
    { style: { display: "grid", gap: 8 } },
    h(
      "button",
      {
        type: "button",
        "aria-label": "Choose model",
        style: { ...BUTTON, display: "flex", justifyContent: "space-between", gap: 12 },
        onClick: () => setOpen(!open),
      },
      h("span", null, current),
      h("span", { style: { opacity: 0.6 } }, open ? "Close" : "Change"),
    ),
    open
      ? h(
          "div",
          { style: { ...PANEL, padding: 10, gap: 8, maxHeight: 320, overflowY: "auto" } },
          h("input", {
            autoFocus: true,
            placeholder: "Search models or providers",
            "aria-label": "Search models or providers",
            style: INPUT,
            value: query,
            onChange: (event) => setQuery(event.target.value),
          }),
          props.inherit
            ? h(
                "button",
                { type: "button", style: BUTTON, onClick: () => choose(null) },
                "Inherit shared model",
              )
            : null,
          props.models.length === 0 && !props.catalogLoading
            ? h("p", { style: MUTED }, "The host returned no models. Check that a provider is authenticated.")
            : null,
          props.catalogLoading
            ? h("p", { style: MUTED, role: "status" }, "Loading models...")
            : null,
          groups.length === 0 && props.models.length > 0
            ? h("p", { style: MUTED }, "No model matches that search.")
            : null,
          groups.map((group) =>
            h(
              "div",
              { key: group.provider || group.providerName, style: { display: "grid", gap: 2 } },
              h("div", { style: { ...SECTION_TITLE, padding: "8px 4px 4px" } }, group.providerName),
              group.items.map((item) =>
                h(
                  "button",
                  {
                    key: `${item.provider}/${item.model}`,
                    type: "button",
                    style: { ...BUTTON, border: "none", padding: "5px 6px" },
                    onClick: () => choose({ provider: item.provider, model: item.model }),
                  },
                  item.model,
                ),
              ),
            ),
          ),
        )
      : null,
  )
}

// ---------------------------------------------------------------- pages

function ModelSection(props) {
  const { state, mainModel } = props
  const values = (state.config && state.config.values) || {}
  const [catalog, setCatalog] = useState([])
  const [catalogError, setCatalogError] = useState("")
  const [catalogLoading, setCatalogLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setCatalogLoading(true)
    Promise.resolve(host.request("model.options"))
      .then((result) => {
        if (alive) {
          setCatalog(flattenModels(result))
          setCatalogLoading(false)
        }
      })
      .catch((err) => {
        if (alive) {
          setCatalogError(messageOf(err))
          setCatalogLoading(false)
        }
      })
    return () => {
      alive = false
    }
  }, [])

  const warning = sameModel(values.coach_model, mainModel)
    ? "This is your main model. Pick a different one: the same model shares the same blind spots, and the plugin refuses to call it."
    : "Pick a model that differs from the one you work in. Everything else inherits it unless you override it."

  return h(
    Card,
    {
      title: "Models",
      description:
        "The coach is a second model. The shared model below is used by every mode; the overrides further down are optional.",
    },
    h(
      Field,
      { label: "Shared coach model", hint: catalogError || warning },
      h(ModelPicker, {
        models: catalog,
        catalogLoading,
        value: values.coach_model,
        inherit: false,
        onChange: (next) => state.save("coach_model", next),
      }),
    ),
    mainModel
      ? h(
          "p",
          { style: MUTED },
          `Your main model is ${mainModel.provider ? `${mainModel.provider} / ` : ""}${mainModel.model}.`,
        )
      : null,
    h("p", { style: { ...SECTION_TITLE, marginTop: 6 } }, "Per-mode overrides"),
    h(
      "p",
      { style: MUTED },
      "Leave these on inherit unless a mode deserves a different model. Active runs on every reviewed turn, so it is the one worth making cheap; Review runs rarely and can afford the strongest model you have.",
    ),
    [
      ["active_model", "Active review model", "Used by the per-turn reviewer."],
      ["review_model", "Completion review model", "Used when the agent is about to claim it is done."],
      ["consult_model", "Consult model", "Used when the agent asks you for advice mid-task."],
    ].map(([key, label, hint]) =>
      h(
        Field,
        { key, label, hint },
        h(ModelPicker, {
          models: catalog,
          catalogLoading,
          value: values[key],
          inherit: true,
          onChange: (next) => state.save(key, next),
        }),
      ),
    ),
    h(NumberField, {
      label: "Longest reply (tokens)",
      hint: "A hard cap on the coach's answer. Output is the expensive half of a review, so this is the main cost lever.",
      value: values.max_reply_tokens,
      min: 80,
      max: 4000,
      step: 20,
      onChange: (next) => state.save("max_reply_tokens", next),
    }),
  )
}

function BehaviourPage(props) {
  const { state } = props
  const values = (state.config && state.config.values) || {}
  return h(
    "div",
    { style: { display: "grid", gap: 16 } },
    h(
      Card,
      {
        title: "Active review",
        description:
          "Reviews each finished turn and tells the agent what it would change. It is the only mode that spends on every turn, which is why it is off by default.",
      },
      h(Toggle, {
        label: "Review every turn",
        hint: "Off by default. A cheap, fast model keeps this close to free.",
        checked: values.active_enabled,
        onChange: (next) => state.save("active_enabled", next),
      }),
      h(Choice, {
        label: "Which turns",
        value: values.active_cadence,
        options: [
          {
            value: "tool_turns_only",
            label: "Turns that used tools",
            description: "Skips turns where the agent only talked, so chat is not reviewed.",
          },
          {
            value: "every_turn",
            label: "Every turn",
            description: "Reviews every completed turn, including plain answers.",
          },
          {
            value: "every_n",
            label: "Every N turns",
            description: "Reviews one turn in N; cheaper and coarser.",
          },
        ],
        onChange: (next) => state.save("active_cadence", next),
      }),
      values.active_cadence === "every_n"
        ? h(NumberField, {
            label: "Turns per review",
            hint: "One review every this many turns.",
            value: values.active_every_n,
            min: 2,
            max: 50,
            onChange: (next) => state.save("active_every_n", next),
          })
        : null,
    ),
    h(
      Card,
      {
        title: "Completion review",
        description:
          "Fires when the agent has edited files and is about to say it is done. It reads a bounded diff of the changed files, not the whole conversation.",
      },
      h(Toggle, {
        label: "Review before the agent claims it is done",
        hint: "Only turns that edited files. Cheap, because it runs rarely.",
        checked: values.review_enabled,
        onChange: (next) => state.save("review_enabled", next),
      }),
      h(Choice, {
        label: "What the review may do",
        value: values.review_authority,
        options: [
          {
            value: "report",
            label: "Report only",
            description: "Posts its findings; the turn finishes.",
          },
          {
            value: "hold_once",
            label: "Hold once",
            description: "Can send the agent back to work one time per request to fix a blocker.",
          },
        ],
        onChange: (next) => state.save("review_authority", next),
      }),
      h(NumberField, {
        label: "Holds allowed per request",
        hint: "One is usually right. More than one turns the gate into an argument.",
        value: values.review_max_holds,
        min: 0,
        max: 3,
        onChange: (next) => state.save("review_max_holds", next),
      }),
    ),
    h(
      Card,
      {
        title: "Decisions",
        description:
          "A typed tiebreaker for two plausible options. It returns a choice with a confidence, not prose, so the agent can act on it or escalate.",
      },
      h(Toggle, {
        label: "Enable the decide tool",
        hint: "Adds coach_decide. Nothing is spent until the agent calls it.",
        checked: values.decide_enabled,
        onChange: (next) => state.save("decide_enabled", next),
      }),
      h(Choice, {
        label: "Where decisions come from",
        value: values.decide_backend,
        options: [
          {
            value: "jev-openrouter",
            label: "Jev via OpenRouter",
            description: "Uses the OpenRouter key already in your Hermes config. Input tokens only.",
          },
          {
            value: "jev-direct",
            label: "Jev via TypeSafe",
            description: "Calls TypeSafe directly; needs TYPESAFE_API_KEY in ~/.hermes/.env.",
          },
          {
            value: "chat-model",
            label: "Any chat model",
            description: "Works with any provider, but the confidence is self-reported and less reliable.",
          },
        ],
        onChange: (next) => state.save("decide_backend", next),
      }),
      h(TextField, {
        label: "Decision model",
        hint: "For Jev use typesafe/jev-1.14. For a chat model, name the model to call.",
        value: values.decide_model,
        onChange: (next) => state.save("decide_model", next),
      }),
      h(NumberField, {
        label: "Decisions per turn",
        hint: "Stops the agent from outsourcing its judgement one call at a time.",
        value: values.decide_max_calls_per_turn,
        min: 1,
        max: 10,
        onChange: (next) => state.save("decide_max_calls_per_turn", next),
      }),
      h(NumberField, {
        label: "Minimum confidence",
        hint: "Below this the tool answers escalate instead of picking. Raise it for destructive choices.",
        value: values.decide_min_confidence,
        min: 0,
        max: 1,
        step: 0.05,
        onChange: (next) => state.save("decide_min_confidence", next),
      }),
    ),
    h(
      Card,
      {
        title: "Consult",
        description:
          "Always available: the agent can ask the coach a question, ask for a plan review before starting, or ask for a review of a finished piece of work.",
      },
      h(NumberField, {
        label: "Consults per turn",
        hint: "A ceiling so a stuck agent cannot ask the same question in a loop.",
        value: values.consult_max_calls_per_turn,
        min: 1,
        max: 10,
        onChange: (next) => state.save("consult_max_calls_per_turn", next),
      }),
    ),
  )
}

function DeliveryPage(props) {
  const { state } = props
  const values = (state.config && state.config.values) || {}
  return h(
    "div",
    { style: { display: "grid", gap: 16 } },
    h(
      Card,
      {
        title: "How advice reaches the agent",
        description:
          "A review can arrive as a card you read now, or as a new turn the agent answers. The first is nearly free; the second costs a full turn.",
      },
      h(Choice, {
        label: "Delivery policy",
        value: values.delivery_policy,
        options: [
          {
            value: "severity",
            label: "By severity (recommended)",
            description:
              "Blockers start a turn so the agent can fix them; notes and concerns wait for the next turn.",
          },
          {
            value: "ambient",
            label: "Always ambient",
            description:
              "A card now, and the agent sees the note at the start of its next turn. No extra model call.",
          },
          {
            value: "corrective",
            label: "Always corrective",
            description: "Every note starts a new turn. Most immediate, and the most expensive.",
          },
        ],
        onChange: (next) => state.save("delivery_policy", next),
      }),
      h(NumberField, {
        label: "Corrective turns per request",
        hint: "Caps how often advice can start its own turn within one request from you.",
        value: values.delivery_max_corrective_per_request,
        min: 0,
        max: 5,
        onChange: (next) => state.save("delivery_max_corrective_per_request", next),
      }),
      h(Toggle, {
        label: "Show a card for every note",
        hint: "Turn this off and ambient advice still reaches the model, but you never see it.",
        checked: values.delivery_show_cards,
        onChange: (next) => state.save("delivery_show_cards", next),
      }),
      h(NumberField, {
        label: "Undelivered notes expire after",
        hint: "Turns. A note nobody delivered is dropped rather than surfacing stale.",
        value: values.delivery_note_ttl_turns,
        min: 1,
        max: 20,
        onChange: (next) => state.save("delivery_note_ttl_turns", next),
      }),
    ),
  )
}

function BudgetPage(props) {
  const { state } = props
  const values = (state.config && state.config.values) || {}
  return h(
    Card,
    {
      title: "Budget",
      description:
        "Coach spend is priced from real usage, including cached tokens. This is a ceiling for the whole profile, not a prediction.",
    },
    h(NumberField, {
      label: "Budget per session (USD)",
      hint: "A review is usually a fraction of a cent; a corrective turn costs more because it is a full turn.",
      value: values.budget_session_usd,
      min: 0,
      max: 100,
      step: 0.1,
      onChange: (next) => state.save("budget_session_usd", next),
    }),
    h(NumberField, {
      label: "Unpriced models: fallback price per million tokens (USD)",
      hint: "The host's price table does not know every provider. An unpriced call is costed from its tokens at this blended rate, so the budget still stops.",
      value: values.fallback_price_per_mtok,
      min: 0,
      max: 100,
      step: 0.1,
      onChange: (next) => state.save("fallback_price_per_mtok", next),
    }),
    h(Choice, {
      label: "When the budget is reached",
      value: values.budget_on_exceed,
      options: [
        {
          value: "warn",
          label: "Warn only",
          description: "Keeps reviewing and records the overrun.",
        },
        {
          value: "soft",
          label: "Stop automatic reviews",
          description: "Turns off Active, Review and Decide; consults you ask for still work.",
        },
        {
          value: "hard",
          label: "Stop everything",
          description: "No coach calls at all until the budget is raised.",
        },
      ],
      onChange: (next) => state.save("budget_on_exceed", next),
    }),
  )
}

const STARTER = `# Coach guidance

- Name the file and line for every finding.
- Do not suggest new dependencies; use what the project already has.
- Prefer one concrete fix over a list of options.
- This project targets Python 3.14 and runs on Linux, macOS and Windows.
`

function GuidancePage(props) {
  const { ctx, state } = props
  const cwd = useValue(host.state.cwd)
  const [scope, setScope] = useState("global")
  const [text, setText] = useState("")
  const [path, setPath] = useState("")
  const [notice, setNotice] = useState("")
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  const seq = useRef(0)

  const load = async (nextScope) => {
    if (nextScope === "project" && !cwd) {
      setText("")
      setPath("")
      setNotice("Open a project chat to edit project guidance; showing global scope instead is available above.")
      setLoading(false)
      return
    }
    const id = ++seq.current
    setNotice("")
    setLoading(true)
    try {
      const query = `/guidance?scope=${encodeURIComponent(nextScope)}&cwd=${encodeURIComponent(cwd || "")}`
      const data = unwrap(await ctx.rest(query))
      if (seq.current !== id) return  // a newer scope won
      setText(data && typeof data.text === "string" ? data.text : "")
      setPath(data && data.path ? String(data.path) : "")
    } catch (err) {
      if (seq.current === id) setNotice(messageOf(err))
    } finally {
      if (seq.current === id) setLoading(false)
    }
  }

  useEffect(() => {
    load(scope)
  }, [scope, cwd])

  const save = async () => {
    if (loading) {
      setNotice("Still loading guidance — try again in a moment.")
      return
    }
    // Capture what the user sees now: if the scope or cwd changes while the
    // PUT is in flight, the result below must not overwrite the new editor.
    const id = seq.current
    const saveScope = scope
    const saveCwd = cwd
    const saveText = text
    setSaving(true)
    setNotice("")
    try {
      const result = unwrap(
        await ctx.rest("/guidance", {
          method: "PUT",
          body: { scope: saveScope, cwd: saveCwd, text: saveText },
        }),
      )
      if (seq.current !== id) {
        // The scope or cwd changed (and reloaded) while the PUT was in
        // flight: the write landed, but this editor now shows another file.
        setNotice("Saved. The editor moved on, so reload to see it.")
        return
      }
      setNotice(result && result.message ? result.message : "Saved.")
      if (result && result.path) setPath(String(result.path))
    } catch (err) {
      if (seq.current === id) setNotice(messageOf(err))
    } finally {
      setSaving(false)
    }
  }

  const tokens = Math.ceil(text.length / 4)

  return h(
    "div",
    { style: { display: "grid", gap: 16 } },
    h(
      Card,
      {
        title: "Guidance",
        description:
          "Extra instructions for the coach, sent with every call. Keep it short: whatever is here is paid on every review.",
      },
      h(
        "div",
        { style: { display: "flex", gap: 8 } },
        ["project", "global"].map((value) =>
          h(
            "button",
            {
              key: value,
              type: "button",
              style: {
                ...BUTTON,
                fontWeight: scope === value ? 700 : 400,
                borderColor:
                  scope === value
                    ? "color-mix(in srgb, currentColor 55%, transparent)"
                    : "color-mix(in srgb, currentColor 24%, transparent)",
              },
              onClick: () => setScope(value),
            },
            value === "project" ? "This project" : "All projects",
          ),
        ),
      ),
      h(
        "p",
        { style: MUTED },
        scope === "project"
          ? "Saved as COACH.md at the root of the current project, so it travels with the repository and can be reviewed like code."
          : "Saved in your Hermes profile and applied to every project. Reserve it for things that are always true.",
      ),
      path ? h("p", { style: { ...MUTED, fontFamily: "monospace" } }, path) : null,
      h("textarea", {
        id: "coach-guidance-text",
        "aria-label": "Guidance text",
        style: {
          ...INPUT,
          minHeight: 220,
          resize: "vertical",
          fontFamily: "monospace",
          fontSize: "0.8125rem",
        },
        placeholder: "Project traps, style rules, things the coach should not suggest here.",
        value: text,
        onChange: (event) => setText(event.target.value),
      }),
      h(
        "div",
        { style: { display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" } },
        h(
          "button",
          { type: "button", style: BUTTON, onClick: save, disabled: saving || loading },
          saving ? "Saving..." : loading ? "Loading..." : "Save guidance",
        ),
        h(
          "button",
          { type: "button", style: BUTTON, onClick: () => setText(STARTER) },
          "Insert a starter",
        ),
        h("span", { style: MUTED }, `${text.length} characters, about ${tokens} tokens`),
        notice ? h("span", { style: { ...MUTED, opacity: 1 } }, notice) : null,
      ),
    ),
    h(
      Card,
      {
        title: "Send guidance at all",
        description: "Turning this off ignores both files without deleting them.",
      },
      h(Toggle, {
        label: "Use guidance",
        checked:
          (state.config && state.config.values && state.config.values.guidance_enabled) || false,
        onChange: (next) => state.save("guidance_enabled", next),
      }),
    ),
  )
}

function useRecent(ctx, sessionParam) {
  // One fetch, one error path: the page had two copies and only one of them
  // reported a failure, so "no reviews" could mean "the request failed".
  const [recent, setRecent] = useState([])
  const [recentError, setRecentError] = useState("")
  const [loading, setLoading] = useState(true)
  const pull = (alive) =>
    ctx
      .rest(`/recent?limit=8${sessionParam}`)
      .then((result) => {
        if (!alive()) return
        const list = (unwrap(result) || {}).reviews
        setRecent(Array.isArray(list) ? list : [])
        setLoading(false)
      })
      .catch((err) => {
        if (alive()) setRecentError(messageOf(err))
      })
  useEffect(() => {
    let live = true
    setRecentError("")
    pull(() => live)
    return () => {
      live = false
    }
  }, [sessionParam])
  const refresh = () => {
    setRecentError("")
    pull(() => true)
  }
  return { recent, recentError, loading, refresh }
}

function usePermissions(ctx, state, setNotice) {
  const [busy, setBusy] = useState(false)
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])
  const apply = async (path, done) => {
    setBusy(true)
    setNotice("")
    try {
      await ctx.rest(path, { method: "POST", body: {} })
      if (!alive.current) return
      setNotice(done)
      await state.reload()
    } catch (err) {
      if (alive.current) setNotice(messageOf(err))
    } finally {
      if (alive.current) setBusy(false)
    }
  }
  return {
    busy,
    grant: () => apply("/permissions/grant", "Model overrides granted."),
    revoke: () => apply("/permissions/revoke", "Model overrides revoked."),
  }
}

function Segmented({ value, options, onChange }) {
  return h(
    "div",
    { style: { display: "flex", gap: 8 } },
    options.map((option) =>
      h(
        "button",
        {
          key: option.value,
          type: "button",
          style: {
            ...BUTTON,
            fontWeight: value === option.value ? 700 : 400,
            borderColor:
              value === option.value
                ? "color-mix(in srgb, currentColor 55%, transparent)"
                : "color-mix(in srgb, currentColor 24%, transparent)",
          },
          onClick: () => onChange(option.value),
        },
        option.label,
      ),
    ),
  )
}

function StatusPage(props) {
  const { ctx, state } = props
  const status = state.status || {}
  const permissions = status.permissions || {}
  const [notice, setNotice] = useState("")
  const [scope, setScope] = useState("chat")
  const focused = useValue(host.state.focusedSessionId)
  // Never silently widen "This chat" to the whole profile.
  const chatScopeUnavailable = scope === "chat" && !focused
  const sessionParam =
    scope === "chat" && focused ? `&session=${encodeURIComponent(focused)}` : ""
  const {
    recent: recentRows,
    recentError,
    loading: recentLoading,
    refresh: refreshRecent,
  } = useRecent(ctx, chatScopeUnavailable ? "&session=__none__" : sessionParam)
  const recent = recentRows
  const { busy: granting, grant, revoke } = usePermissions(ctx, state, setNotice)

  // The host renders plugin cards as notifications, which cannot carry
  // buttons. The composer IS scriptable, so the click lives here instead.
  const sendToChat = (row) => {
    const composer = host.composer
    if (!composer || typeof composer.submit !== "function") {
      setNotice("This Desktop build cannot send to the chat.")
      return
    }
    const outcome = composer.submit(focused || null, findingPrompt(row))
    const done = (ok) =>
      setNotice(ok ? "Sent to this chat." : "Could not reach this chat's composer.")
    if (outcome && typeof outcome.then === "function") {
      outcome.then(done, () => done(false))
    } else {
      done(outcome !== false && outcome !== null && outcome !== undefined)
    }
  }

  const spendMap =
    status.spend_sessions && typeof status.spend_sessions === "object"
      ? status.spend_sessions
      : {}
  const totalSpend = finiteOrNull(status.spend_total_usd ?? status.spend_usd)
  const perSessionBudget = finiteOrNull(
    status.budget_per_session_usd ?? status.budget_usd,
  )
  const thisChatSpend =
    focused && Object.prototype.hasOwnProperty.call(spendMap, focused)
      ? finiteOrNull(spendMap[focused])
      : null
  const rows = [
    ["Coach model", status.configured ? modelLabel(status.coach_model) : "Not configured"],
    ["Active review", status.active_enabled ? "On" : "Off"],
    ["Completion review", status.review_enabled ? "On" : "Off"],
    [
      "Decisions",
      status.decide_enabled
        ? status.decide_backend
          ? `On, ${status.decide_backend}`
          : "On"
        : "Off",
    ],
    ...(thisChatSpend !== null
      ? [
          [
            "This chat spend",
            `${formatUsd(thisChatSpend)} of ${formatUsd(perSessionBudget)} budgeted`,
          ],
        ]
      : []),
    [
      "Spend total, all sessions",
      `${formatUsd(totalSpend)} total · ${formatUsd(perSessionBudget)} budget per session`,
    ],
    ["Pending notes", String(status.pending_notes || 0)],
    ["Sessions with state", String(status.sessions || 0)],
  ]

  return h(
    "div",
    { style: { display: "grid", gap: 16 } },
    h(
      Card,
      {
        title: "Status",
        description: status.configured
          ? "The coach is configured. Each mode only spends when its own trigger fires."
          : "No coach model is set yet. Choose one on the Models tab; until then the tools answer without calling a model.",
      },
      h(
        "dl",
        { style: { display: "grid", gridTemplateColumns: "auto 1fr", gap: "7px 18px", margin: 0 } },
        rows.map(([label, value]) =>
          h(
            "div",
            { key: label, style: { display: "contents" } },
            h("dt", { style: { opacity: 0.65 } }, label),
            h("dd", { style: { margin: 0 } }, value),
          ),
        ),
      ),
      status.last_error ? h("p", { style: MUTED }, `Last error: ${status.last_error}`) : null,
    ),
    h(
      Card,
      {
        title: "Recent reviews",
        description:
          "The last findings the coach logged for this profile, newest first. Consult advice is in the conversation and in /coach last.",
      },
      h(
        "div",
        { style: { display: "flex", gap: 8 } },
        h(Segmented, {
          value: scope,
          options: [
            { value: "chat", label: "This chat" },
            { value: "all", label: "All sessions" },
          ],
          onChange: setScope,
        }),
        h(
          "button",
          { type: "button", style: BUTTON, onClick: refreshRecent },
          "Refresh",
        ),
      ),
      recent.length === 0
        ? h(
            "p",
            { style: MUTED, role: "status" },
            recentLoading
              ? "Loading recent reviews..."
              : emptyRecentMessage(recentError, scope),
          )
        : recent.map((row, index) =>
            h(
              "div",
              {
                // Rows may share session/at/title when fields are missing;
                // the index keeps reconciliation stable on re-order.
                key: `${row.id || ""}-${row.session}-${row.at}-${row.title}-${index}`,
                style: { display: "grid", gap: 2 },
              },
              h(
                "div",
                { style: { fontWeight: 600, fontSize: "0.8125rem" } },
                `${row.severity || "none"} — ${row.title || "no finding"}`,
              ),
              row.body ? h("p", { style: MUTED }, String(row.body).slice(0, 220)) : null,
              row.error ? h("p", { style: MUTED }, `error: ${row.error}`) : null,
              h(
                "button",
                {
                  type: "button",
                  style: { ...BUTTON, justifySelf: "start", fontSize: "0.75rem" },
                  onClick: () => sendToChat(row),
                },
                "Send to chat",
              ),
            ),
          ),
    ),
    h(
      Card,
      {
        title: "Model overrides",
        description:
          "Per-mode models need permission to pass their own provider and model. Granting is a one-time switch for this plugin; inheriting the shared model never needs it.",
      },
      h(
        "p",
        { style: { ...MUTED, opacity: 1, fontWeight: 600 }, role: "status" },
        permissions.granted ? "Granted" : "Not granted",
      ),
      permissions.granted
        ? h(
            "button",
            { type: "button", style: BUTTON, onClick: revoke, disabled: granting },
            granting ? "Working..." : "Revoke model overrides",
          )
        : h(
            "button",
            { type: "button", style: BUTTON, onClick: grant, disabled: granting },
            granting ? "Granting..." : "Grant model overrides",
          ),
      !permissions.granted && (permissions.provider_overrides !== permissions.model_overrides)
        ? h(
            "p",
            { style: MUTED, role: "status" },
            "Partial grant: both provider and model overrides are required.",
          )
        : null,
      notice ? h("p", { style: MUTED, role: "status" }, notice) : null,
    ),
    h(
      Card,
      {
        title: "Clean up",
        description:
          "Pending advice can be listed and released from a normal chat with /coach pending and /coach send.",
      },
      h(
        "p",
        { style: MUTED },
        "State lives under ~/.hermes/coach, pruned to the twenty newest sessions.",
      ),
    ),
  )
}

// ---------------------------------------------------------------- register

export function register(ctx) {
  if (!ctx || typeof ctx.registerSettingsPage !== "function") {
    throw new Error("Coach settings need the Hermes Desktop plugin host.")
  }

  const page = (Component) =>
    function CoachPage() {
      const state = useSettings(ctx)
      const main = useValue(host.state.model)
      if (!state.config) {
        return h(
          "div",
          { style: { display: "grid", gap: 10, padding: 8 } },
          h("p", { style: MUTED, role: "status" }, state.error || "Loading settings..."),
          h("button", { type: "button", style: BUTTON, onClick: () => state.reload() }, "Reload"),
        )
      }
      const raw = String(main || "")
      // Split on the LAST slash: model ids can contain slashes
      // (provider/org/model), and splitting on the first defeats sameModel().
      const slash = raw.lastIndexOf("/")
      const mainModel = raw
        ? {
            provider: slash >= 0 ? raw.slice(0, slash) : "",
            model: slash >= 0 ? raw.slice(slash + 1) : raw,
          }
        : null
      return h(
        "div",
        { style: { display: "grid", gap: 10 } },
        state.saveError
          ? h(
              "p",
              { style: MUTED, role: "status" },
              `Could not save: ${state.saveError}`,
            )
          : null,
        h(Component, { ctx, state, mainModel }),
      )
    }

  ctx.registerSettingsPage({
    id: "coach",
    title: "Coach",
    icon: "sparkle",
    render: page(ModelSection),
    children: [
      { id: "behaviour", title: "Reviews", render: page(BehaviourPage) },
      { id: "delivery", title: "Delivery", render: page(DeliveryPage) },
      { id: "budget", title: "Budget", render: page(BudgetPage) },
      { id: "guidance", title: "Guidance", render: page(GuidancePage) },
      { id: "status", title: "Status", render: page(StatusPage) },
    ],
  })
}

export { findingPrompt, flattenModels, groupModels };

export default { id: "coach", name: "Coach", register }
