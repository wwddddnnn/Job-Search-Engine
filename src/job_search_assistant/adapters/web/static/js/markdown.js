// Deliberate Markdown subset. Every source fragment is escaped before adding markup.
export function escapeHTML(text) {
  return text.replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}
function span(raw, start) {
  return `<span data-source-start="${start}">${escapeHTML(raw)}</span>`;
}
function inline(raw, base) {
  // Tokenize once, so generated tags/attributes are never fed back through replacements.
  const pattern = /`([^`]+)`|\[([^\]\n]+)\]\(([^\s)]+)\)|\*\*([^*]+)\*\*|\*([^*]+)\*/g;
  let result = "", offset = 0;
  for (const match of raw.matchAll(pattern)) {
    result += span(raw.slice(offset, match.index), base + offset);
    const [, code, label, href, strong, emphasis] = match;
    if (code !== undefined) result += `<code>${span(code, base + match.index + 1)}</code>`;
    else if (label !== undefined) {
      let safe = false;
      try {
        const url = new URL(href);
        safe = /^(https?:\/\/|mailto:)/i.test(href) &&
          ["http:", "https:", "mailto:"].includes(url.protocol);
      } catch { /* An invalid or relative URL is displayed as plain text. */ }
      result += safe ? `<a href="${escapeHTML(href)}" rel="noreferrer noopener">` +
        `${span(label, base + match.index + 1)}</a>` : span(match[0], base + match.index);
    } else if (strong !== undefined) result += `<strong>${span(strong, base + match.index + 2)}</strong>`;
    else result += `<em>${span(emphasis, base + match.index + 1)}</em>`;
    offset = match.index + match[0].length;
  }
  return result + span(raw.slice(offset), base + offset);
}
export function renderMarkdown(source) {
  const output = [];
  let paragraph = null, paragraphEnd = 0, fence = false, codeStart = 0, list = null;
  const flush = () => {
    if (paragraph !== null) {
      output.push(`<p>${inline(source.slice(paragraph, paragraphEnd), paragraph)}</p>`);
      paragraph = null;
    }
  };
  const closeList = () => { if (list) { output.push(`</${list}>`); list = null; } };
  for (const match of source.matchAll(/[^\r\n]*(?:\r\n|\r|\n|$)/g)) {
    if (!match[0]) continue;
    const line = match[0].replace(/[\r\n]+$/, ""), offset = match.index;
    if (/^\s*```/.test(line)) {
      flush(); closeList();
      if (fence) output.push(`<pre><code>${span(source.slice(codeStart, offset),
        codeStart)}</code></pre>`);
      else codeStart = offset + match[0].length;
      fence = !fence; continue;
    }
    if (fence) continue;
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    const item = /^\s*(?:([-+*])|\d+\.)\s+(.+)$/.exec(line);
    if (heading) {
      flush(); closeList();
      const level = heading[1].length, start = offset + line.length - heading[2].length;
      output.push(`<h${level}>${inline(heading[2], start)}</h${level}>`);
    } else if (item) {
      flush();
      const type = item[1] ? "ul" : "ol";
      if (type !== list) { closeList(); list = type; output.push(`<${list}>`); }
      output.push(`<li>${inline(item[2], offset + line.length - item[2].length)}</li>`);
    } else if (!line.trim()) { flush(); closeList(); }
    else { closeList(); paragraph ??= offset; paragraphEnd = offset + line.length; }
  }
  flush(); closeList();
  if (fence) output.push(`<pre><code>${span(source.slice(codeStart), codeStart)}</code></pre>`);
  return output.join("\n");
}
