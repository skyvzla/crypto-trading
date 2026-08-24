import dayjs from 'dayjs'
import timezone from 'dayjs/plugin/timezone'
import utc from 'dayjs/plugin/utc'

dayjs.extend(utc)
dayjs.extend(timezone)

/**
 * 账本时间口径的唯一来源。
 *
 * 交易日归属、日期筛选和所有时间展示都固定按 Asia/Shanghai 渲染，
 * 因此浏览器所在时区不会影响页面读数，表格时间与 K 线时间始终一致。
 *
 * 时区换算交给 dayjs（ant-design-vue 已依赖，日期选择器也用它），
 * 夏令时、月末天数、24:00 归零这些边界由库负责，不自己拼 Intl parts。
 */
export const LEDGER_TIMEZONE = 'Asia/Shanghai'

/** 小于该值的 epoch 视为秒级，用于兼容后端混用秒/毫秒时间戳。 */
const SECONDS_EPOCH_CEILING = 10_000_000_000

const DATE_FORMAT = 'YYYY-MM-DD'
const MONTH_FORMAT = 'YYYY-MM'

/**
 * 把后端的时间字段统一成毫秒 epoch。
 * 接受 ISO 字符串、秒级 epoch、毫秒级 epoch；无法解析时返回 null。
 *
 * 秒/毫秒的判定是本项目与后端的约定，不属于日期库的职责，所以保留在这里。
 */
export function timestampMs(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null
  const numeric = typeof value === 'number' ? value : Number(value)
  if (Number.isFinite(numeric)) {
    return numeric < SECONDS_EPOCH_CEILING ? numeric * 1000 : numeric
  }
  const parsed = dayjs(String(value))
  return parsed.isValid() ? parsed.valueOf() : null
}

/** 账本时区下的 dayjs 实例。 */
function inLedgerZone(value: number | Date) {
  return dayjs(value).tz(LEDGER_TIMEZONE)
}

/** 把 `YYYY-MM-DD` 这类账本本地日期按账本时区解析（而不是按浏览器时区）。 */
function parseLedgerDate(date: string) {
  return dayjs.tz(date, LEDGER_TIMEZONE)
}

/** `YYYY-MM-DD HH:mm:ss`（账本时区）。`seconds: false` 时省略秒。 */
export function formatLedgerDateTime(
  value: string | number | null | undefined,
  options: { seconds?: boolean } = {}
): string | null {
  const ms = timestampMs(value)
  if (ms === null) return null
  return inLedgerZone(ms).format(options.seconds === false ? 'YYYY-MM-DD HH:mm' : 'YYYY-MM-DD HH:mm:ss')
}

/** `MM-DD HH:mm`（账本时区），用于空间紧张的列表。 */
export function formatLedgerShortDateTime(value: string | number | null | undefined): string | null {
  const ms = timestampMs(value)
  return ms === null ? null : inLedgerZone(ms).format('MM-DD HH:mm')
}

/** `HH:mm:ss`（账本时区），用于「最近刷新」时间戳。 */
export function formatLedgerClock(value: Date | number = Date.now()): string {
  return inLedgerZone(value).format('HH:mm:ss')
}

/** 账本时区下的自然日，格式 `YYYY-MM-DD`。 */
export function ledgerDate(value: Date | number = Date.now()): string {
  return inLedgerZone(value).format(DATE_FORMAT)
}

/** 账本时区下的自然月，格式 `YYYY-MM`。 */
export function ledgerMonth(value: Date | number = Date.now()): string {
  return inLedgerZone(value).format(MONTH_FORMAT)
}

/** 把 `YYYY-MM-DD` 平移若干天。 */
export function shiftLedgerDate(date: string, days: number): string {
  return parseLedgerDate(date).add(days, 'day').format(DATE_FORMAT)
}

/** 给定年月的首末自然日，格式 `YYYY-MM-DD`。 */
export function ledgerMonthRange(year: number, month: number): { startDate: string; endDate: string } {
  const start = parseLedgerDate(`${year}-${String(month).padStart(2, '0')}-01`)
  return {
    startDate: start.format(DATE_FORMAT),
    endDate: start.endOf('month').format(DATE_FORMAT)
  }
}

/** 按 `delta` 个月平移，返回目标月的年月数字，用于收益日历翻月。 */
export function shiftLedgerMonth(year: number, month: number, delta: number): { year: number; month: number } {
  const shifted = parseLedgerDate(`${year}-${String(month).padStart(2, '0')}-01`).add(delta, 'month')
  return { year: shifted.year(), month: shifted.month() + 1 }
}
