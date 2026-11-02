#!/usr/bin/env python3
"""Which trails did I ride, and in what order?

Give it a recorded ride: a GPX file from Garmin, Strava, Wahoo, an Apple Watch export
app and so on, or a CSV with latitude/longitude columns. It downloads the trail network
around the ride from OpenStreetMap, works out the ordered list of trails you rode,
prints it, draws a numbered map with an elevation profile, and writes a Markdown report
with distance, climbing, speed and heart rate (when the file has it) for every trail.

    python ride_recap.py my-ride.gpx
    python ride_recap.py my-ride.gpx --title "Tiger Mountain 11/8"

Map data (c) OpenStreetMap contributors, ODbL. Keep that credit on anything you share.
"""
import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone

__version__ = "0.2.0"
REPO_URL = "https://github.com/jj026/ride-trail-sequencer"
USER_AGENT = "ride-trail-sequencer/%s (+%s)" % (__version__, REPO_URL)
OVERPASS_SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
FT_PER_M = 3.280839895
M_PER_MI = 1609.344
MPS_TO_MPH = 3600.0 / M_PER_MI
STALE_DAYS = 14
MAX_SPAN_DEG = 0.4  # refuse rides spanning more than ~40 km; be gentle with the free servers

TRAIL_TYPES = {"path", "footway", "cycleway", "bridleway", "steps"}
ROAD_TYPES = {"track", "service", "unclassified"}  # forest and gravel roads
STREET_TYPES = {"residential", "living_street", "tertiary", "secondary", "primary", "pedestrian"}
HIGHWAY_RE = "^(%s)$" % "|".join(sorted(TRAIL_TYPES | ROAD_TYPES | STREET_TYPES))

RATINGS = {  # OSM mtb:scale:imba -> (label, colour)
    "0": ("green", "#2e7d32"), "1": ("green", "#2e7d32"), "2": ("blue", "#1565c0"),
    "3": ("black", "#111111"), "4": ("double black", "#111111"),
}
KIND_STYLE = {"trail": ("unrated", "#757575"), "road": ("road", "#8d6e63"), "street": ("street", "#a1887f")}
RATING_ICON = {"green": "\U0001F7E2", "blue": "\U0001F535", "black": "\u26AB", "double black": "\u26AB\u26AB",
               "unrated": "\u26AA", "road": "\U0001F7E4", "street": "\U0001F7E4"}
HR_TAGS = ("hr", "heartrate", "heart_rate")  # GPX extension tags, e.g. <gpxtpx:hr>
HR_COLUMNS = ("hr", "heart_rate", "heartrate", "heart rate", "bpm")
GAP_LABEL = "[not on OpenStreetMap]"
GAP_COLOUR = "#d81b60"
TRACK_COLOUR = "#e53935"
CLIMB_COLOUR = "#e65100"


# ---------------------------------------------------------------- track input

def parse_time(text):
    s = (text or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")  # Health Auto Export style
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_hr(text):
    try:
        bpm = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return bpm if 25 <= bpm <= 250 else None


def local_time(t):
    return t.astimezone() if t is not None and t.tzinfo is not None else t


def _tag(el):
    return el.tag.rsplit("}", 1)[-1]


def load_track(path):
    """Return ([(lat, lon, elevation_m, datetime, heart_rate), ...], track name); missing values are None."""
    ext = os.path.splitext(path)[1].lower()
    pts, name = [], None
    if ext == ".gpx":
        root = ET.parse(path).getroot()
        for el in root.iter():
            tag = _tag(el)
            if tag == "trk" and name is None:
                for ch in el:
                    if _tag(ch) == "name" and ch.text and ch.text.strip():
                        name = ch.text.strip()
            if tag != "trkpt":
                continue
            ele = when = bpm = None
            for ch in el:
                if _tag(ch) == "ele" and ch.text:
                    try:
                        ele = float(ch.text)
                    except ValueError:
                        pass
                elif _tag(ch) == "time":
                    when = parse_time(ch.text)
            for d in el.iter():  # heart rate lives in <extensions>, e.g. <gpxtpx:hr>
                if _tag(d).lower() in HR_TAGS and d.text:
                    bpm = parse_hr(d.text)
                    break
            pts.append((float(el.get("lat")), float(el.get("lon")), ele, when, bpm))
        if not pts and any(_tag(el) == "rtept" for el in root.iter()):
            sys.exit("This GPX is a planned route, not a recorded ride (it has no track points).")
    elif ext == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                try:
                    la = float(r.get("latitude") or r.get("lat"))
                    lo = float(r.get("longitude") or r.get("lon") or r.get("lng"))
                except (TypeError, ValueError):
                    continue
                alt = r.get("altitude") or r.get("elevation") or r.get("ele")
                try:
                    alt = float(alt) if alt else None
                except ValueError:
                    alt = None
                bpm = parse_hr(next((r[c] for c in HR_COLUMNS if r.get(c)), None))
                pts.append((la, lo, alt, parse_time(r.get("timestamp") or r.get("time")), bpm))
    else:
        sys.exit("Unsupported file type %r. Give it a .gpx or .csv track." % ext)

    out = []  # a paused device can log the same fix many times
    for p in pts:
        if not out or (p[0], p[1]) != (out[-1][0], out[-1][1]):
            out.append(p)
    if len(out) < 2:
        sys.exit("No usable GPS points in %s." % path)
    return out, name


# ---------------------------------------------------------------- geometry

class Projection:
    """Local equirectangular projection to metres; plenty for a single riding area."""

    def __init__(self, lat0):
        self.ky = 111320.0
        self.kx = 111320.0 * math.cos(math.radians(lat0))

    def __call__(self, lat, lon):
        return lon * self.kx, lat * self.ky


def seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    u = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + u * dx), py - (y1 + u * dy))


