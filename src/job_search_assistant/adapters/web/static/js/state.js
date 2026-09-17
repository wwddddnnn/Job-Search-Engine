// Immutable snapshots and subscriptions; no browser persistence or DOM dependency.
let current = freeze({
  ready: false, language: "zh", documents: [], selectedId: null, document: null, view: "rendered",
  profile: null, draft: null, error: null, importStatus: null, busy: false, loading: false,
});
const subscribers = new Set();
function freeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}
export const getState = () => current;
export function update(patch) {
  const previous = current;
  current = freeze({...current, ...patch});
  subscribers.forEach(callback => callback(current, previous));
}
export function subscribe(callback) {
  subscribers.add(callback);
  return () => subscribers.delete(callback);
}
