"""羽田空港 P2/P3 駐車場の空車監視 (10/24〜11/1)。空車が出たら ntfy でiPhoneに通知。"""
import json, os, re, sys, datetime as dt
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright

URL = "https://hnd-rsv.aeif.or.jp/airport2/app/toppage"
START, END = dt.date(2026, 10, 24), dt.date(2026, 11, 1)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
ALERT_ON_BUSY = os.environ.get("ALERT_ON_BUSY", "false").lower() == "true"
STATE = Path("state/state.json")
DEBUG = Path("debug"); DEBUG.mkdir(exist_ok=True)

def classify(s: str) -> str:
    if "満車" in s: return "満車"
    if "混雑" in s: return "混雑"
    if "空車" in s: return "空車"
    if "期間外" in s: return "期間外"
    return "不明"

JS_SCRAPE = """
(idx) => {
  const t = document.querySelectorAll('table')[idx];
  if (!t) return null;
  let block = t;
  while (block.parentElement && block.parentElement.querySelectorAll('table').length === 1) block = block.parentElement;
  let label = '';
  for (let e = block; e && !label; e = e.previousElementSibling) {
    const img = e.matches && e.matches('img') ? e : (e.querySelector && e.querySelector('img[alt*="カレンダー"]'));
    if (img) label = img.alt;
  }
  const cells = [...t.querySelectorAll('td')].map(td => ({
    text: td.innerText.trim(),
    sig: [td.className, td.getAttribute('title')||'', td.innerText,
          ...[...td.querySelectorAll('*')].map(x => (x.className||'')+' '+(x.alt||'')+' '+(x.title||'')+' '+(x.getAttribute('src')||''))].join(' ')
  }));
  const m = (block.innerText.match(/(\\d{4})\\s*年\\s*(\\d{1,2})\\s*月/) || []);
  return {label, year: m[1] ? +m[1] : null, month: m[2] ? +m[2] : null, cells, html: block.outerHTML};
}
"""
JS_NEXT = """
(idx) => {
  let block = document.querySelectorAll('table')[idx];
  while (block.parentElement && block.parentElement.querySelectorAll('table').length === 1) block = block.parentElement;
  const a = [...block.querySelectorAll('a')].find(x => x.innerText.includes('翌月'));
  if (a) { a.click(); return true; } return false;
}
"""

