import {setSelection} from "../editor-actions.js";
import {getState} from "../state.js";
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
  // Rendered text spans carry exact UTF-16 source positions, including repeated passages.
  document.addEventListener("selectionchange", () => {
    const selection = window.getSelection(), state = getState();
    if (!selection?.rangeCount || !state.document) return;
    const range = selection.getRangeAt(0);
    const root = state.view === "source" ? source : rendered;
    if (!root.contains(range.commonAncestorContainer) || range.collapsed) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node, start = null, end = null;
    while ((node = walker.nextNode())) {
      if (!range.intersectsNode(node)) continue;
      const base = root === source ? 0 : Number(node.parentElement.dataset.sourceStart);
      if (!Number.isFinite(base)) continue;
      const left = node === range.startContainer ? range.startOffset : 0;
      const right = node === range.endContainer ? range.endOffset : node.length;
      if (right <= left) continue;
      start ??= base + left; end = base + right;
    }
    if (start === null || end <= start) return;
    const content = state.document.content;
    setSelection({text: content.slice(start, end), locator: {
      document_id: state.document.id, start: [...content.slice(0, start)].length,
      end: [...content.slice(0, end)].length,
    }});
  });
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
