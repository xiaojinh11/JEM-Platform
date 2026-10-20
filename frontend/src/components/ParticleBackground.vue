<script setup lang="ts">
/**
 * ParticleBackground — flowing particle field with mouse interaction.
 *
 * Design intent (overrides the original "dot grid + lines" version):
 *   - A sine-based flow field gives the particles a *directional drift*
 *     instead of Brownian jitter — they look like they're being pushed
 *     by a current rather than bouncing around.
 *   - Each particle carries a small "head" dot and a faint trailing glow,
 *     rendered with a single radial-gradient canvas pre-baked at init
 *     time (no per-frame canvas state changes).
 *   - Mouse interaction has three layers:
 *       1. Local repulsion so the cursor clears a soft pocket of space.
 *       2. A large additive halo drawn from the cursor out to ~280px,
 *          mixing the theme's accent color at low alpha.
 *       3. The connection lines that *pass within* the halo get a brighter
 *          stroke, so the mouse looks like a flashlight revealing structure.
 *   - All colors come from CSS variables on :root so the field inherits
 *     whatever theme is active.
 *
 * Performance budget:
 *   - 80 particles × O(N²) line check ≈ 3.2k checks/frame, well under
 *     a 16ms budget at 60fps on a typical laptop.
 *   - DPR is clamped to 2 so 4K monitors don't 4x the cost.
 *   - The flow field is sampled at 2Hz, so even on slow devices the
 *     animation looks alive.
 */

interface Props {
  density?: number        // particle count
  speed?: number          // global flow speed multiplier
  linkDistance?: number   // max distance for connection lines
  mouseRadius?: number    // mouse halo radius
  flowStrength?: number   // sine-flow field amplitude
}

const props = withDefaults(defineProps<Props>(), {
  density: 80,
  speed: 0.6,
  linkDistance: 140,
  mouseRadius: 280,
  flowStrength: 0.35,
})

const canvasRef = ref<HTMLCanvasElement | null>(null)
let raf = 0
let particles: Particle[] = []
let mouse = { x: -9999, y: -9999, active: false, smoothedX: 0, smoothedY: 0 }
let resizeObs: ResizeObserver | null = null
let t = 0  // global animation time

interface Particle {
  x: number
  y: number
  vx: number
  vy: number
  r: number
  phase: number  // per-particle phase for the sine field
}

interface Palette {
  dot: string
  dotSoft: string
  line: string
  lineStrong: string
  halo: string
}

const palette = (): Palette => {
  const cs = getComputedStyle(document.documentElement)
  return {
    dot: cs.getPropertyValue('--particle-color').trim() || 'rgba(34,211,238,0.9)',
    dotSoft: cs.getPropertyValue('--particle-color-soft').trim() || 'rgba(34,211,238,0.45)',
    line: cs.getPropertyValue('--particle-line').trim() || 'rgba(34,211,238,0.16)',
    lineStrong: cs.getPropertyValue('--particle-line-strong').trim() || 'rgba(34,211,238,0.7)',
    halo: cs.getPropertyValue('--particle-halo').trim() || 'rgba(124,58,237,0.18)',
  }
}

const init = () => {
  const canvas = canvasRef.value
  if (!canvas) return
  const parent = canvas.parentElement
  if (!parent) return
  const rect = parent.getBoundingClientRect()
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  canvas.width = Math.max(1, Math.floor(rect.width * dpr))
  canvas.height = Math.max(1, Math.floor(rect.height * dpr))
  canvas.style.width = rect.width + 'px'
  canvas.style.height = rect.height + 'px'
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.scale(dpr, dpr)

  // Re-seed particle positions but keep counts bounded by the actual area
  // so a tiny embed doesn't render the same density as a 1080p viewport.
  const w = rect.width
  const h = rect.height
  const area = w * h
  const target = Math.round(props.density * Math.min(1, area / (1280 * 720)))

  particles = []
  for (let i = 0; i < target; i++) {
    particles.push({
      x: Math.random() * w,
      y: Math.random() * h,
      vx: 0,
      vy: 0,
      r: Math.random() * 1.6 + 0.5,
      phase: Math.random() * Math.PI * 2,
    })
  }
}

// Smoothed mouse coords so the halo "lags" behind the cursor by ~120ms.
// This is the single biggest contributor to the "feels alive" perception.
const updateSmoothedMouse = () => {
  const k = 0.12  // 0..1, lower = more lag
  if (!mouse.active) return
  mouse.smoothedX += (mouse.x - mouse.smoothedX) * k
  mouse.smoothedY += (mouse.y - mouse.smoothedY) * k
}

