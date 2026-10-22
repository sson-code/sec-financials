#!/usr/bin/env python3
"""
sec_statements.py — as-reported Balance Sheet, Income Statement and Cash Flow
Statement built from one SEC filing, using only SEC endpoints.

  ticker -> CIK      https://www.sec.gov/files/company_tickers.json
  filing list        https://data.sec.gov/submissions/CIK##########.json
  filing folder      https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json
  which roles are    FilingSummary.xml   (SEC renderer's own index of statements)
    statements
  blueprint          *_pre.xml           (presentation linkbase: lines, order, nesting)
  captions           *_lab.xml           (the filer's own line labels)
  math               *_cal.xml           (calculation linkbase, used to validate)
  values             *_htm.xml           (XBRL instance extracted from the iXBRL doc)
  history (opt.)     https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json

Usage
  pip install requests pandas
  export SEC_USER_AGENT="Your Name you@company.com"
  python sec_statements.py AAPL --form 10-K
  python sec_statements.py MSFT --form 10-Q --nth 1 --out ./msft --history
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
import xml.etree.ElementTree as ET

import pandas as pd
import requests

NS = {
    "link": "http://www.xbrl.org/2003/linkbase",
    "xlink": "http://www.w3.org/1999/xlink",
    "xbrli": "http://www.xbrl.org/2003/instance",
    "xbrldi": "http://xbrl.org/2006/xbrldi",
}
LINK = "{%s}" % NS["link"]
XL = "{%s}" % NS["xlink"]
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
STD_LABEL = "http://www.xbrl.org/2003/role/label"
STRUCTURAL = ("Axis", "Domain", "Member")   # dimensional scaffolding: skip subtree
CONTAINERS = ("Table", "LineItems")          # hypercube wrappers: walk through, don't print
CORE = ("balance_sheet", "income_statement", "comprehensive_income", "cash_flow")
# ConsolidationItemsAxis grouping members — qualifiers, not actual segments.
# They should only match 1-dim contexts (their own standalone total).
# When paired with a geography/segment axis they belong to sub-rows, not top-level rows.
CONSOLIDATION_MEMBERS = {
    "OperatingSegmentsMember", "CorporateNonSegmentMember",
    "IntersegmentEliminationMember", "ConsolidationEliminationsMember",
    "MaterialReconcilingItemsMember", "AllOtherSegmentsMember",
    "ReportableSegmentsMember", "AllReportableSegmentsMember",
}


# --------------------------------------------------------------------------- HTTP
class SEC:
    """Thin client honoring SEC fair-access rules: declared UA, <= 10 requests/sec."""

    def __init__(self, user_agent: str, min_interval: float = 0.11):
        if not user_agent or "@" not in user_agent:
            sys.exit("Set SEC_USER_AGENT to 'Name email@domain' (SEC rejects anonymous clients).")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.min_interval = min_interval
        self._last = 0.0

    def get(self, url: str) -> requests.Response:
        for attempt in range(5):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            r = self.s.get(url, timeout=30)
            self._last = time.monotonic()
            if r.status_code in (429, 503) or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r

    def json(self, url: str):
        return self.get(url).json()

    def xml(self, url: str) -> ET.Element:
        return ET.fromstring(self.get(url).content)


# ---------------------------------------------------------------------- discovery
def ticker_to_cik(sec: SEC, ticker: str) -> tuple[str, str]:
    """Resolve a ticker symbol OR CIK number to (zero-padded CIK, company name).

    - Ticker (e.g. "AAPL"): looked up in SEC company_tickers.json (exchange-listed only)
    - CIK (e.g. "1803498" or "0001803498"): all-digit input bypasses the ticker map,
      enabling access to non-traded entities (BDCs, interval funds, closed-end funds).
    """
    t = ticker.strip()

    # CIK input: 1–10 digits (with or without leading zeros)
    if re.match(r"^\d{1,10}$", t):
        cik = f"{int(t):010d}"
        try:
            sub = sec.json(f"https://data.sec.gov/submissions/CIK{cik}.json")
            name = sub.get("name", f"CIK {cik}")
            return cik, name
        except Exception:
            raise ValueError(f"CIK {cik} not found in SEC EDGAR")

    # Ticker lookup
    data = sec.json("https://www.sec.gov/files/company_tickers.json")
    tu = t.upper()
    for row in data.values():
        if row["ticker"].upper() == tu:
            return f'{int(row["cik_str"]):010d}', row["title"]
    raise ValueError(
        f"Ticker '{ticker}' not found in SEC ticker file. "
        f"For companies without a ticker (BDCs, interval funds, closed-end funds), "
        f"pass the CIK number directly (e.g. '1803498' for BCRED)."
    )


def list_filings(sec: SEC, cik: str, form: str) -> list[dict]:
    """Newest-first XBRL filings of one form type (recent block only, ~1000 filings)."""
    r = sec.json(f"https://data.sec.gov/submissions/CIK{cik}.json")["filings"]["recent"]
    out = []
    for i, f in enumerate(r["form"]):
        if f == form and r["isXBRL"][i]:
            out.append({
                "form": f,
                "accession": r["accessionNumber"][i],
                "filed": r["filingDate"][i],
                "period": r["reportDate"][i],
                "primary_doc": r["primaryDocument"][i],
            })
    return out


@dataclass
class FilingFiles:
    base: str
    pre: str | None = None
    lab: str | None = None
    cal: str | None = None
    instance: str | None = None
    summary: str | None = None
    schema: str | None = None


def locate_files(sec: SEC, cik: str, accession: str) -> FilingFiles:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
    names = [i["name"] for i in sec.json(base + "index.json")["directory"]["item"]]
    f, other_xml = FilingFiles(base), []
    for n in names:
        low = n.lower()
        if low.endswith("_pre.xml"):
            f.pre = base + n
        elif low.endswith("_lab.xml"):
            f.lab = base + n
        elif low.endswith("_cal.xml"):
            f.cal = base + n
        elif low.endswith("_def.xml"):
            continue
        elif low == "filingsummary.xml":
            f.summary = base + n
        elif low.endswith(".xsd"):
            f.schema = base + n
        elif low.endswith("_htm.xml"):
            f.instance = base + n          # instance extracted from inline XBRL
        elif low.endswith(".xml"):
            other_xml.append(n)
    if not f.instance:                     # pre-iXBRL filings: e.g. aapl-20180929.xml
        cands = [n for n in other_xml if re.match(r"^[a-z0-9\-]+-\d{8}\.xml$", n.lower())]
        if cands:
            f.instance = base + cands[0]
    return f


# ------------------------------------------------------------- statement roles
def classify(name: str) -> str | None:
    n = name.lower()
    if "parenthetical" in n:
        return None
    if re.search(r"balance sheet|financial position|financial condition", n):
        return "balance_sheet"
    if "cash flow" in n:
        return "cash_flow"
    if re.search(r"equity|stockholders|shareholders|partners.? capital|members.? capital", n) \
            and not re.search(r"operations|earnings|statements? of (income|comprehensive)", n):
        return "equity"
    if re.search(r"operations|earnings|income|loss", n):
        if "comprehensive" in n and not re.search(r"operations|earnings|income and|loss and", n):
            return "comprehensive_income"
        return "income_statement"
    return None


def statement_roles(summary: ET.Element) -> dict[str, dict]:
    """Use the SEC renderer's own classification of which roles are 'Statements'."""
    found: dict[str, dict] = {}
    for rep in summary.iter("Report"):
        cat = (rep.findtext("MenuCategory") or "").strip().lower()
        long_name = rep.findtext("LongName") or ""
        if cat != "statements" and " - statement - " not in long_name.lower():
            continue
        title = (rep.findtext("ShortName") or long_name).strip()
        kind = classify(title)
        role = rep.findtext("Role")
        if kind and role and kind not in found:
            found[kind] = {"role": role.strip(), "title": title,
                           "r_file": rep.findtext("HtmlFileName")}
    return found


