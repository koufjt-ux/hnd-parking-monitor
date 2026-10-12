"""羽田空港 P2/P3 駐車場の空車監視 (10/24〜11/1)。空車が出たら ntfy でiPhoneに通知。"""
import json, os, re, datetime as dt
from collections import Counter
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright

URL = os.environ.get("TARGET_URL", "https://hnd-rsv.aeif.or.jp/airport2/app/toppage")
START, END = dt.date(2026, 10, 24), dt.date(2026, 11, 1)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
ALERT_ON_BUSY = os.environ.get("ALERT_ON_BUSY", "false").lower() == "true"
STATE = Path("state/state.json")
DEBUG = Path("debug"); DEBUG.mkdir(exist_ok=True)
DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")

JS_LEGEND = """
() => {
  const out = {};
  for (const lb of ['空車','混雑','満車','期間外']) {
    const tiers = [new Set(), new Set(), new Set()];
    const add = (s, n) => { if (n && typeof n.className === 'string') n.className.split(/\\s+/).filter(Boolean).forEach(t => s.add(t)); };
    const els = [...document.querySelectorAll('body *')].filter(e =>
      !e.closest('table') && e.textContent.trim() === lb &&
      ![...e.children].some(c => c.textContent.trim() === lb));
    for (const e of els) {
      add(tiers[0], e); [...e.children].forEach(c => add(tiers[0], c));
      add(tiers[1], e.parentElement);
      add(tiers[2], e.previousElementSibling);
    }
    out[lb] = [...(tiers.find(s => s.size) || [])];
  }
  return out;
}
"""
JS_CELLS = """
(idx) => {
  const t = document.querySelectorAll('table')[idx];
  if (!t) return null;
  let block = t;
  while (block.parentElement && block.parentElement.querySelectorAll('table').length === 1) block = block.parentElement;
  let title = '';
  for (let e = block; e && !title; e = e.previousElementSibling) {
    const img = e.matches && e.matches('img') ? e : (e.querySelector && e.querySelector('img[alt*="カレンダー"]'));
    if (img) title = img.alt;
  }
  const cells = [...t.querySelectorAll('td[id]')].map(td => ({
    id: td.id, cls: td.className || '', html: td.outerHTML.slice(0, 300)}));
  return {tid: t.id, title, cells};
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

def classify(s):
    for w in ("満車", "混雑", "空車", "期間外"):
        if w in s: return w
    return "不明"

def build_map(legend):
    cnt = Counter(t for toks in legend.values() for t in set(toks))
    return {t: lb for lb, toks in legend.items() for t in toks if cnt[t] == 1}

  NAME_MAP = {"ok": "空車", "konzatsu": "混雑", "full": "満車", "no": "期間外"}

def cell_status(cls, html, tokmap):
    toks = cls.split()
    hits = {tokmap[t] for t in toks if t in tokmap} | {NAME_MAP[t] for t in toks if t in NAME_MAP}

    if len(hits) == 1: return hits.pop()
    k = classify(html)
    if k != "不明": return k
    return "なし" if not cls.strip() else "不明"
def scrape():
    found, info, classes = {}, [], Counter()
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(locale="ja-JP")
        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(1500)
        (DEBUG / "page.html").write_text(page.content(), encoding="utf-8")
        legend = page.evaluate(JS_LEGEND)
        tokmap = build_map(legend)
        print("legend:", json.dumps(legend, ensure_ascii=False))
        print("tokmap:", json.dumps(tokmap, ensure_ascii=False))
        n = page.evaluate("document.querySelectorAll('table').length")
        for i in range(n):
            for step in range(2):
                d = page.evaluate(JS_CELLS, i)
                if not d: break
                for c in d["cells"]:
                    m = DATE_RE.search(c["id"])
                    if not m: continue
                    day = dt.date(int(m[1]), int(m[2]), int(m[3]))
                    classes[c["cls"]] += 1
                    if START <= day <= END:
                        label = (d["title"] or d["tid"] or str(i)) + "[" + c["id"][:m.start()].rstrip("-") + "]"
                        st = cell_status(c["cls"], c["html"], tokmap)
                        found[label + "|" + str(day)] = st
                        info.append(f'{day} {label} class="{c["cls"]}" -> {st}')
                if step == 0 and not page.evaluate(JS_NEXT, i): break
                page.wait_for_timeout(700)
        b.close()
    print("classes:", json.dumps(dict(classes), ensure_ascii=False))
    print("\n".join(sorted(set(info))))
    return found

def notify(msg, title="羽田駐車場"):
    print(msg)
    if NTFY_TOPIC:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"),
                      headers={"Title": title.encode("utf-8"), "Priority": "high",
                               "Click": URL}, timeout=20)

def main():
    prev = json.loads(STATE.read_text()) if STATE.exists() else {}
      found = scrape()
    if os.environ.get("INCLUDE_DISABLED", "false").lower() != "true":
        found = {k: v for k, v in found.items() if "身障者" not in k}

    known = {k: v for k, v in found.items() if v in ("空車", "混雑", "満車")}
    new_state = {"status": found, "parse_warned": prev.get("parse_warned", False)}

    if not known:
        if not prev.get("parse_warned"):
            notify("カレンダーの状態を読み取れませんでした。ログの確認が必要です。", "羽田監視:要調整")
        new_state["parse_warned"] = True
    else:
        targets = {"空車"} | ({"混雑"} if ALERT_ON_BUSY else set())
        old = prev.get("status", {})
        hits = [k for k, v in found.items() if v in targets and old.get(k) != v]
        if hits:
            lines = [k.split("|")[1] + " " + k.split("|")[0] + " → " + found[k]
                     for k in sorted(hits, key=lambda x: x.split("|")[1])]
            notify("\n".join(lines), "羽田駐車場 空きあり!")
        new_state["parse_warned"] = False

    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(new_state, ensure_ascii=False))

if __name__ == "__main__":
    main()
