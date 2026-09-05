<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Plus, Trash2 } from 'lucide-vue-next'
import type { ChartIndicatorLineSetting, ChartIndicatorSettings } from '@/api/types'
import { CHART_INTERVALS } from '@/shared/chartIntervals'
import {
  CHART_INDICATORS,
  cloneChartIndicatorSettings,
  indicatorEnabled,
  setIndicatorEnabled,
  type ChartIndicatorDefinition,
  type ChartIndicatorKey,
} from './chartIndicatorSettings'

const props = defineProps<{
  open: boolean
  settings: ChartIndicatorSettings
  saving?: boolean
}>()

const emit = defineEmits<{
  'update:open': [open: boolean]
  save: [settings: ChartIndicatorSettings]
}>()

const selectedKey = ref<ChartIndicatorKey>('volume')
const draft = ref(cloneChartIndicatorSettings(props.settings))

const mainIndicators = CHART_INDICATORS.filter((item) => item.group === 'main')
const subIndicators = CHART_INDICATORS.filter((item) => item.group === 'sub')
const selectedDefinition = computed(
  () => CHART_INDICATORS.find((item) => item.key === selectedKey.value) ?? CHART_INDICATORS[0],
)

watch(
  () => props.open,
  (open) => {
    if (open) draft.value = cloneChartIndicatorSettings(props.settings)
  },
)

watch(
  () => props.settings,
  (settings) => {
    if (!props.open) draft.value = cloneChartIndicatorSettings(settings)
  },
  { deep: true },
)

function selectIndicator(definition: ChartIndicatorDefinition) {
  selectedKey.value = definition.key
}

function toggleIndicator(definition: ChartIndicatorDefinition) {
  setIndicatorEnabled(draft.value, definition, !indicatorEnabled(draft.value, definition))
  selectedKey.value = definition.key
}

function addLine(lines: ChartIndicatorLineSetting[], fallbackPeriod: number) {
  if (lines.length >= 8) return
  const lastPeriod = lines.at(-1)?.period ?? fallbackPeriod
  const palette = ['#f5c451', '#4da3ff', '#d98bff', '#22c55e', '#ef4444', '#14b8a6', '#f97316', '#64748b']
  lines.push({ period: Math.min(500, lastPeriod + fallbackPeriod), color: palette[lines.length % palette.length] })
}

function removeLine(lines: ChartIndicatorLineSetting[], index: number) {
  if (lines.length <= 1) return
  lines.splice(index, 1)
}

function save() {
  emit('save', cloneChartIndicatorSettings(draft.value))
}
</script>

