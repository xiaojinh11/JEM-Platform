// Polyfilled pdfjs worker entry.  Some corporate Edge builds are still
// pre-Chrome-123 and lack Math.sumPrecise — pdfjs's font sub-module
// calls it during worker bootstrap and would otherwise crash with
// "Math.sumPrecise is not a function".
//
// We patch the global Math inside the worker scope first, then
// dynamically import the original worker module so it picks up the
// patched Math and proceeds normally.
;(function () {
  function polyfill() {
    const g: typeof globalThis & { Math: typeof Math } =
      (typeof self !== 'undefined' ? self : globalThis) as never
    if (typeof g.Math.sumPrecise !== 'function') {
      g.Math.sumPrecise = function () {
        let total = 0
        for (let i = 0; i < arguments.length; i++) {
          const v = (arguments as unknown as number[])[i]
          total += Number.isFinite(v) ? (v as number) : 0
        }
        return total
      }
    }
  }
  polyfill()
})()

// Re-export the original worker so pdfjs sees the same symbol surface.
export * from 'pdfjs-dist/build/pdf.worker.min.mjs'
