from __future__ import annotations

import csv
import os
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path
import duckdb

DB='data/market/history.duckdb'; SYMBOL=os.environ.get('SYMBOL','AKEUSDT'); OUT=Path(os.environ.get('OUT','reports/ake_dynamic_trigger_orders_20260706_20260802.csv'))
TIER_WEIGHTS=(.30,.40,.30); RETEST_ATR=.75; SPREAD_ATR=.40; ORIGIN_MIN_RISE=.10
START=datetime.fromisoformat(os.environ.get('START','2026-07-06T00:00:00+00:00')); END=datetime.fromisoformat(os.environ.get('END','2026-08-03T00:00:00+00:00'))

def iso(value): return value.isoformat() if value else ''
def pct(value): return f'{value*100:.4f}%' if value is not None else ''
def num(value): return f'{value:.10f}' if value is not None else ''

def main():
 con=duckdb.connect(DB,read_only=True)
 sec=con.execute("select open_time,open,high,low,close,volume from candles where symbol=? and timeframe='1s' and open_time>=? and open_time<? order by open_time",[SYMBOL,START,END]).fetchall()
 mins=con.execute("select open_time,open,high,low,close from candles where symbol=? and timeframe='1m' and open_time>=? - interval '30 hours' and open_time<? order by open_time",[SYMBOL,START,END]).fetchall()
 fives=con.execute("select open_time,open,high,low,close from candles where symbol=? and timeframe='5m' and open_time>=? - interval '40 hours' and open_time<? order by open_time",[SYMBOL,START,END]).fetchall(); con.close()
 mt=[x[0] for x in mins]; candidates=[]
 ft=[x[0] for x in fives]
 for i in range(60,len(sec)):
  if sec[i][0]-sec[i-5][0]!=timedelta(seconds=5) or sec[i][0]-sec[i-60][0]!=timedelta(seconds=60): continue
  med=sorted(x[5] for x in sec[i-60:i])[30]; v5=sum(x[5] for x in sec[i-4:i+1]); r5=sec[i][4]/sec[i-5][4]-1
  if r5<.05 or v5/(med*5)<3: continue
  mi=bisect_left(mt,sec[i][0].replace(second=0,microsecond=0)); low12=min((x[3] for x in mins[max(0,mi-720):mi]) or [sec[i][4]])
  if sec[i][4]/low12-1<.20: continue
  if candidates and sec[i][0]-datetime.fromisoformat(candidates[-1]['signal_at'])<timedelta(seconds=180): continue
  origin=min((x[3] for x in mins[max(0,mi-16*60):mi]) or [sec[i][4]])
  spike_high=max([sec[i][2]]+[x[2] for x in mins[max(0,mi-30):mi+1]])
  fi=bisect_left(ft,sec[i][0].replace(second=sec[i][0].second//300*5, microsecond=0))
  trs=[]
  for j in range(max(1,fi-14),fi): trs.append(max(fives[j][2]-fives[j][3],abs(fives[j][2]-fives[j-1][4]),abs(fives[j][3]-fives[j-1][4])))
  atr=sum(trs)/len(trs) if trs else 0.0
  if not atr: continue
  origin_floor=origin*(1+ORIGIN_MIN_RISE)
  tier_prices=[spike_high-atr*(RETEST_ATR-(n-1)*SPREAD_ATR) for n in range(3)]
  eligible = min(tier_prices) >= origin_floor and min(tier_prices) > sec[i][4]
  invalid=max(spike_high+atr*3.5, tier_prices[1]+atr*2.0)
  arm= i+1; end=min(len(sec),arm+180); window=sec[arm:end]
  row={'signal_at':iso(sec[i][0]),'order_active_at':iso(sec[arm][0]) if window else '', 'order_expire_at':iso(sec[end-1][0]) if window else '', 'trigger_price':num(sec[i][4]), 'return_5s':pct(r5), 'return_15s':pct(sec[i][4]/sec[i-15][4]-1) if sec[i][0]-sec[i-15][0]==timedelta(seconds=15) else '', 'return_60s':pct(sec[i][4]/sec[i-60][4]-1) if sec[i][0]-sec[i-60][0]==timedelta(seconds=60) else '', 'volume_5s':num(v5), 'volume_baseline_1s_median':num(med), 'volume_multiple_5s':f'{v5/(med*5):.4f}x', '12h_low':num(low12), 'rise_from_12h_low':pct(sec[i][4]/low12-1), 'origin_price':num(origin), 'origin_plus_10pct_floor':num(origin_floor), 'spike_high':num(spike_high), 'atr_5m':num(atr), 'pricing_mode':'momentum_atr_estimate', 'predicted_primary':num(tier_prices[1]), 'invalid_price':num(invalid)}
  row.update({'window_high':num(max(x[2] for x in window)) if window else '', 'window_low':num(min(x[3] for x in window)) if window else '', 'window_high_vs_trigger':pct(max(x[2] for x in window)/sec[i][4]-1) if window else '', 'window_low_vs_trigger':pct(min(x[3] for x in window)/sec[i][4]-1) if window else '', 'invalid_touched': 'yes' if any(x[2]>=invalid for x in window) else 'no'})
  filled=0
  for n,(price,weight) in enumerate(zip(tier_prices,TIER_WEIGHTS),1):
   hit=next((x for x in window if x[2]>=price),None); key=f'tier{n}'
   row[f'{key}_price']=num(price); row[f'{key}_weight']=f'{weight:.2f}'; row[f'{key}_status']=('not_placed' if not eligible else ('filled' if hit else 'unfilled')); row[f'{key}_first_touch_at']=iso(hit[0]) if hit and eligible else ''; filled += bool(hit and eligible)
  row['filled_tier_count']=str(filled); row['order_result']=('rejected_origin_floor' if not eligible else ('all_filled' if filled==3 else ('partial_filled' if filled else 'no_fill')))
  later=sec[arm:min(len(sec),arm+300)]; row['next_5m_high']=num(max(x[2] for x in later)) if later else ''; row['next_5m_low']=num(min(x[3] for x in later)) if later else ''; row['next_5m_close']=num(later[-1][4]) if later else ''
  candidates.append(row)
 fields=list(candidates[0]) if candidates else []
 OUT.parent.mkdir(parents=True,exist_ok=True)
 with OUT.open('w',newline='',encoding='utf-8-sig') as f:
  writer=csv.DictWriter(f,fieldnames=fields); writer.writeheader(); writer.writerows(candidates)
 print(f'{OUT} rows={len(candidates)}')

if __name__=='__main__': main()
