import csv
import os
from datetime import datetime,timedelta,timezone
import duckdb

CSV=os.environ.get('CSV','reports/ake_dynamic_trigger_orders_20260706_20260802.csv'); DB=os.environ.get('DUCKDB_PATH','data/market/candles/candles.duckdb'); SYMBOL=os.environ.get('SYMBOL','AKEUSDT'); FEE=.0008
rows=list(csv.DictReader(open(CSV,encoding='utf-8-sig')))
con=duckdb.connect(DB,read_only=True)
start=datetime.fromisoformat(os.environ.get('START','2026-07-06T00:00:00+00:00')); end=datetime.fromisoformat(os.environ.get('END','2026-08-03T00:00:00+00:00'))
sec=con.execute("select open_time,high,low,close from candles where symbol=? and timeframe='1s' and open_time>=? and open_time<? order by open_time",[SYMBOL,start,end]).fetchall(); con.close()
for r in rows:
 if r['order_result']=='rejected_origin_floor' or r['order_result']=='no_fill': r.update({'exit_status':'no_position','entry_avg':'','exit_price':'','gross_return':'','net_return':''}); continue
 fills=[]
 for n in (1,2,3):
  t=r[f'tier{n}_first_touch_at']
  if t: fills.append((float(r[f'tier{n}_price']),float(r[f'tier{n}_weight']),datetime.fromisoformat(t)))
 total=sum(x[1] for x in fills); entry=sum(x[0]*x[1] for x in fills)/total
 target=entry-(float(r['invalid_price'])-entry)*2
 after=max(x[2] for x in fills); i=next((j for j,x in enumerate(sec) if x[0]>=after),len(sec)); horizon=sec[i:min(len(sec),i+900)]
 status='timeout'; exit_price=horizon[-1][3] if horizon else entry
 for x in horizon:
  hit_stop=x[1]>=float(r['invalid_price']); hit_target=x[2]<=target
  if hit_stop and hit_target: status='ambiguous'; exit_price=entry; break
  if hit_stop: status='stop'; exit_price=float(r['invalid_price']); break
  if hit_target: status='target'; exit_price=target; break
 gross=entry/exit_price-1
 net=gross-FEE
 r.update({'exit_status':status,'entry_avg':f'{entry:.10f}','exit_price':f'{exit_price:.10f}','gross_return':f'{gross*100:.4f}%','net_return':f'{net*100:.4f}%'})
fields=list(rows[0])
out=CSV.replace('.csv','_with_pnl.csv')
with open(out,'w',newline='',encoding='utf-8-sig') as f:
 w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
print('output',out)
from collections import Counter
tr=[r for r in rows if r['exit_status'] not in ('no_position','')]
print('trades',len(tr),'statuses',Counter(r['exit_status'] for r in tr))
print('wins',sum(float(r['net_return'][:-1])>0 for r in tr),'win_rate',sum(float(r['net_return'][:-1])>0 for r in tr)/len(tr) if tr else 0,'sum_net_pct',sum(float(r['net_return'][:-1]) for r in tr))
for r in tr: print(r['signal_at'],r['order_result'],r['entry_avg'],r['exit_status'],r['exit_price'],r['net_return'])
