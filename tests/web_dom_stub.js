// Minimal DOM/event seam: run real view branches without claiming browser layout coverage.
class TestNode {
  constructor(tag = "div") {
    this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {};
    this.value = ""; this.textContent = ""; this.disabled = false; this.hidden = false;
  }
  append(...nodes) {
    nodes.forEach(node => { this.children.push(node); node.parentElement = this; });
  }
  replaceChildren() { this.children = []; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this[name] = value; }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  querySelectorAll(selector) {
    if (selector.includes(",")) {
      return selector.split(",").flatMap(part => this.querySelectorAll(part.trim()));
    }
    return this.children.flatMap(child => [
      ...(selector === "[data-i18n]" ? (child.dataset.i18n ? [child] : []) :
        selector === "[data-llm-code]" ? (child.dataset.llmCode ? [child] : []) :
        child.tagName === selector ? [child] : []), ...child.querySelectorAll(selector),
    ]);
  }
}
const roots = new Map();
const document = {
  documentElement: {}, activeElement: null, listeners: {},
  createElement: tag => new TestNode(tag),
  querySelector: selector => {
    if (!roots.has(selector)) roots.set(selector, new TestNode());
    return roots.get(selector);
  },
  querySelectorAll: () => [],
  addEventListener(name, callback) { this.listeners[name] = callback; },
  createTreeWalker: root => {
    let index = 0;
    return {nextNode: () => root.textNodes[index++] || null};
  },
};
const NodeFilter = {SHOW_TEXT: 4};
let selectedRange;
const window = {getSelection: () => ({rangeCount: 1, getRangeAt: () => selectedRange})};
const byKey = key => document.querySelector("#editor").querySelectorAll("[data-i18n]")
  .find(node => node.dataset.i18n === key);
const textOf = node => [node.textContent, ...node.children.map(textOf)].join("\n");
