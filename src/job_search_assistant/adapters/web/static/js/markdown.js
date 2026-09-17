// Deliberate Markdown subset. Every source fragment is escaped before adding markup.
export function escapeHTML(text) {
  return text.replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}
function inline(raw) {
  // Tokenize once, so generated tags/attributes are never fed back through replacements.
  const pattern = /`([^`]+)`|\[([^\]\n]+)\]\(([^\s)]+)\)|\*\*([^*]+)\*\*|\*([^*]+)\*/g;
  let result = "", offset = 0;
  for (const match of raw.matchAll(pattern)) {
    result += escapeHTML(raw.slice(offset, match.index));
    const [, code, label, href, strong, emphasis] = match;
    if (code !== undefined) result += `<code>${escapeHTML(code)}</code>`;
    else if (label !== undefined) {
      let safe = false;
      try {
        const url = new URL(href);
        safe = /^(https?:\/\/|mailto:)/i.test(href) &&
          ["http:", "https:", "mailto:"].includes(url.protocol);
      } catch { /* An invalid or relative URL is displayed as plain text. */ }
      result += safe ? `<a href="${escapeHTML(href)}" rel="noreferrer noopener">` +
        `${escapeHTML(label)}</a>` : escapeHTML(match[0]);
    } else if (strong !== undefined) result += `<strong>${escapeHTML(strong)}</strong>`;
    else result += `<em>${escapeHTML(emphasis)}</em>`;
    offset = match.index + match[0].length;
  }
  return result + escapeHTML(raw.slice(offset));
}
export function renderMarkdown(source) {
  const output = [], paragraph = [], code = [];
  let fence = false, list = null;
  const flushParagraph = () => {
    if (paragraph.length) output.push(`<p>${inline(paragraph.splice(0).join("\n"))}</p>`);
  };
  const closeList = () => { if (list) { output.push(`</${list}>`); list = null; } };
  for (const line of source.replace(/\r\n?/g, "\n").split("\n")) {
    if (/^\s*```/.test(line)) {
      flushParagraph(); closeList();
      if (fence) output.push(`<pre><code>${escapeHTML(code.splice(0).join("\n"))}</code></pre>`);
      fence = !fence;
      continue;
    }
    if (fence) { code.push(line); continue; }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    const item = /^\s*(?:([-+*])|\d+\.)\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph(); closeList();
      const level = heading[1].length;
      output.push(`<h${level}>${inline(heading[2])}</h${level}>`);
    } else if (item) {
      flushParagraph();
      const type = item[1] ? "ul" : "ol";
      if (type !== list) { closeList(); list = type; output.push(`<${list}>`); }
      output.push(`<li>${inline(item[2])}</li>`);
    } else if (!line.trim()) { flushParagraph(); closeList(); }
    else { closeList(); paragraph.push(line); }
  }
  flushParagraph(); closeList();
  if (fence) output.push(`<pre><code>${escapeHTML(code.join("\n"))}</code></pre>`);
  return output.join("\n");
}
