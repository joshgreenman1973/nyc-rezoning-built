#!/usr/bin/env python3
"""
What rezoning built - fetch every input.

Four sources:
  1. nyzma           every adopted zoning map amendment, with geometry
  2. Bloomberg       DCP's own purpose classification for 2002-2013 rezonings
  3. MIH areas       Mandatory Inclusionary Housing, which are upzonings by
                     definition since they require affordable units in exchange
                     for added residential capacity
  4. Housing DB      every completed residential job since 2010, geocoded,
                     with net change in permanent (Class A) units

Fails loudly. A short or empty fetch would silently understate how much
housing got built, which is the exact claim this project makes.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "data", "raw")
os.makedirs(RAW, exist_ok=True)

ARC = "https://services5.arcgis.com/GfwWNkhOj9bNBqoJ/arcgis/rest/services"
SOC = "https://data.cityofnewyork.us/resource"
UA = {"User-Agent": "nyc-rezoning-built/1.0 (housing policy research)"}


def get(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(3 * (i + 1))
    raise SystemExit(f"FATAL: {url} failed after {tries} tries: {last}")


def arcgis_all(service, where="1=1", geometry=True, expect_min=1, page=1000):
    """Page an ArcGIS FeatureServer layer, returning GeoJSON features."""
    out = []
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": "true" if geometry else "false",
            "outSR": "4326",
            "f": "geojson" if geometry else "json",
            "resultOffset": offset,
            "resultRecordCount": page,
            "orderByFields": "OBJECTID",
        }
        url = f"{ARC}/{service}/FeatureServer/0/query?" + urllib.parse.urlencode(params)
        d = get(url)
        chunk = d.get("features", [])
        out.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
        if offset > 100000:
            raise SystemExit(f"FATAL: {service} paging ran away")
    if len(out) < expect_min:
        raise SystemExit(
            f"FATAL: {service} returned {len(out)} features, expected at least "
            f"{expect_min}. Refusing to write a truncated file.")
    return out


def socrata_all(dataset, select=None, where=None, order=":id", page=50000,
                expect_min=1):
    out = []
    offset = 0
    while True:
        p = {"$limit": page, "$offset": offset, "$order": order}
        if select:
            p["$select"] = select
        if where:
            p["$where"] = where
        chunk = get(f"{SOC}/{dataset}.json?" + urllib.parse.urlencode(p))
        out.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
        if offset > 2_000_000:
            raise SystemExit(f"FATAL: {dataset} paging ran away")
    if len(out) < expect_min:
        raise SystemExit(
            f"FATAL: {dataset} returned {len(out)} rows, expected at least "
            f"{expect_min}. Refusing to write a truncated file.")
    return out


def save(name, obj):
    path = os.path.join(RAW, name)
    with open(path, "w") as f:
        json.dump(obj, f)
    n = len(obj) if isinstance(obj, list) else 1
    print(f"  wrote {name}  ({n:,} records, {os.path.getsize(path)/1e6:.1f} MB)")


def main():
    print("What rezoning built - fetching")

    print("\n[1/4] adopted zoning map amendments (nyzma)")
    zma = arcgis_all("nyzma", where="STATUS='Adopted'", expect_min=1000)
    save("nyzma.json", zma)

    print("\n[2/4] DCP purpose classification, 2002-2013")
    bl = arcgis_all("Bloomberg_rezonings", geometry=False, expect_min=120)
    save("bloomberg.json", bl)

    print("\n[3/4] Mandatory Inclusionary Housing areas")
    mih = socrata_all("m79g-k9r4", expect_min=200)
    save("mih.json", mih)

    print("\n[4/4] completed residential jobs since 2010 (large)")
    hd = socrata_all(
        "br6q-ssj3",
        select=("job_number,job_type,job_status,classanet,classainit,classaprop,"
                "units_co,datecomplt,compltyear,latitude,longitude,boro,bbl,"
                "bldg_class,ownership,ntaname20,job_desc"),
        where="job_status like '5%' AND datecomplt IS NOT NULL",
        order="job_number",
        expect_min=50000,
    )
    save("housing.json", hd)

    save("fetch_meta.json", {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "counts": {"nyzma": len(zma), "bloomberg": len(bl), "mih": len(mih),
                   "housing_completions": len(hd)},
        "sources": {
            "nyzma": f"{ARC}/nyzma/FeatureServer/0",
            "bloomberg": f"{ARC}/Bloomberg_rezonings/FeatureServer/0",
            "mih": f"{SOC}/m79g-k9r4",
            "housing": f"{SOC}/br6q-ssj3",
        },
    })
    print("\nAll inputs fetched.")


if __name__ == "__main__":
    main()
