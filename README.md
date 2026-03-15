# COVAS:NEXT Plugins

A collection of plugins for [COVAS:NEXT](https://ratherrude.github.io/Elite-Dangerous-AI-Integration/). Each plugin has its own README with installation instructions and usage details.

> ⚠️ **Stability notice:** Of the plugins in this collection, only **Covasify** is confirmed working against current versions of COVAS:NEXT. Songbird and Covinance are untested and may require updates to function.

**Original plugins by [D. Trintignant](https://github.com/Tokyo8543485/TRINTIGNANT-COVAS-NEXT-PLUGINS).** Covasify has been updated and maintained by Lag0matic. Songbird and Covinance are carried forward from the original repository unchanged.

---

## Available Plugins

| Plugin | Status | Description |
|---|---|---|
| **Covasify** | ✅ Stable | Voice-controlled Spotify playback and track binding |
| **Songbird** | ❓ Unknown | Voice-controlled sound effects via Freesound |
| **Covinance** | ❓ Unknown | Elite Dangerous commodity trading via Ardent API |

- ✅ **Stable** — tested and confirmed working
- ❓ **Unknown** — untested against current COVAS:NEXT; may require updates

---

## Installation

> ⚠️ **GitHub extraction note:** When downloading a release from GitHub, the zip file extracts to a versioned folder (e.g. `Covasify-v4.0.0`). Rename it to just the plugin name (e.g. `Covasify`) before placing it in the plugins directory, otherwise COVAS:NEXT may not load it correctly.

1. Download the plugin release and extract it
2. Rename the folder to strip the version suffix
3. Copy the plugin folder to:
   ```
   %appdata%\com.covas-next.ui\plugins\
   ```
4. Restart COVAS:NEXT
5. Configure plugin settings via the COVAS:NEXT menu if required

---

## Plugin Summary

### Covasify ✅
Voice-controlled Spotify integration. Play tracks, albums, artists and playlists by voice. Bind tracks to custom phrases. COVAS always knows what's playing without being asked. See [Covasify/README.md](Covasify/README.md) for full details.

**Requires:** Spotify Premium account + Spotify API credentials

### Songbird ❓
Voice-controlled sound effects sourced from Freesound with local soundboard support. See [Songbird/README.md](Songbird/README.md) for full details.

**Requires:** Freesound API key

### Covinance ❓
Elite Dangerous commodity trading and market analysis via the Ardent Insight API. See [Covinance/README.md](Covinance/README.md) for full details.

**Requires:** Nothing — uses Ardent Insight API (no key needed)

---

## License

MIT License — see individual plugin folders for details.
