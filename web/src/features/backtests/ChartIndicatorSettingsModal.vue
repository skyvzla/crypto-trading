<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Plus, Trash2 } from 'lucide-vue-next'
import type {
  ChartIndicatorLineSetting,
  ChartIndicatorSettings,
  ChartLineAppearance,
  ChartLineStyle,
} from '@/api/types'
import { CHART_INTERVALS } from '@/shared/chartIntervals'
import {
  CHART_INDICATORS,
  cloneChartIndicatorSettings,
  indicatorEnabled,
  setIndicatorEnabled,
  type ChartIndicatorDefinition,
  type ChartIndicatorKey,
} from './chartIndicatorSettings'

const props = withDefaults(
  defineProps<{
    open: boolean
    settings: ChartIndicatorSettings
    saving?: boolean
    strategyLines?: boolean
  }>(),
  { strategyLines: true },
)

const emit = defineEmits<{
  'update:open': [open: boolean]
  save: [settings: ChartIndicatorSettings]
}>()

const selectedKey = ref<ChartIndicatorKey | 'display'>('display')
const draft = ref(cloneChartIndicatorSettings(props.settings))
const lineStyleOptions: Array<{ label: string; value: ChartLineStyle }> = [
  { label: '实线', value: 'solid' },
  { label: '虚线', value: 'dashed' },
  { label: '点线', value: 'dotted' },
]
const lineWidthOptions = [1, 2, 3, 4] as const
const zoomMarks = { 5: '紧凑', 8: '标准', 12: '放大' }
const priceLineDefinitions = computed(() =>
  [
    { key: 'signal' as const, label: '信号价', strategyOnly: true },
    { key: 'average' as const, label: '开仓均价', strategyOnly: false },
    { key: 'invalid' as const, label: '失效价', strategyOnly: true },
    { key: 'extensions' as const, label: '策略扩展价位', strategyOnly: true },
  ].filter((item) => props.strategyLines !== false || !item.strategyOnly),
)

const mainIndicators = CHART_INDICATORS.filter((item) => item.group === 'main')
const subIndicators = CHART_INDICATORS.filter((item) => item.group === 'sub')
const selectedDefinition = computed(
  () => CHART_INDICATORS.find((item) => item.key === selectedKey.value) ?? CHART_INDICATORS[0],
)
const editorAriaLabel = computed(() =>
  selectedKey.value === 'display' ? '显示设置' : `${selectedDefinition.value.name} 参数`,
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
  lines.push({
    period: Math.min(500, lastPeriod + fallbackPeriod),
    color: palette[lines.length % palette.length],
    style: 'solid',
    width: 1,
  })
}

