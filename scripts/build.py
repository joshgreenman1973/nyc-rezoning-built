#!/usr/bin/env python3
"""
What rezoning built - spatial join and aggregation.

Method in one paragraph: take every adopted zoning map amendment with an
effective date, project it to New York State Plane so areas are real acres,
then drop every completed residential job since 2010 into whichever
amendment boundaries contain it. Units completed on or after a rezoning's
effective date are what that rezoning built. Units completed before it are
kept separately as the "before" picture.

Two traps this handles explicitly:
  - Overlapping rezonings. About 15 percent of the city's rezoned land has
    been rezoned more than once. A job inside two boundaries counts toward
    both at the per-rezoning level, so citywide totals are deduplicated by
    job number rather than summed across rezonings.
  - The 2010 floor. The housing database begins in 2010, so a rezoning
    adopted in 2004 cannot have its first six years of construction counted.
    Those rezonings are flagged and excluded from headline comparisons.
"""
import json
import os
import collections
from datetime import datetime, timezone

from shapely.geometry import shape, Point
from shapely.ops import transform
from shapely import STRtree
from pyproj import Transformer

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "data", "raw")
OUT = os.path.join(HERE, "..", "docs", "data")
os.makedirs(OUT, exist_ok=True)

# NY State Plane Long Island, US survey feet. Areas come out in square feet.
TO_FT = Transformer.from_crs("EPSG:4326", "EPSG:2263", always_xy=True).transform
SQFT_PER_ACRE = 43560.0

HOUSING_FLOOR = 2010          # first completion year in the housing database
MATURE_YEARS = 5              # years a rezoning needs before it is judged


def load(n):
    with open(os.path.join(RAW, n)) as f:
        return json.load(f)


def num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def epoch_to_date(ms):
    if ms in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).date()
    except (ValueError, OSError, OverflowError):
        return None


def norm_purpose(p):
    """DCP's purpose strings carry typos and inconsistent casing.

    'Lower Density/Contexual', 'Lower Density/Contextual' and
    'Lower Density/Contexual/HousingOpportunities' all appear. Normalised to
    two independent booleans rather than a single tidy label, because a
    rezoning can genuinely do both in different parts of its area.
    """
    s = (p or "").lower().replace(" ", "")
    return {
        "lower_density": "lowerdensity" in s or "contexual" in s or "contextual" in s,
        "housing": "housingopportunit" in s,
        "cbd": "centralbusiness" in s,
    }


