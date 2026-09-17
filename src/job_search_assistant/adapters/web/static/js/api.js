import {t} from "./i18n.js";
let token = null;
const generations = new Map();
export class APIError extends Error {
  constructor(code, correlationId = null) {
    super(code);
    this.code = code;
    this.correlationId = correlationId;
  }
  localized(language) { return t(language, this.code); }
}
export const isStale = error => error?.name === "StaleResponse";
export function invalidate(key) {
  generations.set(key, (generations.get(key) || 0) + 1);
}
export async function request(path, {method = "GET", body, latest = null} = {}) {
  if (latest) invalidate(latest);
  const generation = generations.get(latest);
  const stale = () => latest && generation !== generations.get(latest);
  try {
    const response = await fetch(`/api${path}`, {
      method, credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", ...(token ? {"X-JSA-Token": token} : {})},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    const result = await response.json();
    if (stale()) throw Object.assign(new Error(), {name: "StaleResponse"});
    if (!response.ok) {
      throw new APIError(result.error?.code || "unknown_error", result.error?.correlation_id);
    }
    if (path === "/session") token = result.token;
    return result;
  } catch (error) {
    if (stale()) throw Object.assign(new Error(), {name: "StaleResponse"});
    if (error instanceof APIError || isStale(error)) throw error;
    throw new APIError("network_error");
  }
}
