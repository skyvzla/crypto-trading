<script setup lang="ts">
defineProps<{
  id: string
  kicker?: string
  title: string
  description?: string
  note?: string
}>()
</script>

<template>
  <header class="section-heading">
    <div class="section-title-group">
      <h2 :id="id">{{ title }}</h2>
      <p v-if="description" class="section-description">{{ description }}</p>
    </div>
    <div v-if="note || $slots.actions" class="section-actions">
      <span v-if="note" class="section-note">{{ note }}</span>
      <slot name="actions" />
    </div>
  </header>
</template>

<style scoped lang="scss">
.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}
.section-title-group {
  min-width: 0;
}
.section-heading h2 {
  margin: 0;
  font-size: var(--type-section-title);
  line-height: 1.3;
  letter-spacing: 0;
}
.section-description {
  margin: 3px 0 0;
  color: var(--muted);
  font-size: var(--type-meta);
}
.section-note {
  color: var(--muted);
  font-size: var(--type-meta);
}
.section-actions {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  gap: 8px;
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
