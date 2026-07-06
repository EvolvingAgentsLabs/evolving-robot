# gifcap — regenerate docs/img/night-shift.gif

Records the real viewer while a scripted driver acts the two-night story.

```bash
# 1. sim up
../../.venv/bin/python -m sim2d.server &

# 2. recorder (needs Chrome + `npm install puppeteer-core` in this dir)
DURATION_S=38 FPS=8 node record.js &

# 3. the story (start ~2.5s after the recorder)
sleep 2.5 && ../../.venv/bin/python night_story.py

# 4. assemble (trim idle tail first if needed)
ffmpeg -y -framerate 7 -i frames/f%04d.png -vf "setpts=0.75*PTS,fps=10,scale=960:-1:flags=lanczos,palettegen=stats_mode=diff" palette.png
ffmpeg -y -framerate 7 -i frames/f%04d.png -i palette.png -lavfi "setpts=0.75*PTS,fps=10,scale=960:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" night-shift.gif
```

Note: the anti-throttling Chrome flags in record.js are required — without them a
headless page stops receiving WebSocket broadcasts and the capture freezes.
