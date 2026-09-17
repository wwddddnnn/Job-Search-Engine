import {renderMarkdown} from "../markdown.js";
import {t} from "../i18n.js";
export function readerView(changeView) {
  const rendered = document.querySelector("#rendered"), source = document.querySelector("#source");
  const empty = document.querySelector("#reader-empty");
  const title = document.querySelector("#document-title");
  const renderTab = document.querySelector("#rendered-tab");
  const sourceTab = document.querySelector("#source-tab");
  renderTab.addEventListener("click", () => changeView("rendered"));
  sourceTab.addEventListener("click", () => changeView("source"));
  let previousDocument;
  return state => {
    if (state.document !== previousDocument) {
      previousDocument = state.document;
      // Only this view inserts generated, escaped Markdown. Source always uses textContent.
      rendered.innerHTML = state.document ? renderMarkdown(state.document.content) : "";
      source.textContent = state.document?.content ?? "";
      title.textContent = state.document?.filename ?? "";
    }
    empty.hidden = !!state.document;
    empty.textContent = t(state.language, state.loading ? "loading" : "selectDocument");
    rendered.hidden = !state.document || state.view !== "rendered";
    source.hidden = !state.document || state.view !== "source";
    renderTab.setAttribute("aria-pressed", String(state.view === "rendered"));
    sourceTab.setAttribute("aria-pressed", String(state.view === "source"));
  };
}
