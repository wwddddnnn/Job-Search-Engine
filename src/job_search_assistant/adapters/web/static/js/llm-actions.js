import {request} from "./api.js";
import {getLLMState, updateLLM} from "./llm-state.js";
// Pending commands exist only in memory for exact idempotent retries.
let pending = null;
export function editConfig(config = {}) {
  if (getLLMState().busy || pending) return;
  updateLLM({editing: {...config}, resetting: !config.has_key, error: null});
}
export function resetKey() { updateLLM({resetting: true}); }
export function cancelConfig() {
  pending = null;
  updateLLM({editing: null, resetting: false, error: null});
}
export async function loadConfigs() {
  try {
    const [configs, selection] = await Promise.all([
      request("/llm/configs"), request("/llm/selection"),
    ]);
    updateLLM({configs, selected: selection.config_id, loaded: true, error: null});
  } catch (error) { updateLLM({error: error.code || "unknown_error"}); }
}
export function saveConfig(fields, keyValue) {
  const state = getLLMState();
  if (state.busy || pending) return;
  const id = state.editing?.id;
  const body = {...fields};
  // A displayed mask is never a field value. Omission means retain the saved credential.
  if (state.resetting) body.api_key = keyValue;
  return send(id ? `/llm/configs/${encodeURIComponent(id)}` : "/llm/configs",
    id ? "PUT" : "POST", body);
}
export function deleteConfig(id) {
  return send(`/llm/configs/${encodeURIComponent(id)}`, "DELETE", {});
}
export function selectConfig(id) {
  return send("/llm/selection", "PUT", {config_id: id});
}
function send(path, method, body) {
  if (getLLMState().busy || pending) return;
  pending = {path, method, body: {...body, idempotency_key: crypto.randomUUID()}};
  return retryConfig();
}
export async function retryConfig() {
  if (!pending || getLLMState().busy) return;
  updateLLM({busy: true, error: null});
  try {
    await request(pending.path, {method: pending.method, body: pending.body});
    pending = null;
    updateLLM({editing: null, resetting: false});
    await loadConfigs();
  } catch (error) {
    // Validation is definitive; transport failures retain the exact command for retry.
    if (error.code === "validation_error") pending = null;
    updateLLM({error: error.code || "unknown_error"});
  } finally { updateLLM({busy: false}); }
}
export const hasPendingConfig = () => pending !== null;
