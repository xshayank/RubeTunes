# musicdl Integration

RubeTunes integrates [CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl)
as an optional additional download backend.  It is **lazy-imported**: if the package is
missing or broken, only the `!musicdl` bot commands are affected — the rest of RubeTunes
continues to work normally.

---

## Setup

### 1 — Install musicdl

```bash
pip install musicdl==2.11.1
```

musicdl ships with `nodejs-wheel`, a bundled Node.js binary, so you do **not** need a
system-level `nodejs` installation for most sources.

### 2 — Environment variables

| Variable | Default | Description |
|---|---|---|
| `MUSICDL_DOWNLOAD_DIR` | `<repo>/downloads/musicdl` | Directory where downloaded files are saved. |
| `MUSICDL_DEFAULT_SOURCES` | *(musicdl upstream defaults)* | Comma-separated source client names, e.g. `NeteaseMusicClient,QQMusicClient`. Leave empty to use musicdl's own defaults. |
| `MUSICDL_PROXY` | *(none)* | HTTP/HTTPS proxy URL for all musicdl requests, e.g. `http://user:pass@host:8080`. |

Add these to your `.env` file:

```dotenv
# musicdl
MUSICDL_DOWNLOAD_DIR=/app/downloads/musicdl
MUSICDL_DEFAULT_SOURCES=NeteaseMusicClient,QQMusicClient,KuwoMusicClient
MUSICDL_PROXY=
```

---

## Bot commands

| Command | Description |
|---|---|
| `!musicdl sources` | List all source client names registered in the installed musicdl version. |
| `!musicdl search <query>` | Search across the default sources; returns a numbered list. |
| `!musicdl search <query> <SrcMusicClient>` | Search a specific source only. |
| `!musicdl <number>` | Download the track selected from the previous `!musicdl search` result. |

Example session:

```
User:  !musicdl search Bohemian Rhapsody
Bot:   🎵 musicdl results for 'Bohemian Rhapsody' (8 tracks):
         1. Queen — Bohemian Rhapsody — A Night at the Opera [Netease][FLAC]
         2. Queen — Bohemian Rhapsody — A Night at the Opera [QQ][MP3]
         …
       Reply !musicdl <number> to download

User:  !musicdl 1
Bot:   ⬇️ Downloading…
Bot:   📤 Uploading…
Bot:   (sends file)
```

---

## Supported sources (runtime-discovered)

The exact list depends on the installed musicdl version.  Use `!musicdl sources` to get
the authoritative list at runtime.  As of v2.11.1 the registered modules include:

### Platforms in Greater China
| Client Name | Platform |
|---|---|
| `QQMusicClient` | QQ Music (腾讯音乐) |
| `NeteaseMusicClient` | NetEase Cloud Music (网易云音乐) |
| `KuwoMusicClient` | Kuwo Music (酷我音乐) |
| `KugouMusicClient` | Kugou Music (酷狗音乐) |
| `MiguMusicClient` | Migu Music (咪咕音乐) |
| `QianqianMusicClient` | Qianqian Music (千千音乐) |
| `BilibiliMusicClient` | Bilibili Music |
| `FiveSingMusicClient` | 5sing |
| `SodaMusicClient` | Soda Music |
| `StreetVoiceMusicClient` | StreetVoice (街声) |

### Global Streaming / Indie
| Client Name | Platform |
|---|---|
| `SpotifyMusicClient` | Spotify |
| `DeezerMusicClient` | Deezer |
| `QobuzMusicClient` | Qobuz |
| `TIDALMusicClient` | Tidal |
| `AppleMusicClient` | Apple Music |
| `YouTubeMusicClient` | YouTube Music |
| `JooxMusicClient` | JOOX |
| `SoundCloudMusicClient` | SoundCloud |
| `JamendoMusicClient` | Jamendo (royalty-free) |
| `FMAMusicClient` | Free Music Archive |

### Audio / Podcast
| Client Name | Platform |
|---|---|
| `XimalayaMusicClient` | Ximalaya (喜马拉雅) |
| `LizhiMusicClient` | Lizhi (荔枝) |
| `QingtingMusicClient` | Qingting FM (蜻蜓.fm) |
| `LRTSMusicClient` | LRTS |

### Aggregators / Multi-Source Gateways
| Client Name | Platform |
|---|---|
| `GDStudioMusicClient` | GDStudio |
| `TuneHubMusicClient` | TuneHub |
| `MP3JuiceMusicClient` | MP3Juice |
| `MyFreeMP3MusicClient` | MyFreeMP3 |
| `JBSouMusicClient` | JBSou |

### Unofficial Download Sites / Scrapers
Many more — see `!musicdl sources` for the full runtime list.

---

## Known limitations

- **Geo-restrictions**: Many Chinese platforms (QQ, Netease, Kuwo, etc.) restrict access
  from non-CN IP addresses.  Use `MUSICDL_PROXY` with a CN proxy to work around this.
- **Source breakages**: musicdl sources may break when upstream platforms change their APIs.
  Update to the latest musicdl release if a source stops working.
- **File quality**: Quality varies per source and per track.  Some sources only provide MP3;
  others provide FLAC.  The `ext` field in search results hints at the format.
- **Rate limits**: Aggressive use may trigger per-IP rate limits on public music APIs.
- **Audiobook / podcast sources** (Ximalaya, Lizhi, Qingting) are included but untested
  for music use-cases.
- **Default sources**: musicdl's upstream defaults are
  `MiguMusicClient, NeteaseMusicClient, QQMusicClient, KuwoMusicClient, QianqianMusicClient`.
  Override via `MUSICDL_DEFAULT_SOURCES`.

---

## Attribution

musicdl is developed by [Zhenchao Jin (CharlesPikachu)](https://github.com/CharlesPikachu/musicdl)
and licensed under the
[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/).
Commercial use of musicdl is **prohibited** per upstream license terms.
