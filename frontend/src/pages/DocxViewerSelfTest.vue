<script setup lang="ts">
import DocxViewer from '@/components/DocxViewer.vue'
</script>

<template>
  <div style="padding: 20px; max-width: 900px; margin: 0 auto; background: #1a1a1a; color: #fff;">
    <h2 style="color: #fff; margin-bottom: 20px;">DocxViewer 自检页</h2>
    <p style="color: #aaa; margin-bottom: 16px;">
      直接挂载 DocxViewer，绕开 DocumentsPage 的所有逻辑。
      如果这个能看到 PDF 内容，说明 DocxViewer 本身没问题，是 DocumentsPage 那边有 bug。
    </p>
    <DocxViewer
      :doc-id="10"
      ext="pdf"
      title="V08S02C017-_VPN_-企业VPN技术.pdf"
      filename="V08S02C017-_VPN_-企业VPN技术.pdf"
    />
  </div>
</template>
