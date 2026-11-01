import DOMPurify from 'dompurify';
import { marked } from 'marked';
import type { AskResponse } from '../types';

export function citationHtml(response: AskResponse): string {
  const html = DOMPurify.sanitize(marked.parse(response.answer) as string);
  if (response.runtime_mode !== 'react_v2') return html;
  const root = document.createElement('div');
  root.innerHTML = html;
  // All answer links are server citation metadata, never model-specified URLs.
  root.querySelectorAll('a').forEach(a => a.replaceWith(...a.childNodes));
  root.querySelectorAll('img').forEach(img => img.remove());
  const citations = new Map((response.citation_metadata || []).map(c => [c.number, c]));
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  while (walker.nextNode()) nodes.push(walker.currentNode as Text);
  for (const node of nodes) {
    const fragment = document.createDocumentFragment();
    let offset = 0;
    for (const match of node.data.matchAll(/\[(\d+)\]/g)) {
      const citation = citations.get(Number(match[1]));
      if (!citation) continue;
      fragment.append(node.data.slice(offset, match.index));
      const link = document.createElement('a');
      link.href = `#citation-${citation.number}`;
      link.textContent = match[0];
      link.title = `${citation.title}\n${citation.page_numbers.length ? `页码：${citation.page_numbers.join(', ')}\n` : ''}${citation.excerpt}`;
      fragment.append(link);
      offset = match.index! + match[0].length;
    }
    if (offset) {
      fragment.append(node.data.slice(offset));
      node.replaceWith(fragment);
    }
  }
  return DOMPurify.sanitize(root.innerHTML);
}