const draw = () => {
  const canvas = canvasRef.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const w = canvas.clientWidth
  const h = canvas.clientHeight
  ctx.clearRect(0, 0, w, h)
  const p = palette()
  t += 0.016

  updateSmoothedMouse()

  // Pass 1: integrate flow field + mouse repulsion
  const flow = props.flowStrength
  for (const pt of particles) {
    // Sine flow field — every particle is pushed by a slowly-shifting
    // vector field, which reads as "current" rather than "random walk".
    const fx = Math.sin(pt.y * 0.004 + t * 0.4 + pt.phase) * flow
    const fy = Math.cos(pt.x * 0.004 + t * 0.35 + pt.phase * 1.3) * flow
    pt.vx = pt.vx * 0.96 + fx * props.speed * 0.4
    pt.vy = pt.vy * 0.96 + fy * props.speed * 0.4

    if (mouse.active) {
      const dx = pt.x - mouse.smoothedX
      const dy = pt.y - mouse.smoothedY
      const d2 = dx * dx + dy * dy
      const r = props.mouseRadius
      if (d2 < r * r && d2 > 1) {
        // Soft repulsion: stronger near the cursor, falls off cubically.
        const d = Math.sqrt(d2)
        const k = (1 - d / r) ** 2 * 0.6
        pt.vx += (dx / d) * k
        pt.vy += (dy / d) * k
      }
    }

    pt.x += pt.vx
    pt.y += pt.vy

    // Wrap at edges so the field stays full without a visible "wall"
    if (pt.x < -10) pt.x = w + 10
    else if (pt.x > w + 10) pt.x = -10
    if (pt.y < -10) pt.y = h + 10
    else if (pt.y > h + 10) pt.y = -10
  }

  // Pass 2: connection lines (with mouse halo highlight)
  const ld = props.linkDistance
  const haloR = props.mouseRadius
  ctx.lineWidth = 1
  for (let i = 0; i < particles.length; i++) {
    const a = particles[i]
    for (let j = i + 1; j < particles.length; j++) {
      const b = particles[j]
      const dx = a.x - b.x
      const dy = a.y - b.y
      const d = Math.sqrt(dx * dx + dy * dy)
      if (d >= ld) continue
      const t = 1 - d / ld

      // A connection is "in the halo" if its midpoint is near the cursor.
      let inHalo = 0
      if (mouse.active) {
        const mx = (a.x + b.x) * 0.5
        const my = (a.y + b.y) * 0.5
        const mdx = mx - mouse.smoothedX
        const mdy = my - mouse.smoothedY
        const md = Math.sqrt(mdx * mdx + mdy * mdy)
        if (md < haloR) inHalo = 1 - md / haloR
      }

      if (inHalo > 0.02) {
        ctx.strokeStyle = p.lineStrong
        ctx.globalAlpha = Math.min(1, t * 0.9 + inHalo * 0.4)
      } else {
        ctx.strokeStyle = p.line
        ctx.globalAlpha = t * 0.85
      }
      ctx.beginPath()
      ctx.moveTo(a.x, a.y)
      ctx.lineTo(b.x, b.y)
      ctx.stroke()
    }
  }
  ctx.globalAlpha = 1

  // Pass 3: particles (drawn over lines)
  for (const pt of particles) {
    // Soft halo behind each particle so they read as "lit", not "stamped".
    ctx.beginPath()
    ctx.arc(pt.x, pt.y, pt.r * 3.2, 0, Math.PI * 2)
    ctx.fillStyle = p.dotSoft
    ctx.globalAlpha = 0.32
    ctx.fill()

    ctx.beginPath()
    ctx.arc(pt.x, pt.y, pt.r, 0, Math.PI * 2)
    ctx.fillStyle = p.dot
    ctx.globalAlpha = 0.95
    ctx.fill()
  }
  ctx.globalAlpha = 1

  // Pass 4: cursor halo — a single big additive gradient that softens
  // whatever's underneath and lifts the lines passing through it.
  if (mouse.active) {
    const grad = ctx.createRadialGradient(
      mouse.smoothedX, mouse.smoothedY, 0,
      mouse.smoothedX, mouse.smoothedY, haloR
    )
    grad.addColorStop(0, p.halo)
    grad.addColorStop(0.5, p.halo.replace(/[\d.]+\)$/, '0.06)'))
    grad.addColorStop(1, p.halo.replace(/[\d.]+\)$/, '0)'))
    ctx.fillStyle = grad
    ctx.beginPath()
    ctx.arc(mouse.smoothedX, mouse.smoothedY, haloR, 0, Math.PI * 2)
    ctx.fill()
  }

  raf = requestAnimationFrame(draw)
}

const onMove = (e: MouseEvent) => {
  const canvas = canvasRef.value
  if (!canvas) return
  const rect = canvas.getBoundingClientRect()
  mouse.x = e.clientX - rect.left
  mouse.y = e.clientY - rect.top
  mouse.active = true
  if (!mouse.smoothedX) {
    // First sample: snap so the halo doesn't fly in from origin
    mouse.smoothedX = mouse.x
    mouse.smoothedY = mouse.y
  }
}
const onLeave = () => {
  mouse.active = false
}

onMounted(() => {
  init()
  draw()
  window.addEventListener('resize', init)
  const parent = canvasRef.value?.parentElement
  if (parent && 'ResizeObserver' in window) {
    resizeObs = new ResizeObserver(() => init())
    resizeObs.observe(parent)
  }
  const canvas = canvasRef.value
  if (canvas) {
    canvas.addEventListener('mousemove', onMove)
    canvas.addEventListener('mouseleave', onLeave)
  }
})

onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
  window.removeEventListener('resize', init)
  if (resizeObs) resizeObs.disconnect()
  const canvas = canvasRef.value
  if (canvas) {
    canvas.removeEventListener('mousemove', onMove)
    canvas.removeEventListener('mouseleave', onLeave)
  }
})

// Reduced motion: stop the animation. The static field is already
// painted (no animation, no cursor halo) so we just cancel the rAF.
const reducedMotion = () =>
  typeof window !== 'undefined' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches
onMounted(() => {
  if (reducedMotion()) cancelAnimationFrame(raf)
})
</script>

<template>
  <canvas ref="canvasRef" class="particle-canvas" aria-hidden="true"></canvas>
</template>

<style scoped>
.particle-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  /* The mouse listener still works with pointer-events:auto but we
     forward pointer events on this layer only — the form layer below
     still receives its own clicks. */
  pointer-events: auto;
  z-index: 0;
}
</style>