# ------------------------------------------------------------------- linkbases
def _concept(href: str) -> str:
    frag = href.split("#", 1)[1]
    return frag.replace("_", ":", 1) if "_" in frag else frag


def parse_labels(root: ET.Element) -> dict[str, dict[str, str]]:
    labels: dict[str, dict[str, str]] = defaultdict(dict)
    for ll in root.iter(LINK + "labelLink"):
        locs, res = {}, defaultdict(list)
        for el in ll:
            t = el.get(XL + "type")
            if t == "locator":
                locs[el.get(XL + "label")] = _concept(el.get(XL + "href"))
            elif t == "resource" and el.get(XML_LANG, "en").lower().startswith("en"):
                res[el.get(XL + "label")].append((el.get(XL + "role", STD_LABEL),
                                                  (el.text or "").strip()))
        for el in ll:
            if el.get(XL + "type") == "arc" and el.get(XL + "from") in locs:
                c = locs[el.get(XL + "from")]
                for role, text in res.get(el.get(XL + "to"), []):
                    labels[c][role] = text
    return labels


def role_arcs(root: ET.Element, link_tag: str, arc_tag: str, role: str):
    """(from_concept, to_concept, arc_element) for every extended link with this role."""
    for lk in root.iter(LINK + link_tag):
        if lk.get(XL + "role") != role:
            continue
        locs = {el.get(XL + "label"): _concept(el.get(XL + "href"))
                for el in lk if el.get(XL + "type") == "locator"}
        for el in lk:
            if el.tag == LINK + arc_tag:
                a, b = locs.get(el.get(XL + "from")), locs.get(el.get(XL + "to"))
                if a and b:
                    yield a, b, el


@dataclass
class Line:
    concept: str
    depth: int
    preferred_label: str | None
    abstract: bool


def statement_lines(pre: ET.Element, role: str) -> tuple[list[Line], set[str], dict, set]:
    """Walk the presentation tree for one role in filed order.
    Returns (lines, axes, axis_members, leaf_set)."""
    children, is_child, parents = defaultdict(list), set(), []
    for frm, to, arc in role_arcs(pre, "presentationLink", "presentationArc", role):
        children[frm].append((float(arc.get("order", "0")), to, arc.get("preferredLabel")))
        is_child.add(to)
        if frm not in parents:
            parents.append(frm)
    roots = [p for p in parents if p not in is_child]
    lines, axes, axis_members, leaf_set = [], set(), {}, set()

    def _postorder(node, result, leaves):
        """Collect dimensional member nodes in post-order; mark leaves.
        Handles any member naming — including srt:US, srt:CN, country codes, etc."""
        local = node.split(":")[-1]
        is_axis = local.endswith("Axis")
        is_domain = local.endswith("Domain")
        # Traverse ALL children in the dimensional subtree (not just "Member"/"Domain")
        sub = [ch for _, ch, _ in sorted(children.get(node, []), key=lambda x: x[0])]
        for ch in sub:
            _postorder(ch, result, leaves)
        # Emit anything that is not the Axis or Domain node itself
        if not is_axis and not is_domain:
            result.append(node)
            member_children = [ch for ch in sub
                               if not ch.split(":")[-1].endswith(("Axis", "Domain"))]
            if not member_children:
                leaves.add(node)

    def walk(c, depth, pref, path):
        local = c.split(":")[-1]
        if local.endswith(STRUCTURAL):
            if local.endswith("Axis"):
                axes.add(c)
                members, leaves = [], set()
                _postorder(c, members, leaves)
                axis_members[c] = members
                leaf_set.update(leaves)
            return
        emit = not local.endswith(CONTAINERS)
        if emit:
            lines.append(Line(c, depth, pref, local.endswith("Abstract")))
        for _, ch, p in sorted(children.get(c, []), key=lambda x: x[0]):
            if ch not in path:
                walk(ch, depth + (1 if emit else 0), p, path | {ch})

    for r in roots:
        walk(r, 0, None, {r})
    return lines, axes, axis_members, leaf_set


# -------------------------------------------------------------------- instance
@dataclass
class Ctx:
    start: date | None
    end: date
    dims: tuple


def _d(s: str) -> date:
    return date.fromisoformat(s.strip()[:10])


class FactStore:
    def __init__(self):
        self.by_local = defaultdict(list)

    def add(self, fact: dict):
        self.by_local[fact["qname"].split(":")[-1]].append(fact)

    def get(self, concept: str) -> list[dict]:
        cands = self.by_local.get(concept.split(":")[-1], [])
        exact = [f for f in cands if f["qname"] == concept]
        return exact or cands   # fall back to local name if prefixes differ


