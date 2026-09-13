const ESCAPE = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ESCAPE[ch])
}

function inline(text) {
  return text
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
}

/**
 * Render the small Markdown subset the agents actually emit: headings, bullet
 * and numbered lists, bold, italic and inline code.
 *
 * The input is HTML-escaped first, so any markup in the model's output is inert
 * and only the tags produced below reach the DOM. That keeps v-html safe here
 * without pulling in a parser and a sanitizer.
 */
export function renderMarkdown(text) {
  const lines = escapeHtml(text).split('\n')
  const html = []
  let list = null

  const closeList = () => {
    if (list) {
      html.push(`</${list}>`)
      list = null
    }
  }

  for (const raw of lines) {
    const line = raw.trim()
    if (!line) {
      closeList()
      continue
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/)
    const bullet = line.match(/^[-*]\s+(.*)$/)
    const numbered = line.match(/^\d+\.\s+(.*)$/)

    if (heading) {
      closeList()
      html.push(`<h4>${inline(heading[2])}</h4>`)
    } else if (bullet) {
      if (list !== 'ul') {
        closeList()
        html.push('<ul>')
        list = 'ul'
      }
      html.push(`<li>${inline(bullet[1])}</li>`)
    } else if (numbered) {
      if (list !== 'ol') {
        closeList()
        html.push('<ol>')
        list = 'ol'
      }
      html.push(`<li>${inline(numbered[1])}</li>`)
    } else {
      closeList()
      html.push(`<p>${inline(line)}</p>`)
    }
  }

  closeList()
  return html.join('')
}
