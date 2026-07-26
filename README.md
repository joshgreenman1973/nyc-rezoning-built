# What rezoning built

Every adopted zoning map amendment in New York City since 1977, mapped against
every residential unit completed inside its boundary since 2010.

**Live site:** https://joshgreenman1973.github.io/nyc-rezoning-built/

The headline finding, using the city's own classification of its own rezonings:
New York City lowered density on roughly 44,000 acres and raised it on about
4,600. The land where it allowed more housing produced about twenty times as
many homes per acre as the land where it lowered density.

## Important caveat

This is a record of what happened inside the lines, not a measurement of what
the rezonings caused. A boundary containing four thousand new homes may have
caused them, or may have been drawn around land that was going to be developed
anyway. Nothing here separates the two.

## Data

| Feed | Source |
| --- | --- |
| Adopted zoning map amendments | DCP `nyzma` feature service |
| Housing Database, project level | NYC Open Data `br6q-ssj3` |
| 2002-2013 rezoning purpose classification | DCP `Bloomberg_rezonings` |
| Mandatory Inclusionary Housing areas | NYC Open Data `m79g-k9r4` |

## Running it

```bash
pip install shapely pyproj
python3 scripts/fetch.py   # pulls every input into data/raw
python3 scripts/build.py   # spatial join, writes docs/data/
```

## Method notes

- Areas are computed in EPSG:2263, not from degrees.
- Rates are divided by **years actually observed**, from the later of the
  effective date and 1 January 2010, so rezonings that predate the housing data
  are neither flattered nor penalised.
- Citywide totals are deduplicated by job number, because boundaries overlap.
  The individual rows deliberately do not sum to the citywide figure.
- No purpose was inferred. Every label comes from the city's own files.

Full detail, including a correction made during the build, is in
[docs/METHODOLOGY.html](docs/METHODOLOGY.html).
