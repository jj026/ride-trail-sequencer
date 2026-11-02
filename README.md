# ride-trail-sequencer

**Which trails did I ride, and in what order?** Give it a recorded mountain-bike ride (a GPX
file) and it prints the ordered list of named trails you rode, then draws a numbered map with an
elevation profile. Trail names, ratings and lines come from
[OpenStreetMap](https://www.openstreetmap.org).

```
 #  Trail                     Rating     mi  up/down ft
 3  Upward Mobility           green    2.91  +1,035/-12
 4  Raging Ridge              blue     3.40  +1,131/-173
 6  Return Policy             blue     1.35  +3/-469
 7  Wild Waves                blue     2.38  +32/-1,245
15  [not on OpenStreetMap]    -        0.76  +0/-398
```

## What you need

- **Python 3.8 or newer** ([python.org/downloads](https://www.python.org/downloads/)) and matplotlib:
  `python -m pip install matplotlib`. On a Mac, type `python3` wherever this page says `python`.
- **This tool:** on GitHub, click the green **Code** button → **Download ZIP**, then unzip it.
- A GPX file of your ride (see below)
- An internet connection the first time you process a ride, to download the trail map

## Get a GPX file

- **Strava (website):** open the activity → **⋯** menu → **Export GPX**.
- **Garmin Connect (website):** open the activity → gear icon → **Export to GPX**.
- **Apple Watch:** Apple's Fitness app can't export GPX. Let Strava import the workout from Apple
  Health and export it there, or use a third-party export app.

A CSV works too, if it has `latitude` and `longitude` columns (optionally `altitude` or
`elevation`, and `timestamp` or `time`).

## Run it

Open a terminal in the unzipped folder (Windows: right-click inside the folder → **Open in
Terminal**), then:

```
python ride_recap.py my-ride.gpx
python ride_recap.py my-ride.gpx --title "Tiger Mountain 11/8"
```

Tip: instead of typing the GPX file's location, drag the file onto the terminal window.

It prints the trail list and writes `my-ride-map.png` next to the GPX. The trail map it downloads is
cached in `my-ride-map.osm.json`, so re-running is instant; add `--refresh` to download it again.
`python ride_recap.py --help` lists every option.

## Reading the output

- **Rating** is OpenStreetMap's `mtb:scale:imba` tag: green, blue, black, double black. *unrated*
  means nobody has tagged the trail yet.
- **`[not on OpenStreetMap]`**: you rode something the map doesn't have. It's drawn as a dashed pink
  line rather than silently left out, so you know there's a gap.
- **`bikes not allowed, per OSM`**: the trail is tagged `bicycle=no`. The tag can be wrong, but check
  the local rules.
- **up/down ft** only counts while you were moving. A barometric altimeter drifts while you stand
  around, and that would otherwise show up as climbing you never did.
- Times are shown in your computer's time zone.

## How accurate is it?

It's as good as OpenStreetMap is where you ride. In spot checks around Tiger Mountain and Raging
River in Washington, it named 82–93% of the trail mileage correctly. Unnamed forest roads and the
odd missing trail are normal. A GPS point counts as "on" a trail if it's within 25 m, so two trails
closer together than that can get mixed up.

If the download server answers with old map data, the tool says so and looks for a fresher copy.
Re-run with `--refresh` later if a recently built trail is missing.

## Fixing a missing trail

Anyone can edit OpenStreetMap: go to [openstreetmap.org](https://www.openstreetmap.org) and click
**Edit**. Trace the trail from **your own GPS track** and the signs at the trailhead. Don't copy from
Trailforks, Strava or other maps; their licenses don't allow it.

## Privacy

- Your GPX stays on your computer. The only thing sent over the internet is the rectangle around your
  ride, to download the trail map.
- By default the map starts at the first trail or forest road you rode and ends at the last one, so
  the streets around where you started (often your home) aren't drawn. That won't help if you start on
  a forest road or a trail by your house; add `--hide-ends-m 500` to cut more.
  **Look at the map before you post it.**

## Using it with an AI coding agent

This folder is also an agent skill ([SKILL.md](SKILL.md)). Copy it to
`~/.copilot/skills/ride-trail-sequencer` (GitHub Copilot CLI) or
`~/.claude/skills/ride-trail-sequencer` (Claude Code), then ask *"which trails did I ride? Here's
my GPX: …"*.

## Credits and license

- Code: MIT, see [LICENSE](LICENSE).
- Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), under the Open
  Database License. Every map this tool draws carries that credit; keep it when you share.
- The trail map comes from the public [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API)
  servers. They're free and shared, so they're sometimes busy; the tool retries other servers and
  caches what it downloads. Please don't run it in a loop.
