"""羽田空港 P5 一般枠の空き監視。対象日が 混雑/空車 に変わったら ntfy で通知。"""
import json, os, re, datetime as dt
from collections import Counter
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright

URL = os.environ.get("TARGET_URL", "https://pk-reserve.haneda-airport.jp/airport/entrance/0000.jsf")
TARGETS = { dt.date(2026, 10, 25), dt.date(2026, 11, 1)}
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE = Path("state5/state.json")
DEBUG = Path("debug5"); DEBUG.mkdir(exist_ok=True)

JS_TOK = """
const tokOne = el => {
  const t = [];
  if (!el) return t;
  if (typeof el.className === 'string') el.className.split(/\\s+/).filter(Boolean).forEach(c => t.push('c:' + c));
  const bg = el.getAttribute && el.getAttribute('bgcolor'); if (bg) t.push('bg:' + bg.toLowerCase());
  const st = el.getAttribute && el.getAttribute('style');
  if (st) st.split(';').map(s => s.trim().toLowerCase().replace(/\\s+/g, '')).filter(Boolean).forEach(s => t.push('s:' + s));
  const bi = el.getAttribute && el.getAttribute('background'); if (bi) t.push('bi:' + bi);
  if (el.tagName === 'IMG') t.push('img:' + (el.getAttribute('src') || ''));
  return t;
};
const tokens = el => [...tokOne(el), ...[...(el ? el.children : [])].flatMap(tokOne)];
"""
JS_SNAP = "() => {" + JS_TOK + """
  const legend = {};
  for (const lb of ['空車','混雑','満車','期間外']) {
    const e = [...document.querySelectorAll('td,th,span,div,li')].find(x =>
      x.textContent.trim() === lb && ![...x.children].some(c => c.textContent.trim() === lb));
    let tk = [];
    if (e) tk = tokens(e.previousElementSibling);
    if (e && !tk.length) tk = tokens(e);
    if (e && !tk.length) tk = tokens(e.parentElement);
    legend[lb] = tk;
  }
  const hdr = document.querySelector('img[src*="public_month"]');
  const after = n => !hdr || (hdr.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING);
  const isNum = td => /^\\d{1,2}$/.test(td.innerText.trim());
  const grid = [...document.querySelectorAll('table')].filter(t => !t.querySelector('table'))
    .find(t => after(t) && [...t.querySelectorAll('td')].filter(isNum).length >= 28);
  const arrow = [...document.querySelectorAll('img[src*="arrow_r"]')].find(after);
  let month = null;
  for (let e = arrow, i = 0; e && i < 6 && month === null; i++, e = e.parentElement) {
    const m = e.innerText && e.innerText.match(/(\\d{1,2})\\s*月/); if (m) month = +m[1];
  }
  const cells = grid ? [...grid.querySelectorAll('td')].filter(isNum).map(td =>
    ({n: +td.innerText.trim(), tok: tokens(td), html: td.outerHTML.slice(0, 200)})) : [];
  return {legend, month, cells, gridHtml: grid ? grid.outerHTML.slice(0, 1500) : null};
}
"""
JS_NEXT = "() => {" + JS_TOK + """
  const hdr = document.querySelector('img[src*="public_month"]');
  const after = n => !hdr || (hdr.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING);
  const arrow = [...document.querySelectorAll('img[src*="arrow_r"]')].find(after);
  if (!arrow) return false;
  (arrow.closest('a') || arrow).click(); return true;
}
"""

def build_map(legend):
    cnt = Counter(t for toks in legend.values() for t in set(toks))
    return {t: lb for lb, toks in legend.items() for t in toks if cnt[t] == 1}

def status_of(tok, tokmap):
    hits = {tokmap[t] for t in tok if t in tokmap}
    return hits.pop() if len(hits) == 1 else "不明"

def infer_year(month, today):
    return min((today.year - 1, today.year, today.year + 1),
               key=lambda y: abs((dt.date(y, month, 15) - today).days))

def month_cells(cells, month, year):
    out, ones = [], 0
    for c in cells:
        if c["n"] == 1: ones += 1
        if ones == 1:
            try: out.append((dt.date(year, month, c["n"]), c))
            except ValueError: pass
    return out
def scrape():
    found = {}
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(locale="ja-JP")
        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(1500)
        (DEBUG / "page.html").write_text(page.content(), encoding="utf-8")
        for step in range(2):
            s = page.evaluate(JS_SNAP)
            if step == 0:
                tokmap = build_map(s["legend"])
                print("legend:", json.dumps(s["legend"], ensure_ascii=False))
                print("tokmap:", json.dumps(tokmap, ensure_ascii=False))
            print("month:", s["month"], "cells:", len(s["cells"]))
            if not s["month"] or not s["cells"]:
                print("grid:", s["gridHtml"]); break
            year = infer_year(s["month"], today)
            for d, c in month_cells(s["cells"], s["month"], year):
                if d in TARGETS:
                    found[str(d)] = status_of(c["tok"], tokmap)
                    print(d, c["tok"], "->", found[str(d)])
            if step == 0:
                try:
                    if not page.evaluate(JS_NEXT): break
                    page.wait_for_load_state("networkidle")
                except Exception as e:
                    print("next failed:", e); break
                page.wait_for_timeout(1500)
        b.close()
    return found

def notify(msg, title="P5駐車場"):
    print(msg)
    if NTFY_TOPIC:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"),
                      headers={"Title": title.encode("utf-8"), "Priority": "high", "Click": URL}, timeout=20)

def main():
    prev = json.loads(STATE.read_text()) if STATE.exists() else {}
    found = scrape()
    print(json.dumps(found, ensure_ascii=False))
    known = {k: v for k, v in found.items() if v in ("空車", "混雑", "満車")}
    new_state = {"status": found, "parse_warned": prev.get("parse_warned", False)}
    if not known:
        if not prev.get("parse_warned"):
            notify("P5のカレンダーを読み取れませんでした。ログの確認が必要です。", "P5監視:要調整")
        new_state["parse_warned"] = True
    else:
        old = prev.get("status", {})
        hits = sorted(k for k, v in found.items() if v in ("空車", "混雑") and old.get(k) != v)
        if hits:
            notify("\n".join("P5 " + k[5:].replace("-", "/") + " → " + found[k] for k in hits), "P5駐車場 空きあり!")
        new_state["parse_warned"] = False
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(new_state, ensure_ascii=False))

if __name__ == "__main__":
    main()

