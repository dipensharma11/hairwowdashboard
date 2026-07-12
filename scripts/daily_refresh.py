"""
Daily refresh for the Meta Weekly Creative dashboard.

Pulls DD + Overall Mapping from the MM25 Umbrella Sheet, PWC from the mirror,
and the 3 Creative Trackers (Hair/Nutrition/Beard), via the Google Sheets API
(read-only, precise UNFORMATTED_VALUE -- not the rounded display text you get
from CSV export). Joins ad rows to trackers by ad name (exact, then a safe
fuzzy fallback on same trailing ad number + token overlap), computes daily
KPIs + full section breakdowns + Ad Explorer rows per product, and MERGES
the result into whatever DAILY_DATA/DAILY_DETAIL already exist in index.html
-- new/updated days overwrite, but days that have rolled off the source
(older than its rolling retention window) are preserved from history rather
than dropped.

Fails loudly (non-zero exit, no commit) rather than publish partial/wrong
data if any fetch or sanity check fails.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.parse
from collections import defaultdict

API_KEY = os.environ["SHEETS_API_KEY"]
UMBRELLA_ID = "1FhU5tkfTREdeVlMjyIdqhyaTIdeBoSZczQ2rTd2B-KI"
MIRROR_ID = "1Cf1cQEbrQkjMVQo3SFr9-jdBBNcXFNy3YXpITRE-4To"
HAIR_TRACKER_ID = "1DbcV58XCrHQqjqg1Kl6JaEsBHrpBA7jGiqGOB71wj1Y"
NUTRITION_TRACKER_ID = "1wOYhs0IB24u_fIrrssyZaAuFvpbaItjHgiDIkXBv0WU"
BEARD_TRACKER_ID = "1lguNY_9CQIOk6Rsm9hsixJ-GTxMgCpCRhnZIAaxuh2c"
F = 0.76

HAIR_TABS = ["BGK", "Biotin", "S1", "S2", "S3", "Cetosomal"]
NUTRITION_TABS = ["Shilajit", "Creatine", "Magnesium"]
BEARD_TABS = ["Beard"]

DIMS = ["narrative", "adtype", "format", "source", "funnel", "creator", "language", "region"]
DASH = ["Stage 2", "Stage 3", "Advance Regime", "Beard Growth Kit", "Shilajit Gummies",
        "Stage 1 Serum", "Biotin Gummies", "Magnesium Gummies", "Creatine Powder",
        "Creatine Electrolyte", "AI Hair Test"]

PRODMAP = {
    "stage 2": "Stage 2", "stage2": "Stage 2", "stage 3": "Stage 3", "stage3": "Stage 3",
    "advance regime": "Advance Regime", "cetosomal": "Advance Regime", "stage 3 (cetosomal)": "Advance Regime",
    "beard growth kit": "Beard Growth Kit", "bgk": "Beard Growth Kit",
    "biotin": "Biotin Gummies", "biotin30": "Biotin Gummies", "biotin gummies": "Biotin Gummies",
    "stage 1": "Stage 1 Serum", "stage1 serum": "Stage 1 Serum", "stage 1 serum": "Stage 1 Serum",
    "selfasst": "AI Hair Test", "ai hair test": "AI Hair Test",
    "creatine powder": "Creatine Powder", "creatine electrolyte": "Creatine Electrolyte",
    "shilajit": "Shilajit Gummies", "shilajit gummies": "Shilajit Gummies",
    "magnesium": "Magnesium Gummies", "magnesium gummies": "Magnesium Gummies",
    "magnesium glycinate": "Magnesium Gummies",
}


def prod_norm(p):
    return PRODMAP.get(str(p).strip().lower())


def prod_from_name(n):
    n = str(n).lower()
    for pre, pr in [("stage3", "Stage 3"), ("stage2", "Stage 2"), ("stage_1", "Stage 1 Serum"),
                    ("beard", "Beard Growth Kit"), ("shilajit", "Shilajit Gummies"),
                    ("biotin", "Biotin Gummies"), ("magnesium", "Magnesium Gummies"),
                    ("advance", "Advance Regime"), ("selfasst", "AI Hair Test")]:
        if n.startswith(pre):
            return pr
    if n.startswith("creatine"):
        return "Creatine Electrolyte" if "electrolyte" in n else "Creatine Powder"
    return None


def norm_src(v):
    v = str(v).strip().lower()
    return {"int": "Internal", "inf": "Influencer", "affluence": "Affluence"}.get(v, str(v).strip() if v else "")


def num(s):
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(",", "").strip()
    if s in ("", "—", "-", "–", "#N/A"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def proas(s):
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().rstrip("x")
    try:
        return float(s)
    except ValueError:
        return 0.0


def pday(v):
    # With valueRenderOption=UNFORMATTED_VALUE, Sheets API returns date cells as a
    # raw serial number (days since 1899-12-30), NOT a formatted string -- handle
    # that as the primary case, with the ISO-string form as a defensive fallback
    # for any cell that happens to be stored as literal text.
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            import datetime
            d = datetime.date(1899, 12, 30) + datetime.timedelta(days=float(v))
            return d.isoformat()
        except (ValueError, OverflowError):
            return None
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s[:10]
    try:
        serial = float(s)
        import datetime
        d = datetime.date(1899, 12, 30) + datetime.timedelta(days=serial)
        return d.isoformat()
    except (ValueError, OverflowError):
        return None


def canon(n):
    return "_".join(t for t in str(n).strip().lower().split("_") if t and t not in ("si", "bca"))


def toks(n):
    return set(t for t in re.split(r"[^a-z0-9]+", str(n).strip().lower()) if t and t not in ("si", "bca"))


def tail(n):
    m = re.search(r"_(\d{3,6})(?:_si|_bca)*$", str(n).strip().lower())
    return m.group(1) if m else None


def akey(l):
    return re.sub(r"[^a-z0-9]", "", str(l).lower())


def notcreator(n):
    return str(n).strip().lower() in ("none", "internal / none", "internal/none", "internal", "na", "n/a", "", "-")


def sheets_get(spreadsheet_id, ranges):
    """Fetch multiple A1 ranges from one spreadsheet via values:batchGet. Returns {range: rows}.

    batchGet's response valueRanges are documented to come back in the same order
    as the requested `ranges` query params, so we zip by position rather than by
    the echoed-back range string (which Sheets may reformat, e.g. quoting).
    """
    qs = "&".join("ranges=" + urllib.parse.quote(r, safe="") for r in ranges)
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchGet"
           f"?{qs}&valueRenderOption=UNFORMATTED_VALUE&key={API_KEY}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = json.load(resp)
    value_ranges = data.get("valueRanges", [])
    return {ranges[i]: (value_ranges[i].get("values", []) if i < len(value_ranges) else [])
            for i in range(len(ranges))}


def q(sheet_name):
    return "'" + sheet_name.replace("'", "''") + "'"


def load_tracker(spreadsheet_id, tab_names, exact, bynum):
    ranges = [f"{q(t)}!A1:AC" for t in tab_names]
    fetched = sheets_get(spreadsheet_id, ranges)
    for t in tab_names:
        rows = fetched.get(f"{q(t)}!A1:AC", [])
        if not rows:
            continue
        # The real header isn't row 1 -- rows 1-2 are instructions/blank in these
        # trackers. Scan the first few rows for the one that actually looks like
        # the column-name header.
        hdr_i = None
        header = None
        for i, r in enumerate(rows[:6]):
            cand = [str(c).strip() for c in r]
            if "Ad Name" in cand and any("Broad Narrative" in h for h in cand):
                hdr_i, header = i, cand
                break
        if header is None:
            continue
        data_rows = rows[hdr_i + 1:]

        def col(*names):
            for nm in names:
                for i, h in enumerate(header):
                    if h == nm:
                        return i
            for nm in names:
                for i, h in enumerate(header):
                    if nm.lower() in h.lower():
                        return i
            return None

        iN = col("Ad Name")
        iPr = col("Product Name")
        iNar = col("Broad Narrative - P0", "Broad Narrative")
        iFmt = col("Ad Format")
        iTyp = col("Ad Type")
        iFun = col("Funnel Type")
        iSrc = col("INT / INF", "INT/INF")
        iLan = col("Language")
        iPer = col("Person Full Name")
        i45 = col("4:5")
        i916 = col("9:16")

        def g(r, i):
            return str(r[i]).strip() if (i is not None and i < len(r) and r[i] not in (None, "")) else ""

        for r in data_rows:
            if iN is None or iN >= len(r) or not r[iN]:
                continue
            nm = str(r[iN]).strip()
            if nm.lower() == "ad name":
                continue
            l45 = g(r, i45)
            l916 = g(r, i916)
            video = l45 if l45.startswith("http") else (l916 if l916.startswith("http") else "")
            dims = {
                "product": prod_norm(g(r, iPr)) or prod_from_name(nm),
                "narrative": g(r, iNar), "format": g(r, iFmt), "adtype": g(r, iTyp),
                "funnel": g(r, iFun), "source": norm_src(g(r, iSrc)), "language": g(r, iLan),
                "creator": g(r, iPer), "video": video,
            }
            exact.setdefault(canon(nm), dims)
            tn = tail(nm)
            if tn:
                bynum[tn].append((toks(nm), dims))


def lookup(name, exact, bynum):
    c = canon(name)
    if c in exact:
        return exact[c]
    tn = tail(name)
    if tn and tn in bynum:
        T = toks(name)
        best, bs = None, 0
        for tt, dm in bynum[tn]:
            s = len(T & tt) / len(T | tt) if (T | tt) else 0
            if s > bs:
                bs, best = s, dm
        if bs >= 0.7:
            return best
    return None


def canonicalize(secmap):
    groups = defaultdict(list)
    for lab, v in secmap.items():
        groups[akey(lab)].append((lab, v))
    out, keymap = {}, {}
    for k, items in groups.items():
        cl = max(items, key=lambda x: x[1][0])[0]
        agg = [0.0, 0.0, 0.0]
        for lab, v in items:
            agg[0] += v[0]
            agg[1] += v[1]
            agg[2] += v[2]
        out[cl] = agg
        keymap[cl] = k
    return out, keymap


def mkrows(secmap, tot, narr_cr_day=None, is_narr=False):
    sm, keymap = canonicalize(secmap)
    rows = []
    for lab, (sp, nc, s) in sm.items():
        row = {"segment": lab, "spend": round(sp, 2), "spend_pct": round(sp / tot * 100, 1) if tot else 0,
               "nc": nc, "roas": round(s * F / sp, 4) if sp else 0, "cac": round(sp / (nc * F), 1) if nc else 0,
               "top_creators": []}
        if is_narr and narr_cr_day is not None:
            crs = narr_cr_day.get(keymap[lab], {})
            top = sorted(crs.items(), key=lambda kv: -kv[1][0])[:8]
            row["top_creators"] = [
                {"name": cn, "spend": round(v[0], 2), "roas": round(v[1] * F / v[0], 2) if v[0] else 0}
                for cn, v in top if v[0] > 0 and not notcreator(cn)
            ]
        rows.append(row)
    rows.sort(key=lambda x: -x["spend"])
    return rows


def week_label(ws, we):
    import datetime
    s = datetime.date.fromisoformat(ws)
    e = datetime.date.fromisoformat(we)
    return f"{s.strftime('%b')} {s.day}\u2013{e.day}, {e.year}"


def complete_weeks(days_present):
    """All Monday-start weeks whose 7 days are all present in the data window."""
    import datetime
    dset = set(days_present)
    out = []
    d0 = datetime.date.fromisoformat(days_present[0])
    d1 = datetime.date.fromisoformat(days_present[-1])
    cur = d0 - datetime.timedelta(days=d0.weekday())
    while cur + datetime.timedelta(days=6) <= d1:
        wk = [(cur + datetime.timedelta(days=i)).isoformat() for i in range(7)]
        if all(x in dset for x in wk):
            out.append(wk)
        cur += datetime.timedelta(days=7)
    return out


def build_week_from_days(day_entries, pwc_daily_prod, week_days):
    """Merge 7 daily-detail entries into one weekly DASH_DATA-shaped entry.

    Same math as the dashboard's client-side buildVirtualWeek: revenue is backed
    out of stored roas*spend per day and re-blended; ads merged by name with
    their per-region adsets summed. Creator dimension drops non-creators
    ('None'/'Internal / None' etc.) per Dipen's correction; narrative
    top_creators trimmed to top 3 (also non-creator-filtered).
    """
    ws, we = week_days[0], week_days[-1]
    days = [d for d in day_entries if d["date"] in set(week_days)]
    if not days:
        return None
    # KPIs from PWC daily rows summed across the week
    sp = nc = rev = 0.0
    for dd in week_days:
        pk = pwc_daily_prod.get(dd)
        if pk:
            sp += pk["spend"] or 0
            nc += pk["nc"] or 0
            rev += (pk["roas"] or 0) * (pk["spend"] or 0)
    kpis = {"spend": round(sp, 2), "nc": nc, "roas": round(rev / sp, 4) if sp else 0,
            "cac": round(sp / (nc * F), 1) if nc else None, "aov": round(rev / nc, 1) if nc else None}

    # sections: merge by canonical segment key
    sections = {}
    for dim in DIMS:
        merged = {}
        for d in days:
            for r in d["sections"].get(dim, []):
                if dim == "creator" and notcreator(r["segment"]):
                    continue
                k = akey(r["segment"])
                m = merged.setdefault(k, {"segment": r["segment"], "sp": 0.0, "nc": 0.0, "rev": 0.0, "cmap": {}})
                if r["spend"] > m["sp"]:
                    m["segment"] = r["segment"]
                m["sp"] += r["spend"] or 0
                m["nc"] += r["nc"] or 0
                m["rev"] += (r["roas"] or 0) * (r["spend"] or 0)
                for c in r.get("top_creators", []):
                    if notcreator(c["name"]):
                        continue
                    cm = m["cmap"].setdefault(c["name"], {"sp": 0.0, "rev": 0.0})
                    cm["sp"] += c["spend"] or 0
                    cm["rev"] += (c["roas"] or 0) * (c["spend"] or 0)
        tot = sum(m["sp"] for m in merged.values())
        rows = []
        for m in merged.values():
            row = {"segment": m["segment"], "spend": round(m["sp"], 2),
                   "spend_pct": round(m["sp"] / tot * 100, 1) if tot else 0,
                   "nc": m["nc"], "roas": round(m["rev"] / m["sp"], 4) if m["sp"] else 0,
                   "cac": round(m["sp"] / (m["nc"] * F), 1) if m["nc"] else 0, "top_creators": []}
            if dim == "narrative":
                top = sorted(m["cmap"].items(), key=lambda kv: -kv[1]["sp"])[:3]
                row["top_creators"] = [{"name": cn, "spend": round(v["sp"], 2),
                                        "roas": round(v["rev"] / v["sp"], 2) if v["sp"] else 0}
                                       for cn, v in top if v["sp"] > 0]
            rows.append(row)
        rows.sort(key=lambda x: -x["spend"])
        sections[dim] = rows

    # ads: merge by name; adsets summed per region
    admap = {}
    for d in days:
        for a in d["ads"]:
            m = admap.setdefault(a["name"], {"a": dict(a), "sp": 0.0, "nc": 0.0, "rev": 0.0, "reg": {},
                                              "imp": 0, "clk": 0, "v3": 0, "v75": 0})
            m["sp"] += a["spend"] or 0
            m["nc"] += a["nc"] or 0
            m["rev"] += (a["roas"] or 0) * (a["spend"] or 0)
            m["imp"] += a.get("imp", 0) or 0
            m["clk"] += a.get("clk", 0) or 0
            m["v3"] += a.get("v3", 0) or 0
            m["v75"] += a.get("v75", 0) or 0
            if a.get("video") and not m["a"].get("video"):
                m["a"]["video"] = a["video"]
            for s in a.get("adsets", []):
                rm = m["reg"].setdefault(s["region"], {"sp": 0.0, "nc": 0.0, "rev": 0.0, "funnel": s.get("funnel", "")})
                rm["sp"] += s["spend"] or 0
                rm["nc"] += s["nc"] or 0
                rm["rev"] += (s["roas"] or 0) * (s["spend"] or 0)
    tot_ads = sum(m["sp"] for m in admap.values())
    ads = []
    for name, m in admap.items():
        base = m["a"]
        regs = sorted(m["reg"].items(), key=lambda kv: -kv[1]["sp"])
        adsets = [{"label": rg, "region": rg, "funnel": v["funnel"], "spend": round(v["sp"], 2), "nc": v["nc"],
                   "roas": round(v["rev"] / v["sp"], 4) if v["sp"] else 0,
                   "cac": round(v["sp"] / (v["nc"] * F), 1) if v["nc"] else None} for rg, v in regs]
        ad = {"name": name, "spend": round(m["sp"], 2),
              "spend_pct": round(m["sp"] / tot_ads * 100, 2) if tot_ads else 0, "nc": m["nc"],
              "roas": round(m["rev"] / m["sp"], 4) if m["sp"] else 0,
              "cac": round(m["sp"] / (m["nc"] * F), 1) if m["nc"] else None,
              "narrative": base.get("narrative", ""), "adtype": base.get("adtype", ""),
              "format": base.get("format", ""), "source": base.get("source", ""),
              "funnel": base.get("funnel", ""), "creator": base.get("creator", ""),
              "language": base.get("language", ""),
              "region": regs[0][0] if regs else "", "adsets": adsets}
        if m["imp"] > 0:
            ad["imp"] = int(m["imp"])
            ad["clk"] = int(m["clk"])
            ad["v3"] = int(m["v3"])
            ad["v75"] = int(m["v75"])
        if base.get("video"):
            ad["video"] = base["video"]
        ads.append(ad)
    ads.sort(key=lambda x: -x["spend"])

    if kpis["spend"] <= 0 and not ads:
        return None
    return {"week_label": week_label(ws, we), "week_start": ws, "auto": True,
            "kpis": kpis, "sections": sections, "ads": ads}


def main():
    print("== Daily refresh starting ==")

    exact, bynum = {}, defaultdict(list)
    load_tracker(HAIR_TRACKER_ID, HAIR_TABS, exact, bynum)
    load_tracker(NUTRITION_TRACKER_ID, NUTRITION_TABS, exact, bynum)
    load_tracker(BEARD_TRACKER_ID, BEARD_TABS, exact, bynum)
    print(f"tracker exact keys: {len(exact)}")
    if len(exact) < 1000:
        sys.exit(f"FATAL: only {len(exact)} tracker rows loaded -- looks wrong, aborting without publishing.")

    # Range starts at column F, so relative index 0=F(Ad ID) .. 6=L(Region) -- NOT
    # the same as full-sheet indices (Ad ID=5, Region=11 there).
    om_fetch = sheets_get(UMBRELLA_ID, [f"{q('Overall Mapping')}!F1:L"])
    om_rows = om_fetch.get(f"{q('Overall Mapping')}!F1:L", [])
    region_of = {}
    for r in om_rows[1:]:
        if not r or not r[0]:
            continue
        reg = str(r[6]).strip() if len(r) > 6 else ""
        if reg.lower() == "pan india":
            reg = "Pan India"
        region_of[str(r[0]).strip()] = reg or "Unmapped"
    print(f"Overall Mapping ad-id->region entries: {len(region_of)}")
    if len(region_of) < 5000:
        sys.exit(f"FATAL: only {len(region_of)} region entries -- looks wrong, aborting.")

    pwc_fetch = sheets_get(MIRROR_ID, [f"{q('PWC')}!A4:G"])
    pwc_rows = pwc_fetch.get(f"{q('PWC')}!A4:G", [])
    pwc_daily = defaultdict(dict)
    for r in pwc_rows:
        if len(r) < 7 or not r[0]:
            continue
        d = pday(r[0])
        prod = prod_norm(r[1]) if len(r) > 1 else None
        if d is None or not prod:
            continue
        pwc_daily[prod][d] = {
            "spend": round(num(r[2]), 2), "nc": num(r[3]),
            "roas": round(proas(r[6]), 4),
            "cac": round(num(r[4]), 1) if r[4] not in (None, "") else None,
            "aov": round(num(r[5]), 1) if r[5] not in (None, "") else None,
        }
    print(f"PWC days loaded: {sum(len(v) for v in pwc_daily.values())} product-day rows")

    dd_fetch = sheets_get(UMBRELLA_ID, [f"{q('DD')}!A1:AC"])
    dd_all = dd_fetch.get(f"{q('DD')}!A1:AC", [])
    dd_rows = []
    for r in dd_all[1:]:
        if len(r) < 29 or not r[0] or not r[4]:
            continue
        dd_rows.append((r[0], r[4], r[6], r[21] if len(r) > 21 else "", r[25] if len(r) > 25 else 0,
                         r[28] if len(r) > 28 else 0,
                         r[7] if len(r) > 7 else 0, r[8] if len(r) > 8 else 0,
                         r[13] if len(r) > 13 else 0, r[16] if len(r) > 16 else 0))
    print(f"DD rows loaded: {len(dd_rows)}")
    days_present = sorted({pday(r[0]) for r in dd_rows if pday(r[0])})
    if not days_present:
        sys.exit("FATAL: no days found in DD -- aborting.")
    print(f"Days to build: {len(days_present)} | {days_present[0]} to {days_present[-1]}")

    new_daily_data = {p: {} for p in DASH}
    new_daily_detail = {p: {} for p in DASH}

    for day in days_present:
        prodsec = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0])))
        prodads = defaultdict(lambda: defaultdict(
            lambda: {"sp": 0.0, "nc": 0.0, "sum": 0.0, "dims": None, "byreg": defaultdict(lambda: [0.0, 0.0, 0.0])}))
        prodtot = defaultdict(float)
        narr_cr = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0.0, 0.0])))

        for dt_raw, name, spend_s, adid, nc_s, sum_s, imp_s, clk_s, v3_s, v75_s in dd_rows:
            if pday(dt_raw) != day:
                continue
            sp, nc, sm = num(spend_s), num(nc_s), num(sum_s)
            imp, clk, v3, v75 = num(imp_s), num(clk_s), num(v3_s), num(v75_s)
            adid = str(adid).strip()
            dm = lookup(name, exact, bynum)
            if dm:
                prod = dm["product"] or prod_from_name(name)
            else:
                prod = prod_from_name(name)
                dm = {"narrative": "", "format": "", "adtype": "", "source": "", "funnel": "",
                      "language": "", "creator": "", "video": ""}
            if not prod:
                continue
            reg = region_of.get(adid, "Unmapped")
            prodtot[prod] += sp
            vals = {"narrative": dm.get("narrative") or "Other", "adtype": dm.get("adtype") or "Other",
                    "format": dm.get("format") or "Other", "source": dm.get("source") or "",
                    "funnel": dm.get("funnel") or "", "creator": dm.get("creator") or "Internal / None",
                    "language": dm.get("language") or "", "region": reg}
            for dim in DIMS:
                lab = vals[dim]
                if lab:
                    s = prodsec[prod][dim][lab]
                    s[0] += sp
                    s[1] += nc
                    s[2] += sm
            cr = vals["creator"]
            nk = akey(vals["narrative"])
            c = narr_cr[prod][nk][cr]
            c[0] += sp
            c[1] += sm
            a = prodads[prod][str(name).strip()]
            a["sp"] += sp
            a["nc"] += nc
            a["sum"] += sm
            a["imp"] = a.get("imp", 0) + imp
            a["clk"] = a.get("clk", 0) + clk
            a["v3"] = a.get("v3", 0) + v3
            a["v75"] = a.get("v75", 0) + v75
            a["dims"] = dm
            br = a["byreg"][reg]
            br[0] += sp
            br[1] += nc
            br[2] += sm

        for prod in DASH:
            tot = prodtot[prod]
            pk = pwc_daily.get(prod, {}).get(day)
            if pk is None:
                if tot <= 0:
                    continue
                kpis = {"spend": round(tot, 2), "nc": 0, "roas": 0, "cac": None, "aov": None}
            else:
                kpis = pk
            new_daily_data[prod][day] = {"date": day, **kpis}

            sections = {dim: mkrows(prodsec[prod][dim], tot, narr_cr[prod], dim == "narrative") for dim in DIMS}
            ads = []
            for name, a in prodads[prod].items():
                if a["sp"] <= 0:
                    continue
                dm = a["dims"] or {}
                regs = sorted(a["byreg"].items(), key=lambda kv: -kv[1][0])
                adsets = [{"label": rg, "region": rg, "funnel": dm.get("funnel", "") or "",
                           "spend": round(v[0], 2), "nc": v[1],
                           "roas": round(v[2] * F / v[0], 4) if v[0] else 0,
                           "cac": round(v[0] / (v[1] * F), 1) if v[1] else None} for rg, v in regs]
                dom = regs[0][0] if regs else ""
                ad = {"name": name, "spend": round(a["sp"], 2),
                      "spend_pct": round(a["sp"] / tot * 100, 2) if tot else 0, "nc": a["nc"],
                      "roas": round(a["sum"] * F / a["sp"], 4) if a["sp"] else 0,
                      "cac": round(a["sp"] / (a["nc"] * F), 1) if a["nc"] else None,
                      "narrative": dm.get("narrative", "") or "", "adtype": dm.get("adtype", "") or "",
                      "format": dm.get("format", "") or "", "source": dm.get("source", "") or "",
                      "funnel": dm.get("funnel", "") or "", "creator": dm.get("creator", "") or "",
                      "language": dm.get("language", "") or "", "region": dom, "adsets": adsets}
                if a.get("imp", 0) > 0:
                    ad["imp"] = int(a["imp"])
                    ad["clk"] = int(a.get("clk", 0))
                    ad["v3"] = int(a.get("v3", 0))
                    ad["v75"] = int(a.get("v75", 0))
                if dm.get("video"):
                    ad["video"] = dm["video"]
                ads.append(ad)
            ads.sort(key=lambda x: -x["spend"])
            if tot <= 0 and not ads:
                continue
            new_daily_detail[prod][day] = {"date": day, "kpis": kpis, "sections": sections, "ads": ads}

    total_new_ads = sum(len(v["ads"]) for p in new_daily_detail.values() for v in p.values())
    print(f"Computed {len(days_present)} days x {len(DASH)} products, {total_new_ads} total ad rows")
    if total_new_ads < 1000:
        sys.exit(f"FATAL: only {total_new_ads} ad rows computed across all days -- looks wrong, aborting.")

    # ---- merge into existing index.html (accumulate, don't replace) ----
    html_path = "index.html"
    with open(html_path, encoding="utf-8") as f:
        html = f.read()

    m_data = re.search(r"const DAILY_DATA=(\{.*?\});\n", html, re.DOTALL)
    m_detail = re.search(r"const DAILY_DETAIL=(\{.*?\});\n", html, re.DOTALL)
    if not m_data or not m_detail:
        sys.exit("FATAL: could not find DAILY_DATA/DAILY_DETAIL in index.html -- aborting.")

    existing_data = json.loads(m_data.group(1))
    existing_detail = json.loads(m_detail.group(1))

    merged_data = {}
    for p in DASH:
        by_date = {d["date"]: d for d in existing_data.get(p, [])}
        by_date.update(new_daily_data[p])
        merged_data[p] = sorted(by_date.values(), key=lambda x: x["date"])

    merged_detail = {}
    for p in DASH:
        by_date = {d["date"]: d for d in existing_detail.get(p, [])}
        by_date.update(new_daily_detail[p])
        merged_detail[p] = sorted(by_date.values(), key=lambda x: x["date"])

    html = html[:m_data.start()] + "const DAILY_DATA=" + json.dumps(merged_data, separators=(",", ":")) + ";\n" + html[m_data.end():]
    m_detail2 = re.search(r"const DAILY_DETAIL=(\{.*?\});\n", html, re.DOTALL)
    html = html[:m_detail2.start()] + "const DAILY_DETAIL=" + json.dumps(merged_detail, separators=(",", ":")) + ";\n" + html[m_detail2.end():]

    # ---- weekly: append/refresh completed Monday-start weeks into DASH_DATA ----
    m_dash = re.search(r"const DASH_DATA=(\{.*?\});\n", html, re.DOTALL)
    if not m_dash:
        sys.exit("FATAL: could not find DASH_DATA in index.html -- aborting.")
    dash = json.loads(m_dash.group(1))
    week_added, week_updated = [], []
    for wk_days in complete_weeks(days_present):
        for prod in DASH:
            entry = build_week_from_days(merged_detail.get(prod, []), pwc_daily.get(prod, {}), wk_days)
            if not entry:
                continue
            arr = dash.get(prod, [])
            idx = next((i for i, w in enumerate(arr) if w.get("week_start") == entry["week_start"]), None)
            if idx is None:
                arr.append(entry)
                week_added.append((prod, entry["week_label"]))
            else:
                ex = arr[idx]
                kpi_only = not (ex.get("ads") or []) and not any((ex.get("sections") or {}).get(k) for k in (ex.get("sections") or {}))
                if ex.get("auto") or kpi_only:
                    arr[idx] = entry
                    week_updated.append((prod, entry["week_label"]))
            dash[prod] = arr
    for prod in dash:
        dash[prod] = sorted(dash[prod], key=lambda w: (w.get("week_start") or "9999-99-99"))
    if week_added or week_updated:
        html = re.sub(r"const DASH_DATA=\{.*?\};\n",
                      lambda _: "const DASH_DATA=" + json.dumps(dash, separators=(",", ":")) + ";\n",
                      html, count=1, flags=re.DOTALL)
    print(f"Weekly: {len(week_added)} added {sorted(set(w for _, w in week_added))}, "
          f"{len(week_updated)} upgraded {sorted(set(w for _, w in week_updated))}")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    total_days = {p: len(merged_data[p]) for p in DASH}
    print("Merged. Total days per product now:", total_days)
    print("== Daily refresh done ==")


if __name__ == "__main__":
    main()
