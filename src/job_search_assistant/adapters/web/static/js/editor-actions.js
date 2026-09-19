import {request, isStale} from "./api.js";
import {getState, update} from "./state.js";
import {SaveQueue} from "./editor-state.js";
let timer;
export const writer = new SaveQueue(
  (action, body) => request(`/review/${action}`, {method: "POST", body}),
  queue => update({
    ...(queue.clean && getState().composer &&
      queue.draft?.items.length > (getState().draft?.items.length || 0) ? {composer: null} : {}),
    draft: queue.draft, editItems: queue.items(), saveStatus: queue.status,
    saveError: queue.failure ? {code: queue.failure.code,
      correlationId: queue.failure.correlationId} : null}),
);
export function loadEditor(draft) { writer.load(draft); }
export function editItem(item_id, changes) {
  if (getState().publishing) return;
  cancelPreview();
  writer.enqueue("edit", {item_id, changes});
  clearTimeout(timer); timer = setTimeout(() => writer.flush(), 450);
}
export function operate(action, payload) {
  if (!writer.clean || getState().publishing) return;
  cancelPreview(); writer.enqueue(action, payload); writer.flush();
}
export function retrySave() { writer.retry(); }
export async function previewPublication() {
  if (!writer.clean || getState().composer || getState().publishing) return;
  const draft = writer.draft;
  update({publishing: true, error: null});
  try {
    const preview = await request("/review/preview", {method: "POST", body: {
      draft_id: draft.id, expected_version: draft.version, base_version_id: draft.base_version_id,
    }});
    if (writer.clean && writer.draft.version === draft.version) update({preview});
  } catch (error) { update({error}); }
  finally { update({publishing: false}); }
}
let publication = null;
export async function publish() {
  const state = getState(), preview = state.preview;
  if (!preview || !writer.clean || state.composer || state.publishing) return;
  if (preview.draft_version !== writer.draft.version) return;
  publication ??= {draft_id: preview.draft_id, expected_version: preview.draft_version,
    base_version_id: preview.base_version_id, idempotency_key: crypto.randomUUID()};
  update({publishing: true, error: null});
  try {
    const result = await request("/review/publish", {method: "POST", body: publication});
    loadEditor(result.draft); publication = null;
    update({preview: null, profile: await request("/profile")});
    await history();
  } catch (error) { update({error}); }
  finally { update({publishing: false}); }
}
export function cancelPreview() { publication = null; update({preview: null}); }
export async function history(versionId = null) {
  try {
    const versions = await request("/profile/versions", {latest: "history-list"});
    const snapshot = await request(versionId ? `/profile/versions/${versionId}` : "/profile",
      {latest: "history-snapshot"});
    update({versions, snapshot});
  } catch (error) { if (!isStale(error)) update({error}); }
}
export function setSelection(selection) { update({selection}); }
export function compose(kind, parent_id = null) {
  if (!writer.clean || getState().publishing) return;
  cancelPreview();
  const selection = getState().selection;
  const text = selection?.text || "";
  const fields = kind === "experience" ? {organization: "", role: "", summary: text} :
    kind === "achievement" ? {action_text: text} : {raw_skill_name: text};
  update({composer: {kind, parent_id, fields, selection: selection?.locator || null}});
}
export function changeComposer(fields) {
  if (writer.failure?.code === "validation_error" && writer.queue[0]?.action === "create") {
    // Validation made no write. Keep corrected text; createItem assigns a fresh command key.
    writer.queue.shift(); writer.failure = null; writer.status = "saved"; writer.emit();
  }
  update({composer: {...getState().composer, fields}});
}
export function createItem() {
  const composer = getState().composer;
  if (!composer || !writer.clean) return;
  operate("create", composer);
  // Keep the composer until persistence succeeds, including on a failed create.
}
export function cancelComposer() { if (writer.clean) update({composer: null}); }
