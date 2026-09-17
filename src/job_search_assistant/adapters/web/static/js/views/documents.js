import {t} from "../i18n.js";
export function documentList(select) {
  const root = document.querySelector("#documents");
  const empty = document.querySelector("#no-documents");
  const nodes = new Map();
  return state => {
    empty.hidden = state.documents.length > 0;
    for (const item of state.documents) {
      let node = nodes.get(item.id);
      if (!node) {
        const li = document.createElement("li");
        const button = document.createElement("button");
        const name = document.createElement("span"), meta = document.createElement("small");
        button.append(name, meta); li.append(button); root.append(li);
        button.addEventListener("click", () => select(item.id));
        node = {button, name, meta}; nodes.set(item.id, node);
      }
      node.name.textContent = item.filename;
      const status = item.status === "imported" ? "imported_status" : item.status;
      node.meta.textContent = `${t(state.language, status)} · ` +
        new Date(item.imported_at).toLocaleString(state.language === "zh" ? "zh-CN" : "en-CA");
      node.button.setAttribute("aria-current", String(item.id === state.selectedId));
    }
  };
}
