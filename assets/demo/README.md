# Demo assets

Media used by the top-level README. Everything here is generated from a real
run of the server against a throwaway Nextcloud, except the Claude chat clips
listed under "Still to record".

| File | What it is |
|---|---|
| `demo.gif`, `demo.mp4` | Terminal-style replay of `transcript.json` (real tool calls and results) |
| `calendar.png`, `tasks.png`, `notes.png` | Nextcloud UI after the demo run |
| `transcript.json` | Recorded prompts, tool calls and results |

The prompts are scripted: a client picks the tool calls, the server and
Nextcloud do the real work. The replay is labelled "scripted client" for that
reason.

## Regenerate

```bash
# 1. Throwaway Nextcloud (podman or docker), then install the apps
podman run -d --name nc-demo -p 8088:80 -e SQLITE_DATABASE=nextcloud \
  -e NEXTCLOUD_ADMIN_USER=demo -e NEXTCLOUD_ADMIN_PASSWORD=demo-pass-2026 nextcloud:latest
# wait for http://localhost:8088/status.php to report "installed":true
for a in calendar tasks notes; do podman exec -u www-data nc-demo php occ app:install $a; done
podman exec -u www-data nc-demo php occ app:disable firstrunwizard

# 2. Run the scripted scenes against the real server
export NEXTCLOUD_BASE_URL=http://localhost:8088 NEXTCLOUD_USERNAME=demo \
  NEXTCLOUD_APP_PASSWORD=demo-pass-2026 PUBLIC_BASE_URL=http://localhost:8000
uv run python scripts/demo/run_demo.py assets/demo/transcript.json

# 3. Record the replay and screenshot the UI (serve the repo root first)
python3 -m http.server 8099 &
uv run --with playwright python scripts/demo/record.py /tmp/demo-out

# 4. Convert (H.264 needs an ffmpeg build with libx264 or libopenh264)
ffmpeg -ss 0.4 -i /tmp/demo-out/demo.webm -c:v libopenh264 -b:v 900k -pix_fmt yuv420p -movflags +faststart assets/demo/demo.mp4
ffmpeg -ss 0.4 -i /tmp/demo-out/demo.webm \
  -vf "fps=8,scale=880:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=none" assets/demo/demo.gif
```

Run the demo once per fresh Nextcloud: the scenes create a `Work` list and
calendar, so a second run fails on the name.

## Still to record: Claude clips

These need a real Claude session against a deployed instance, so they are not
generated here. Capture them with fake data, drop them in this directory and
add them to the README slot marked in the "What you can say" section.

1. **Web, plan the week** (about 20 s): "Add a high-priority task: finish the Q4 report by Thursday 5pm", then "Block Thursday 9-11 for it", then "What does my Thursday look like?". Show Claude's tool-call chips expanding.
2. **Mobile, quick capture** (about 10 s): from the Claude app, "Remind me to call the dentist tomorrow at 9". Then cut to the Nextcloud Tasks app on the phone showing it.
3. **Connector setup** (about 15 s): Settings, Connectors, Add custom connector, paste URL, consent page, connected.

Keep each under 5 MB as GIF, or link an MP4 like the main demo.
