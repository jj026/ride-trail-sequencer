# Sample ride (made-up data)

This is what the tool produces, shown on a **made-up ride**.

> **For privacy, everything personal in this sample is made up:** the rider, the date, the times,
> the speed and the heart rate. None of it is anyone's real health or location data. The trails,
> their ratings and the terrain are real: a classic loop at Raging River State Forest, near
> Preston, Washington.

| File | What it is |
|---|---|
| [sample-ride-report.md](sample-ride-report.md) | The report the tool writes |
| [sample-ride-map.png](sample-ride-map.png) | The map it draws |
| [sample-ride.gpx](sample-ride.gpx) | The made-up ride, in the same GPX format Garmin and Strava export |

## The made-up rider

A strong rider on a regular (non-electric) bike, putting out about 255 W on climbs. That works out
to about 7 mph up the 5.9-mile gravel climb and 11–14 mph down the singletrack.

## How the made-up ride was generated

- **Path:** traced along OpenStreetMap trail lines, plus a few metres of simulated GPS wobble.
- **Elevation:** USGS 10 m terrain data.
- **Speed:** a simple physics model. On climbs it depends on power and steepness; on descents, on
  the trail's difficulty and how tight the corners are.
- **Heart rate:** a simple effort model. It averages the mid-160s on the long climb, stays high on
  technical descents, and drops during the two stops.

The report was made with:

```
python ride_recap.py examples/sample-ride.gpx --note "**Made-up sample.** For privacy, the rider, date, times, speed and heart rate in this report are all invented. The trails, ratings and terrain are real (Raging River State Forest, Washington)."
```
