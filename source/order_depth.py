"""Bounded public partial-depth snapshots and hypothetical visible fills."""
from __future__ import annotations
import asyncio
import json
import math
import time
import logging
from urllib.parse import quote
import websockets

LOG=logging.getLogger(__name__)
MAX_SYMBOLS=32
MAX_AGE_SECONDS=3.
MAX_IMPACT=.0002  # Same 2 bps adverse-slippage budget as paper assumptions.
ENDPOINT='wss://fstream.binance.com/public/stream'


def levels(rows, descending):
    if not isinstance(rows,list) or not 1<=len(rows)<=20:raise ValueError('depth size')
    result=[]
    for row in rows:
        if not isinstance(row,(list,tuple)) or len(row)!=2:raise ValueError('depth level')
        price,qty=map(float,row)
        if not all(map(math.isfinite,(price,qty))) or price<=0 or qty<=0:raise ValueError('depth value')
        result.append((price,qty))
    if any((a[0]<=b[0] if descending else a[0]>=b[0]) for a,b in zip(result,result[1:])):
        raise ValueError('unordered depth')
    return result


def parse_depth(message,allowed,now=None):
    try:
        obj=json.loads(message) if isinstance(message,(str,bytes)) else message
        if not isinstance(obj,dict):return {}
        stream=obj.get('stream','')
        if not isinstance(stream,str):return {}
        data=obj.get('data',obj)
        if data.get('e')!='depthUpdate' or data.get('s') not in allowed or data.get('st',1)!=1:return {}
        if stream!=data['s'].lower()+'@depth20@500ms':return {}
        stamp=float(data['E'])/1000;update=int(data['u'])
        if update<0:return {}
        now=time.time() if now is None else now
        if not math.isfinite(stamp) or not 0<=now-stamp<MAX_AGE_SECONDS:return {}
        bids=levels(data['b'],True);asks=levels(data['a'],False)
        if bids[0][0]>asks[0][0]:return {}
        return {data['s']:{'bid':bids[0][0],'ask':asks[0][0],
            'bid_qty':bids[0][1],'ask_qty':asks[0][1],'bids':bids,'asks':asks,
            'updated_at':stamp,'update_id':update,'source':'depth20_snapshot'}}
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):return {}


def visible_fill(rows, *, notional=None, quantity=None):
    """Walk a trusted sorted snapshot; never claim more than visible liquidity."""
    if (notional is None)==(quantity is None):raise ValueError('one fill target required')
    target=float(notional if notional is not None else quantity)
    if not math.isfinite(target) or target<=0:return None
    cost=qty=0.
    for price,available in rows:
        remain=target-(cost if notional is not None else qty)
        take=min(available,remain/price if notional is not None else remain)
        qty+=take;cost+=take*price
        if target-(cost if notional is not None else qty)<=target*1e-10:
            return {'price':cost/qty,'quantity':qty,'notional':cost}
    return None


def execution_book(book,now_ms,notional=100.):
    """Return the same shape as top-of-book, with size-aware VWAP if available."""
    if not isinstance(book,dict) or book.get('source')!='depth20_snapshot':return book
    try:
        if not 0<=now_ms/1000-float(book['updated_at'])<MAX_AGE_SECONDS:return None
        bids=levels(book['bids'],True);asks=levels(book['asks'],False)
        if bids[0][0]>asks[0][0]:return None
        buy=visible_fill(asks,notional=notional);sell=visible_fill(bids,notional=notional)
        available=min(sum(p*q for p,q in bids),sum(p*q for p,q in asks))
        if not buy or not sell:
            return dict(book,depth_approved=False,depth_error='size',depth_available_notional=available)
        impact=max(buy['price']/asks[0][0]-1,1-sell['price']/bids[0][0])
        return dict(book,bid=sell['price'],ask=buy['price'],
            bid_qty=sum(q for _,q in bids),ask_qty=sum(q for _,q in asks),
            depth_available_notional=available,depth_impact=impact,depth_approved=impact<=MAX_IMPACT)
    except (ValueError,TypeError,KeyError,OverflowError):return None


def selected_symbols(universe,tickers,signals,open_symbols=()):
    active=[s for s,p in signals.items() if p.get('candidate_signal') in ('LONG','SHORT')]
    watch=[s for s,p in signals.items() if (p.get('technical') or {}).get('score',0)>=6.5]
    volume=lambda s:(-tickers.get(s,{}).get('quote_volume',0),s)
    ordered=list(open_symbols)+sorted(active,key=volume)+sorted(watch,key=volume)+sorted(universe,key=volume)
    return list(dict.fromkeys(s for s in ordered if s in universe))[:MAX_SYMBOLS]


async def depth_worker(symbols,on_update,on_status):
    retry=3.
    while True:
        group=symbols()
        if not group:
            await asyncio.sleep(5);continue
        allowed=set(group)
        streams='/'.join(quote(s.lower()+'@depth20@500ms',safe='@_') for s in group)
        try:
            async with websockets.connect(ENDPOINT+'?streams='+streams,ping_interval=25,ping_timeout=25,
                    open_timeout=20,close_timeout=8,max_size=128000,max_queue=32,compression=None) as ws:
                on_status('Bağlandı');retry=3.;rotate=time.monotonic()+30
                while True:
                    message=await asyncio.wait_for(ws.recv(),timeout=20)
                    updates=parse_depth(message,allowed)
                    if updates:on_update(updates)
                    if time.monotonic()>=rotate:
                        if symbols()!=group:break
                        rotate=time.monotonic()+30
        except asyncio.CancelledError:raise
        except Exception as exc:
            LOG.warning('Partial depth unavailable: %s',str(exc)[:160]);on_status('Bekleniyor')
            await asyncio.sleep(retry);retry=min(120,retry*2)
