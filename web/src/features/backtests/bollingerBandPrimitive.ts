import type {
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  Time,
  UTCTimestamp,
} from 'lightweight-charts'

export interface BollingerBandPoint {
  time: UTCTimestamp
  upper: number
  lower: number
}

interface BollingerBandOptions {
  fillColor: string
}

interface BollingerBandCoordinate {
  x: number
  upper: number
  lower: number
}

type RenderingTarget = Parameters<IPrimitivePaneRenderer['draw']>[0]

class BollingerBandRenderer implements IPrimitivePaneRenderer {
  constructor(
    private readonly segments: readonly (readonly BollingerBandCoordinate[])[],
    private readonly fillColor: string,
  ) {}

  draw() {}

  drawBackground(target: RenderingTarget) {
    if (!this.segments.length) return
    target.useMediaCoordinateSpace(({ context }) => {
      context.save()
      context.fillStyle = this.fillColor
      for (const points of this.segments) {
        context.beginPath()
        context.moveTo(points[0].x, points[0].upper)
        for (let index = 1; index < points.length; index += 1) {
          context.lineTo(points[index].x, points[index].upper)
        }
        for (let index = points.length - 1; index >= 0; index -= 1) {
          context.lineTo(points[index].x, points[index].lower)
        }
        context.closePath()
        context.fill()
      }
      context.restore()
    })
  }
}

class BollingerBandPaneView implements IPrimitivePaneView {
  private segments: BollingerBandCoordinate[][] = []

  constructor(private readonly source: BollingerBandPrimitive) {}

  update() {
    this.segments = this.source.coordinateSegments()
  }

  zOrder(): PrimitivePaneViewZOrder {
    return 'bottom'
  }

  renderer(): IPrimitivePaneRenderer | null {
    return this.segments.length ? new BollingerBandRenderer(this.segments, this.source.fillColor) : null
  }
}

export class BollingerBandPrimitive implements ISeriesPrimitive<Time> {
  private readonly paneView = new BollingerBandPaneView(this)
  private attachment: SeriesAttachedParameter<Time> | null = null
  private points: Array<BollingerBandPoint | null> = []

  constructor(private readonly options: BollingerBandOptions) {}

  get fillColor(): string {
    return this.options.fillColor
  }

  attached(parameters: SeriesAttachedParameter<Time>) {
    this.attachment = parameters
    this.paneView.update()
  }

  detached() {
    this.attachment = null
  }

  setData(points: readonly (BollingerBandPoint | null)[]) {
    this.points = points.map((point) =>
      point && Number.isFinite(point.upper) && Number.isFinite(point.lower) && point.upper >= point.lower
        ? point
        : null,
    )
    this.paneView.update()
    this.attachment?.requestUpdate()
  }

  updateAllViews() {
    this.paneView.update()
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return [this.paneView]
  }

  coordinateSegments(): BollingerBandCoordinate[][] {
    const attachment = this.attachment
    if (!attachment) return []
    const timeScale = attachment.chart.timeScale()
    const segments: BollingerBandCoordinate[][] = []
    let segment: BollingerBandCoordinate[] = []
    const finishSegment = () => {
      if (segment.length >= 2) segments.push(segment)
      segment = []
    }
    for (const point of this.points) {
      if (point === null) {
        finishSegment()
        continue
      }
      const x = timeScale.timeToCoordinate(point.time)
      const upper = attachment.series.priceToCoordinate(point.upper)
      const lower = attachment.series.priceToCoordinate(point.lower)
      if (x === null || upper === null || lower === null) {
        finishSegment()
        continue
      }
      segment.push({ x, upper, lower })
    }
    finishSegment()
    return segments
  }
}

export function colorWithOpacity(color: string, opacity: number): string {
  const match = /^#([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?$/.exec(color)
  if (!match) return color
  const alpha = Math.round(Math.min(1, Math.max(0, opacity)) * 255)
    .toString(16)
    .padStart(2, '0')
  return `#${match[1]}${alpha}`
}
