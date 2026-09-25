# Swiss Train Map

A live map of Switzerland in which every train is a dot with a fading trail. Each dot sits where
the **official timetable** says the train should be at the current Swiss time, and it moves along
the **real railway tracks from OpenStreetMap**, not along straight lines between stations.

- Timetable: the Swiss GTFS feed from [opentransportdata.swiss](https://opentransportdata.swiss/en/cookbook/timetable-cookbook/gtfs/), covering every train of every Swiss railway (SBB, BLS, SOB, RhB, MGB, zb, …) plus cross-border trains. No API key needed.
- Tracks: `railway=rail|narrow_gauge|light_rail|tram` ways from OpenStreetMap, fetched through the Overpass API or read from a Geofabrik extract.
- Map: Swiss borders, cantons and lakes (swisstopo, via the `swiss-maps` package), bundled in `web/basemap.json`.

There is no backend. A Python script builds two JSON files once, and the website is plain static files.

```
pipeline/build_data.py   downloads the timetable + OSM tracks, snaps stops to the tracks,
                         routes every stop-to-stop leg along the rails, writes web/data/*.json
web/                     static site (deck.gl): dots, fading trails, Swiss clock, controls
```

## Quick start (on your computer)

You need Python 3.10 or newer.

```bash
pip install -r pipeline/requirements.txt
python pipeline/build_data.py          # the first run takes a while: big downloads, all cached
python -m http.server 8000 -d web      # then open http://localhost:8000
```

The first run downloads the Swiss GTFS zip (a few hundred MB) and about six Overpass requests'
worth of rail geometry. Both are cached in `pipeline/cache/`, so later runs only re-download the
timetable when a newer feed has been published. Delete that folder to force fresh downloads.

The website has to be served over HTTP (as above). Opening `index.html` directly from disk does not
work, because browsers block `fetch()` for `file://` pages.

### Useful options

```bash
python pipeline/build_data.py --days 14                     # smaller files (default: 60 days)
python pipeline/build_data.py --start 2026-12-24            # a specific first service day
python pipeline/build_data.py --gtfs GTFS_FP2026_20260923.zip   # use a GTFS zip you downloaded
python pipeline/build_data.py --osm-pbf switzerland-latest.osm.pbf   # Geofabrik extract instead of Overpass
python pipeline/build_data.py --route-types 2,100-199,900   # also show trams (route_type 900)
```

If the public Overpass servers are busy, the script retries, switches servers, and splits the
request into smaller tiles. If they still fail, download
[switzerland-latest.osm.pbf](https://download.geofabrik.de/europe/switzerland.html),
`pip install osmium`, and pass `--osm-pbf`. The Geofabrik extract only reaches a few km past the
border, so legs further abroad (for example Milano or Paris) are then drawn as straight lines.

## Publish it for free (GitHub Pages, no Python on your computer)

1. Push this folder to a new GitHub repository.
2. Go to **Settings → Pages → Build and deployment** and set **Source** to **GitHub Actions**.
3. Go to **Actions → Build data and deploy → Run workflow**.

The workflow in `.github/workflows/build-and-deploy.yml` builds the data on GitHub's machines,
publishes `web/` at `https://<you>.github.io/<repo>/`, and rebuilds every Monday so the site follows
timetable changes and construction work. GitHub pauses scheduled workflows in repositories with no
activity for 60 days. If that happens, re-enable the workflow on the Actions tab.

## Using the map

- **1× / 10× / 60× / 300×** controls playback speed. **Now** jumps back to live Swiss time.
- **Trail** sets how many minutes of each train's path stay visible while they fade out.
- Click a category in the legend to show or hide it.
- Hover over or tap a train to see its line, destination, next stop and scheduled times. Stations appear when you zoom in.
- URL parameters let you share a view:
  `?time=2026-12-24T17:30&speed=60&trail=8&theme=light&view=8.31,47.05,11`
  (the time is Swiss local time, and `view` is longitude, latitude and zoom).

## How it works

**Timetable** (`load_timetable`)
1. Keeps rail routes only (`route_type` 2 and 100-199, the Swiss extended types: IC, IR, RE, S-Bahn, rack railways and so on).
2. Turns `calendar.txt` and `calendar_dates.txt` into one bit per day for the output window, and expands `frequencies.txt`.
3. Streams `stop_times.txt` in chunks. The file is huge, but only rail trips are kept.
4. Compresses the data: identical stop sequences become *patterns*, identical time profiles become *timings*, and each trip is then just `(start time, timing, service calendar, route, headsign, train number)`. Times after 24:00 are kept on the day the train started, as in GTFS.

**Tracks** (`RailNetwork`)
1. Builds a graph from the OSM rail ways, skipping yards (`service=yard`).
2. **Snaps each stop to candidate points on the tracks** within 300 m, taking up to three parallel tracks from each separate network. Separate networks include standard gauge, metre gauge and trams, so a zb platform in Luzern that happens to lie closer to the SBB tracks still finds its metre-gauge line.
3. Tram tracks are included, because a few railways run on street tracks in cities, but they count triple their length. Trains therefore use them only where no railway fits.
4. Contracts the graph to junctions and builds a **turn-restricted routing graph**: a train cannot change direction by more than 90° at a switch. This stops the shortest path from "reversing" through a switch, which real trains cannot do without stopping.
5. Runs Dijkstra searches (`scipy`) for every stop-to-stop leg, choosing the candidate combination with the shortest track distance plus a small snapping penalty. If no legal path exists, it tries once without turn limits, then falls back to a straight line. The log reports how many legs used each method.
6. Simplifies the geometry to about 4 m (Douglas-Peucker) and stores it delta-encoded.

**Website** (`web/app.js`)
- Uses Swiss local time regardless of the viewer's time zone (`Intl` with `Europe/Zurich`), including daylight saving time.
- Keeps a small window of active trips (the trail length behind now plus a few minutes ahead) and rebuilds it as time moves on. Each trip gets per-vertex timestamps interpolated by distance along the track between its scheduled departure and arrival, with dwell times at stops.
- Uses deck.gl `TripsLayer` for the fading trails and scatterplot layers for the dots. deck.gl 9.4 is bundled in `web/vendor`, so the site needs no CDN.

## Limitations

- These are **scheduled** positions, not live ones. Delays and cancellations are not shown. The
  GTFS-RT feed on opentransportdata.swiss has predicted delays and could be added later, while
  actual vehicle positions are not published as open data.
- Trains move at constant speed between two stops, without acceleration or braking.
- Where OSM is missing a connection, or a stop lies more than 300 m from any track, a leg falls
  back to a less realistic path or a straight line. The build log lists these stops.
- Beyond the map area (about 45.7-48.0°N, 5.8-10.7°E), for example towards Milano, Paris or
  Munich, legs are drawn as straight lines.

## Tests

The tests run without internet. They use a synthetic dataset in the exact Swiss GTFS and Overpass
formats, with SLOID stop ids, calendar exceptions, `frequencies.txt`, trips past midnight, a
metre-gauge network, a tram ring and a yard shortcut, plus a small triangle-junction network that
checks the turn restriction.

```bash
cd pipeline
python tests/make_synthetic.py
python build_data.py --gtfs tests/out/gtfs_synthetic.zip --osm-json tests/out/osm_synthetic.json \
    --start 2026-09-24 --days 7 --out tests/out/data
python tests/test_pipeline.py
python tests/test_downloads.py
```

To view the synthetic data in the browser, copy `tests/out/data/*.json` to `web/data/`. The
stations are real, but the tracks between them are invented curves.

## Data licences and attribution

- Timetable: © opentransportdata.swiss, free to use with attribution ([terms of use](https://opentransportdata.swiss/en/terms-of-use/)).
- Tracks: © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright). The generated `web/data/network.json` is a database derived from OSM, so if you publish it, it is also covered by the ODbL.
- Borders and lakes: © swisstopo, via [swiss-maps](https://github.com/interactivethings/swiss-maps) (BSD-3). `pipeline/tools/make_basemap.py` regenerates `web/basemap.json`.
- deck.gl: MIT licence (`web/vendor/deck.gl-LICENSE.txt`).

The map's bottom corner shows the attribution line. Keep it if you publish the site.
