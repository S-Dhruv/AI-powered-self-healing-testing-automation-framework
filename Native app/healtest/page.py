"""Page inspection: compact page view for the LLM, locator extraction, fingerprints, lookups.

Nothing in here knows about any particular application.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from selenium.webdriver.common.by import By

logger = logging.getLogger(__name__)

LOCATOR_ORDER = ["id", "testid", "css", "xpath", "role_and_name", "label", "text"]

_HELPERS = r"""
const norm = s => (s || '').replace(/\s+/g, ' ').trim();
const isVisible = el => {
  // Hidden by a transparent or invisible ancestor (e.g. a hover-only overlay) counts as not visible.
  if (el.checkVisibility && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true, checkOpacity: true, checkVisibilityCSS: true })) return false;
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  // Clipped away by an ancestor with overflow hidden/clip (hover-only overlays are hidden this way).
  for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
    const o = getComputedStyle(p);
    if (['hidden', 'clip'].includes(o.overflowX) || ['hidden', 'clip'].includes(o.overflowY)) {
      const pr = p.getBoundingClientRect();
      if (pr.width <= 0 || pr.height <= 0 || r.right <= pr.left || r.left >= pr.right || r.bottom <= pr.top || r.top >= pr.bottom) return false;
    }
  }
  const cs = getComputedStyle(el);
  return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) !== 0;
};
const isFile = el => el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'file';
const usable = el => isVisible(el) || isFile(el);
const INTERACTIVE = 'a[href],a.btn,a[class*=btn],[class*=btn][data-product-id],[data-slide],[data-toggle],button,input:not([type=hidden]),select,textarea,summary,label[for],[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=switch],[role=option],[onclick]';
// A label only counts as its own target when the control it points to is not itself visible (custom-styled radios/checkboxes).
const hiddenControlLabel = el => el.tagName === 'LABEL' && el.control && !usable(el.control);
const wanted = el => el.tagName !== 'LABEL' || hiddenControlLabel(el);
// Anything the browser shows a pointer cursor for is clickable even without href/role/onclick (e.g. a cart's delete "X").
// Only the outermost pointer element of a clickable region is kept, so its children are not listed again.
const pointerOnly = el => {
  if (el.matches(INTERACTIVE) || el.closest(INTERACTIVE)) return false;
  if (getComputedStyle(el).cursor !== 'pointer') return false;
  const p = el.parentElement;
  return !(p && getComputedStyle(p).cursor === 'pointer');
};
const collect = () => {
  const base = Array.from(document.querySelectorAll(INTERACTIVE));
  const extra = Array.from(document.querySelectorAll('a,span,div,i,li,img,td,button')).filter(pointerOnly);
  return base.concat(extra).sort((a, b) => (a === b ? 0 : (a.compareDocumentPosition(b) & 4 ? -1 : 1)));
};
const ATTRS = ['id','name','type','placeholder','aria-label','href','data-qa','data-testid','data-test','data-cy','role','title','alt','for','value'];
const TESTID_ATTRS = ['data-qa','data-testid','data-test','data-cy'];
function roleOf(el) {
  const r = el.getAttribute('role'); if (r) return r;
  const tag = el.tagName.toLowerCase();
  if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
  if (tag === 'button') return 'button';
  if (tag === 'select') return 'combobox';
  if (tag === 'textarea') return 'textbox';
  if (tag === 'input') {
    const ty = (el.getAttribute('type') || 'text').toLowerCase();
    if (['button','submit','reset','image'].includes(ty)) return 'button';
    if (ty === 'checkbox') return 'checkbox';
    if (ty === 'radio') return 'radio';
    if (ty === 'file') return 'file';
    return 'textbox';
  }
  return 'generic';
}
function labelOf(el) {
  if (el.labels && el.labels.length) return norm(Array.from(el.labels).map(l => l.innerText).join(' '));
  const wrap = el.closest('label'); return wrap ? norm(wrap.innerText) : '';
}
function nameOf(el) {
  const al = el.getAttribute('aria-label'); if (al) return norm(al);
  const lb = el.getAttribute('aria-labelledby');
  if (lb) { const t = lb.split(/\s+/).map(i => { const n = document.getElementById(i); return n ? n.innerText : ''; }).join(' '); if (norm(t)) return norm(t); }
  const lab = labelOf(el); if (lab) return lab;
  const tag = el.tagName.toLowerCase();
  if (tag === 'input') {
    const ty = (el.getAttribute('type') || 'text').toLowerCase();
    if (['submit','button','reset'].includes(ty)) return norm(el.value || el.getAttribute('title') || '');
    return norm(el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '');
  }
  const t = norm(el.innerText || el.textContent || ''); if (t) return t.slice(0, 80);
  return norm(el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('placeholder') || '');
}
function attrsOf(el) {
  const o = {};
  for (const a of ATTRS) { if (a === 'value' && !['radio','checkbox','submit','button'].includes((el.getAttribute('type')||'').toLowerCase())) continue;
    const v = el.getAttribute(a); if (v !== null && v !== '' && v.length <= 120) o[a] = v; }
  return o;
}
function textOf(el) { return norm(el.innerText || '').slice(0, 80); }
function fpOf(el) {
  const r = el.getBoundingClientRect();
  return { tag: el.tagName.toLowerCase(), text: textOf(el), attrs: attrsOf(el), role: roleOf(el), name: nameOf(el),
           position: { x: Math.round(r.x + scrollX), y: Math.round(r.y + scrollY), w: Math.round(r.width), h: Math.round(r.height) } };
}
function evalX(x) {
  const r = document.evaluate(x, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
  const out = []; for (let i = 0; i < r.snapshotLength; i++) out.push(r.snapshotItem(i)); return out;
}
const q = s => '"' + s.replace(/"/g, "'") + '"';
"""

SNAPSHOT_JS = _HELPERS + r"""
const MAX = arguments[0];
document.querySelectorAll('[data-aitf-idx]').forEach(e => e.removeAttribute('data-aitf-idx'));
function ctxOf(el) {
  const parts = [];
  const form = el.closest('form');
  if (form) parts.push('form ' + (form.id || form.getAttribute('name') || form.getAttribute('action') || '').toString().slice(0, 40));
  let p = el.parentElement, hops = 0, near = '';
  while (p && hops < 6) {
    const h = p.querySelector('h1,h2,h3,h4,h5,legend');
    if (h && !h.contains(el)) { near = norm(h.innerText).slice(0, 40); break; }
    p = p.parentElement; hops++;
  }
  if (near) parts.push('under "' + near + '"');
  // The repeated item (product card, table row, list entry) the element belongs to: the nearest ancestor that adds
  // some text of its own but is still small, so "View Product" can be told apart as the one under "Blue Top".
  const own = norm(el.innerText || el.value || '').length;
  let anc = el.parentElement, up = 0;
  while (anc && up < 6 && anc !== document.body) {
    const t = norm(anc.innerText || '');
    if (t.length > 160) break;
    if (t.length >= own + 6) { parts.push('in "' + t.slice(0, 90) + '"'); break; }
    anc = anc.parentElement; up++;
  }
  const region = el.closest('header,nav,footer,aside,[role=dialog],.modal,[id*=footer],[id*=header]');
  if (region) parts.push('region ' + (region.id || region.tagName.toLowerCase() || '').toString().slice(0, 20));
  return parts.join('; ');
}
const seen = new Set(); const lines = []; let idx = 0, total = 0;
for (const el of collect()) {
  if (seen.has(el) || !usable(el) || !wanted(el)) continue; seen.add(el); total++;
  if (idx >= MAX) continue;
  el.setAttribute('data-aitf-idx', String(idx));
  const tag = el.tagName.toLowerCase(); const ty = (el.getAttribute('type') || '').toLowerCase();
  let line = idx + '|' + tag + (ty ? ':' + ty : '');
  const isField = ['input','select','textarea'].includes(tag);
  const txt = isField ? labelOf(el) : textOf(el).slice(0, 60);
  if (txt) line += '|text=' + q(txt);
  else {
    // Icon-only controls have no label: their own class and their icon's class are the best hint (cart_quantity_delete, fa-times)
    const cl = (el.getAttribute('class') || '').trim().slice(0, 50); if (cl) line += '|cls=' + cl;
    const ic = el.querySelector('i,svg,img');
    if (ic) { const icl = (ic.getAttribute('class') || ic.getAttribute('alt') || '').trim().slice(0, 40); if (icl) line += '|icon=' + icl; }
  }
  for (const [a, k] of [['id','id'],['name','name'],['placeholder','ph'],['aria-label','aria'],['href','href'],['data-qa','qa'],['data-testid','tid'],['data-test','tid'],['title','title'],['for','for'],['value','val']]) {
    let v = el.getAttribute(a); if (!v) continue;
    if (a === 'value' && !['radio','checkbox','submit','button'].includes(ty)) continue;
    if (v.length > 60) v = v.slice(0, 60); line += '|' + k + '=' + v;
  }
  const c = ctxOf(el); if (c) line += '|@' + c;
  lines.push(line); idx++;
}
return { lines: lines, total: total };
"""

LOCATORS_JS = _HELPERS + r"""
const el = arguments[0];
const tag = el.tagName.toLowerCase();
const uniqueVis = (nodes) => { const v = nodes.filter(usable); return v.length === 1 && v[0] === el; };
const out = {};
// id
if (el.id) { const n = Array.from(document.querySelectorAll('[id="' + el.id.replace(/"/g, '\\"') + '"]')); if (uniqueVis(n)) out.id = el.id; }
// test id
for (const a of TESTID_ATTRS) { const v = el.getAttribute(a); if (v) { const n = Array.from(document.querySelectorAll('[' + a + '="' + v.replace(/"/g, '\\"') + '"]')); if (uniqueVis(n)) { out.testid = { attr: a, value: v }; break; } } }
// css
const cssCands = [];
for (const a of ['name','placeholder','aria-label','href','title','alt','for','value']) {
  const v = el.getAttribute(a); if (v && v.length < 80 && !(a === 'value' && !['radio','checkbox','submit','button'].includes((el.getAttribute('type')||'').toLowerCase()))) cssCands.push(tag + '[' + a + '="' + v.replace(/"/g, '\\"') + '"]');
}
const ty = el.getAttribute('type'), nm = el.getAttribute('name');
if (ty && nm) cssCands.unshift(tag + '[type="' + ty + '"][name="' + nm + '"]');
for (const c of cssCands) { try { if (uniqueVis(Array.from(document.querySelectorAll(c)))) { out.css = c; break; } } catch (e) {} }
if (!out.css) {
  const segs = []; let n = el;
  while (n && n.nodeType === 1 && n !== document.body) {
    if (n.id && n !== el) { segs.unshift('#' + CSS.escape(n.id)); break; }
    let i = 1, s = n; while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) i++;
    segs.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + i + ')'); n = n.parentElement;
  }
  const c = segs.join(' > '); try { if (document.querySelectorAll(c)[0] === el) out.css = c; } catch (e) {}
}
// xpath
const text = textOf(el);
const xCands = [];
if (el.id) xCands.push('//' + tag + '[@id=' + q(el.id) + ']');
for (const a of TESTID_ATTRS.concat(['name','placeholder','aria-label','href','title','for'])) { const v = el.getAttribute(a); if (v && v.length < 80) xCands.push('//' + tag + '[@' + a + '=' + q(v) + ']'); }
if (text && text.length < 60) xCands.push('//' + tag + '[normalize-space(.)=' + q(text) + ']');
let bestPos = null;
for (const c of xCands) {
  let all; try { all = evalX(c); } catch (e) { continue; }
  const vis = all.filter(usable);
  if (vis.length === 1 && vis[0] === el) { out.xpath = c; break; }
  const at = all.indexOf(el);
  if (at >= 0 && (!bestPos || all.length < bestPos.n)) bestPos = { x: '(' + c + ')[' + (at + 1) + ']', n: all.length };
}
if (!out.xpath && bestPos) out.xpath = bestPos.x;
if (!out.xpath) {
  const segs = []; let n = el;
  while (n && n.nodeType === 1) { let i = 1, s = n; while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) i++; segs.unshift(n.tagName.toLowerCase() + '[' + i + ']'); n = n.parentElement; }
  out.xpath = '/' + segs.join('/');
}
// role + accessible name, label, text
const role = roleOf(el), name = nameOf(el);
if (name) out.role_and_name = { role: role, name: name };
const lab = labelOf(el); if (lab) out.label = lab;
if (text && ['a','button','summary'].includes(tag)) out.text = text;
return out;
"""

FIND_JS = _HELPERS + r"""
const strategy = arguments[0], value = arguments[1];
const all = Array.from(document.querySelectorAll(INTERACTIVE)).filter(e => usable(e) && wanted(e));
if (strategy === 'role_and_name') return all.filter(e => roleOf(e) === value.role && norm(nameOf(e)).toLowerCase() === norm(value.name).toLowerCase());
if (strategy === 'label') return all.filter(e => ['input','select','textarea'].includes(e.tagName.toLowerCase()) && labelOf(e).toLowerCase() === norm(value).toLowerCase());
if (strategy === 'text') return all.filter(e => textOf(e).toLowerCase() === norm(value).toLowerCase());
return [];
"""

CANDIDATES_JS = _HELPERS + r"""
document.querySelectorAll('[data-aitf-idx]').forEach(e => e.removeAttribute('data-aitf-idx'));
const out = []; let i = 0;
for (const el of document.querySelectorAll(arguments[0])) { if (!usable(el) || i >= 400) continue; el.setAttribute('data-aitf-idx', String(i)); out.push({ i: i, fp: fpOf(el) }); i++; }
return out;
"""

FINGERPRINT_JS = _HELPERS + "return fpOf(arguments[0]);"
CLEAN_JS = "document.querySelectorAll('[data-aitf-idx]').forEach(e => e.removeAttribute('data-aitf-idx'));"
SEEN_TEXT_JS = r"""
var text = document.body ? (document.body.innerText || '') : '';
var bad = Array.from(document.querySelectorAll('input:invalid, textarea:invalid, select:invalid'))
  .map(function (e) { return e.validationMessage; }).join(' ');
return text + ' ' + bad;
"""


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def similarity(stored: Dict[str, Any], current: Dict[str, Any]) -> float:
    """Score 0..1 of how well `current` matches a stored fingerprint. Tag must match."""
    if not stored or stored.get("tag") != current.get("tag"):
        return 0.0
    score = weight = 0.0
    if stored.get("text"):
        weight += 2
        a, b = _norm(stored["text"]), _norm(current.get("text"))
        if a == b:
            score += 2
        elif a and b and (a in b or b in a):
            score += 1
    for key, value in (stored.get("attrs") or {}).items():
        w = 1.5 if key in ("id", "name", "data-qa", "data-testid", "data-test", "data-cy") else 1.0
        weight += w
        if (current.get("attrs") or {}).get(key) == value:
            score += w
    return score / weight if weight else 1.0


def summarize_fingerprint(fp: Optional[Dict[str, Any]]) -> str:
    if not fp:
        return "?"
    attrs = fp.get("attrs") or {}
    keep = [f"{k}={attrs[k]}" for k in ("id", "name", "type", "placeholder", "href", "data-qa", "data-testid") if k in attrs]
    text = f' "{fp["text"][:30]}"' if fp.get("text") else ""
    return f"<{fp.get('tag')}{text} {' '.join(keep)}>".replace("  ", " ")


class PageTools:
    def __init__(self, driver):
        self.driver = driver

    def snapshot(self, max_elements: int = 300) -> Tuple[List[str], int]:
        result = self.driver.execute_script(SNAPSHOT_JS, max_elements)
        return result["lines"], result["total"]

    def marked_element(self, index: int):
        found = self.driver.find_elements(By.CSS_SELECTOR, f'[data-aitf-idx="{int(index)}"]')
        return found[0] if found else None

    def clean_markers(self):
        try:
            self.driver.execute_script(CLEAN_JS)
        except Exception:
            pass

    def locators_for(self, element) -> Dict[str, Any]:
        return self.driver.execute_script(LOCATORS_JS, element)

    def fingerprint_of(self, element) -> Dict[str, Any]:
        return self.driver.execute_script(FINGERPRINT_JS, element)

    def find(self, strategy: str, value: Any) -> List[Any]:
        """Visible elements matched by one stored locator strategy."""
        if not value:
            return []
        try:
            if strategy == "id":
                found = self.driver.find_elements(By.CSS_SELECTOR, f'[id="{value}"]')
            elif strategy == "testid":
                found = self.driver.find_elements(By.CSS_SELECTOR, f'[{value["attr"]}="{value["value"]}"]')
            elif strategy == "css":
                found = self.driver.find_elements(By.CSS_SELECTOR, value)
            elif strategy == "xpath":
                found = self.driver.find_elements(By.XPATH, value)
            else:
                return list(self.driver.execute_script(FIND_JS, strategy, value))
        except Exception as exc:
            logger.debug("locator %s failed: %s", strategy, str(exc)[:80])
            return []
        visible = []
        for el in found:
            try:
                if el.is_displayed() or (el.tag_name.lower() == "input" and (el.get_attribute("type") or "") == "file"):
                    visible.append(el)
            except Exception:
                continue
        return visible

    def candidates(self, tag: str) -> List[Tuple[Any, Dict[str, Any]]]:
        rows = self.driver.execute_script(CANDIDATES_JS, tag)
        pairs = []
        for row in rows:
            el = self.marked_element(row["i"])
            if el is not None:
                pairs.append((el, row["fp"]))
        return pairs

    def seen_text(self) -> str:
        return self.driver.execute_script(SEEN_TEXT_JS) or ""
