import {loadEditor} from "./editor-actions.js";
import {request, APIError, isStale, invalidate} from "./api.js";
import {getState, update} from "./state.js";
let pendingImport = null;
let languageQueue = Promise.resolve();
let languageGeneration = 0;
let savedLanguage = "zh";
function failed(error) {
  if (!isStale(error)) update({error: {
    code: error.code || "unknown_error", correlationId: error.correlationId,
  }});
}
export async function initialize() {
  try {
    const session = await request("/session");
    savedLanguage = session.language;
    update({language: session.language, profile: session.profile});
    const [documents, draft] = await Promise.all([
      request("/documents"), request("/draft"),
    ]);
    update({documents, draft, ready: true});
    loadEditor(draft);
    if (documents.length) await selectDocument(documents[0].id);
  } catch (error) { failed(error); }
}
export async function selectDocument(id) {
  update({selectedId: id, document: null, loading: true, error: null, selection: null});
  try {
    const document = await request(`/documents/${encodeURIComponent(id)}`, {latest: "document"});
    update({document, loading: false});
  } catch (error) {
    if (!isStale(error)) { update({loading: false}); failed(error); }
  }
}
export function changeView(view) { update({view}); }
export function changeLanguage(language) {
  const generation = ++languageGeneration;
  update({language, error: null});
  // Writes run in order; an old completion cannot replace the latest language selection.
  languageQueue = languageQueue.then(async () => {
    try {
      await request("/settings/ui", {method: "PUT", body: {
        language, idempotency_key: crypto.randomUUID(),
      }});
      savedLanguage = language;
    } catch (error) {
      if (generation === languageGeneration) { update({language: savedLanguage}); failed(error); }
    }
  });
}
export async function openDraft() {
  update({busy: true, error: null});
  try {
    const draft = await request("/draft", {method: "POST", body: {
      idempotency_key: crypto.randomUUID(),
    }});
    const profile = await request("/profile");
    update({draft, profile});
    loadEditor(draft);
  } catch (error) { failed(error); }
  finally { update({busy: false}); }
}
export async function importFile(file) {
  if (getState().busy || !file) return;
  update({busy: true, error: null, importStatus: "importing"});
  pendingImport = null;
  try {
    if (!/\.md$/i.test(file.name)) throw new APIError("invalid_file");
    if (file.size > 2 * 1024 * 1024) throw new APIError("payload_too_large");
    let content;
    try {
      content = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true})
        .decode(await file.arrayBuffer());
    } catch { throw new APIError("invalid_file"); }
    pendingImport = {filename: file.name, content, idempotency_key: crypto.randomUUID()};
    await submitImport();
  } catch (error) { failed(error); update({importStatus: null}); }
  finally { update({busy: false}); }
}
async function submitImport() {
  const result = await request("/documents", {method: "POST", body: pendingImport});
  const documents = await request("/documents");
  update({documents, importStatus: "imported"});
  invalidate("document");
  await selectDocument(result.document_id);
  pendingImport = null;
}
export function canRetryImport() { return pendingImport !== null; }
export async function retryImport() {
  if (!pendingImport || getState().busy) return;
  update({busy: true, error: null, importStatus: "importing"});
  try { await submitImport(); }
  catch (error) { failed(error); update({importStatus: null}); }
  finally { update({busy: false}); }
}
