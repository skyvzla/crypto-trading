import type { RouteLocationRaw } from 'vue-router'

export interface CampaignRouteSource {
  account_id: string
  strategy_id: string
  symbol: string
  campaign_id: string | null | undefined
}

export function campaignRoute(source: CampaignRouteSource): RouteLocationRaw | null {
  if (!source.campaign_id) return null
  return {
    name: 'campaign-trade-detail',
    params: { campaignId: source.campaign_id },
    query: {
      account_id: source.account_id,
      strategy_id: source.strategy_id,
      symbol: source.symbol
    }
  }
}
