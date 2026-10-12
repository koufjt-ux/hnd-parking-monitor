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
def scrape():
    found, seen_tables = {}, 0
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(locale="ja-JP")
        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(1500)
        (DEBUG / "page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG / "page.png"), full_page=True)
        n = page.evaluate("document.querySelectorAll('table').length")
        print(page.evaluate("() => JSON.stringify({tables: document.querySelectorAll('table').length, iframes: document.querySelectorAll('iframe').length, text: document.body.innerText.slice(0,800), cal: [...document.querySelectorAll('*')].filter(e=>e.children.length===0 && /^\\d{1,2}$/.test(e.innerText.trim())).slice(10,14).map(e=>e.parentElement.parentElement.outerHTML.slice(0,600))}, null, 1)"))
        for i in range(n):
            for step in range(2):
                d = page.evaluate(JS_SCRAPE, i)
                if not d: break
                (DEBUG / f"table{i}_{step}.html").write_text(d["html"], encoding="utf-8")
                if d["year"] and d["month"]:
                    seen_tables += 1
                    for c in d["cells"]:
                        m = re.match(r"\s*(\d{1,2})", c["text"])
                        if not m: continue
                        try: day = dt.date(d["year"], d["month"], int(m.group(1)))
                        except ValueError: continue
                        if START <= day <= END:
                            found[f'{d["label"] or "table"+str(i)}|{day}'] = classify(c["sig"])
                if step == 0 and not page.evaluate(JS_NEXT, i): break
                page.wait_for_timeout(700)
        b.close()
    return found, seen_tables

def notify(msg, title="羽田駐車場"):
    print(msg)
    if NTFY_TOPIC:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"),
                      headers={"Title": title.encode("utf-8"), "Priority": "high",
                               "Click": URL}, timeout=20)

def main():
    prev = json.loads(STATE.read_text()) if STATE.exists() else {}
    found, tables = scrape()
    print(json.dumps(found, ensure_ascii=False, indent=1))
    known = {k: v for k, v in found.items() if v != "不明"}
    new_state = {"status": found, "parse_warned": prev.get("parse_warned", False)}

    if not known:
        if not prev.get("parse_warned"):
            notify("カレンダーの状態を読み取れませんでした。debugログの確認が必要です。", "羽田監視:要調整")
        new_state["parse_warned"] = True
    else:
        targets = {"空車"} | ({"混雑"} if ALERT_ON_BUSY else set())
        old = prev.get("status", {})
        hits = [k for k, v in found.items() if v in targets and old.get(k) != v]
        if hits:
            lines = [f'{k.split("|")[1]} {k.split("|")[0]} → {found[k]}' for k in sorted(hits, key=lambda x: x.split("|")[1])]
            notify("\n".join(lines), "羽田駐車場 空きあり!")
        new_state["parse_warned"] = False

    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(new_state, ensure_ascii=False))

if __name__ == "__main__":
    main()

