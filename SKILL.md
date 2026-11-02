---
name: ride-trail-sequencer
description: >
  Recaps a mountain-bike ride from a GPS file: the ordered list of named trails ridden, with a
  numbered map, an elevation profile and a Markdown report (distance, climbing, speed and heart
  rate per trail), using OpenStreetMap trail data. Use when the user asks "which trails did I
  ride", "what was my route", "map my ride", "trail order", "ride recap" or "sequence my ride",
  and has (or can export) a GPX or CSV track.
---

# Ride trail sequencer

`ride_recap.py` (in this folder) matches a recorded ride to OpenStreetMap trails, prints the
ordered trail list, and writes a numbered map with an elevation profile plus a Markdown report.
`examples/` has a made-up sample ride and its output.

## Steps

1. **Get the track.** Ask for the GPX path if the user hasn't given one. If they don't have one,
   point them to "Get a GPX file" in README.md. A CSV with latitude/longitude columns also works.
2. **Check matplotlib:** `python -c "import matplotlib"`. If that fails, `pip install matplotlib`.
3. **Run it** (`<skill-dir>` is the folder containing this file):
   ```
   python <skill-dir>/ride_recap.py <ride.gpx> --title "<area> <m/d>"
   ```
   On Windows, set `PYTHONIOENCODING=utf-8` first if trail names print as `?`.
4. **Show the map and the report** (both paths are printed at the end) and summarize the trail list.

## Reporting rules

- Report trail names exactly as printed. **Never fill in a name for a `[not on OpenStreetMap]` leg**
  from memory or another website. Say it's unmapped, and give its distance and time.
- Pass on any `bikes not allowed, per OSM` note.
- Use the printed elevation figures; they count gain only while moving.
- Speed and heart rate are what the device recorded. Report the numbers; don't add medical
  interpretations.
- If the output has a `WARNING` about old map data, tell the user and suggest re-running later with
  `--refresh`.
- If every server is busy, wait a few minutes and retry once. Don't loop.

## Privacy

The map is trimmed to start at the first trail or forest road and end at the last one. Before the
user shares anything, remind them to check the map doesn't show where they live (`--hide-ends-m 500`
cuts more off both ends), and that the report includes their heart rate and ride times.

## Useful options

| Option | Use |
|---|---|
| `--refresh` | Download the map data again (e.g. after a stale-data warning) |
| `--min-run 30` | Keep very short trails that were dropped (default 60 m) |
| `--thresh 35` | Sloppy GPS: heavy tree cover, phone in a pack (default 25 m) |
| `--no-trim` | Keep the whole track |
| `--hide-ends-m N` | Hide another N metres at both ends |
| `--no-profile` | Skip the elevation profile |
| `--note "..."` | Add a line at the top of the report |
| `--no-report` | Skip the Markdown report |
