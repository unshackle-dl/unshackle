# A complete annotated example

The following config sets a handful of common keys. Every key not shown keeps its default.

```yaml title="unshackle.yaml"
directories:
  downloads: ~/Media/unshackle
  temp: /mnt/scratch/unshackle

network:
  browser: Chrome131

headers:
  Accept-Language: en-US,en;q=0.9

cdm:
  default: chromecdm_l3
  EXAMPLE2: android_l1

decryption: shaka

credentials:
  EXAMPLE2: user@example.com:hunter2

output_template:
  movies: "{title}.{year}.{quality?}.{source}-{tag}"
  series: "{title}.{season_episode}.{quality?}.{source}-{tag}"
  folder:
    movies: "{title} ({year})"
    series: "{title} ({year})"

tag: MYGRP

key_vaults:
  - type: SQLite
    name: Local
    path: ~/.unshackle/keys.db

proxy_providers:
  basic:
    us: http://user:pass@1.2.3.4:8080

omdb_api_key: "your-omdb-key"
tmdb_api_key: "your-tmdb-key"
tvdb_api_key: "your-tvdb-key"
title_cache_time: 3600
redact_paths: true
```

For how these interact at runtime, continue to [Downloading](../../guide/downloading.md),
[DRM & CDM](../../guide/drm-and-cdm.md), and [Output & naming](../../guide/output-and-naming.md).
