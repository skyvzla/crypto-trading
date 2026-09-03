<script setup lang="ts">
defineProps<{
  id: string
  kicker: string
  title: string
  description?: string
  note?: string
}>()
</script>

<template>
  <header class="section-heading">
    <div>
      <span class="section-kicker">{{ kicker }}</span>
      <h2 :id="id">{{ title }}</h2>
    </div>
    <div v-if="note || $slots.actions" class="section-actions">
      <span v-if="note" class="section-note">{{ note }}</span>
      <slot name="actions" />
    </div>
  </header>
  <p v-if="description" class="section-description">{{ description }}</p>
</template>

<style scoped lang="scss">
.section-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 10px;
}
.section-heading h2 {
  margin: 4px 0 0;
  font-size: var(--type-page-title);
  line-height: 1.25;
  letter-spacing: 0;
}
.section-kicker {
  display: block;
  color: var(--color-gold);
  font: var(--type-meta)/1.3 var(--font-family-mono);
  letter-spacing: 0;
}
.section-note {
  color: var(--muted);
  font-size: var(--type-meta);
}
.section-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-description {
  margin: 0 0 14px;
  color: var(--muted);
  font-size: var(--type-secondary);
}

@media (max-width: 900px) {
  .section-heading {
    align-items: flex-start;
    flex-direction: column;
  }
  .section-actions {
    width: 100%;
  }
  .section-actions :deep(.ant-btn) {
    flex: 1;
  }
}

@media (max-width: 600px) {
  .section-note {
    display: none;
  }
}
</style>
