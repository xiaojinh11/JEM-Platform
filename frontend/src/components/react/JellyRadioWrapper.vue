<script lang="ts">
/**
 * Vue 3 wrapper for the React JellyRadio component.
 *
 * Strategy mirrors PromptBarWrapper: Vue owns the DOM node (via a <div ref>),
 * React owns the component tree inside.
 *
 * Colour handling:
 *   The wrapper used to ship a hard-coded PALETTES table per theme. That made
 *   the control feel "imported" — its greys never matched the rest of the
 *   app. Now we read the live CSS variables off `:root` (or
 *   `:root[data-theme="…"]`) and forward them as the four colour props. The
 *   result is that JellyRadio inherits whatever surface tones the host theme
 *   defines, with zero duplication, and a theme change re-renders the React
 *   tree so the new values land immediately.
 */
import { defineComponent, h, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { createRoot, type Root } from 'react-dom/client';
import type React from 'react';
import JellyRadio from './JellyRadio';
import { useThemeStore } from '@/stores/theme';

type Size = 'sm' | 'md' | 'lg';

interface JellyItem {
  value: string;
  label?: string;
  icon?: unknown;
  disabled?: boolean;
}

/**
 * Read a CSS custom property from `:root`, falling back to a default if
 * the variable is missing (e.g. during SSR or before theme.css loads).
 *
 * We deliberately read from `document.documentElement` rather than the
 * wrapper's own element so we get the resolved theme value, not whatever
 * the wrapper's scoped style has set.
 */
function readVar(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

interface ThemeColours {
  chip: string;
  active: string;
  text: string;
  activeText: string;
  accent: string;
}

const COLOUR_FALLBACK: ThemeColours = {
  chip: '#27272a',
  active: '#f5f5f5',
  text: '#f5f5f5',
  activeText: '#18181b',
  accent: '#22d3ee',
};

function readThemeColours(): ThemeColours {
  // For the segmented control, "chip" surface and "active" surface both need
  // to read as *elevated* surfaces — we want a clear visual lift between
  // inactive and active chips. The host theme exposes --bg-elev (slightly
  // raised panel tone) and --bg-canvas (highest surface), which give us that
  // lift naturally without inventing new colours.
  const muted = readVar('--text-3', COLOUR_FALLBACK.text);
  const strong = readVar('--text-1', COLOUR_FALLBACK.activeText);
  return {
    chip: 'transparent', // the *container* paints the chip surface, see CSS
    active: readVar('--bg-canvas', COLOUR_FALLBACK.active),
    text: muted,
    activeText: strong,
    accent: readVar('--accent', COLOUR_FALLBACK.accent),
  };
}

export default defineComponent({
  name: 'JellyRadioWrapper',
  props: {
    items: {
      type: Array as () => (string | JellyItem)[],
      default: () => [],
    },
    modelValue: { type: String, default: undefined },
    defaultValue: { type: String, default: undefined },
    size: { type: String as () => Size, default: 'md' },
    gap: { type: Number, default: 8 },
    radius: { type: Number, default: 18 },
    swell: { type: Number, default: 0.2 },
    barge: { type: Number, default: 6 },
    shrink: { type: Number, default: 0.05 },
    jelly: { type: Number, default: 1 },
    bounce: { type: Number, default: 0.25 },
    stagger: { type: Number, default: 22 },
    stiffness: { type: Number, default: 580 },
    disabled: { type: Boolean, default: false },
    ariaLabel: { type: String, default: 'Options' },
    className: { type: String, default: '' },
  },
  emits: ['update:modelValue', 'change'],
  setup(props, { emit }) {
    const container = ref<HTMLDivElement | null>(null);
    let reactRoot: Root | null = null;
    // Force re-render of the React tree whenever these change. We don't try to
    // mutate the React props in place because some (e.g. items, value) need
    // React's reconciliation to pick them up reliably.
    let renderTick = 0;

    function render(React: typeof import('react'), ticks: number) {
      if (!container.value || !reactRoot) return;
      const colours = readThemeColours();
      // Capture-tick guards against stale renders if the user unmounts quickly.
      const live = ticks;

      reactRoot.render(
        React.createElement(JellyRadio, {
          items: props.items as unknown[],
          value: props.modelValue,
          defaultValue: props.defaultValue,
          size: props.size,
          gap: props.gap,
          radius: props.radius,
          swell: props.swell,
          barge: props.barge,
          shrink: props.shrink,
          jelly: props.jelly,
          bounce: props.bounce,
          stagger: props.stagger,
          stiffness: props.stiffness,
          disabled: props.disabled,
          ariaLabel: props.ariaLabel,
          className: props.className,
          chipColor: colours.chip,
          activeColor: colours.active,
          textColor: colours.text,
          activeTextColor: colours.activeText,
          onChange: (value: string, index: number) => {
            emit('update:modelValue', value);
            emit('change', value, index);
          },
          // Tag for the live tick — unused by the React side, but proves the
          // render is current if we ever need to debug.
          key: live,
        } as React.ComponentProps<typeof JellyRadio>),
      );
    }

    onMounted(() => {
      if (!container.value) return;
      Promise.all([import('react'), import('react-dom/client')]).then(
        ([ReactMod, ReactDOMMod]) => {
          if (!container.value) return;
          reactRoot = ReactDOMMod.createRoot(container.value);
          renderTick += 1;
          render(ReactMod.default ?? (ReactMod as unknown as typeof React), renderTick);
        },
      );
    });

    onBeforeUnmount(() => {
      setTimeout(() => {
        reactRoot?.unmount();
        reactRoot = null;
      }, 0);
    });

    // Re-render the React tree when any prop or the theme changes.
    watch(
      () => [
        props.items,
        props.modelValue,
        props.defaultValue,
        props.size,
        props.gap,
        props.radius,
        props.swell,
        props.barge,
        props.shrink,
        props.jelly,
        props.bounce,
        props.stagger,
        props.stiffness,
        props.disabled,
        props.ariaLabel,
        props.className,
        useThemeStore().theme,
      ],
      () => {
        renderTick += 1;
        import('react').then((ReactMod) => {
          render(ReactMod.default ?? (ReactMod as unknown as typeof React), renderTick);
        });
      },
      { deep: true },
    );

    return () => h('div', { ref: container, class: 'jelly-radio-host' });
  },
});
</script>