function updateLineStyle(line: ChartLineAppearance, value: ChartLineStyle) {
  line.style = value
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
    <div class="indicator-settings-layout">
      <nav class="indicator-list" aria-label="技术指标列表">
        <h3>通用</h3>
        <div
          class="indicator-list-item"
          :class="{ 'is-selected': selectedKey === 'display' }"
          role="button"
          tabindex="0"
          @click="selectedKey = 'display'"
          @keydown.enter="selectedKey = 'display'"
        >
          <span class="list-item-spacer" aria-hidden="true" />
          <span>
            <strong>显示</strong>
            <small>周期、缩放与标线</small>
          </span>
        </div>

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

      <section class="indicator-editor" :aria-label="editorAriaLabel">
        <header>
          <div v-if="selectedKey === 'display'">
            <h3>显示</h3>
            <p>默认周期、可视范围与策略标线</p>
          </div>
          <div v-else>
            <h3>{{ selectedDefinition.name }}</h3>
            <p>{{ selectedDefinition.description }}</p>
          </div>
          <a-switch
            v-if="selectedKey !== 'display'"
            :checked="indicatorEnabled(draft, selectedDefinition)"
            checked-children="显示"
            un-checked-children="隐藏"
            @change="toggleIndicator(selectedDefinition)"
          />
        </header>

        <template v-if="selectedKey === 'display'">
          <div class="number-settings-grid">
            <label>
              <span>默认周期</span>
              <a-select
                id="default-chart-interval"
                v-model:value="draft.default_interval"
                class="default-interval-select"
                size="small"
              >
                <a-select-option v-for="item in CHART_INTERVALS" :key="item" :value="item">{{ item }}</a-select-option>
              </a-select>
            </label>
            <label>
              <span>默认 K 线宽度</span>
              <span class="zoom-setting-control">
                <a-slider
                  v-model:value="draft.display.default_bar_spacing"
                  :min="2"
                  :max="30"
                  :step="0.5"
                  :marks="zoomMarks"
                />
                <a-input-number
                  v-model:value="draft.display.default_bar_spacing"
                  :min="2"
                  :max="30"
                  :step="0.5"
                  addon-after="px"
                />
              </span>
            </label>
          </div>

          <h4 class="settings-subheading">标线</h4>
          <div class="price-line-table-header">
            <span>显示</span>
            <span>线型</span>
            <span>粗细</span>
          </div>
          <div v-for="item in priceLineDefinitions" :key="item.key" class="price-line-row">
            <a-checkbox v-model:checked="draft.display.price_lines[item.key].visible">{{ item.label }}</a-checkbox>
            <a-select
              :value="draft.display.price_lines[item.key].style"
              :aria-label="`${item.label}线型`"
              size="small"
              @update:value="updateLineStyle(draft.display.price_lines[item.key], $event)"
            >
              <a-select-option v-for="option in lineStyleOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </a-select-option>
            </a-select>
            <a-select
              v-model:value="draft.display.price_lines[item.key].width"
              :aria-label="`${item.label}粗细`"
              size="small"
            >
              <a-select-option v-for="width in lineWidthOptions" :key="width" :value="width"
                >{{ width }} px</a-select-option
              >
            </a-select>
          </div>
        </template>

        <template v-else-if="selectedKey === 'ema' || selectedKey === 'ma'">
          <div class="settings-table-header">
            <span>周期</span>
            <span>线条颜色</span>
            <span>线型</span>
            <span>粗细</span>
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
            <a-select v-model:value="line.style" :aria-label="`第 ${index + 1} 条线线型`" size="small">
              <a-select-option v-for="option in lineStyleOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </a-select-option>
            </a-select>
            <a-select v-model:value="line.width" :aria-label="`第 ${index + 1} 条线粗细`" size="small">
              <a-select-option v-for="width in lineWidthOptions" :key="width" :value="width"
                >{{ width }} px</a-select-option
              >
            </a-select>
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
          <div class="named-line-settings">
            <div class="named-line-row">
              <span>通道边界</span>
              <label class="color-control">
                <input v-model="draft.main.boll.colors.upper" type="color" aria-label="BOLL 通道边界颜色" />
                <code>{{ draft.main.boll.colors.upper }}</code>
              </label>
              <a-select
                v-model:value="draft.main.boll.lines.boundary.style"
                aria-label="BOLL 通道边界线型"
                size="small"
              >
                <a-select-option v-for="option in lineStyleOptions" :key="option.value" :value="option.value">
                  {{ option.label }}
                </a-select-option>
              </a-select>
              <a-select
                v-model:value="draft.main.boll.lines.boundary.width"
                aria-label="BOLL 通道边界粗细"
                size="small"
              >
                <a-select-option v-for="width in lineWidthOptions" :key="width" :value="width"
                  >{{ width }} px</a-select-option
                >
              </a-select>
            </div>
            <div class="named-line-row">
              <span>中轨</span>
              <label class="color-control">
                <input v-model="draft.main.boll.colors.middle" type="color" aria-label="BOLL 中轨颜色" />
                <code>{{ draft.main.boll.colors.middle }}</code>
              </label>
              <a-select v-model:value="draft.main.boll.lines.middle.style" aria-label="BOLL 中轨线型" size="small">
                <a-select-option v-for="option in lineStyleOptions" :key="option.value" :value="option.value">
                  {{ option.label }}
                </a-select-option>
              </a-select>
              <a-select v-model:value="draft.main.boll.lines.middle.width" aria-label="BOLL 中轨粗细" size="small">
                <a-select-option v-for="width in lineWidthOptions" :key="width" :value="width"
                  >{{ width }} px</a-select-option
                >
              </a-select>
            </div>
            <div class="named-line-row fill-color-row">
              <span>通道填充</span>
              <label class="color-control">
                <input v-model="draft.main.boll.colors.lower" type="color" aria-label="BOLL 通道填充颜色" />
                <code>{{ draft.main.boll.colors.lower }}</code>
              </label>
            </div>
          </div>
        </template>

        <template v-else-if="selectedKey === 'volume'">
          <div class="settings-table-header">
            <span>均量周期</span>
            <span>线条颜色</span>
            <span>线型</span>
            <span>粗细</span>
            <span class="action-column">操作</span>
          </div>
          <div v-for="(line, index) in draft.sub.volume.ma_lines" :key="index" class="settings-table-row">
            <a-input-number v-model:value="line.period" :min="1" :max="500" :precision="0" />
            <label class="color-control">
              <input v-model="line.color" type="color" :aria-label="`第 ${index + 1} 条均量线颜色`" />
              <code>{{ line.color }}</code>
            </label>
            <a-select v-model:value="line.style" :aria-label="`第 ${index + 1} 条均量线线型`" size="small">
              <a-select-option v-for="option in lineStyleOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </a-select-option>
            </a-select>
            <a-select v-model:value="line.width" :aria-label="`第 ${index + 1} 条均量线粗细`" size="small">
              <a-select-option v-for="width in lineWidthOptions" :key="width" :value="width"
                >{{ width }} px</a-select-option
              >
            </a-select>
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
          <div class="named-line-settings">
            <div v-for="name in ['dif', 'dea'] as const" :key="name" class="named-line-row">
              <span>{{ name.toUpperCase() }}</span>
              <label class="color-control">
                <input v-model="draft.sub.macd.colors[name]" type="color" :aria-label="`MACD ${name} 颜色`" />
                <code>{{ draft.sub.macd.colors[name] }}</code>
              </label>
              <a-select v-model:value="draft.sub.macd.lines[name].style" :aria-label="`MACD ${name} 线型`" size="small">
                <a-select-option v-for="option in lineStyleOptions" :key="option.value" :value="option.value">
                  {{ option.label }}
                </a-select-option>
              </a-select>
              <a-select v-model:value="draft.sub.macd.lines[name].width" :aria-label="`MACD ${name} 粗细`" size="small">
                <a-select-option v-for="width in lineWidthOptions" :key="width" :value="width"
                  >{{ width }} px</a-select-option
                >
              </a-select>
            </div>
          </div>
          <div class="named-colors compact-colors">
            <label v-for="name in ['histogram_up', 'histogram_down'] as const" :key="name" class="color-control">
              <span>{{ { histogram_up: '正柱', histogram_down: '负柱' }[name] }}</span>
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
          <div class="named-line-settings">
            <div v-for="name in ['k', 'd', 'j'] as const" :key="name" class="named-line-row">
              <span>{{ name.toUpperCase() }} 线</span>
              <label class="color-control">
                <input v-model="draft.sub.kdj.colors[name]" type="color" :aria-label="`KDJ ${name} 颜色`" />
                <code>{{ draft.sub.kdj.colors[name] }}</code>
              </label>
              <a-select v-model:value="draft.sub.kdj.lines[name].style" :aria-label="`KDJ ${name} 线型`" size="small">
                <a-select-option v-for="option in lineStyleOptions" :key="option.value" :value="option.value">
                  {{ option.label }}
                </a-select-option>
              </a-select>
              <a-select v-model:value="draft.sub.kdj.lines[name].width" :aria-label="`KDJ ${name} 粗细`" size="small">
                <a-select-option v-for="width in lineWidthOptions" :key="width" :value="width"
                  >{{ width }} px</a-select-option
                >
              </a-select>
            </div>
          </div>
        </template>

        <template v-else-if="selectedKey === 'rsi'">
          <div class="settings-table-header">
            <span>周期</span>
            <span>线条颜色</span>
            <span>线型</span>
            <span>粗细</span>
            <span class="action-column">操作</span>
          </div>
          <div v-for="(line, index) in draft.sub.rsi.lines" :key="index" class="settings-table-row">
            <a-input-number v-model:value="line.period" :min="1" :max="500" :precision="0" />
            <label class="color-control">
              <input v-model="line.color" type="color" :aria-label="`第 ${index + 1} 条 RSI 线颜色`" />
              <code>{{ line.color }}</code>
            </label>
            <a-select v-model:value="line.style" :aria-label="`第 ${index + 1} 条 RSI 线线型`" size="small">
              <a-select-option v-for="option in lineStyleOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </a-select-option>
            </a-select>
            <a-select v-model:value="line.width" :aria-label="`第 ${index + 1} 条 RSI 线粗细`" size="small">
              <a-select-option v-for="width in lineWidthOptions" :key="width" :value="width"
                >{{ width }} px</a-select-option
              >
            </a-select>
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
            <label>
              <span>线型</span>
              <a-select v-model:value="draft.sub.atr.line.style" aria-label="ATR 线型" size="small">
                <a-select-option v-for="option in lineStyleOptions" :key="option.value" :value="option.value">
                  {{ option.label }}
                </a-select-option>
              </a-select>
            </label>
            <label>
              <span>粗细</span>
              <a-select v-model:value="draft.sub.atr.line.width" aria-label="ATR 粗细" size="small">
                <a-select-option v-for="width in lineWidthOptions" :key="width" :value="width"
                  >{{ width }} px</a-select-option
                >
              </a-select>
            </label>
          </div>
        </template>
      </section>
    </div>
  </a-modal>
</template>

<style scoped lang="scss">
.indicator-settings-layout {
  display: grid;
  grid-template-columns: minmax(210px, 0.7fr) minmax(0, 1.6fr);
  min-height: 470px;
  border: 1px solid var(--line);
  border-radius: 6px;
  overflow: hidden;
}

.list-item-spacer {
  width: 16px;
  height: 16px;
  flex: 0 0 16px;
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
  grid-template-columns: minmax(72px, 0.65fr) minmax(130px, 1.25fr) 92px 76px 40px;
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

.zoom-setting-control {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) 94px;
  align-items: center;
  gap: 14px;
}

.settings-subheading {
  margin: 24px 0 7px;
  color: var(--text);
  font-size: var(--type-secondary);
}

.price-line-table-header,
.price-line-row {
  display: grid;
  grid-template-columns: minmax(140px, 1fr) 120px 90px;
  align-items: center;
  gap: 12px;
}

.price-line-table-header {
  padding: 0 0 6px;
  color: var(--muted);
  font-size: var(--type-meta);
}

.price-line-row {
  min-height: 46px;
  border-top: 1px solid var(--line);
}

.named-line-settings {
  margin-top: 18px;
}

.named-line-row {
  display: grid;
  grid-template-columns: 92px minmax(145px, 1fr) 100px 80px;
  align-items: center;
  gap: 10px;
  min-height: 48px;
  border-top: 1px solid var(--line);

  > span:first-child {
    color: var(--text);
    font-size: var(--type-secondary);
  }
}

.fill-color-row {
  grid-template-columns: 92px minmax(145px, 1fr);
}

.compact-colors {
  margin-top: 12px;
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
  .price-line-table-header,
  .price-line-row,
  .named-line-row,
  .fill-color-row,
  .number-settings-grid,
  .number-settings-grid.three-columns,
  .zoom-setting-control,
  .named-colors {
    grid-template-columns: 1fr;
  }

  .settings-table-header {
    display: none;
  }

  .price-line-table-header {
    display: none;
  }

  .settings-table-row,
  .price-line-row,
  .named-line-row {
    padding: 9px 0;
  }
}
</style>