def parse_instance(raw: bytes) -> tuple[dict[str, Ctx], FactStore]:
    prefixes = {uri: p for p, uri in re.findall(rb'xmlns:([\w\-.]+)="([^"]+)"', raw[:200_000])}
    prefixes = {u.decode(): p.decode() for u, p in prefixes.items()}
    root = ET.fromstring(raw)

    ctxs: dict[str, Ctx] = {}
    for c in root.iter("{%s}context" % NS["xbrli"]):
        p = c.find("xbrli:period", NS)
        dims = tuple(sorted(
            (m.get("dimension"), "".join(m.itertext()).strip())
            for m in c.iter()
            if m.tag in ("{%s}explicitMember" % NS["xbrldi"], "{%s}typedMember" % NS["xbrldi"])))
        inst = p.findtext("xbrli:instant", namespaces=NS)
        if inst:
            ctxs[c.get("id")] = Ctx(None, _d(inst), dims)
        else:
            ctxs[c.get("id")] = Ctx(_d(p.findtext("xbrli:startDate", namespaces=NS)),
                                    _d(p.findtext("xbrli:endDate", namespaces=NS)), dims)

    store = FactStore()
    for el in root:
        if el.get("contextRef") is None or not el.tag.startswith("{"):
            continue
        uri, local = el.tag[1:].split("}", 1)
        value = None
        if el.get(XSI_NIL) != "true":
            text = (el.text or "").strip()
            if el.get("unitRef"):
                try:
                    value = float(text)
                except ValueError:
                    value = None
            else:
                value = text
        store.add({"qname": f"{prefixes.get(uri, uri)}:{local}", "ctx": el.get("contextRef"),
                   "unit": el.get("unitRef"), "decimals": el.get("decimals"), "value": value})
    return ctxs, store


def nondim_index(store: FactStore, concept: str, ctxs: dict[str, Ctx]) -> dict:
    """{(start, end): fact} for the concept's facts with no dimensions (the face-statement value)."""
    idx = {}
    for f in store.get(concept):
        cx = ctxs.get(f["ctx"])
        if cx is None or cx.dims or not isinstance(f["value"], float):
            continue
        idx.setdefault((cx.start, cx.end), f)
    return idx


def dim_index(store: FactStore, concept: str, ctxs: dict[str, Ctx],
              axis: str, member: str, require_sole_dim: bool = False) -> dict:
    """{(start,end): fact} for concept where the context dims include (axis, member).

    Handles multi-dimensional XBRL contexts (e.g. IBM segments tagged with both
    ConsolidationItemsAxis=OperatingSegmentsMember AND StatementBusinessSegmentsAxis=SoftwareMember).
    When multiple facts match the same period, prefers the one with fewer total dimensions
    so that aggregate rows (1-dim) don't collide with individual segment rows (2-dim).

    require_sole_dim: when True, only accept contexts where (axis, member) is the sole
    dimension. Used for ConsolidationItemsAxis grouping members (OperatingSegmentsMember
    etc.) that are qualifiers, not actual segments — prevents them from inheriting values
    from paired geography/segment contexts.
    """
    axis_local = axis.split(":")[-1]
    member_local = member.split(":")[-1]
    idx: dict = {}
    for f in store.get(concept):
        cx = ctxs.get(f["ctx"])
        if cx is None or not cx.dims or not isinstance(f["value"], float):
            continue
        if not any(d[0].split(":")[-1] == axis_local and d[1].split(":")[-1] == member_local
                   for d in cx.dims):
            continue
        if require_sole_dim and len(cx.dims) > 1:
            continue
        key = (cx.start, cx.end)
        # Prefer the fact whose context has the fewest dimensions
        if key not in idx or len(cx.dims) < len(ctxs[idx[key]["ctx"]].dims):
            idx[key] = f
    return idx


# ------------------------------------------------------------------- statements
def _lookup(idx: dict, col: tuple, pref: str | None):
    f = idx.get(col)
    if f is None and col[0] is not None:   # duration column but instant concept (cash in CF)
        if pref and "periodstart" in pref.lower():
            f = idx.get((None, col[0] - timedelta(days=1))) or idx.get((None, col[0]))
        else:
            f = idx.get((None, col[1]))
    return f


def col_label(k: tuple) -> str:
    s, e = k
    if s is None:
        return e.isoformat()
    return f"{round((e - s).days / 30.44)}M ended {e.isoformat()}"


def col_label_pretty(col_str: str) -> str:
    """Convert internal col_label to a readable date format.

    12M  → YYYY-MM            (e.g. "2025-09")
    3M   → "3M 10–12/25"      (standalone quarter, no YTD)
    6M   → "6M YTD 1–6/26"   (cumulative, YTD prefix)
    9M   → "9M YTD 1–9/25"
    Cross-year: "3M 11/25–1/26"
    Plain date (from meta['period']) → YYYY-MM
    """
    m = re.match(r"(\d+)M ended (\d{4})-(\d{2})-\d{2}$", col_str)
    if m:
        dur = int(m.group(1))
        end_year, end_month = int(m.group(2)), int(m.group(3))

        if dur == 12:
            return f"{end_year}-{end_month:02d}"

        start_month = end_month - dur + 1
        start_year = end_year
        while start_month <= 0:
            start_month += 12
            start_year -= 1

        sy = str(start_year)[2:]  # 2-digit year
        ey = str(end_year)[2:]
        ytd = "YTD " if dur > 3 else ""

        return f"{ytd}{start_month}/{sy} - {end_month}/{ey} ({dur}M)"

    m2 = re.match(r"(\d{4})-(\d{2})-\d{2}$", col_str)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}"
    return col_str


