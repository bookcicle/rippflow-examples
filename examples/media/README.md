# Media Segment Transcoder

Simulates parallel video segment transcoding, loudness QA rework on `scene-02.mp4`, and HLS master playlist fan-in stitching.

## Run Locally from Repository Root

```sh
rippflow dev examples/media/media.workflow.json \
  --handlers examples/media/media.handlers.json \
  --shards scene-01.mp4,scene-02.mp4 \
  --report media-report.json
```

Expected: `loudness-qa` returns `rework` once for `scene-02.mp4` (`override_inputs: { "normalize_audio": true }`), re-runs `transcode-segment` for that shard, and converges with exit code `0`.