<template>
  <a-modal
    :open="open"
    title="图表设置"
    width="900px"
    :confirm-loading="saving"
    ok-text="保存设置"
    cancel-text="取消"
    @update:open="emit('update:open', $event)"
    @ok="save"
  >
    <div class="default-interval-setting">
      <label for="default-chart-interval">默认周期</label>
      <a-select
        id="default-chart-interval"
        v-model:value="draft.default_interval"
        class="default-interval-select"
        size="small"
      >
        <a-select-option v-for="item in CHART_INTERVALS" :key="item" :value="item">{{ item }}</a-select-option>
      </a-select>
    </div>
    <div class="indicator-settings-layout">
      <nav class="indicator-list" aria-label="技术指标列表">
        <h3>主图指标</h3>
        <div
          v-for="definition in mainIndicators"
          :key="definition.key"
          class="indicator-list-item"
          :class="{ 'is-selected': selectedKey === definition.key }"
          role="button"
          tabindex="0"
          @click="selectIndicator(definition)"
          @keydown.enter="selectIndicator(definition)"
        >
          <a-checkbox
            :checked="indicatorEnabled(draft, definition)"
            :aria-label="`显示 ${definition.name}`"
            @click.stop
            @change="toggleIndicator(definition)"
          />
          <span>
            <strong>{{ definition.name }}</strong>
            <small>{{ definition.description }}</small>
          </span>
        </div>

        <h3>副图指标</h3>
        <div
          v-for="definition in subIndicators"
          :key="definition.key"
          class="indicator-list-item"
          :class="{ 'is-selected': selectedKey === definition.key }"
          role="button"
          tabindex="0"
          @click="selectIndicator(definition)"
          @keydown.enter="selectIndicator(definition)"
        >
          <a-checkbox
            :checked="indicatorEnabled(draft, definition)"
            :aria-label="`显示 ${definition.name}`"
            @click.stop
            @change="toggleIndicator(definition)"
          />
          <span>
            <strong>{{ definition.name }}</strong>
            <small>{{ definition.description }}</small>
          </span>
        </div>
      </nav>

      <section class="indicator-editor" :aria-label="`${selectedDefinition.name} 参数`">
        <header>
          <div>
            <h3>{{ selectedDefinition.name }}</h3>
            <p>{{ selectedDefinition.description }}</p>
          </div>
          <a-switch
            :checked="indicatorEnabled(draft, selectedDefinition)"
            checked-children="显示"
            un-checked-children="隐藏"
            @change="toggleIndicator(selectedDefinition)"
          />
        </header>

        <template v-if="selectedKey === 'ema' || selectedKey === 'ma'">
          <div class="settings-table-header">
            <span>周期</span>
            <span>线条颜色</span>
            <span class="action-column">操作</span>
          </div>
          <div
            v-for="(line, index) in selectedKey === 'ema' ? draft.main.ema.lines : draft.main.ma.lines"
            :key="index"
            class="settings-table-row"
          >
            <a-input-number v-model:value="line.period" :min="1" :max="500" :precision="0" />
            <label class="color-control">
              <input v-model="line.color" type="color" :aria-label="`第 ${index + 1} 条线颜色`" />
              <code>{{ line.color }}</code>
            </label>
            <a-tooltip title="删除周期">
              <a-button
                type="text"
                shape="circle"
                :disabled="(selectedKey === 'ema' ? draft.main.ema.lines : draft.main.ma.lines).length <= 1"
                :aria-label="`删除第 ${index + 1} 个周期`"
                @click="removeLine(selectedKey === 'ema' ? draft.main.ema.lines : draft.main.ma.lines, index)"
              >
                <template #icon>
                  <Trash2 :size="16" />
                </template>
              </a-button>
            </a-tooltip>
          </div>
          <a-button
            type="dashed"
            :disabled="(selectedKey === 'ema' ? draft.main.ema.lines : draft.main.ma.lines).length >= 8"
            @click="addLine(selectedKey === 'ema' ? draft.main.ema.lines : draft.main.ma.lines, 5)"
          >
            <template #icon>
              <Plus :size="16" />
            </template>
            增加周期
          </a-button>
        </template>

        <template v-else-if="selectedKey === 'boll'">
          <div class="number-settings-grid">
            <label>
              <span>计算周期</span>
              <a-input-number v-model:value="draft.main.boll.period" :min="2" :max="500" :precision="0" />
            </label>
            <label>
              <span>标准差倍数</span>
              <a-input-number v-model:value="draft.main.boll.deviation" :min="0.1" :max="10" :step="0.1" />
            </label>
          </div>
          <div class="named-colors">
            <label v-for="name in ['upper', 'middle', 'lower'] as const" :key="name" class="color-control">
              <span>{{ { upper: '通道边界', middle: '中轨', lower: '通道填充' }[name] }}</span>
              <input
                v-model="draft.main.boll.colors[name]"
                type="color"
                :aria-label="`BOLL ${{ upper: '通道边界', middle: '中轨', lower: '通道填充' }[name]}颜色`"
              />
              <code>{{ draft.main.boll.colors[name] }}</code>
            </label>
          </div>
        </template>

        <template v-else-if="selectedKey === 'volume'">
          <div class="settings-table-header">
            <span>均量周期</span>
            <span>线条颜色</span>
            <span class="action-column">操作</span>
          </div>
          <div v-for="(line, index) in draft.sub.volume.ma_lines" :key="index" class="settings-table-row">
            <a-input-number v-model:value="line.period" :min="1" :max="500" :precision="0" />
            <label class="color-control">
              <input v-model="line.color" type="color" :aria-label="`第 ${index + 1} 条均量线颜色`" />
              <code>{{ line.color }}</code>
            </label>
            <a-tooltip title="删除均量周期">
              <a-button
                type="text"
                shape="circle"
                :disabled="draft.sub.volume.ma_lines.length <= 1"
                :aria-label="`删除第 ${index + 1} 个均量周期`"
                @click="removeLine(draft.sub.volume.ma_lines, index)"
              >
                <template #icon>
                  <Trash2 :size="16" />
                </template>
              </a-button>
            </a-tooltip>
          </div>
          <a-button
            type="dashed"
            :disabled="draft.sub.volume.ma_lines.length >= 8"
            @click="addLine(draft.sub.volume.ma_lines, 5)"
          >
            <template #icon>
              <Plus :size="16" />
            </template>
            增加均量线
          </a-button>
        </template>

        <template v-else-if="selectedKey === 'macd'">
          <div class="number-settings-grid three-columns">
            <label>
              <span>快线周期</span>
              <a-input-number v-model:value="draft.sub.macd.fast_period" :min="1" :max="499" :precision="0" />
            </label>
            <label>
              <span>慢线周期</span>
              <a-input-number v-model:value="draft.sub.macd.slow_period" :min="2" :max="500" :precision="0" />
            </label>
            <label>
              <span>信号周期</span>
              <a-input-number v-model:value="draft.sub.macd.signal_period" :min="1" :max="500" :precision="0" />
            </label>
          </div>
          <div class="named-colors">
            <label
              v-for="name in ['dif', 'dea', 'histogram_up', 'histogram_down'] as const"
              :key="name"
              class="color-control"
            >
              <span>{{ { dif: 'DIF', dea: 'DEA', histogram_up: '正柱', histogram_down: '负柱' }[name] }}</span>
              <input v-model="draft.sub.macd.colors[name]" type="color" :aria-label="`MACD ${name} 颜色`" />
              <code>{{ draft.sub.macd.colors[name] }}</code>
            </label>
          </div>
        </template>

        <template v-else-if="selectedKey === 'kdj'">
          <div class="number-settings-grid">
            <label>
              <span>计算周期</span>
              <a-input-number v-model:value="draft.sub.kdj.period" :min="1" :max="500" :precision="0" />
            </label>
          </div>
          <div class="named-colors">
            <label v-for="name in ['k', 'd', 'j'] as const" :key="name" class="color-control">
              <span>{{ name.toUpperCase() }} 线</span>
              <input v-model="draft.sub.kdj.colors[name]" type="color" :aria-label="`KDJ ${name} 颜色`" />
              <code>{{ draft.sub.kdj.colors[name] }}</code>
            </label>
          </div>
        </template>

        <template v-else-if="selectedKey === 'rsi'">
          <div class="settings-table-header">
            <span>周期</span>
            <span>线条颜色</span>
            <span class="action-column">操作</span>
          </div>
          <div v-for="(line, index) in draft.sub.rsi.lines" :key="index" class="settings-table-row">
            <a-input-number v-model:value="line.period" :min="1" :max="500" :precision="0" />
            <label class="color-control">
              <input v-model="line.color" type="color" :aria-label="`第 ${index + 1} 条 RSI 线颜色`" />
              <code>{{ line.color }}</code>
            </label>
            <a-tooltip title="删除 RSI 周期">
              <a-button
                type="text"
                shape="circle"
                :disabled="draft.sub.rsi.lines.length <= 1"
                :aria-label="`删除第 ${index + 1} 个 RSI 周期`"
                @click="removeLine(draft.sub.rsi.lines, index)"
              >
                <template #icon>
                  <Trash2 :size="16" />
                </template>
              </a-button>
            </a-tooltip>
          </div>
          <a-button type="dashed" :disabled="draft.sub.rsi.lines.length >= 8" @click="addLine(draft.sub.rsi.lines, 6)">
            <template #icon>
              <Plus :size="16" />
            </template>
            增加 RSI 周期
          </a-button>
        </template>

        <template v-else-if="selectedKey === 'atr'">
          <div class="number-settings-grid">
            <label>
              <span>计算周期</span>
              <a-input-number v-model:value="draft.sub.atr.period" :min="1" :max="500" :precision="0" />
            </label>
            <label>
              <span>线条颜色</span>
              <span class="color-control">
                <input v-model="draft.sub.atr.color" type="color" aria-label="ATR 线颜色" />
                <code>{{ draft.sub.atr.color }}</code>
              </span>
            </label>
          </div>
        </template>
      </section>
    </div>
  </a-modal>