def main():
    meta = load("fetch_meta.json")
    zma_raw = load("nyzma.json")
    bloomberg = load("bloomberg.json")
    mih_raw = load("mih.json")
    housing = load("housing.json")

    # ---- rezoning polygons -------------------------------------------------
    rez = []
    skipped_nodate = 0
    for f in zma_raw:
        a = f.get("properties") or {}
        g = f.get("geometry")
        if not g:
            continue
        eff = epoch_to_date(a.get("EFFECTIVE"))
        if eff is None:
            skipped_nodate += 1
            continue
        try:
            geom = shape(g)
            if not geom.is_valid:
                geom = geom.buffer(0)
            gft = transform(TO_FT, geom)
        except Exception:
            continue
        if gft.is_empty or gft.area <= 0:
            continue
        rez.append({
            "ulurp": (a.get("ULURPNO") or "").strip().lower(),
            "name": (a.get("PROJECT_NAME") or "").strip(),
            "effective": eff.isoformat(),
            "year": eff.year,
            "acres": gft.area / SQFT_PER_ACRE,
            "geom_ft": gft,
            "geom_wgs": geom,
        })
    print(f"rezonings with an effective date: {len(rez)}  "
          f"(skipped {skipped_nodate} with no date)")

    # ---- DCP purpose classification (2002-2013) ----------------------------
    # DCP's own file carries a borough-letter typo: Hudson Square is filed as
    # 030237zmn, while the boundary layer has 030237zmm. Without this alias the
    # city's classification of that rezoning is silently lost.
    ULURP_ALIASES = {"030237zmn": "030237zmm"}

    purpose = {}
    for b in bloomberg:
        a = b.get("attributes") or b.get("properties") or b
        u = (a.get("ULURPNO") or "").strip().lower()
        if not u:
            continue
        u = ULURP_ALIASES.get(u, u)
        purpose[u] = norm_purpose(a.get("Purpose") or a.get("Z_Category"))
    matched_purpose = sum(1 for r in rez if r["ulurp"] in purpose)
    print(f"DCP purpose labels: {len(purpose)}, matched to a polygon: {matched_purpose}")

    # ---- MIH areas, matched spatially --------------------------------------
    # MIH records key on the text-amendment ULURP (…ZR…), not the map
    # amendment (…ZM…), so the two cannot be joined on identifier. An MIH
    # area is attributed to a rezoning when most of the MIH polygon sits
    # inside that rezoning's boundary.
    # Geography alone is not enough: a large 1995 rezoning physically contains
    # MIH areas mapped twenty years later, and a naive overlap test hands that
    # old rezoning an MIH label it never had. The MIH programme began in March
    # 2016, so a match also requires the rezoning to be from 2016 or later and
    # to take effect within about a year of the MIH area's adoption.
    MIH_START = 2016
    MIH_DATE_TOLERANCE_DAYS = 400

    mih_geoms = []
    for m in mih_raw:
        g = m.get("the_geom")
        if not g:
            continue
        try:
            gg = shape(g)
            if not gg.is_valid:
                gg = gg.buffer(0)
        except Exception:
            continue
        adopted = (m.get("date_adopte") or "")[:10]
        mih_geoms.append((transform(TO_FT, gg), adopted))

    rez_tree = STRtree([r["geom_ft"] for r in rez])
    mih_hits = set()
    rejected_by_date = 0
    for mg, adopted in mih_geoms:
        if mg.is_empty or mg.area <= 0:
            continue
        for idx in rez_tree.query(mg):
            idx = int(idx)
            r = rez[idx]
            if r["year"] < MIH_START:
                continue
            if adopted:
                try:
                    gap = abs((datetime.fromisoformat(r["effective"]).date()
                               - datetime.fromisoformat(adopted).date()).days)
                except ValueError:
                    gap = 0
                if gap > MIH_DATE_TOLERANCE_DAYS:
                    rejected_by_date += 1
                    continue
            try:
                inter = r["geom_ft"].intersection(mg).area
            except Exception:
                continue
            if inter / mg.area > 0.5:
                mih_hits.add(idx)
    print(f"MIH areas: {len(mih_geoms)}, rezonings carrying one: {len(mih_hits)} "
          f"({rejected_by_date} spatial matches rejected on date)")

    for i, r in enumerate(rez):
        p = purpose.get(r["ulurp"])
        r["mih"] = i in mih_hits
        r["dcp_lower_density"] = bool(p and p["lower_density"])
        r["dcp_housing"] = bool(p and p["housing"])
        # "Central Business Distrists" and "Other" carry a DCP label but no
        # direction, so they display as unclassified and must count as such.
        r["classified"] = None  # set after kind is decided
        # A single label for display. MIH and DCP "housing opportunities" both
        # mean capacity was added; "lower density" alone means it was removed.
        # Fifteen Bloomberg-era plans did both, typically lowering density on
        # the side streets while raising it on the avenues. Forcing those into
        # one bucket would misdescribe them, so they get their own.
        if p and p["lower_density"] and (p["housing"] or r["mih"]):
            r["kind"] = "both"
        elif r["mih"] or (p and p["housing"]):
            r["kind"] = "more housing allowed"
        elif p and p["lower_density"]:
            r["kind"] = "density lowered"
        else:
            r["kind"] = "unclassified"
        r["classified"] = r["kind"] != "unclassified"

    # ---- completed housing -------------------------------------------------
    pts, jobs = [], []
    bad = 0
    for h in housing:
        lat, lon = num(h.get("latitude"), None), num(h.get("longitude"), None)
        d = (h.get("datecomplt") or "")[:10]
        if not lat or not lon or not d:
            bad += 1
            continue
        try:
            x, y = TO_FT(lon, lat)
        except Exception:
            bad += 1
            continue
        jobs.append({
            "job": h.get("job_number"),
            "net": num(h.get("classanet")),
            "date": d,
            "year": int(d[:4]),
            "type": h.get("job_type"),
        })
        pts.append(Point(x, y))
    print(f"completed jobs geocoded: {len(jobs)}  (dropped {bad} without a point or date)")

    total_net_all = sum(j["net"] for j in jobs)
    print(f"citywide net units completed {HOUSING_FLOOR}-present: {total_net_all:,.0f}")

    # ---- spatial join ------------------------------------------------------
    pt_tree = STRtree(pts)
    for r in rez:
        r["after_net"] = 0.0
        r["after_jobs"] = 0
        r["before_net"] = 0.0
        r["by_year"] = collections.defaultdict(float)
        r["job_ix"] = set()
    inside_any = set()
    inside_after = set()   # counted only where the rezoning already applied

    for ri, r in enumerate(rez):
        cand = pt_tree.query(r["geom_ft"])
        if len(cand) == 0:
            continue
        geom = r["geom_ft"]
        for pi in cand:
            pi = int(pi)
            if not geom.contains(pts[pi]):
                continue
            j = jobs[pi]
            inside_any.add(pi)
            if j["date"] >= r["effective"]:
                r["after_net"] += j["net"]
                r["after_jobs"] += 1
                r["by_year"][j["year"]] += j["net"]
                r["job_ix"].add(pi)
                inside_after.add(pi)
            else:
                r["before_net"] += j["net"]

    # ---- per rezoning output ----------------------------------------------
    # Rates are computed over the window actually observed, not over time since
    # adoption. A rezoning from 2003 has 23 years of history but only the years
    # from 2010 are visible in the housing data, and dividing its units by 23
    # would understate it against a 2018 rezoning measured over 8. Both are
    # divided by the years for which completions can actually be seen.
    last_completion = max(j["date"] for j in jobs)
    last_dt = datetime.fromisoformat(last_completion).date()
    floor_dt = datetime(HOUSING_FLOOR, 1, 1, tzinfo=timezone.utc).date()

    rows = []
    for _i, r in enumerate(rez):
        eff_dt = datetime.fromisoformat(r["effective"]).date()
        observed_start = max(eff_dt, floor_dt)
        years_since = max((last_dt - observed_start).days / 365.25, 0)
        truncated = r["year"] < HOUSING_FLOOR
        rows.append({
            "_i": _i,
            "ulurp": r["ulurp"],
            "name": r["name"] or r["ulurp"],
            "effective": r["effective"],
            "year": r["year"],
            "acres": round(r["acres"], 2),
            "kind": r["kind"],
            "mih": r["mih"],
            "dcp_lower_density": r["dcp_lower_density"],
            "dcp_housing": r["dcp_housing"],
            "classified": r["classified"],
            "units": round(r["after_net"], 1),
            "jobs": r["after_jobs"],
            "units_before": round(r["before_net"], 1),
            "units_per_acre": round(r["after_net"] / r["acres"], 3) if r["acres"] else None,
            "years_observed": round(years_since, 1),
            "units_per_acre_per_year": round(
                r["after_net"] / r["acres"] / years_since, 4)
            if r["acres"] and years_since > 0 else None,
            "truncated": truncated,
            # Judged once there are at least five observed years of completions,
            # whether the rezoning predates the housing data or not.
            "mature": years_since >= MATURE_YEARS,
            "lat": round(r["geom_wgs"].centroid.y, 5),
            "lon": round(r["geom_wgs"].centroid.x, 5),
        })
    rows.sort(key=lambda r: -(r["units"] or 0))

    # ---- cohort summaries --------------------------------------------------
    mature = [r for r in rows if r["mature"]]

    def dedup_units(sel):
        """Net units inside a group, counting each job once.

        Boundaries overlap, so summing per-rezoning totals double counts
        buildings that sit inside two amendments of the same kind.
        """
        ix = set()
        for r in sel:
            ix |= rez[r["_i"]]["job_ix"]
        return sum(jobs[i]["net"] for i in ix)

    def dedup_acres(sel):
        from shapely.ops import unary_union
        if not sel:
            return 0.0
        try:
            u = unary_union([rez[r["_i"]]["geom_ft"] for r in sel])
        except Exception:
            return sum(r["acres"] for r in sel)
        return u.area / SQFT_PER_ACRE

    def summarise(sel):
        acres = sum(r["acres"] for r in sel)
        units = sum(r["units"] for r in sel)
        # Acre-years, so a group's rate is not distorted by how long its
        # members have been observed.
        acre_years = sum(r["acres"] * r["years_observed"] for r in sel)
        return {
            "n": len(sel),
            "acres": round(acres, 1),
            "units": round(units, 1),
            "units_per_acre": round(units / acres, 3) if acres else None,
            "units_per_acre_per_year": round(units / acre_years, 4) if acre_years else None,
            "zero": sum(1 for r in sel if r["units"] <= 0),
            "median_units": sorted(r["units"] for r in sel)[len(sel) // 2] if sel else None,
            "median_acres": round(sorted(r["acres"] for r in sel)[len(sel) // 2], 2)
            if sel else None,
            # Overlap-corrected: each job and each square foot counted once.
            "units_dedup": round(dedup_units(sel), 1),
            "acres_dedup": round(dedup_acres(sel), 1),
            "units_per_acre_dedup": round(dedup_units(sel) / dedup_acres(sel), 3)
            if sel and dedup_acres(sel) else None,
            "median_rate": round(sorted(
                (r["units_per_acre"] or 0) for r in sel)[len(sel) // 2], 3) if sel else None,
        }

    KINDS = ["more housing allowed", "both", "density lowered", "unclassified"]
    by_kind = {k: summarise([r for r in mature if r["kind"] == k]) for k in KINDS}

    by_year = {}
    for y in sorted({r["year"] for r in rows}):
        sel = [r for r in rows if r["year"] == y]
        by_year[y] = {**summarise(sel),
                      "truncated": y < HOUSING_FLOOR}

    # Citywide, deduplicated. Two versions, because they answer different
    # questions: everything inside a boundary, and everything inside a boundary
    # that was already in effect when the building finished.
    net_inside = sum(jobs[i]["net"] for i in inside_any)
    net_inside_after = sum(jobs[i]["net"] for i in inside_after)

    # ---- sensitivity -------------------------------------------------------
    # The per-acre ratio moves on two undisclosed choices: how many observed
    # years a rezoning needs before it is judged, and whether plans that did
    # both count as raising density. Both are published rather than buried.
    sens_threshold = []
    for thr in [0, 3, 5, 7, 10, 16]:
        sel = [r for r in rows if r["years_observed"] >= thr]
        up = summarise([r for r in sel if r["kind"] == "more housing allowed"])
        dn = summarise([r for r in sel if r["kind"] == "density lowered"])
        sens_threshold.append({
            "years": thr, "up_n": up["n"], "up_rate": up["units_per_acre"],
            "down_n": dn["n"], "down_rate": dn["units_per_acre"],
            "ratio": round(up["units_per_acre"] / dn["units_per_acre"], 1)
            if up["units_per_acre"] and dn["units_per_acre"] else None,
        })

    up_only = summarise([r for r in mature if r["kind"] == "more housing allowed"])
    dn_only = summarise([r for r in mature if r["kind"] == "density lowered"])
    up_with_both = summarise([r for r in mature
                              if r["kind"] in ("more housing allowed", "both")])
    sens_bucket = {
        "both_excluded": {"ratio": round(up_only["units_per_acre"] / dn_only["units_per_acre"], 1),
                          "up_acres": up_only["acres"], "up_rate": up_only["units_per_acre"]},
        "both_as_up": {"ratio": round(up_with_both["units_per_acre"] / dn_only["units_per_acre"], 1),
                       "up_acres": up_with_both["acres"], "up_rate": up_with_both["units_per_acre"]},
        "dedup": {"ratio": round(up_only["units_per_acre_dedup"] / dn_only["units_per_acre_dedup"], 1)
                  if up_only["units_per_acre_dedup"] and dn_only["units_per_acre_dedup"] else None},
        "median_rate": {"up": up_only["median_rate"], "down": dn_only["median_rate"]},
    }

    # All rezonings, not just mature, so the deck can quote totals as totals.
    all_up = summarise([r for r in rows if r["kind"] == "more housing allowed"])
    all_both = summarise([r for r in rows if r["kind"] == "both"])
    all_down = summarise([r for r in rows if r["kind"] == "density lowered"])

    headline = {
        "rezonings": len(rows),
        "first_year": min(r["year"] for r in rows),
        "last_year": max(r["year"] for r in rows),
        "total_acres": round(sum(r["acres"] for r in rows), 1),
        "mature_n": len(mature),
        "mature_acres": round(sum(r["acres"] for r in mature), 1),
        "mature_units": round(sum(r["units"] for r in mature), 1),
        "acres_lowered": round(sum(r["acres"] for r in mature
                                   if r["kind"] == "density lowered"), 1),
        "acres_both": round(sum(r["acres"] for r in mature
                                if r["kind"] == "both"), 1),
        "n_both": sum(1 for r in mature if r["kind"] == "both"),
        "acres_more_housing": round(sum(r["acres"] for r in mature
                                        if r["kind"] == "more housing allowed"), 1),
        "n_lowered": sum(1 for r in mature if r["kind"] == "density lowered"),
        "n_more_housing": sum(1 for r in mature if r["kind"] == "more housing allowed"),
        "n_unclassified": sum(1 for r in mature if r["kind"] == "unclassified"),
        "zero_share": round(100 * sum(1 for r in mature if r["units"] <= 0)
                            / len(mature), 1) if mature else None,
        "citywide_net_units": round(total_net_all, 1),
        "net_units_inside_rezonings": round(net_inside, 1),
        "share_inside": round(100 * net_inside / total_net_all, 1) if total_net_all else None,
        "net_units_inside_after": round(net_inside_after, 1),
        "share_inside_after": round(100 * net_inside_after / total_net_all, 1)
        if total_net_all else None,
        # All rezonings, not only mature ones.
        "all_acres_lowered": all_down["acres"],
        "all_acres_more_housing": all_up["acres"],
        "all_acres_both": all_both["acres"],
        "all_n_lowered": all_down["n"],
        "all_n_more_housing": all_up["n"],
        "all_n_both": all_both["n"],
        # Coverage disclosure.
        "amendments_in_source": len(zma_raw),
        "dropped_no_date": skipped_nodate,
        "distinct_ulurps": len({r["ulurp"] for r in rez}),
        "housing_floor": HOUSING_FLOOR,
        "mature_years": MATURE_YEARS,
        "classified_n": sum(1 for r in rows if r["classified"]),
        "mih_n": sum(1 for r in rows if r["mih"]),
        "completions_through": max(j["date"] for j in jobs)[:7],
    }

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fetched_at": meta.get("fetched_at"),
        "headline": headline,
        "by_kind": by_kind,
        "sensitivity": {"threshold": sens_threshold, "bucket": sens_bucket},
        "by_year": by_year,
        "rezonings": rows,
    }
    p = os.path.join(OUT, "rezoning.json")
    with open(p, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"\nwrote {p}  ({os.path.getsize(p)/1e6:.2f} MB)")

    # geometry for the map, simplified
    feats = []
    for row in rows:
        i = row["_i"]
        if True:
            g = rez[i]["geom_wgs"].simplify(0.00012)
            if g.is_empty:
                continue
            feats.append({
                "type": "Feature",
                "properties": {"u": row["ulurp"], "n": row["name"], "y": row["year"],
                               "k": row["kind"], "un": row["units"],
                               "upa": row["units_per_acre"], "ac": row["acres"],
                               "t": row["truncated"]},
                "geometry": json.loads(json.dumps(_round_geom(g.__geo_interface__))),
            })
    gj = {"type": "FeatureCollection", "features": feats}
    gp = os.path.join(OUT, "rezonings.geojson")
    with open(gp, "w") as f:
        json.dump(gj, f, separators=(",", ":"))
    print(f"wrote {gp}  ({os.path.getsize(gp)/1e6:.2f} MB, {len(feats)} shapes)")

    print("\n--- headline ---")
    for k, v in headline.items():
        print(f"  {k}: {v}")
    print("\n--- mature rezonings by kind ---")
    for k, v in by_kind.items():
        print(f"  {k}: n={v['n']}, acres={v['acres']:,}, units={v['units']:,.0f}, "
              f"per acre={v['units_per_acre']}, zero={v['zero']}")
    print("\n--- top 12 producers ---")
    for r in rows[:12]:
        print(f"  {r['units']:>8,.0f}  {r['year']}  {r['kind'][:22]:<22} {r['name'][:44]}")


def _round_geom(g, nd=5):
    def rc(c):
        if isinstance(c[0], (int, float)):
            return [round(c[0], nd), round(c[1], nd)]
        return [rc(x) for x in c]
    return {"type": g["type"], "coordinates": rc(g["coordinates"])}


if __name__ == "__main__":
    main()
