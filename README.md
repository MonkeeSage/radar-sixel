# radar-sixel

Show the latest NOAA NEXRAD/TDWR radar captures for a station, or MRMS
regional mosaics, as a sixel slideshow in your terminal.

Imagery comes from the NWS/NCEP
[opengeo GeoServer](https://opengeo.ncep.noaa.gov/geoserver/www/index.html)
(WMS for images, WFS for the station list). Each frame shows the radar product
with state/country boundaries and a color legend.

## Install

```sh
pipx install .
# or
pip install .
```

Requires Python 3.10+ and a terminal with sixel support (e.g. foot, WezTerm,
xterm with `-ti vt340`, mlterm, Konsole, or tmux 3.4+ built with sixel).

## Usage

```sh
radar-sixel --list              # all stations with imagery
radar-sixel --list fort         # filter by ID, name or forecast office
radar-sixel -s KABX --products  # products available at a station
radar-sixel -s KABX             # last 10 base reflectivity captures
radar-sixel -s KABX -n 1        # just the newest capture
radar-sixel -s KFWS -p sr_bvel -n 5 -d 0.8 --loop --theme light
radar-sixel -s KFWS -o counties -o warnings      # add map overlays
radar-sixel -s KFWS -s KGRK -s KDYX              # press s to switch stations
radar-sixel -r conus -p cref_qcd -o hazards      # regional mosaic
radar-sixel -s KFWS --marker 32.7555,-97.3308,"Fort Worth" --marker 32.78,-96.8
radar-sixel -s KFWS --marker 32.7555,-97.3308,Home --marker-color "#f80" --marker-opacity 60 --marker-size 32
radar-sixel -r conus -p cref_qcd -n 5 --products # what would be shown
```

`--products` honors the other flags: it lists the products for the selected
station or region, marks the one `-p` (or the default) selects, shows the time
range the newest `-n` captures cover, and describes any `-o` overlays.

The newest `-n` captures are fetched one at a time, oldest first, and the
slideshow advances as each one finishes loading. It plays once and stays on
the last frame until you quit (or loops with `--loop` or the `l` key).

| Key         | Action                                      |
|-------------|---------------------------------------------|
| space       | pause / resume (replays from the start at the end) |
| left/right  | pause and step to the previous / next frame |
| m           | read the full alert messages (with `-o hazards`/`-o warnings`) |
| h, w        | show / hide the `hazards` / `warnings` overlays |
| l           | turn looping on / off                       |
| t           | show / hide the `--marker` pins and labels  |
| s           | switch to the next `-s` station (with several) |
| q, esc      | quit                                        |

Options:

- `-s, --station` / `-r, --region` – a radar station, or one of the MRMS
  mosaics `conus`, `alaska`, `hawaii`, `carib`, `guam`. `-s` is repeatable:
  the `s` key cycles through the stations in the order given. Each station
  starts loading when first shown and resumes where it left off when you come
  back to it; `-p` must be available at every station (without it, each uses
  its own default), and `--products` lists each station's products
- `-p, --product` – base reflectivity by default. NEXRAD: `sr_bref`, `sr_bvel`,
  `bdhc`, `boha`, `bdsa`; TDWR stations (`T...`): `bref1`, `brefl`, `bvel`;
  regions: `bref_qcd`, `cref_qcd` (composite), `neet_v18` (echo tops),
  `pcpn_typ` (precipitation type)
- `-o, --overlay` – repeatable; see below
- `--marker LAT,LON[,LABEL]` – repeatable; draws a pin at 30% opacity at that
  location, with the label (if given) above it. Markers outside the map are
  skipped with a message on stderr
- `--marker-opacity PERCENT` – pin opacity from 0 to 100 (default 30); labels
  are always opaque
- `--marker-size PX` – pin height in pixels (default 22); the label sits just
  above the pin
- `--marker-color HEX` – pin and label color, `#RRGGBB` or `#RGB` (`#`
  optional; default white on the dark theme, black on light). The outline is
  black or white, whichever contrasts more
- `-n, --count` – number of captures (default 10)
- `-d, --delay` – seconds between frames (default 1.5)
- `--loop` – loop instead of stopping on the last frame
- `--size` – maximum image width/height in pixels (default 800; shrunk to fit
  if the terminal reports a smaller window)
- `--theme dark|light` – map background
- `--no-legend` – omit the color legend
- `--force` – skip the terminal sixel support check

Overlays (state and country borders are always drawn):

| Overlay     | Layer                 | Notes |
|-------------|-----------------------|-------|
| `counties`  | `nws:us_counties`     | light grey county lines |
| `provinces` | `nws:ca_provinces`    | Canadian provinces |
| `cwa`       | `nws:cwa_boundary`    | NWS forecast office areas |
| `rfc`       | `nws:rfc_boundary`    | River Forecast Center basins |
| `artcc`     | `nws:artcc_boundary`  | FAA air route traffic control centers |
| `hazards`   | `wwa:hazards`         | advisories/watches/warnings, filled, drawn under the radar |
| `warnings`  | `wwa:warnings`        | tornado/severe/flash flood/marine warnings, outlined on top |

`hazards` and `warnings` are matched to each frame's capture time. The `h`
and `w` keys turn them on and off in the slideshow, whether or not they were
given with `-o`. The frames for a new combination are fetched starting from
the one on screen, and each combination is kept, so switching back is
instant.

With either of them, the alerts in view for the current frame are listed to
the right of the map: one entry per event type with its forecast offices and
end time, colored by significance (warnings red, watches yellow, advisories
cyan), with short-fuse warnings such as tornado and severe thunderstorm
first. If the terminal does not report its pixel size, or leaves fewer than
30 columns beside the map, a single line under the status line shows the most
severe one with a count of the others instead (or "No warning or alert
overlays." without them). Press `m` to pause and read
the full messages (headline, areas, description and instructions) from
[api.weather.gov](https://www.weather.gov/documentation/services-web-api);
scroll with the arrow keys, PgUp/PgDn, Home/End or j/k, and press `m` or esc
to return to the map.