</template>

<style scoped lang="scss">
.default-interval-setting {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  color: var(--text);
  font-size: var(--type-secondary);
}

.default-interval-select {
  width: 136px;
}

.indicator-settings-layout {
  display: grid;
  grid-template-columns: minmax(210px, 0.7fr) minmax(0, 1.6fr);
  min-height: 470px;
  border: 1px solid var(--line);
  border-radius: 6px;
  overflow: hidden;
}

.indicator-list {
  padding: 12px 8px;
  border-right: 1px solid var(--line);
  background: var(--surface-hover);

  h3 {
    margin: 6px 10px 7px;
    color: var(--muted);
    font-size: var(--type-meta);
    font-weight: 600;
  }
}

.indicator-list-item {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 8px 10px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;

  &:hover,
  &.is-selected {
    background: var(--surface-hover);
  }

  &.is-selected {
    box-shadow: inset 3px 0 var(--color-primary);
  }

  > span:last-child {
    min-width: 0;
  }

  strong,
  small {
    display: block;
  }

  strong {
    font-size: var(--type-secondary);
    font-weight: 600;
  }

  small {
    margin-top: 2px;
    color: var(--muted);
    font-size: var(--type-meta);
  }
}

.indicator-editor {
  min-width: 0;
  padding: 18px;

  > header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 20px;
    padding-bottom: 13px;
    border-bottom: 1px solid var(--line);
  }

  h3,
  p {
    margin: 0;
  }

  h3 {
    font-size: var(--type-primary);
  }

  p {
    margin-top: 3px;
    color: var(--muted);
    font-size: var(--type-meta);
  }
}

