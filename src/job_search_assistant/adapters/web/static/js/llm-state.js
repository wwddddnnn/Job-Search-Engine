// No credential values or browser persistence in configuration state.
let state = {configs: [], selected: null, editing: null, resetting: false,
  busy: false, error: null, loaded: false};
const listeners = new Set();
export const getLLMState = () => state;
export function updateLLM(patch) {
  state = {...state, ...patch};
  listeners.forEach(listener => listener(state));
}
export function subscribeLLM(listener) { listeners.add(listener); }
