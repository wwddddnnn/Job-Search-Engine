import {t} from "../i18n.js";
import {getState, update} from "../state.js";
import {writer, editItem, operate, retrySave, compose, changeComposer, createItem,
  cancelComposer, previewPublication, publish, cancelPreview, history} from "../editor-actions.js";
const fields = {
  experience: ["organization", "role", "date_range", "summary"],
  achievement: ["action_text", "outcome_text", "metric_value", "metric_unit"],
  skill: ["raw_skill_name", "canonical_name", "proficiency"],
};
function element(tag, parent, key = null) {
  const node = document.createElement(tag);
  if (key) node.dataset.i18n = key;
  parent.append(node); return node;
}
function button(parent, key, action) {
  const node = element("button", parent, key);
  node.type = "button"; node.addEventListener("click", action); return node;
}
function values(inputs) {
  return Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key,
    key === "metric_value" ? (input.value === "" ? null : Number(input.value)) :
      (input.value || (["organization", "role", "action_text", "raw_skill_name"].includes(key) ?
        "" : null)),
  ]));
}
function form(root, item, onInput) {
  const inputs = {};
  for (const key of fields[item.kind]) {
    const label = element("label", root); element("span", label, key);
    const input = element(key === "summary" || key === "action_text" ? "textarea" : "input",
      label);
    if (key === "metric_value") { input.type = "number"; input.step = "any"; }
    inputs[key] = input;
    input.value = item.fields[key] ?? "";
    input.addEventListener("input", () => onInput(values(inputs)));
  }
  return inputs;
}
function translate(root, language) {
  root.querySelectorAll("[data-i18n]").forEach(node => {
    node.textContent = t(language, node.dataset.i18n);
  });
}
export function editorView() {
  const root = document.querySelector("#editor"), toolbar = element("div", root);
  element("h2", toolbar, "editTitle");
  const selection = element("p", toolbar), clear = button(toolbar, "clearSelection", () => {
    update({selection: null}); window.getSelection()?.removeAllRanges();
  });
  const add = button(toolbar, "newExperience", () => compose("experience"));
  const status = element("p", root); status.setAttribute("role", "status");
  const retry = button(root, "retrySave", retrySave);
  const composerRoot = element("section", root);
  const itemsRoot = element("div", root), nodes = new Map();
  const previewButton = button(root, "previewPublish", previewPublication);
  const previewRoot = element("section", root);
  const historyButton = button(root, "history", () => history());
  const historyRoot = element("section", root);
  let composerNode = null, previousPreview = null, previousSnapshot = null, previousVersions = null;
  function card(item) {
    const node = element("section", itemsRoot); node.className = "edit-card";
    const title = element("h3", node, item.kind), progress = element("p", node);
    const inputs = form(node, item, changes => editItem(item.id, changes));
    const source = element("p", node); source.className = "hint";
    const actions = element("div", node); actions.className = "item-actions";
    const controls = {};
    for (const decision of ["confirm", "reject", "clarify", "confirm_delete"]) {
      controls[decision] = button(actions, decision, () => operate("decide", {
        item_id: item.id, decision,
      }));
    }
    for (const action of ["delete", "restore"]) {
      controls[action] = button(actions, action, () => operate(action, {item_id: item.id}));
    }
    if (item.kind === "experience") {
      controls.achievement = button(actions, "addAchievement", () => compose("achievement", item.id));
      controls.skill = button(actions, "addSkill", () => compose("skill", item.id));
    }
    return {node, title, progress, inputs, source, controls};
  }
  return state => {
    root.hidden = !state.draft;
    const locked = !writer.clean || state.publishing;
    selection.textContent = state.selection ? `${t(state.language, "selected")}: ` +
      state.selection.text : t(state.language, "manualHint");
    clear.hidden = !state.selection;
    add.disabled = locked || !!state.composer;
    status.textContent = t(state.language, state.composer && writer.clean ? "unsaved" :
      state.saveStatus || "unsaved") + (state.saveError ? " · " +
        t(state.language, state.saveError.code) : "");
    retry.hidden = !state.saveError; retry.disabled = writer.running;
    const composer = state.composer;
    if (!composer) { composerRoot.replaceChildren(); composerNode = null; }
    else {
      if (!composerNode) {
        element("h3", composerRoot, composer.kind);
        const inputs = form(composerRoot, composer, changeComposer);
        const save = button(composerRoot, "createItem", createItem);
        const cancel = button(composerRoot, "cancel", cancelComposer);
        composerNode = {inputs, save, cancel};
      }
      Object.values(composerNode.inputs).forEach(input => {
        input.disabled = locked && state.saveError?.code !== "validation_error";
      });
      composerNode.save.disabled = locked; composerNode.cancel.disabled = locked;
    }
    const items = state.editItems || state.draft?.items || [];
    // Stable keyed forms: only field values that differ are touched, never reinsert focused nodes.
    for (const item of items) {
      let view = nodes.get(item.id);
      if (!view) { view = card(item); nodes.set(item.id, view); }
      const parent = items.find(parent => parent.id === item.parent_id);
      view.progress.textContent = t(state.language, item.deleted ? "deleted" :
        item.status === "draft" ? "item_draft" : item.status) +
        (parent ? ` · ${parent.fields.organization} / ${parent.fields.role}` : "");
      for (const [key, input] of Object.entries(view.inputs)) {
        const value = String(item.fields[key] ?? "");
        if (document.activeElement !== input && input.value !== value) input.value = value;
        input.disabled = item.deleted || !!parent?.deleted || state.publishing;
      }
      view.source.textContent = item.sources.length ? item.sources.map(source =>
        source.source_excerpt).join("\n") : t(state.language, "manualSource");
      for (const [key, control] of Object.entries(view.controls)) {
        control.hidden = ["restore", "confirm_delete"].includes(key) ? !item.deleted : item.deleted;
        control.disabled = locked || !!parent?.deleted || !!state.composer;
      }
    }
    previewButton.disabled = locked || !!composer;
    if (state.preview !== previousPreview) {
      previousPreview = state.preview; previewRoot.replaceChildren();
      if (state.preview) {
        element("h3", previewRoot, "publicationSummary");
        element("h4", previewRoot, "publicationContent");
        for (const item of state.preview.content) {
          element("p", previewRoot).textContent = Object.values(item.fields)
            .filter(v => v !== null).join(" · ");
        }
        const summary = state.preview.summary;
        for (const category of ["added", "modified", "deleted"]) {
          element("h4", previewRoot, category);
          for (const item of summary[category]) {
            const row = element("p", previewRoot);
            row.textContent = Object.values(item.fields).filter(v => v !== null).join(" · ");
          }
        }
        const confirm = button(previewRoot, "confirmPublish", publish);
        confirm.disabled = !Object.values(summary).some(list => list.length);
        button(previewRoot, "cancel", cancelPreview);
      }
    }
    previewRoot.querySelectorAll("button").forEach(control => {
      if (control.dataset.i18n === "cancel") control.disabled = state.publishing;
      else control.disabled = state.publishing || !state.preview ||
        !Object.values(state.preview.summary).some(list => list.length);
    });
    historyButton.disabled = state.publishing;
    if (state.versions !== previousVersions || state.snapshot !== previousSnapshot) {
      previousVersions = state.versions; previousSnapshot = state.snapshot;
      historyRoot.replaceChildren();
      for (const version of state.versions || []) {
        const control = button(historyRoot, null, () => history(version.id));
        control.textContent = `v${version.version} · ${version.created_at}`;
      }
      if (state.snapshot) {
        const heading = element("h3", historyRoot);
        heading.textContent = `${state.snapshot.display_name} · v${state.snapshot.version}`;
        for (const experience of state.snapshot.experiences) {
          const section = element("section", historyRoot);
          element("h4", section).textContent = `${experience.organization} · ${experience.role}`;
          element("p", section).textContent = [experience.date_range, experience.summary]
            .filter(Boolean).join(" · ");
          for (const value of [...experience.achievements, ...experience.skills]) {
            element("p", section).textContent = Object.values(value).filter(v => v !== null)
              .join(" · ");
          }
        }
      }
    }
    translate(root, state.language);
  };
}