.settings-table-header,
.settings-table-row {
  display: grid;
  grid-template-columns: minmax(100px, 0.7fr) minmax(180px, 1.3fr) 48px;
  align-items: center;
  gap: 12px;
}

.settings-table-header {
  margin-bottom: 5px;
  color: var(--muted);
  font-size: var(--type-meta);
}

.settings-table-row {
  min-height: 48px;
  border-top: 1px solid var(--line);
}

.action-column {
  text-align: center;
}

.color-control {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  min-width: 0;

  input[type='color'] {
    width: 38px;
    height: 30px;
    padding: 2px;
    border: 1px solid var(--line);
    border-radius: 4px;
    background: var(--surface);
    cursor: pointer;
  }

  code {
    overflow: hidden;
    color: var(--muted);
    font: var(--type-code)/1.35 var(--font-family-mono);
    text-overflow: ellipsis;
  }
}

.number-settings-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;

  &.three-columns {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  > label {
    display: grid;
    gap: 6px;
    color: var(--muted);
    font-size: var(--type-meta);
  }
}

.named-colors {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 18px;

  > label > span:first-child {
    min-width: 48px;
    color: var(--text);
    font-size: var(--type-secondary);
  }
}

@media (max-width: 720px) {
  .indicator-settings-layout {
    grid-template-columns: 1fr;
  }

  .indicator-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    border-right: 0;
    border-bottom: 1px solid var(--line);

    h3 {
      grid-column: 1 / -1;
    }
  }

  .settings-table-header,
  .settings-table-row,
  .number-settings-grid,
  .number-settings-grid.three-columns,
  .named-colors {
    grid-template-columns: 1fr;
  }

  .settings-table-header {
    display: none;
  }

  .settings-table-row {
    padding: 9px 0;
  }
}
</style>
