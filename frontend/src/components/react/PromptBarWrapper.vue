<script lang="ts">
/**
 * Vue 3 wrapper for the React PromptBar component.
 *
 * Strategy: use ReactDOM.createRoot imperatively so that:
 *   - Vue owns the DOM node (via a <div ref="container"> in the template)
 *   - React owns the component tree inside that node
 *   - Vue ←→ React communication goes through plain callbacks
 */
import { defineComponent, h, onBeforeUnmount, onMounted, ref } from 'vue';
import { createRoot } from 'react-dom/client';
import type React from 'react';
import PromptBar from './PromptBar';

interface PromptBarSource {
  key: string;
  name: string;
  description?: string;
  icon?: unknown;
  attach?: boolean;
}
interface PromptBarCommand {
  key: string;
  name: string;
  description?: string;
}

export default defineComponent({
  name: 'PromptBarWrapper',
  props: {
    placeholder: { type: String, default: 'Ask anything' },
    sources: {
      type: Array as () => PromptBarSource[],
      default: () => [],
    },
    commands: {
      type: Array as () => PromptBarCommand[],
      default: () => [],
    },
    busy: { type: Boolean, default: false },
  },
  emits: ['send', 'stop', 'attach'],
  setup(props, { emit }) {
    const container = ref<HTMLDivElement | null>(null);
    let reactRoot: ReturnType<typeof createRoot> | null = null;

    onMounted(() => {
      if (!container.value) return;

      // Dynamically import React so the Vue bundle only pays the cost when
      // this component is actually mounted.
      Promise.all([
        import('react'),
        import('react-dom/client'),
      ]).then(([React, { createRoot: mkRoot }]) => {
        if (!container.value) return;
        reactRoot = mkRoot(container.value);

        const onSend = (_text: string, meta: { attachments: string[]; model?: unknown; effort?: string }) => {
          emit('send', { text: _text, ...meta });
        };

        reactRoot!.render(
          React.createElement(PromptBar, {
            placeholder: props.placeholder,
            sources: props.sources,
            commands: props.commands,
            busy: props.busy,
            onSend,
            onStop: () => emit('stop'),
            onAttach: () => {
              emit('attach');
              return []; // file chips managed by Vue parent via send event
            },
          }),
        );
      });
    });

    onBeforeUnmount(() => {
      // Give React time to clean up subscriptions before we blow away the DOM.
      setTimeout(() => {
        reactRoot?.unmount();
        reactRoot = null;
      }, 0);
    });

    // Render the div that React will mount into. We avoid ``display: contents``
    // because some browsers (notably older Safari) drop pointer events on
    // children of a ``display:contents`` element when the parent re-flows,
    // which would break the PromptBar's outside-click-to-close logic.
    return () => h('div', { ref: container, class: 'prompt-bar-host' });
  },
});
</script>
