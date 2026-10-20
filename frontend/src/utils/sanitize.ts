import DOMPurify from 'dompurify'

/**
 * Strict allowlist used for rendered document markdown.
 *
 * Why this is restrictive
 * ------------------------
 * `marked` preserves raw inline HTML in source markdown, including
 * `<script>`, `<svg onload=...>`, `<a href="javascript:...">`, and event
 * handlers. Without a whitelist the document preview becomes a stored-XSS
 * vector: an operator uploads a malicious .md file and any admin/operator
 * who opens it has their JWT stolen from localStorage. The audit
 * identified this as a critical issue.
 *
 * `USE_PROFILES.html` is the safest starting point, but we additionally:
 *   - strip every element that can execute code or embed remote frames
 *   - strip `style` attributes (CSS keylogging is a real exfil vector)
 *   - restrict URIs to http(s)/mailto only — `javascript:`, `data:`,
 *     `vbscript:`, and `file:` are all rejected via the explicit
 *     `ALLOWED_URI_REGEXP`. The previous regex allowed any non-letter
 *     character or any scheme followed by a non-letter as "safe", which
 *     let `javascript:alert(1)` through because `:` was treated as the
 *     scheme terminator. The new regex matches the URI prefix only.
 */
const ALLOWED_TAGS = [
  'a', 'abbr', 'b', 'blockquote', 'br', 'code', 'del', 'div', 'dd', 'dl',
  'dt', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'ins',
  'kbd', 'li', 'mark', 'ol', 'p', 'pre', 'q', 's', 'small', 'span', 'strong',
  'sub', 'sup', 'table', 'tbody', 'td', 'th', 'thead', 'tr', 'u', 'ul',
]

/**
 * Permit only http(s) and mailto URIs. Anything else (javascript:, data:,
 * vbscript:, file:, ftp:, …) is stripped.
 *
 * Note: ``marked`` adds target/rel attributes to ``<a>`` automatically;
 * those are kept by the explicit allowlist below. ``href`` is constrained
 * to the same scheme list so a manual ``<a href="javascript:...">`` in
 * source markdown is neutralised.
 */
const SAFE_URI_REGEXP = /^(?:https?:|mailto:)/i

export function sanitizeMarkdownHtml(html: string): string {
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR: ['href', 'title', 'alt', 'src', 'target', 'rel', 'lang'],
    ALLOWED_URI_REGEXP: SAFE_URI_REGEXP,
    FORBID_TAGS: [
      'form', 'input', 'button', 'iframe', 'object', 'embed',
      'script', 'style', 'svg', 'math', 'meta', 'link', 'base',
      'frame', 'frameset', 'noframes', 'noscript', 'video', 'audio',
      'source', 'track',
    ],
    FORBID_ATTR: [
      'style', 'onload', 'onerror', 'onclick', 'onmouseover', 'onfocus',
      'onblur', 'onkeydown', 'onkeyup', 'onkeypress', 'onsubmit',
      'onchange', 'oninput', 'onanimationend', 'onanimationstart',
    ],
    USE_PROFILES: { html: true },
  })
}

/**
 * Render markdown snippets (search hits, summaries) as plain text only.
 *
 * The audit noted that search snippets are already bound to ``<pre>`` in
 * the UI so HTML escaping happens automatically, but if any caller ever
 * routes them through ``v-html`` the stored-XSS risk returns. Use this
 * helper to make the intent explicit and belt-and-braces the safety.
 */
export function escapePlainText(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}
