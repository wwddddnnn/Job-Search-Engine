import {initialize, selectDocument, changeView, changeLanguage, openDraft,
  importFile, retryImport, canRetryImport} from "../actions.js";
import {subscribe, getState} from "../state.js";
import {t} from "../i18n.js";
import {APIError} from "../api.js";
import {documentList} from "./documents.js";
import {readerView} from "./reader.js";
const language = document.querySelector("#language"), file = document.querySelector("#file");
const draftButton = document.querySelector("#open-draft");
const retry = document.querySelector("#retry-import");
const errorBox = document.querySelector("#error");
const profileStatus = document.querySelector("#profile-status");
const draftStatus = document.querySelector("#draft-status");
const importStatus = document.querySelector("#import-status");
const list = documentList(selectDocument), reader = readerView(changeView);
language.addEventListener("change", () => changeLanguage(language.value));
file.addEventListener("change", () => importFile(file.files[0]));
draftButton.addEventListener("click", openDraft);
retry.addEventListener("click", retryImport);
function render(state, previous = {}) {
  if (state.language !== previous.language) {
    document.documentElement.lang = state.language;
    document.title = t(state.language, "title");
    document.querySelectorAll("[data-i18n]").forEach(node => {
      node.textContent = t(state.language, node.dataset.i18n);
    });
    file.setAttribute("aria-label", t(state.language, "import"));
    document.querySelector("#view-switch").setAttribute(
      "aria-label", t(state.language, "viewLabel"),
    );
    language.value = state.language;
  }
  profileStatus.textContent = state.profile?.version ?
    `${t(state.language, "profile")} · ${state.profile.display_name} · ` +
      `${t(state.language, "version")} ${state.profile.version}` : t(state.language, "noProfile");
  draftStatus.textContent = state.draft ? `${t(state.language, "draft")} · ` +
    `${t(state.language, "version")} ${state.draft.version}` : t(state.language, "noDraft");
  draftButton.hidden = !!state.draft;
  draftButton.disabled = state.busy || !state.ready;
  language.disabled = !state.ready;
  file.disabled = state.busy || !state.ready;
  retry.hidden = !canRetryImport() || state.busy;
  importStatus.textContent = state.importStatus ? t(state.language, state.importStatus) : "";
  errorBox.hidden = !state.error;
  errorBox.textContent = state.error ? new APIError(state.error.code).localized(state.language) +
    (state.error.correlationId ? ` (${state.error.correlationId})` : "") : "";
  if (state.documents !== previous.documents || state.language !== previous.language ||
      state.selectedId !== previous.selectedId) list(state);
  reader(state);
}
subscribe(render);
render(getState());
initialize();