class WayIndex:
    """Grid index over way segments for fast nearest-way lookups."""

    def __init__(self, ways, proj, pad, cell=60.0):
        self.cell, self.grid = cell, defaultdict(list)
        for k, w in enumerate(ways):
            pts = [proj(*c) for c in w["coords"]]
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                for gx in range(int((min(x1, x2) - pad) // cell), int((max(x1, x2) + pad) // cell) + 1):
                    for gy in range(int((min(y1, y2) - pad) // cell), int((max(y1, y2) + pad) // cell) + 1):
                        self.grid[(gx, gy)].append((k, x1, y1, x2, y2))

    def nearest(self, x, y, limit):
        best, found = limit, None
        for k, x1, y1, x2, y2 in self.grid.get((int(x // self.cell), int(y // self.cell)), ()):
            d = seg_dist(x, y, x1, y1, x2, y2)
            if d <= best:
                best, found = d, k
        return found


# ---------------------------------------------------------------- OpenStreetMap

def data_age_days(stamp):
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400.0


def warn_if_stale(stamp):
    age = data_age_days(stamp)
    if age is not None and age > STALE_DAYS:
        print("  WARNING: this map data is %d days old, so recently added trails will be missing. "
              "Re-run later with --refresh to try for a fresher copy." % age)


def fetch_osm(bbox, cache_path, refresh=False):
    s, w, n, e = bbox
    query = ('[out:json][timeout:120];'
             'way["highway"~"%s"]["service"!~"^(driveway|parking_aisle|drive-through)$"]'
             '(%.5f,%.5f,%.5f,%.5f);out tags geom;') % (HIGHWAY_RE, s, w, n, e)
    if cache_path and os.path.exists(cache_path) and not refresh:
        try:
            cached = json.load(open(cache_path, encoding="utf-8"))
            if cached.get("query") == query:
                stamp = cached["osm"].get("osm3s", {}).get("timestamp_osm_base", "?")
                print("map data: cached copy (OpenStreetMap as of %s); --refresh downloads it again" % stamp)
                warn_if_stale(stamp)
                return cached["osm"]
        except (ValueError, KeyError):
            pass
    body = urllib.parse.urlencode({"data": query}).encode()
    last_error, fallback = None, None
    for attempt in range(2):
        for url in OVERPASS_SERVERS:
            host = urllib.parse.urlparse(url).netloc
            try:
                req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
                osm = json.loads(urllib.request.urlopen(req, timeout=150).read())
                if "elements" not in osm:
                    raise ValueError("unexpected response")
            except urllib.error.HTTPError as exc:  # 429 = busy, 502/504 = overloaded
                last_error = "HTTP %d" % exc.code
                print("  %s didn't answer (%s); trying the next server" % (host, last_error))
                continue
            except Exception as exc:  # timeouts, connection errors, bad JSON
                last_error = type(exc).__name__
                print("  %s didn't answer (%s); trying the next server" % (host, last_error))
                continue
            stamp = osm.get("osm3s", {}).get("timestamp_osm_base", "?")
            age = data_age_days(stamp)
            if age is not None and age > STALE_DAYS:
                # Mirrors can lag the main server by months; keep looking for a current copy.
                print("  %s answered, but its map data is %d days old; looking for a fresher copy" % (host, age))
                fallback = fallback or (osm, host, stamp)
                continue
            return _accept_osm(osm, host, stamp, query, cache_path)
        if attempt == 0:
            time.sleep(15)
    if fallback:
        return _accept_osm(fallback[0], fallback[1], fallback[2], query, cache_path)
    sys.exit("Couldn't reach an OpenStreetMap (Overpass) server (%s).\n"
             "They're free shared services and get busy. Try again in a few minutes." % last_error)


def _accept_osm(osm, host, stamp, query, cache_path):
    print("map data: %d paths and roads from %s (OpenStreetMap as of %s)"
          % (sum(1 for x in osm["elements"] if x.get("type") == "way"), host, stamp))
    warn_if_stale(stamp)
    if cache_path:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"query": query, "osm": osm}, f)
    return osm


def load_ways(osm):
    ways = []
    for el in osm.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        tags = el.get("tags", {})
        hw = tags.get("highway", "")
        kind = "trail" if hw in TRAIL_TYPES else ("road" if hw in ROAD_TYPES else "street")
        name = (tags.get("name") or tags.get("mtb:name") or "").strip()
        ways.append({
            "label": name or "unnamed " + kind, "named": bool(name), "kind": kind,
            "imba": tags.get("mtb:scale:imba"), "bicycle": tags.get("bicycle"),
            "coords": [(g["lat"], g["lon"]) for g in el["geometry"]],
        })
    return ways


# ---------------------------------------------------------------- matching

def smooth_labels(labels, window):
    out = []
    for i in range(len(labels)):
        c = Counter(x for x in labels[max(0, i - window):i + window + 1] if x)
        out.append(c.most_common(1)[0][0] if c else None)
    return out


def build_legs(smooth, step, s0, s1, min_run, gap_min):
    """Collapse per-point labels into ordered legs; None = a stretch with no mapped path."""
    runs, cur, start = [], smooth[s0], s0
    for i in range(s0 + 1, s1 + 1):
        if smooth[i] != cur:
            runs.append([cur, start, i - 1])
            cur, start = smooth[i], i
    runs.append([cur, start, s1])
    items = [[lab, sum(step[a + 1:b + 1]), a, b] for lab, a, b in runs]
    items = [r for r in items if r[1] >= (gap_min if r[0] is None else min_run)]
    legs = []
    for r in items:
        if legs and legs[-1][0] == r[0]:
            legs[-1][1] += r[1]
            legs[-1][3] = r[3]
        else:
            legs.append(r)
    return legs


def find_climbs(z, cum, lo, hi, min_ft, min_m):
    """Sustained climbs in [lo, hi]: rising stretches, bridged across brief dips."""
    if min_ft <= 0:
        return []
    spans, i = [], lo
    while i < hi:
        if z[i + 1] > z[i]:
            j = i
            while j < hi and z[j + 1] > z[j]:
                j += 1
            if spans and cum[i] - cum[spans[-1][1]] < 40 and (z[spans[-1][1]] - z[i]) * FT_PER_M < 8:
                spans[-1] = (spans[-1][0], j)
            else:
                spans.append((i, j))
            i = j
        else:
            i += 1
    return [(a, b) for a, b in spans if (z[b] - z[a]) * FT_PER_M >= min_ft and cum[b] - cum[a] >= min_m]


def hms(seconds):
    seconds = int(round(seconds))
    return "%d h %02d m" % (seconds // 3600, seconds % 3600 // 60)


# ---------------------------------------------------------------- rendering

def render(out_path, title, ctx):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    track, ways, legs, raw, smooth = ctx["track"], ctx["ways"], ctx["legs"], ctx["raw"], ctx["smooth"]
    s0, s1, proj, z, cum, climbs = ctx["s0"], ctx["s1"], ctx["proj"], ctx["z"], ctx["cum"], ctx["climbs"]
    lat = [p[0] for p in track[s0:s1 + 1]]
    lon = [p[1] for p in track[s0:s1 + 1]]
    pad = 0.002
    span_y = (max(lat) - min(lat) + 2 * pad) * proj.ky
    span_x = (max(lon) - min(lon) + 2 * pad) * proj.kx
    ratio = span_x / span_y
    map_h, map_w = 12.0, 12.0 * ratio  # size the figure to the ride's shape so the map fills its cell
    if map_w > 12.0:
        map_w, map_h = 12.0, max(5.0, 12.0 / ratio)
    map_w = max(4.5, map_w)
    has_prof = z is not None
    side_w = 3.9
    prof_h = 3.0 if has_prof else 0.0
    fig_h = map_h + prof_h + 1.0
    fig = plt.figure(figsize=(map_w + side_w, fig_h), dpi=130)
    margins = dict(top=1.0 - 0.55 / fig_h, bottom=0.45 / fig_h, left=0.04, right=0.98, wspace=0.03)
    if has_prof:
        gs = fig.add_gridspec(2, 2, width_ratios=[map_w, side_w], height_ratios=[map_h, prof_h],
                              hspace=0.9 / ((map_h + prof_h) / 2.0), **margins)
    else:
        gs = fig.add_gridspec(1, 2, width_ratios=[map_w, side_w], **margins)
    ax = fig.add_subplot(gs[0, 0])
    side = fig.add_subplot(gs[0, 1])
    side.axis("off")

    for w in ways:  # the whole network, faint
        ax.plot([c[1] for c in w["coords"]], [c[0] for c in w["coords"]], color="#dddddd", lw=0.9, zorder=1)
    colour_of = {}
    for leg in legs:
        colour_of.setdefault(leg["label"], leg["colour"])
    ridden = {raw[i] for i in range(s0, s1 + 1)
              if raw[i] is not None and ways[raw[i]]["label"] == smooth[i] and smooth[i] in colour_of}
    for k in ridden:
        w = ways[k]
        ax.plot([c[1] for c in w["coords"]], [c[0] for c in w["coords"]], color=colour_of[w["label"]],
                lw=3.0, zorder=3, solid_capstyle="round")
    ax.plot(lon, lat, color=TRACK_COLOUR, lw=1.0, alpha=0.6, zorder=4)
    for leg in legs:
        if leg["gap"]:
            seg = track[leg["start"]:leg["end"] + 1]
            ax.plot([p[1] for p in seg], [p[0] for p in seg], color=GAP_COLOUR, lw=2.6, ls=(0, (3, 2)), zorder=5)
    for a, b in climbs:
        seg = track[a:b + 1]
        ax.plot([p[1] for p in seg], [p[0] for p in seg], color=CLIMB_COLOUR, lw=3.2, alpha=0.9, zorder=5,
                solid_capstyle="round")
    labelled = set()
    for leg in sorted(legs, key=lambda L: -L["dist"]):
        if leg["gap"] or not leg["named"] or leg["label"] in labelled:
            continue
        labelled.add(leg["label"])
        mid = track[(leg["start"] + leg["end"]) // 2]
        ax.annotate(leg["label"], (mid[1], mid[0]), fontsize=7.5, fontweight="bold", color=leg["colour"],
                    ha="center", va="bottom", xytext=(0, 7), textcoords="offset points", zorder=7,
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.8))
    for n, leg in enumerate(legs, 1):
        p = track[leg["start"]]
        ax.scatter([p[1]], [p[0]], s=220, color="white", ec="black", lw=1.2, zorder=10)
        ax.text(p[1], p[0], str(n), fontsize=8, fontweight="bold", ha="center", va="center", zorder=11)
    st, fi = track[s0], track[s1]
    loop = math.hypot(*(a - b for a, b in zip(proj(st[0], st[1]), proj(fi[0], fi[1])))) < 150
    if loop:
        ax.scatter([st[1]], [st[0]], marker="*", s=420, color="#ffb300", ec="black", lw=1.2, zorder=12)
    else:
        ax.scatter([st[1]], [st[0]], marker="^", s=200, color="#43a047", ec="black", lw=1.2, zorder=12)
        ax.scatter([fi[1]], [fi[0]], marker="s", s=150, color="#212121", ec="white", lw=1.2, zorder=12)
    ax.set_aspect(1.0 / math.cos(math.radians(sum(lat) / len(lat))))
    ax.set_xlim(min(lon) - pad, max(lon) + pad)
    ax.set_ylim(min(lat) - pad, max(lat) + pad)
    ax.set_xticks([])
    ax.set_yticks([])
    fig.suptitle(title, fontsize=13, fontweight="bold", y=1.0 - 0.18 / fig_h)
    ax.text(0.995, 0.005, "Map data \u00a9 OpenStreetMap contributors", transform=ax.transAxes, ha="right",
            va="bottom", fontsize=7, color="#444444", zorder=13,
            bbox=dict(boxstyle="square,pad=0.2", fc="white", ec="none", alpha=0.8))

    handles = [Line2D([0], [0], color=c, lw=3, label=l) for l, c in
               (("green", "#2e7d32"), ("blue", "#1565c0"), ("black", "#111111"),
                ("unrated trail", "#757575"), ("road", "#8d6e63"))]
    handles.append(Line2D([0], [0], color=TRACK_COLOUR, lw=1.5, label="your GPS track"))
    if any(leg["gap"] for leg in legs):
        handles.append(Line2D([0], [0], color=GAP_COLOUR, lw=2.6, ls=(0, (3, 2)), label="not on OpenStreetMap"))
    if climbs:
        handles.append(Line2D([0], [0], color=CLIMB_COLOUR, lw=3.2, label="climbing"))
    if loop:
        handles.append(Line2D([0], [0], marker="*", color="w", markerfacecolor="#ffb300", markeredgecolor="k",
                              markersize=14, label="start / finish"))
    else:
        handles += [Line2D([0], [0], marker="^", color="w", markerfacecolor="#43a047", markeredgecolor="k",
                           markersize=11, label="start"),
                    Line2D([0], [0], marker="s", color="w", markerfacecolor="#212121", markeredgecolor="w",
                           markersize=10, label="finish")]
    lg = side.legend(handles=handles, loc="upper left", fontsize=8.5, borderaxespad=0.0)
    fig.canvas.draw()
    bb = lg.get_window_extent(fig.canvas.get_renderer())
    y_top = side.transAxes.inverted().transform((bb.x0, bb.y0))[1] - 0.03
    lines = ["RIDE ORDER"] + ["%2d. %s" % (n, leg["label"][:34]) for n, leg in enumerate(legs, 1)]
    size = 8.0 if len(legs) <= 30 else (7.0 if len(legs) <= 45 else 6.0)
    side.text(0.0, y_top, "\n".join(lines), transform=side.transAxes, va="top", ha="left", fontsize=size,
              family="monospace")

    if has_prof:
        ax2 = fig.add_subplot(gs[1, 0])
        xs = [(cum[i] - cum[s0]) / M_PER_MI for i in range(s0, s1 + 1)]
        ys = [z[i] * FT_PER_M for i in range(s0, s1 + 1)]
        lo_y, hi_y = min(ys), max(ys)
        rng = max(hi_y - lo_y, 60.0)
        ax2.fill_between(xs, lo_y - 0.06 * rng, ys, color="#cfd8dc", zorder=1)
        for leg in legs:
            a, b = leg["start"] - s0, leg["end"] - s0
            if b > a:
                ax2.plot(xs[a:b + 1], ys[a:b + 1], color=leg["colour"], lw=2.4, zorder=3,
                         ls=(0, (3, 2)) if leg["gap"] else "-")
        for a, b in climbs:
            ax2.plot(xs[a - s0:b - s0 + 1], ys[a - s0:b - s0 + 1], color=CLIMB_COLOUR, lw=3.8, zorder=4,
                     solid_capstyle="round")
            gain = ys[b - s0] - ys[a - s0]
            if gain >= 100:
                mid = (a + b) // 2 - s0
                ax2.annotate("+%d ft" % round(gain), (xs[mid], ys[mid]), textcoords="offset points",
                             xytext=(0, 10), ha="center", fontsize=8, fontweight="bold", color=CLIMB_COLOUR,
                             zorder=7)
        prev_x, row = None, 0
        for n, leg in enumerate(legs, 1):
            x = xs[leg["start"] - s0]
            ax2.axvline(x, color="#9e9e9e", lw=0.7, ls=":", zorder=2)
            row = (row + 1) % 3 if prev_x is not None and x - prev_x < 0.035 * max(xs[-1], 0.1) else 0
            ax2.text(x, hi_y + (0.05 + 0.075 * row) * rng, str(n), fontsize=8, fontweight="bold", ha="center",
                     zorder=5)
            prev_x = x
        ax2.set_xlim(0, max(xs[-1], 0.01))
        ax2.set_ylim(lo_y - 0.08 * rng, hi_y + 0.32 * rng)
        ax2.set_xlabel("miles", fontsize=9)
        ax2.set_ylabel("elevation (ft)", fontsize=9)
        ax2.grid(alpha=0.25)
        ax2.set_title("Elevation: +{:,.0f} / \u2212{:,.0f} ft while moving (from your device, roughly "
                      "\u00b15%; numbers = ride order)".format(ctx["gain_ft"], ctx["loss_ft"]), fontsize=9)

    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)


# ---------------------------------------------------------------- report

def md_escape(text):
    for ch in "\\`*_[]<>|":
        text = text.replace(ch, "\\" + ch)
    return text


def write_report(path, map_path, ctx):
    """Write a Markdown report: summary, map, and the trail-order table."""
    legs = ctx["legs"]
    have_time, have_hr, have_elev = ctx["have_time"], ctx["have_hr"], ctx["have_elev"]
    dash = "\u2014"
    try:
        rel = os.path.relpath(os.path.abspath(map_path), os.path.dirname(os.path.abspath(path)))
    except ValueError:  # map and report on different Windows drives
        rel = os.path.abspath(map_path)

    lines = ["# " + md_escape(ctx["title"]), ""]
    if ctx["note"]:
        lines += ["> " + line for line in ctx["note"].splitlines()] + [""]
    lines += ["![Ride map](<%s>)" % rel.replace(os.sep, "/"), "", "| | |", "|---|---|"]
    when = ctx["when"]
    if when is not None:
        stamp = "%s, %s %d, %d" % (when.strftime("%a"), when.strftime("%b"), when.day, when.year)
        if have_time:
            stamp += " \u00b7 %s\u2013%s" % (when.strftime("%H:%M"), ctx["end"].strftime("%H:%M"))
        lines.append("| **When** | %s |" % stamp)
    lines.append("| **Distance** | %.1f mi \u00b7 %d%% on named trails and roads |"
                 % (ctx["miles"], ctx["named_pct"]))
    if have_elev:
        lines.append("| **Climbing** | +{:,.0f} / \u2212{:,.0f} ft |".format(ctx["gain_ft"], ctx["loss_ft"]))
    if have_time:
        lines.append("| **Time** | %s total \u00b7 %s moving |" % (hms(ctx["elapsed"]), hms(ctx["moved"])))
        if ctx["mph"]:
            lines.append("| **Moving speed** | %.1f mph average |" % ctx["mph"])
    if have_hr:
        lines.append("| **Heart rate** | avg %d \u00b7 max %d bpm |" % (round(ctx["hr_avg"]), round(ctx["hr_max"])))

    def rating(leg):
        if leg["gap"]:
            return dash
        return (RATING_ICON.get(leg["rating"], "") + " " + leg["rating"]).strip()

    cols = [("#", "--:", lambda n, L: str(n)),
            ("Trail", "---", lambda n, L: ("**%s**" if L["gap"] else "%s") % md_escape(L["label"])),
            ("Rating", "---", lambda n, L: rating(L)),
            ("mi", "--:", lambda n, L: "%.2f" % (L["dist"] / M_PER_MI))]
    if have_time:
        cols.append(("mph", "--:", lambda n, L: "%.1f" % L["mph"] if L["mph"] else dash))
    if have_elev:
        cols.append(("\u00b1ft", "---",
                     lambda n, L: "+{:,.0f} / \u2212{:,.0f}".format(L["up_ft"], L["down_ft"])))
    if have_time:
        cols.append(("Time", "---", lambda n, L: "%s\u2013%s" % L["span"] if L["span"] else dash))
    if have_hr:
        cols.append(("HR avg / max", "---",
                     lambda n, L: "%d / %d" % (round(L["hr"][0]), round(L["hr"][1])) if L["hr"] else dash))
    if any(L["note"] for L in legs):
        cols.append(("Note", "---", lambda n, L: L["note"]))
    lines += ["", "## Trail order", "",
              "| " + " | ".join(c[0] for c in cols) + " |",
              "|" + "|".join(c[1] for c in cols) + "|"]
    lines += ["| " + " | ".join(c[2](n, L) for c in cols) + " |" for n, L in enumerate(legs, 1)]
    lines += ["", "**Sequence:** " + " \u2192 ".join(md_escape(L["label"]) for L in legs), "",
              "## How to read this", ""]
    if have_time:
        lines.append("- **mph** is moving speed; time spent stopped doesn't count.")
    if have_elev:
        lines.append("- **Climbing** and **\u00b1ft** only count while moving, so altimeter drift during stops "
                     "doesn't show up as climbing. They come from the device and are roughly \u00b15%.")
    if have_hr:
        lines.append("- **Heart rate** is what the device recorded, averaged over each trail.")
    lines.append("- **Rating** is OpenStreetMap's `mtb:scale:imba` tag; *unrated* means nobody has tagged "
                 "the trail yet.")
    if any(L["gap"] for L in legs):
        lines.append("- **%s** is a stretch with no trail on OpenStreetMap yet (dashed pink on the map)."
                     % md_escape(GAP_LABEL))
    lines += ["", "---", "",
              "Made with [ride-trail-sequencer](%s) %s. Map data \u00a9 "
              "[OpenStreetMap contributors](https://www.openstreetmap.org/copyright)." % (REPO_URL, __version__)]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- main

def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # Windows consoles can't print every trail name
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        description="Which trails did I ride? Matches a GPS track to OpenStreetMap trails and draws a "
                    "numbered map.",
        epilog="Map data (c) OpenStreetMap contributors, ODbL. Keep the credit on anything you share.")
    ap.add_argument("track", help="recorded ride: .gpx, or .csv with latitude/longitude columns")
    ap.add_argument("--title", help="map title (default: the ride's name and date)")
    ap.add_argument("--out", help="output map PNG (default: <track>-map.png next to the track)")
    ap.add_argument("--report", help="output Markdown report (default: <track>-report.md next to the map)")
    ap.add_argument("--no-report", action="store_true", help="don't write the Markdown report")
    ap.add_argument("--note", help="text to put at the top of the report (Markdown allowed)")
    ap.add_argument("--refresh", action="store_true", help="re-download map data instead of using the cache")
    ap.add_argument("--no-trim", action="store_true",
                    help="keep the whole track; by default streets before the first trail and after the "
                         "last are cut off, so the map doesn't show where you live")
    ap.add_argument("--hide-ends-m", type=float, default=0.0,
                    help="also cut this many metres off both ends, for extra privacy (default 0)")
    ap.add_argument("--thresh", type=float, default=25.0,
                    help="how close (metres) a GPS point must be to a trail to count as on it (default 25)")
    ap.add_argument("--min-run", type=float, default=60.0,
                    help="ignore trails ridden for less than this many metres, e.g. crossings (default 60)")
    ap.add_argument("--gap-min", type=float, default=100.0,
                    help="flag stretches with no mapped path longer than this many metres (default 100)")
    ap.add_argument("--window", type=int, default=6, help="GPS points either side used to smooth matching")
    ap.add_argument("--no-profile", action="store_true", help="skip the elevation profile")
    ap.add_argument("--elev-smooth", type=float, default=15.0,
                    help="seconds either side used to smooth elevation (default 15)")
    ap.add_argument("--climb-min-ft", type=float, default=25.0, help="smallest climb to highlight (0 = off)")
    ap.add_argument("--climb-min-m", type=float, default=150.0, help="shortest climb to highlight, metres")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    a = ap.parse_args(argv)

    track, trk_name = load_track(a.track)
    out = a.out or os.path.splitext(a.track)[0] + "-map.png"
    cache = os.path.splitext(out)[0] + ".osm.json"
    base = os.path.splitext(out)[0]
    report = a.report or (base[:-4] if base.endswith("-map") else base) + "-report.md"
    lats = [p[0] for p in track]
    lons = [p[1] for p in track]
    if max(lats) - min(lats) > MAX_SPAN_DEG or max(lons) - min(lons) > MAX_SPAN_DEG:
        sys.exit("This track covers too large an area for one riding area (over ~40 km). "
                 "Trim it to the mountain-bike part first.")
    proj = Projection(sum(lats) / len(lats))
    xy = [proj(p[0], p[1]) for p in track]
    step = [0.0] + [math.hypot(xy[i][0] - xy[i - 1][0], xy[i][1] - xy[i - 1][1]) for i in range(1, len(xy))]
    cum = [0.0] * len(step)
    for i in range(1, len(step)):
        cum[i] = cum[i - 1] + step[i]

    print("track: %s, %d points, %.2f mi" % (os.path.basename(a.track), len(track), cum[-1] / M_PER_MI))
    pad = 0.003
    osm = fetch_osm((min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad), cache, a.refresh)
    ways = load_ways(osm)
    if not ways:
        sys.exit("OpenStreetMap has no paths or roads around this ride.")

    index = WayIndex(ways, proj, pad=max(30.0, a.thresh + 5.0))
    raw = [index.nearest(x, y, a.thresh) for x, y in xy]
    smooth = smooth_labels([ways[k]["label"] if k is not None else None for k in raw], a.window)
    kind_of, named = {}, {}
    for w in ways:
        kind_of.setdefault(w["label"], w["kind"])
        named.setdefault(w["label"], w["named"])

    s0, s1 = 0, len(track) - 1
    if not a.no_trim:
        keep = [lab is not None and kind_of.get(lab) != "street" for lab in smooth]
        if not any(keep):
            sys.exit("None of this ride is on a mapped trail or forest road. Is it a road ride, or is "
                     "the area not mapped in OpenStreetMap yet?")
        s0 = keep.index(True)
        s1 = len(keep) - 1 - keep[::-1].index(True)
    if a.hide_ends_m > 0:
        base0, base1 = cum[s0], cum[s1]
        while s0 < s1 and cum[s0] - base0 < a.hide_ends_m:
            s0 += 1
        while s1 > s0 and base1 - cum[s1] < a.hide_ends_m:
            s1 -= 1
    if cum[s1] - cum[s0] < 100:
        sys.exit("Less than 100 m of this ride is on mapped trails, so there's nothing to sequence.")

    times = [p[3] for p in track]
    have_time = all(t is not None for t in times[s0:s1 + 1])
    moving = [True] * len(track)
    if have_time:
        for i in range(1, len(track)):
            if times[i] is not None and times[i - 1] is not None:
                dt = (times[i] - times[i - 1]).total_seconds()
                moving[i] = dt > 0 and step[i] / dt >= 0.5
    hrs = [p[4] for p in track]
    have_hr = any(h is not None for h in hrs[s0:s1 + 1])

    alts = [p[2] for p in track]
    have_elev = not a.no_profile and sum(1 for v in alts[s0:s1 + 1] if v is not None) >= (s1 - s0 + 1) // 2
    z = None
    gain = loss = 0.0
    if have_elev:
        first = next(v for v in alts if v is not None)
        filled, last = [], first
        for v in alts:
            last = v if v is not None else last
            filled.append(last)
        pre = [0.0]
        for v in filled:
            pre.append(pre[-1] + v)
        # Smooth over a fixed time span, not a fixed number of points: Garmin "smart" recording logs a
        # point every few seconds, and a 15-point window there flattens real climbs.
        dts = sorted((times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))
                     if times[i] is not None and times[i - 1] is not None and times[i] > times[i - 1])
        interval = dts[len(dts) // 2] if dts else 1.0
        W = max(1, int(round(a.elev_smooth / interval)))
        z = [(pre[min(len(filled), i + W + 1)] - pre[max(0, i - W)]) / (min(len(filled), i + W + 1) - max(0, i - W))
             for i in range(len(filled))]
        for i in range(s0 + 1, s1 + 1):
            if moving[i]:
                gain += max(0.0, z[i] - z[i - 1])
                loss += max(0.0, z[i - 1] - z[i])

    legs = []
    for lab, dist, a_i, b_i in build_legs(smooth, step, s0, s1, a.min_run, a.gap_min):
        leg = {"label": lab or GAP_LABEL, "gap": lab is None, "named": bool(lab and named.get(lab)),
               "dist": dist, "start": a_i, "end": b_i, "note": ""}
        if leg["gap"]:
            leg["rating"], leg["colour"] = "-", GAP_COLOUR
            leg["note"] = "a trail OpenStreetMap doesn't have"
        else:
            ks = [raw[i] for i in range(a_i, b_i + 1) if raw[i] is not None and ways[raw[i]]["label"] == lab]
            kind = Counter(ways[k]["kind"] for k in ks).most_common(1)[0][0] if ks else kind_of.get(lab, "trail")
            imba = Counter(ways[k]["imba"] for k in ks if ways[k]["imba"] in RATINGS).most_common(1)
            leg["rating"], leg["colour"] = RATINGS[imba[0][0]] if imba else KIND_STYLE[kind]
            bikes = Counter(ways[k]["bicycle"] or "-" for k in ks).most_common(1)
            if kind == "trail" and dist >= 200 and bikes and bikes[0][0] == "no":
                leg["note"] = "bikes not allowed, per OSM"
        legs.append(leg)
    for n, leg in enumerate(legs):
        stop = legs[n + 1]["start"] if n + 1 < len(legs) else s1
        up = down = 0.0
        if z is not None:
            for i in range(leg["start"] + 1, stop + 1):
                if moving[i]:
                    up += max(0.0, z[i] - z[i - 1])
                    down += max(0.0, z[i - 1] - z[i])
        leg["up_ft"], leg["down_ft"] = up * FT_PER_M, down * FT_PER_M
        leg["mph"] = leg["span"] = None
        if have_time:
            dist_m = secs = 0.0
            for i in range(leg["start"] + 1, leg["end"] + 1):
                if moving[i]:
                    dist_m += step[i]
                    secs += (times[i] - times[i - 1]).total_seconds()
            leg["mph"] = dist_m / secs * MPS_TO_MPH if secs > 0 else None
            leg["span"] = (local_time(times[leg["start"]]).strftime("%H:%M"),
                           local_time(times[leg["end"]]).strftime("%H:%M"))
        vals = [hrs[i] for i in range(leg["start"], leg["end"] + 1) if hrs[i] is not None]
        leg["hr"] = (sum(vals) / len(vals), max(vals)) if vals else None

    total = cum[s1] - cum[s0]
    on_map = sum(step[i] for i in range(s0 + 1, s1 + 1) if smooth[i] is not None)
    on_named = sum(step[i] for i in range(s0 + 1, s1 + 1) if smooth[i] is not None and named.get(smooth[i]))
    climbs = find_climbs(z, cum, s0, s1, a.climb_min_ft, a.climb_min_m) if z is not None else []

    when = local_time(times[s0])
    if a.title:
        title = a.title
    else:
        title = trk_name or os.path.splitext(os.path.basename(a.track))[0]
        if when is not None:
            title += " - %s %d, %d" % (when.strftime("%b"), when.day, when.year)
    elapsed = moved = 0.0
    mph = None
    if have_time:
        elapsed = (times[s1] - times[s0]).total_seconds()
        moved = sum((times[i] - times[i - 1]).total_seconds() for i in range(s0 + 1, s1 + 1) if moving[i])
        moved_m = sum(step[i] for i in range(s0 + 1, s1 + 1) if moving[i])
        mph = moved_m / moved * MPS_TO_MPH if moved > 0 else None
    hr_vals = [h for h in hrs[s0:s1 + 1] if h is not None]
    hr_avg = sum(hr_vals) / len(hr_vals) if hr_vals else None
    hr_max = max(hr_vals) if hr_vals else None

    head = "%s: %.1f mi" % (title, total / M_PER_MI)
    if z is not None:
        head += ", +{:,.0f} ft".format(gain * FT_PER_M)
    if have_time:
        head += ", %s (%s moving%s)" % (hms(elapsed), hms(moved), ", %.1f mph" % mph if mph else "")

    print()
    print(head)
    print("%d%% of the ride is on mapped paths and roads; %d%% is on named ones."
          % (round(100 * on_map / total), round(100 * on_named / total)))
    if have_hr:
        print("Heart rate: avg %d, max %d bpm." % (round(hr_avg), round(hr_max)))
    print()
    print(" #  %-36s %-12s %6s %5s  %-14s %-11s %-8s %s"
          % ("Trail", "Rating", "mi", "mph", "up/down ft", "Time", "HR", "Note"))
    for n, leg in enumerate(legs, 1):
        updown = "+{:,.0f}/-{:,.0f}".format(leg["up_ft"], leg["down_ft"]) if z is not None else ""
        span = "%s-%s" % leg["span"] if leg["span"] else ""
        speed = "%.1f" % leg["mph"] if leg["mph"] else ""
        bpm = "%d/%d" % (round(leg["hr"][0]), round(leg["hr"][1])) if leg["hr"] else ""
        print("%2d  %-36s %-12s %6.2f %5s  %-14s %-11s %-8s %s"
              % (n, leg["label"][:36], leg["rating"], leg["dist"] / M_PER_MI, speed, updown, span, bpm,
                 leg["note"]))
    print()
    print("Sequence: " + " -> ".join(leg["label"] for leg in legs))
    if any(leg["gap"] for leg in legs):
        print("\n%s = you rode something OpenStreetMap doesn't have yet (dashed pink on the map). "
              "You can add it at https://www.openstreetmap.org using your own GPS track." % GAP_LABEL)

    render(out, title + " - %.1f mi" % (total / M_PER_MI), {
        "track": track, "ways": ways, "legs": legs, "raw": raw, "smooth": smooth, "s0": s0, "s1": s1,
        "proj": proj, "z": z, "cum": cum, "climbs": climbs,
        "gain_ft": gain * FT_PER_M, "loss_ft": loss * FT_PER_M})
    print("\nmap: %s" % os.path.abspath(out))
    if not a.no_report:
        write_report(report, out, {
            "title": title, "note": a.note, "legs": legs, "when": when, "end": local_time(times[s1]),
            "have_time": have_time, "have_hr": have_hr, "have_elev": z is not None,
            "miles": total / M_PER_MI, "named_pct": round(100 * on_named / total),
            "gain_ft": gain * FT_PER_M, "loss_ft": loss * FT_PER_M, "elapsed": elapsed, "moved": moved,
            "mph": mph, "hr_avg": hr_avg, "hr_max": hr_max})
        print("report: %s" % os.path.abspath(report))


if __name__ == "__main__":
    main()
