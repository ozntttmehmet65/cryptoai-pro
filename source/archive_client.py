"""Official public Binance USD-M monthly kline archives; not the rate-limited Futures REST API.

Archives are delayed publication; archive candles MUST NOT be shown as current data.
Only completed months, never a missing/current-month file; one request per symbol.
"""
from __future__ import annotations
import csv
import io
import logging
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile, BadZipFile
import httpx
from urllib.parse import quote
from symbol_utils import symbol_valid, symbol_file_key

LOG=logging.getLogger(__name__)
ARCHIVE_BASE="https://data.binance.vision/data/futures/um/monthly/klines"
DAILY_BASE="https://data.binance.vision/data/futures/um/daily/klines"
MAX_ARCHIVE_SIZE = 8_000_000


def last_complete_month(today=None):
    today=today or datetime.now(timezone.utc)
    year=today.year
    month=today.month-1
    if month==0:month=12;year-=1
    return f"{year:04d}-{month:02d}"


def parse_kline_zip(blob, symbol, month, minimum=380):
    """Supports Binance futures ZIP CSV with or without a header row."""
    filename=f"{symbol}-15m-{month}.csv"
    with ZipFile(io.BytesIO(blob)) as zf:
        if filename not in zf.namelist():
            raise ValueError("Arşiv içindeki CSV adı eşleşmiyor")
        if zf.getinfo(filename).file_size>5_000_000:
            raise ValueError("Açılmış CSV çok büyük")
        text=zf.read(filename).decode("utf-8-sig")
    data=[]
    for row in csv.reader(io.StringIO(text)):
        if len(row)<7:continue
        try:
            t=int(float(row[0]))
            # USD-M Futures historical timestamps are milliseconds.
            if not 1000000000000<=t<=9999999999999:continue
            high=float(row[2]);low=float(row[3]);
            if low<=0 or high<low:continue
            data.append({"time":t,"open":float(row[1]),"high":high,
                         "low":low,"close":float(row[4]),"volume":float(row[5]),
                         "close_time":int(float(row[6]))})
        except (ValueError,TypeError):continue
    data={b["time"]:b for b in data}
    arr=[data[t] for t in sorted(data)]
    if len(arr)<minimum:raise ValueError("Tarihsel mum sayısı yetersiz")
    # Archive input must be continuous to avoid labeling price jumps across gaps.
    arr=[b for b in arr if b["time"]+900_000-1 <= b["close_time"] <= b["time"]+900_000]
    # Only the last uninterrupted segment is safe for chronological training.
    start=0
    for i in range(1,len(arr)):
        if arr[i]["time"]-arr[i-1]["time"]!=900_000:start=i
    arr=arr[start:]
    if len(arr)<minimum:raise ValueError("Arşivde yeterli kesintisiz 15dk veri yok")
    return arr


class ArchiveClient:
    def __init__(self, data_dir):
        self.root=Path(data_dir)/"archive_history"
        self.root.mkdir(parents=True,exist_ok=True)
        self.client=httpx.AsyncClient(timeout=30,follow_redirects=False,
                                      headers={"User-Agent":"CryptoAI-Pro/4.0 archival research"})

    async def month_candles(self,symbol,month=None):
        symbol=symbol.upper();month=month or last_complete_month()
        if not symbol_valid(symbol):raise ValueError("Sembol geçersiz")
        # Never download arbitrary files via user-supplied period/URL.
        if len(month)!=7 or month[4]!='-' or not month[:4].isdigit() or not month[5:].isdigit():
            raise ValueError("Ay geçersiz")
        fname=f"{symbol}-15m-{month}.zip"
        cached=self.root/(symbol_file_key(symbol)+"-15m-"+month+".zip")
        if cached.exists():
            try:return parse_kline_zip(cached.read_bytes(),symbol,month)
            except (OSError,BadZipFile,ValueError):cached.unlink(missing_ok=True)
        url=f"{ARCHIVE_BASE}/{quote(symbol, safe='')}/15m/{quote(fname, safe='')}"
        response=await self.client.get(url)
        if response.status_code==404:raise FileNotFoundError(f"{fname} arşivi henüz yayınlanmadı")
        response.raise_for_status()
        blob=response.content
        if len(blob)>MAX_ARCHIVE_SIZE:raise ValueError("Arşiv beklenenden büyük")
        bars=parse_kline_zip(blob,symbol,month)
        temp=cached.with_suffix('.tmp')
        temp.write_bytes(blob);temp.replace(cached)
        LOG.info("Archive ready: %s %s (%s candles; latest %s)",symbol,month,len(bars),bars[-1]['time'])
        return bars

    async def day_candles(self, symbol, day):
        """One UTC completed day of Binance official futures 15m candles (may be unpublished)."""
        symbol=symbol.upper()
        if not symbol_valid(symbol):raise ValueError("Sembol geçersiz")
        try: selected=datetime.strptime(day,"%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):raise ValueError("Gün geçersiz")
        if selected.date()>=datetime.now(timezone.utc).date():
            raise ValueError("Günlük arşiv yalnızca bitmiş UTC günler içindir")
        filename=f"{symbol}-15m-{day}.zip"
        saved=self.root/(symbol_file_key(symbol)+"-15m-"+day+".zip")
        if saved.exists():
            try:return parse_kline_zip(saved.read_bytes(),symbol,day,minimum=1)
            except (BadZipFile,OSError,ValueError):saved.unlink(missing_ok=True)
        url=f"{DAILY_BASE}/{quote(symbol, safe='')}/15m/{quote(filename, safe='')}"
        response=await self.client.get(url)
        if response.status_code==404:raise FileNotFoundError(filename+" henüz yayımlanmadı")
        response.raise_for_status()
        blob=response.content
        if len(blob)>1_000_000:raise ValueError("Günlük arşiv çok büyük")
        bars=parse_kline_zip(blob,symbol,day,minimum=1)
        tmp=saved.with_suffix('.tmp');tmp.write_bytes(blob);tmp.replace(saved)
        LOG.info("Daily archive ready: %s %s (%s)",symbol,day,len(bars))
        return bars

    async def close(self):await self.client.aclose()
