# Flight Near Me

Live display of aircraft passing overhead, driven by a dump1090 receiver.

- **Radar** centred on your location, with range rings, altitude-coloured
  aircraft, heading arrows, 3-minute trails, a coastline outline and
  landmark markers (e.g. your nearest airport).
- **Featured card** for the nearest aircraft. It turns gold and says
  **OVERHEAD** when the aircraft is within `overhead_radius`, and tells you
  where to look ("Look NE, 42° above horizon").
- **List** of every aircraft with a position, sorted by distance, showing
  type, altitude, speed, climb/descent, route and bearing.
- **Online lookups** of type, registration, airline and route from
  [adsbdb.com](https://www.adsbdb.com), cached in `lookup_cache.json`.
  If a cached route doesn't fit where the aircraft actually is, the route is
  hidden; the airline is still shown.

Data comes from dump1090's SBS/BaseStation feed (port 30003).

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json`: set your `latitude`/`longitude`, your dump1090 `feed_host`,
set `coastline_file` to `coastline.json`, and change the landmarks. Then build
your coastline (see [Coastline](#coastline)).

The example config is centred on Fistral Beach, Newquay, with Newquay Airport
(EGHQ) as a landmark. Without a `config.json` the program runs with the example.
`config.json`, `coastline.json` and `lookup_cache.json` are git-ignored, so your
location stays out of the repo.

## Running

```sh
.venv/bin/python flight_near_me.py            # fullscreen
.venv/bin/python flight_near_me.py --windowed # in a window
```

Options: `--host`, `--port`, `--no-lookups`, `--config path/to/config.json`.

Keys: `+`/`-` or mouse wheel to zoom, `F` toggles fullscreen, `S` saves a
screenshot, `Q`/`Esc` quits.

## Configuration (`config.json`)

| key | meaning |
| --- | --- |
| `latitude`, `longitude` | your location |
| `feed_host`, `feed_port` | dump1090 SBS feed |
| `units` | `km`, `nm` or `mi` for distances |
| `range` | starting radar radius |
| `overhead_radius` | distance at which the card says OVERHEAD |
| `fullscreen`, `window_size`, `fps` | display settings |
| `online_lookups` | turn adsbdb lookups on or off |
| `trail_seconds`, `stale_seconds` | trail length; when to drop silent aircraft |
| `landmarks` | list of `{"name", "lat", "lon"}` to mark on the radar |
| `coastline_file` | coastline outline to draw (empty string to disable) |

## Coastline

`coastline.json` is built from the public-domain
[Natural Earth](https://www.naturalearthdata.com) 10m coastline. It covers
450 km around the location in `config.json`. Build it after setting your
location, and again if you move:

```sh
.venv/bin/python make_coastline.py           # downloads the source (~10 MB)
.venv/bin/python make_coastline.py --input ne_10m_coastline.geojson   # or use a local copy
```

## Raspberry Pi

```sh
sudo apt install python3-venv python3-pygame  # pygame wheel via apt is fastest
git clone https://github.com/paulhardman/flights-near-me.git ~/flights-near-me
cd ~/flights-near-me
python3 -m venv --system-site-packages .venv
.venv/bin/pip install certifi
cp config.example.json config.json   # then edit it, see Setup
.venv/bin/python make_coastline.py
.venv/bin/python flight_near_me.py
```

To start it automatically with the desktop:

```sh
mkdir -p ~/.config/systemd/user
cp flights-near-me.service ~/.config/systemd/user/
systemctl --user enable --now flights-near-me
```

On Raspberry Pi OS Lite (no desktop), pygame can draw straight to the screen:
run it with `SDL_VIDEODRIVER=kmsdrm` from the console.