def humanize(local: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", local)


_SHARE_RE = re.compile(r"(weighted.?average|number.?of).?(shares|units)|shares.?outstanding|shares.?issued", re.IGNORECASE)

def _is_share_concept(concept: str) -> bool:
    """True for share-count concepts (not EPS — those are per-share, not counts)."""
    local = concept.split(":")[-1] if ":" in concept else concept
    return bool(_SHARE_RE.search(local))


def build_statement(lines, store, ctxs, labels, kind, axis_members=None):
    duration = kind != "balance_sheet"
    if axis_members is None:
        axis_members = {}
    idxs = {l.concept: nondim_index(store, l.concept, ctxs) for l in lines if not l.abstract}

    counts = defaultdict(int)
    for idx in idxs.values():
        for k in idx:
            if (k[0] is not None) == duration:
                counts[k] += 1
    # Also count dimensional facts so their periods appear in cols
    for l in lines:
        if l.abstract:
            continue
        for axis, members in axis_members.items():
            for member in members:
                for k in dim_index(store, l.concept, ctxs, axis, member):
                    if (k[0] is not None) == duration:
                        counts[k] += 1
    if not counts:
        return pd.DataFrame(), []
    top = max(counts.values())
    cols = [k for k, v in counts.items() if v >= max(1, 0.25 * top)]
    cols.sort(key=lambda k: (k[1], -((k[1] - k[0]).days if k[0] else 0)), reverse=True)

    rows = []
    for n, l in enumerate(lines, 1):
        lab = labels.get(l.concept, {})
        pref = l.preferred_label
        label = (lab.get(pref) if pref else None) or lab.get(STD_LABEL) \
            or humanize(l.concept.split(":")[-1])
        negated = bool(pref and "negated" in pref.lower())
        base = {"line": n, "depth": l.depth, "concept": l.concept, "label": label,
                "abstract": l.abstract, "total": bool(pref and "total" in pref.lower()),
                "negated": negated, "preferred_label": pref.rsplit("/", 1)[-1] if pref else None,
                "dimension_member": ""}

        # Collect member rows first (needed to decide whether to drop the plain row)
        member_rows = []
        if not l.abstract:
            for axis, members in axis_members.items():
                for member in members:
                    sole = member.split(":")[-1] in CONSOLIDATION_MEMBERS
                    didx = dim_index(store, l.concept, ctxs, axis, member, require_sole_dim=sole)
                    if not any(_lookup(didx, c, pref) is not None for c in cols):
                        continue
                    mem_label = (labels.get(member, {}).get(STD_LABEL)
                                 or humanize(member.split(":")[-1]))
                    mrow = {**base, "label": mem_label, "depth": l.depth + 1,
                            "dimension_member": member, "abstract": False,
                            "total": False, "preferred_label": None}
                    for c in cols:
                        f = _lookup(didx, c, pref)
                        mrow[col_label(c)] = (-f["value"] if negated else f["value"]) if f else None
                    member_rows.append(mrow)

        # Plain (non-dimensional) row
        plain = {**base}
        all_empty = True
        for c in cols:
            v = None
            if not l.abstract:
                f = _lookup(idxs[l.concept], c, pref)
                if f is not None:
                    v = -f["value"] if negated else f["value"]
                    all_empty = False
            plain[col_label(c)] = v

        # Emit plain row first (drop only if non-abstract, all-empty, and member rows exist)
        if not l.abstract and all_empty and member_rows:
            pass  # drop plain — data lives entirely in member rows
        else:
            rows.append(plain)

        # Member rows follow their parent so breakdowns always appear directly below the total
        rows.extend(member_rows)

    return pd.DataFrame(rows), cols


def calc_checks(cal: ET.Element, role: str, store, ctxs, cols, kind,
                axis_members=None, leaf_set=None) -> list[dict]:
    """Does each parent equal sum(weight * child) as filed? Raw (un-negated) values."""
    if axis_members is None:
        axis_members = {}
    if leaf_set is None:
        leaf_set = set()
    rel = defaultdict(list)
    for frm, to, arc in role_arcs(cal, "calculationLink", "calculationArc", role):
        rel[frm].append((to, float(arc.get("weight", "1"))))
    out = []
    for parent, kids in rel.items():
        pidx = nondim_index(store, parent, ctxs)
        kidx = {k: nondim_index(store, k, ctxs) for k, _ in kids}
        for c in cols:
            pf = pidx.get(c)
            if pf is None:
                continue
            parts = []
            for k, w in kids:
                f = kidx[k].get(c)
                if f is None and leaf_set:
                    # Leaf-member fallback: sum leaf-member facts for this child
                    total = 0.0
                    found = False
                    for axis, members in axis_members.items():
                        for member in members:
                            if member not in leaf_set:
                                continue
                            df_val = _lookup(dim_index(store, k, ctxs, axis, member), c, None)
                            if df_val is not None:
                                total += df_val["value"]
                                found = True
                    if found:
                        f = {"value": total, "decimals": None}
                parts.append((f, w, k))
            present = [(f, w) for f, w, _ in parts if f is not None]
            if not present:
                continue
            computed = sum(f["value"] * w for f, w in present)
            unit = max([10 ** (-int(f["decimals"])) for f, _ in present + [(pf, 1)]
                        if f.get("decimals") not in (None, "INF")] or [0])
            tol = unit / 2 * (len(present) + 1)
            out.append({"statement": kind, "parent": parent, "period": col_label(c),
                        "reported": pf["value"], "computed": computed,
                        "diff": pf["value"] - computed,
                        "ok": abs(pf["value"] - computed) <= tol,
                        "missing_children": ",".join(k for f, _, k in parts if f is None)})
    return out


# ------------------------------------------------------------------- markdown
def _fmt_val(v) -> str:
    """Format a numeric value: decimals for small numbers, no -0."""
    if pd.isna(v):
        return ""
    if abs(v) < 1e-9:
        return "0"
    # Values < 1: ratios, small EPS (e.g. 0.14, 0.152) — show up to 4 decimal places
    if abs(v) < 1:
        return f"{v:.4f}".rstrip('0').rstrip('.')
    # Per-share / ratio values with fractional parts (1 ≤ abs < 10,000)
    if abs(v) < 10_000 and abs(v - round(v)) > 0.005:
        return f"{v:,.2f}"
    return f"{v:,.0f}"


def _role_title(kind: str) -> str:
    """Human-readable title from kind name, short_name, or role URI."""
    if kind.startswith("http"):
        # Fallback: split URI slug on CamelCase (shouldn't happen when short_name is used)
        slug = kind.rstrip("/").split("/")[-1]
        return re.sub(r"(?<!^)(?=[A-Z])", " ", slug)
    if "_" in kind:
        # CORE statement name like 'income_statement'
        return kind.replace("_", " ").title()
    # Filing short_name — already human-readable; just normalize ALL CAPS to title case
    if kind == kind.upper():
        return kind.title()
    return kind


def to_markdown(df: pd.DataFrame, kind: str, ticker: str = "",
                prefer_months: int = None, include_tags: bool = False) -> str:
    """Render a statement DataFrame as a markdown table.

    prefer_months: filter columns to a specific duration (3=quarterly, 12=annual).
    include_tags: show XBRL concept tags on each row (default False).
    Values scaled to millions ($M) when largest value >= 1,000,000.
    """
    if df.empty:
        return f"*No data for {kind}*\n"

    all_vals = [c for c in df.columns if c not in META and c != "label"]

    # Filter to preferred duration
    if prefer_months is not None:
        filtered = []
        for c in all_vals:
            m = re.match(r"(\d+)M ended", c)
            if m:
                if abs(int(m.group(1)) - prefer_months) <= 2:
                    filtered.append(c)
            else:
                filtered.append(c)
        all_vals = filtered or all_vals

    # Deduplicate same YYYY-MM labels
    seen_labels: dict = {}
    for c in all_vals:
        lbl = col_label_pretty(c)
        if lbl not in seen_labels:
            seen_labels[lbl] = c
        elif prefer_months is not None:
            m_new = re.match(r"(\d+)M ended", c)
            m_old = re.match(r"(\d+)M ended", seen_labels[lbl])
            if m_new and m_old:
                if abs(int(m_new.group(1)) - prefer_months) < abs(int(m_old.group(1)) - prefer_months):
                    seen_labels[lbl] = c
    vals = list(seen_labels.values())

    # If all columns share the same duration, drop the "NM " prefix — it's redundant.
    # Mixed durations (e.g. notes with 3M+6M): keep full label so "3M Apr–Jun" vs "6M YTD Jan–Jun" is clear.
    pretty_cols = [col_label_pretty(c) for c in vals]

    def _scale(v):
        if pd.isna(v):
            return ""
        return _fmt_val(v)

    title = _role_title(kind)
    header_prefix = f"{ticker} | " if ticker else ""
    lines = [f"### {header_prefix}{title}\n"]
    lines.append("| | " + " | ".join(pretty_cols) + " |")
    lines.append("|:---|" + "|".join("---:" for _ in vals) + "|")

    # Pre-scan: find member labels repeated across multiple concepts (e.g. "Reportable Segment")
    # Also collect which concepts have any dimension member rows (for consolidated label suffix).
    member_concept_counts: dict = {}
    concept_labels: dict = {}
    concepts_with_members: set = set()
    for _, r in df.iterrows():
        dim = r.get("dimension_member", "") or ""
        if not isinstance(dim, str):
            dim = ""
        concept = r.get("concept", "") or ""
        raw_label = r["label"] or humanize(concept.split(":")[-1])
        clean = re.sub(r'\s*\[Member\]$', '', raw_label, flags=re.IGNORECASE).strip()
        if dim and concept and not bool(r["abstract"]):
            member_concept_counts.setdefault(clean, set()).add(concept)
            concepts_with_members.add(concept)
        elif not dim and not bool(r["abstract"]) and concept:
            concept_labels[concept] = clean
    repeated_members = {m for m, concepts in member_concept_counts.items() if len(concepts) > 1}

    # plain_vals[concept] = val_key of the rendered plain row, used to dedup single-segment
    # member rows (when member values == plain values the breakdown is redundant).
    plain_vals: dict = {}

    for _, r in df.iterrows():
        depth = int(r["depth"])
        indent = "&nbsp;&nbsp;" * depth
        label = r["label"] or humanize((r.get("concept", "") or "").split(":")[-1])
        concept = r.get("concept", "") or ""
        dim = r.get("dimension_member", "") or ""
        if not isinstance(dim, str):
            dim = ""
        is_abstract = bool(r["abstract"])
        is_total = bool(r.get("total", False))
        all_empty = all(pd.isna(r[c]) for c in vals)

        if is_abstract and all_empty and depth == 0:
            continue

        # Dedup: plain rows appear first; skip a member row whose values are identical to
        # the plain row (single-segment: breakdown adds no information).
        if not is_abstract and concept and not all_empty:
            val_key = tuple(None if pd.isna(r[c]) else r[c] for c in vals)
            if not dim:
                plain_vals[concept] = val_key   # record plain row values
            elif plain_vals.get(concept) == val_key:
                continue                         # member = plain → redundant, skip

        # Strip [Member] suffix
        label = re.sub(r'\s*\[Member\]$', '', label, flags=re.IGNORECASE).strip()

        # Plain row for a concept that also has segment/member rows: mark as consolidated total
        if not dim and not is_abstract and concept in concepts_with_members:
            label = f"{label} (consolidated)"

        tag = f" `{concept}`" if (concept and include_tags) else ""

        if dim:
            if label in repeated_members:
                concept_lbl = concept_labels.get(concept, humanize(concept.split(":")[-1]))
                display = f"{label} — {concept_lbl}"
            else:
                display = label
            dim_tag = f" `{dim}`" if include_tags else ""
            cell_label = f"{indent}&nbsp;&nbsp;*{display}*{dim_tag}"
        elif is_abstract:
            cell_label = f"{indent}**{label}**"
        elif is_total:
            cell_label = f"{indent}**{label}**{tag}"
        else:
            cell_label = f"{indent}{label}{tag}"

        cells = []
        for c in vals:
            formatted = _scale(r[c])
            cells.append(f"**{formatted}**" if (is_total and formatted) else formatted)
        lines.append(f"| {cell_label} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def to_compact(df: pd.DataFrame, kind: str, ticker: str = "",
               prefer_months: int = None, include_tags: bool = False) -> str:
    """Token-efficient plain-text format for Claude consumption (~40% fewer tokens than markdown).

    prefer_months: if set, only include duration columns within ±2 months of this value.
                   Pass 3 for quarterly, 12 for annual. Instant-date columns (balance sheet)
                   are always included. When multiple columns have the same YYYY-MM label after
                   filtering, only the one closest to prefer_months is kept.

    Header line states the company, statement, periods, and unit.
    Each data row: INDENT Label: v1 | v2 | v3 [concept]
    Member rows:   INDENT   - Label: v1 | v2 | v3 [dim_member]
    Abstract rows: INDENT [Section Header]
    Totals:        INDENT LABEL IN CAPS: v1 | v2 | v3 [concept]
    Values scaled to millions (M) when the largest absolute value >= 1,000,000.
    Values that are already small (abs < 1 after scaling) are shown at original scale.
    """
    if df.empty:
        return f"No data for {kind}\n"

    all_vals = [c for c in df.columns if c not in META and c != "label"]

    # Filter to preferred duration and deduplicate same YYYY-MM labels
    if prefer_months is not None:
        filtered = []
        for c in all_vals:
            m = re.match(r"(\d+)M ended", c)
            if m:
                if abs(int(m.group(1)) - prefer_months) <= 2:
                    filtered.append(c)
            else:
                filtered.append(c)  # instant dates (balance sheet) always pass
        all_vals = filtered or all_vals

    # Drop duplicate YYYY-MM labels — keep the column whose duration is closest to prefer_months
    seen_labels: dict = {}  # pretty_label -> col_name
    for c in all_vals:
        lbl = col_label_pretty(c)
        if lbl not in seen_labels:
            seen_labels[lbl] = c
        elif prefer_months is not None:
            m_new = re.match(r"(\d+)M ended", c)
            m_old = re.match(r"(\d+)M ended", seen_labels[lbl])
            if m_new and m_old:
                if abs(int(m_new.group(1)) - prefer_months) < abs(int(m_old.group(1)) - prefer_months):
                    seen_labels[lbl] = c
    vals = list(seen_labels.values())

    title = _role_title(kind)
    pretty_cols = [col_label_pretty(c) for c in vals]

    header = f"{ticker + ' | ' if ticker else ''}{title} | {', '.join(pretty_cols)}"
    lines = [header]

    # Pre-scan: find member labels that appear across multiple concepts.
    # When the same member (e.g. "Reportable Segment") is used on many concepts,
    # prefix each member row with its concept label so rows are distinguishable.
    # Also collect which concepts have member rows (for consolidated label suffix).
    member_concept_counts: dict = {}  # member_label -> set of concepts
    concept_labels: dict = {}         # concept -> plain-row label
    concepts_with_members: set = set()
    for _, r in df.iterrows():
        dim = r.get("dimension_member", "") or ""
        if not isinstance(dim, str):
            dim = ""
        concept = r.get("concept", "") or ""
        raw_label = r["label"] or humanize(concept.split(":")[-1])
        clean = re.sub(r'\s*\[Member\]$', '', raw_label, flags=re.IGNORECASE).strip()
        if dim and concept and not bool(r["abstract"]):
            member_concept_counts.setdefault(clean, set()).add(concept)
            concepts_with_members.add(concept)
        elif not dim and not bool(r["abstract"]) and concept:
            concept_labels[concept] = clean
    repeated_members = {m for m, concepts in member_concept_counts.items() if len(concepts) > 1}

    plain_vals: dict = {}  # concept -> val_key of rendered plain row (for single-segment dedup)

    for _, r in df.iterrows():
        depth = int(r["depth"])
        label = r["label"] or humanize((r.get("concept", "") or "").split(":")[-1])
        concept = r.get("concept", "") or ""
        dim = r.get("dimension_member", "") or ""
        if not isinstance(dim, str):
            dim = ""
        is_abstract = bool(r["abstract"])
        is_total = bool(r.get("total", False))
        all_empty = all(pd.isna(r[c]) for c in vals)

        if is_abstract and all_empty and depth == 0:
            continue

        # Dedup: plain rows appear first; skip a member row whose values are identical to
        # the plain row (single-segment: breakdown adds no information).
        if not is_abstract and concept and not all_empty:
            val_key = tuple(None if pd.isna(r[c]) else r[c] for c in vals)
            if not dim:
                plain_vals[concept] = val_key   # record plain row values
            elif plain_vals.get(concept) == val_key:
                continue                         # member = plain → redundant, skip

        # Strip "[Member]" suffix from labels — it's XBRL boilerplate, not useful display text
        label = re.sub(r'\s*\[Member\]$', '', label, flags=re.IGNORECASE).strip()

        # Plain row for a concept that also has segment/member rows: mark as consolidated total
        if not dim and not is_abstract and concept in concepts_with_members:
            label = f"{label} (consolidated)"

        indent = "  " * depth

        if is_abstract:
            lines.append(f"{indent}[{label}]")
            continue

        def _scale(v):
            if pd.isna(v):
                return "-"
            return _fmt_val(v)

        val_str = " | ".join(_scale(r[c]) for c in vals)
        tag = f" [{concept}]" if (concept and include_tags) else ""

        if dim:
            dim_tag = f" [{dim}]" if include_tags else ""
            # If this member label is reused across multiple concepts, prefix with concept label
            if label in repeated_members:
                concept_lbl = concept_labels.get(concept, humanize(concept.split(":")[-1]))
                display = f"{label} — {concept_lbl}"
            else:
                display = label
            lines.append(f"{indent}  - {display}: {val_str}{dim_tag}")
        elif is_total:
            lines.append(f"{indent}{label.upper()}: {val_str}{tag}")
        else:
            lines.append(f"{indent}{label}: {val_str}{tag}")

    return "\n".join(lines) + "\n"


def _find_fact_meta(store: FactStore, ctxs: dict, concept: str,
                    col: tuple, dim_member: str = "") -> dict:
    """Return {decimals, unit} for a concept/period/member combo. Best-effort."""
    member_local = dim_member.split(":")[-1] if dim_member else ""
    for f in store.get(concept):
        cx = ctxs.get(f["ctx"])
        if cx is None or not isinstance(f["value"], float):
            continue
        if member_local:
            if not any(d[1].split(":")[-1] == member_local for d in cx.dims):
                continue
        elif cx.dims:
            continue
        if (cx.start == col[0] and cx.end == col[1]) or \
                (cx.start is None and col[0] is not None and cx.end == col[1]):
            return {"decimals": f.get("decimals"), "unit": f.get("unit")}
    return {"decimals": None, "unit": None}


def to_json(df: pd.DataFrame, cols: list, store: FactStore, ctxs: dict,
            filing_meta: dict = None) -> str:
    """Convert statement DataFrame to long-format JSON with concept IDs and fact metadata.
    Each row = one (concept, dimension_member, period) data point.
    Suitable for segment summing, EPS checks, and cross-filing stitching by concept ID."""
    col_map = {col_label(c): c for c in cols}
    val_cols = [c for c in df.columns if c not in META and c != "label"]
    rows = []
    for _, r in df.iterrows():
        if r["abstract"]:
            continue
        concept = r["concept"]
        dim = r.get("dimension_member", "") or ""
        if not isinstance(dim, str):
            dim = ""
        for col_str in val_cols:
            v = r[col_str]
            if pd.isna(v):
                continue
            col_tuple = col_map.get(col_str)
            if col_tuple is None:
                continue
            fm = _find_fact_meta(store, ctxs, concept, col_tuple, dim)
            rows.append({
                "label": r["label"],
                "concept": concept,
                "dimension_member": dim,
                "period": col_str,
                "start": col_tuple[0].isoformat() if col_tuple[0] else None,
                "end": col_tuple[1].isoformat(),
                "value": v,
                "decimals": fm["decimals"],
                "unit": fm["unit"],
                "depth": int(r["depth"]),
                "total": bool(r.get("total", False)),
                "negated": bool(r.get("negated", False)),
            })
    out: dict = {"rows": rows}
    if filing_meta:
        out["meta"] = {k: v for k, v in filing_meta.items()
                       if isinstance(v, (str, int, float, bool, type(None)))}
    return json.dumps(out, indent=2, default=str)


def list_all_roles(summary: ET.Element) -> list[dict]:
    """Return all report roles from FilingSummary.xml, excluding renderer-only artifacts."""
    roles = []
    for rep in summary.iter("Report"):
        short = (rep.findtext("ShortName") or rep.findtext("LongName") or "").strip()
        role = (rep.findtext("Role") or "").strip()
        cat = (rep.findtext("MenuCategory") or "").strip()
        # Skip renderer artifacts: (Tables), (Parenthetical), empty roles
        if not role or "(Tables)" in short or "Parenthetical" in short:
            continue
        roles.append({"category": cat, "short_name": short, "role": role})
    return roles


def build_any_role(pre: ET.Element, cal, store, ctxs, lab, role_uri: str):
    """Build a DataFrame for any presentation role URI (face statement or note)."""
    lines, _, axis_members, leaf_set = statement_lines(pre, role_uri)
    if not lines:
        return pd.DataFrame(), [], []
    # Auto-detect instant (balance sheet) vs duration (income/CF)
    inst, dur = 0, 0
    for l in lines[:15]:
        if l.abstract:
            continue
        for (s, _) in nondim_index(store, l.concept, ctxs):
            if s is None:
                inst += 1
            else:
                dur += 1
    kind = "balance_sheet" if inst >= dur else "income_statement"
    df, cols = build_statement(lines, store, ctxs, lab, kind, axis_members)
    checks = calc_checks(cal, role_uri, store, ctxs, cols, kind, axis_members, leaf_set) \
        if cal is not None and cols else []
    return df, cols, checks


def fetch_filing_resources(sec: "SEC", ticker: str, form: str = "10-K", nth: int = 0):
    """Fetch and parse all XBRL resources for one filing. Returns parsed objects."""
    cik, name = ticker_to_cik(sec, ticker)
    filings = list_filings(sec, cik, form)
    if len(filings) <= nth:
        raise ValueError(f"No {form} #{nth} for {ticker}")
    filing = filings[nth]
    files = locate_files(sec, cik, filing["accession"])
    if not (files.summary and files.instance and (files.pre or files.schema)):
        raise RuntimeError(f"Missing XBRL files: {files}")
    pre = sec.xml(files.pre or files.schema)
    lab = parse_labels(sec.xml(files.lab or files.schema))
    cal = sec.xml(files.cal) if files.cal else None
    ctxs, store = parse_instance(sec.get(files.instance).content)
    summary = sec.xml(files.summary)
    roles = statement_roles(summary)
    meta = {"ticker": ticker.upper(), "cik": cik, "company": name, **filing}
    return pre, lab, cal, ctxs, store, roles, summary, meta


# ------------------------------------------------------------- history (optional)
def build_quarterly_trend(sec: "SEC", ticker: str, section: str,
                          num_quarters: int = 6):
    """Fetch the last N individual quarters by stitching together multiple 10-Q filings.

    Each 10-Q only contains the current quarter + the year-ago comparison quarter.
    To get a true quarter-by-quarter trend (e.g. 2026-06, 2026-03, 2025-12, 2025-09)
    we fetch the last N filings and take the most recent 3M column from each.

    Returns (df, meta) where df has one column per quarter newest-first.
    """
    seen: set = set()
    result: pd.DataFrame | None = None
    first_meta = None

    for i in range(num_quarters + 4):  # fetch a few extra in case some are sparse
        if result is not None:
            three_m = [c for c in result.columns
                       if c not in set(META) | {"label"} and re.match(r"^3M ended", c)]
            if len(three_m) >= num_quarters:
                break
        try:
            pre, lab, cal, ctxs, store, roles, _, meta = fetch_filing_resources(
                sec, ticker, "10-Q", i)
        except Exception:
            break

        if first_meta is None:
            first_meta = meta
        if section not in roles:
            continue

        lines, _, axis_members, _ = statement_lines(pre, roles[section]["role"])
        df_q, _ = build_statement(lines, store, ctxs, lab, section, axis_members)
        if df_q.empty:
            continue

        val_cols = sorted(
            [c for c in df_q.columns
             if c not in set(META) | {"label"} and re.match(r"^3M ended", c)],
            reverse=True
        )
        new_cols = [c for c in val_cols if c not in seen]
        if not new_cols:
            continue
        new_col = new_cols[0]
        seen.add(new_col)

        if result is None:
            result = df_q.copy()
        else:
            # Map (concept, dimension_member) → value from this filing
            lookup = {
                (r["concept"], r.get("dimension_member", "") or ""):
                    r.get(new_col)
                for _, r in df_q.iterrows()
            }
            result[new_col] = result.apply(
                lambda r: lookup.get(
                    (r["concept"], r.get("dimension_member", "") or ""), None),
                axis=1
            )

    if result is None:
        raise ValueError(f"No quarterly filings found for {ticker}")

    # Drop columns from the base filing that are not the primary quarter we want
    # (the base filing also contributed a year-ago 3M column we don't need
    #  if it was already added by a later iteration)
    keep_meta = [c for c in result.columns if c in set(META) | {"label"}]
    keep_vals = sorted(
        [c for c in result.columns if c not in set(META) | {"label"}
         and re.match(r"^3M ended", c)],
        reverse=True
    )[:num_quarters]
    result = result[keep_meta + keep_vals]

    return result, first_meta


def company_facts_long(sec: SEC, cik: str) -> pd.DataFrame:
    d = sec.json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    rows = []
    for tax, concepts in d["facts"].items():
        for name, body in concepts.items():
            for unit, pts in body["units"].items():
                for p in pts:
                    rows.append({"concept": f"{tax}:{name}", "unit": unit, "start": p.get("start"),
                                 "end": p["end"], "val": p["val"], "accn": p["accn"],
                                 "form": p["form"], "filed": p["filed"]})
    return pd.DataFrame(rows)


def history_on_template(stmt: pd.DataFrame, cf: pd.DataFrame, kind: str, form: str,
                        first_reported: bool = True) -> pd.DataFrame:
    """Hang every period's value on the latest filing's lines, keyed by XBRL tag.
    first_reported=True -> originally reported values; False -> latest (restated) values.
    Note: companyfacts has no company-extension tags and no dimensional facts."""
    # Exclude dimension member rows — companyfacts has no dimensional data so they'd
    # get the aggregate value assigned to them, producing mislabeled rows.
    if "dimension_member" in stmt.columns:
        plain_rows = stmt[stmt["dimension_member"].fillna("").eq("")]
    else:
        plain_rows = stmt
    tmpl = plain_rows[["line", "depth", "concept", "label", "abstract", "negated"]]
    h = cf[cf.concept.isin(stmt.loc[~stmt.abstract, "concept"].unique())].copy()
    if kind == "balance_sheet":
        h = h[h.start.isna()]
    else:
        h = h[h.start.notna()]
        days = (pd.to_datetime(h.end) - pd.to_datetime(h.start)).dt.days
        h = h[days.between(350, 380)] if form == "10-K" else h[days.between(80, 100)]
    if h.empty:
        return tmpl
    h = h.sort_values("filed", ascending=first_reported).drop_duplicates(
        ["concept", "start", "end"], keep="first")
    wide = h.pivot_table(index="concept", columns="end", values="val", aggfunc="first")
    wide = wide[sorted(wide.columns, reverse=True)]
    out = tmpl.merge(wide, left_on="concept", right_index=True, how="left")
    neg = out["negated"]
    for c in wide.columns:
        out.loc[neg, c] = -out.loc[neg, c]
    return out


# ------------------------------------------------------------------------ main
def build_filing(sec: SEC, ticker: str, form: str = "10-K", nth: int = 0) -> dict:
    cik, name = ticker_to_cik(sec, ticker)
    filings = list_filings(sec, cik, form)
    if len(filings) <= nth:
        raise ValueError(f"No {form} #{nth} with XBRL for {ticker}")
    filing = filings[nth]
    files = locate_files(sec, cik, filing["accession"])
    if not (files.summary and files.instance and (files.pre or files.schema)):
        raise RuntimeError(f"Filing folder is missing XBRL pieces: {files}")

    pre = sec.xml(files.pre or files.schema)       # some filers embed linkbases in the .xsd
    lab = parse_labels(sec.xml(files.lab or files.schema))
    cal = sec.xml(files.cal) if files.cal else None
    ctxs, store = parse_instance(sec.get(files.instance).content)
    roles = statement_roles(sec.xml(files.summary))

    result = {"meta": {"ticker": ticker.upper(), "cik": cik, "company": name, **filing,
                       "folder": files.base, "roles": roles},
              "statements": {}, "checks": []}
    for kind in CORE:
        if kind not in roles:
            continue
        lines, axes, axis_members, leaf_set = statement_lines(pre, roles[kind]["role"])
        df, cols = build_statement(lines, store, ctxs, lab, kind, axis_members)
        result["statements"][kind] = df
        result["meta"]["roles"][kind]["axes_on_face"] = sorted(axes)
        if cal is not None and cols:
            result["checks"] += calc_checks(cal, roles[kind]["role"], store, ctxs, cols, kind,
                                            axis_members, leaf_set)
    return result


META = ("line", "depth", "concept", "abstract", "total", "negated", "preferred_label",
        "dimension_member")


def presentable_df(df: pd.DataFrame) -> pd.DataFrame:
    vals = [c for c in df.columns if c not in META and c != "label"]
    rows = []
    for _, r in df.iterrows():
        label = "  " * int(r["depth"]) + r["label"]
        row = {"label": label}
        for c in vals:
            v = r[c]
            row[c] = "" if pd.isna(v) else f"{v:,.0f}"
        row["concept"] = r["concept"]
        row["dimension_member"] = r.get("dimension_member", "")
        rows.append(row)
    return pd.DataFrame(rows)


def print_statement(kind: str, df: pd.DataFrame):
    if df.empty:
        print(f"\n== {kind}: no data")
        return
    vals = [c for c in df.columns if c not in META and c != "label"]
    print(f"\n== {kind.replace('_', ' ').upper()}")
    print(f"{'':60}" + "".join(f"{c:>22}" for c in vals))
    for _, r in df.iterrows():
        label = ("  " * int(r["depth"]) + r["label"])[:58]
        cells = "".join(f"{'' if pd.isna(r[c]) else f'{r[c]:,.0f}':>22}" for c in vals)
        dim = r.get("dimension_member", "")
        suffix = f"  ({dim})" if dim else ""
        print(f"{label:60}{cells}{suffix}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker")
    ap.add_argument("--form", default="10-K", choices=["10-K", "10-Q", "20-F", "40-F"])
    ap.add_argument("--nth", type=int, default=0, help="0 = latest filing, 1 = one before, ...")
    ap.add_argument("--out", default=None, help="directory for CSV/JSON output")
    ap.add_argument("--history", action="store_true", help="also build tag-linked history via companyfacts")
    ap.add_argument("--pretty", action="store_true", help="also save indented, human-readable CSVs")
    ap.add_argument("--ua", default=os.environ.get("SEC_USER_AGENT"))
    a = ap.parse_args()

    sec = SEC(a.ua)
    res = build_filing(sec, a.ticker, a.form, a.nth)
    m = res["meta"]
    print(f"{m['company']} ({m['ticker']}, CIK {m['cik']}) {m['form']} for {m['period']}, filed {m['filed']}")
    for kind, df in res["statements"].items():
        print_statement(kind, df)
    checks = pd.DataFrame(res["checks"])
    if not checks.empty:
        bad = checks[~checks.ok]
        print(f"\nCalc checks: {len(checks) - len(bad)}/{len(checks)} pass")
        if not bad.empty:
            print(bad[["statement", "parent", "period", "reported", "computed", "diff"]].to_string(index=False))

    if a.out:
        os.makedirs(a.out, exist_ok=True)
        for kind, df in res["statements"].items():
            df.to_csv(os.path.join(a.out, f"{kind}.csv"), index=False)
        checks.to_csv(os.path.join(a.out, "calc_checks.csv"), index=False)
        with open(os.path.join(a.out, "metadata.json"), "w") as fh:
            json.dump(m, fh, indent=2, default=str)
        if a.pretty:
            for kind, df in res["statements"].items():
                presentable_df(df).to_csv(os.path.join(a.out, f"{kind}_pretty.csv"), index=False)
        if a.history:
            cf = company_facts_long(sec, m["cik"])
            for kind, df in res["statements"].items():
                history_on_template(df, cf, kind, a.form).to_csv(
                    os.path.join(a.out, f"{kind}_history.csv"), index=False)
        print(f"\nWrote output to {a.out}")


if __name__ == "__main__":
    main()
