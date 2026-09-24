import {getLLMState, subscribeLLM} from "../llm-state.js";
import {editConfig, resetKey, cancelConfig, saveConfig, deleteConfig, selectConfig,
  retryConfig, hasPendingConfig, loadConfigs} from "../llm-actions.js";
import {t} from "../i18n.js";

export function llmView() {
  const root = document.querySelector("#llm-configs");
  let language = "zh", previousEditing = null;
  const heading = document.createElement("h2"), hint = document.createElement("p");
  const list = document.createElement("ul"), form = document.createElement("form");
  const error = document.createElement("p");
  error.setAttribute("role", "alert");
  function button(code, action, parent = root) {
    const node = document.createElement("button");
    node.type = "button"; node.dataset.llmCode = code;
    node.addEventListener("click", action); parent.append(node);
    return node;
  }
  root.append(heading, hint, error, list);
  const add = button("llmAdd", () => editConfig());
  const reload = button("llmReload", loadConfigs);
  root.append(form);
  const inputs = {};
  for (const field of ["name", "api_url", "model", "api_key"]) {
    const label = document.createElement("label"), title = document.createElement("span");
    title.dataset.llmCode = `llm_${field}`;
    const input = document.createElement("input");
    input.type = field === "api_key" ? "password" : "text";
    input.autocomplete = "off";
    label.append(title, input); form.append(label); inputs[field] = input;
  }
  const mask = document.createElement("span");
  form.append(mask);
  const reset = button("llmReset", resetKey, form);
  const save = button("llmSave", submit, form);
  const cancel = button("cancel", () => { inputs.api_key.value = ""; cancelConfig(); }, form);
  const retry = button("llmRetry", retryConfig);
  const discard = button("llmDiscard", () => {
    inputs.api_key.value = ""; cancelConfig();
  });
  function submit() {
    const fields = Object.fromEntries(["name", "api_url", "model"]
      .map(field => [field, inputs[field].value]));
    const keyValue = inputs.api_key.value;
    // Keep input through validation failures; editing reset clears it on success/cancel.
    saveConfig(fields, keyValue);
  }
  form.addEventListener("submit", event => { event.preventDefault(); submit(); });
  function render() {
    const state = getLLMState(), pending = hasPendingConfig();
    heading.textContent = t(language, "llmTitle");
    hint.textContent = t(language, "llmNotice");
    error.textContent = state.error ? t(language, state.error) : "";
    list.replaceChildren();
    for (const config of state.configs) {
      const row = document.createElement("li"), text = document.createElement("span");
      text.textContent = `${config.name} · ${config.model} · ${config.api_url} · ` +
        (config.has_key ? "••••••••" : t(language, "llmNoKey")) +
        (state.selected === config.id ? ` · ${t(language, "llmCurrent")}` : "");
      row.append(text);
      button("llmEdit", () => editConfig(config), row);
      button("delete", () => deleteConfig(config.id), row);
      button("llmSelect", () => selectConfig(config.id), row);
      list.append(row);
    }
    if (state.editing !== previousEditing) {
      for (const field of ["name", "api_url", "model"]) {
        inputs[field].value = state.editing?.[field] || "";
      }
      inputs.api_key.value = "";
      previousEditing = state.editing;
    }
    form.hidden = !state.editing;
    inputs.api_key.parentElement.hidden = !state.resetting;
    inputs.api_key.placeholder = t(language, "llmKeyHint");
    mask.hidden = state.resetting;
    mask.textContent = "••••••••";
    reset.hidden = state.resetting;
    root.querySelectorAll("[data-llm-code]").forEach(node => {
      node.textContent = t(language, node.dataset.llmCode);
    });
    root.querySelectorAll("button, input").forEach(node => {
      node.disabled = state.busy || pending || !state.loaded;
    });
    reload.disabled = state.busy;
    retry.hidden = !pending; retry.disabled = state.busy;
    discard.hidden = !pending; discard.disabled = state.busy;
    add.disabled = state.busy || pending || !state.loaded;
    cancel.disabled = state.busy;
    save.disabled = state.busy || pending;
  }
  subscribeLLM(render);
  return state => { language = state.language; render(); };
}
