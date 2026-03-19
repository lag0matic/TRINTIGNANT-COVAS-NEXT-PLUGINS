# Covasify v4.1.0

> ⚠️ **Note:** This plugin was built with AI assistance (Claude). I'm not a Python expert — there may be bugs or rough edges. Feedback welcome!

Voice-controlled Spotify integration for [COVAS:NEXT](https://ratherrude.github.io/Elite-Dangerous-AI-Integration/). Play music, control playback, and bind tracks to custom voice phrases — all hands-free.

**⚠️ Requires Spotify Premium** — Free accounts cannot use playback control features.

## What It Does

- **Play by voice** — tracks, albums, artists, playlists, and your Liked Songs
- **Full playback control** — pause, skip, seek, volume, shuffle, repeat
- **Liked Songs** — save or remove the current track by voice
- **Track bindings** — bind any track to a custom phrase and play it instantly
- **Ambient now-playing status** — COVAS always knows what's playing without being asked

## How It Works

Covasify connects to Spotify via OAuth and registers a set of voice-activated tools with COVAS:NEXT. A status generator passively pushes the current track and play/pause state into COVAS's context every turn at no extra cost, so she can reference what's playing naturally in conversation without needing to call a tool first.

---

## Setup

### Step 1 — Get Spotify API Credentials

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Click **Create App** with these settings:
   - **Redirect URI**: `http://127.0.0.1:8888/callback`
3. Copy your **Client ID** and **Client Secret**
4. Click the **User Management** tab and add your name and Spotify email address

### Step 2 — Install the Plugin

> ⚠️ **GitHub extraction note:** When downloading a release from GitHub, the zip file will extract to a folder named something like `Covasify-v4.0.0`. You must rename this folder to just `Covasify` before placing it in your plugins directory, otherwise COVAS:NEXT may not load it correctly.

1. Download the latest release and extract it
2. Rename the folder to `Covasify` (strip the version suffix)
3. Place the `Covasify` folder in:
   ```
   %appdata%\com.covas-next.ui\plugins\
   ```
4. Dependencies are bundled — no installation step needed
5. Restart COVAS:NEXT
6. Open the COVAS:NEXT menu → navigate to **Covasify Spotify Integration** settings
7. Enter your credentials:
   - **Client ID**: paste your Spotify Client ID
   - **Client Secret**: paste your Spotify Client Secret
   - **Redirect URI**: leave as default `http://127.0.0.1:8888/callback`
8. Start your COVAS chat session — your browser will open for Spotify authorisation on first use (one-time only)

**Requirements:**
- Spotify Premium account (mandatory — free accounts cannot control playback)
- Active Spotify device (desktop app, mobile, or web player must be open)

---

## Voice Commands

### Playing Music

```
"Play Bohemian Rhapsody"          # Track search
"Play Abbey Road album"           # Album
"Play Queen"                      # Artist (shuffled)
"Play Queen's top tracks"         # Top 10 most popular tracks
"Play workout playlist"           # Playlist by name
"Play Liked Songs"                # Your saved library
```

### Playback Control

```
"Pause" / "Resume" / "Stop"
"Next" / "Previous" / "Restart"
"Seek to 2:30"
"Volume up" / "Volume down" / "Set volume to 50" / "Mute"
"Shuffle on" / "Shuffle off"
"Repeat track" / "Repeat playlist" / "Repeat off"
```

### Library

```
"What's playing?"                 # Full track detail including progress
"Save this track"                 # Add to Liked Songs
"Remove this track"               # Remove from Liked Songs
```

### Track Bindings

Bind any currently playing track to a custom phrase and play it back instantly by saying that phrase.

```
"Bind this to workout intro"      # Bind current track to a phrase
"Workout intro"                   # Play the bound track
"List bindings"                   # See all your bindings
"Unbind workout intro"            # Remove a specific binding
"Unbind all"                      # Clear all bindings
```

---

## Troubleshooting

**"No active Spotify devices found"**
- Open Spotify on any device and start playing something first, then try again

**"Not connected to Spotify"**
- Check your credentials are correctly entered in the plugin settings
- Delete `_spotify_cache` from the plugin's data folder and restart COVAS to re-authenticate

**Need to re-authorise**
- Delete `_spotify_cache` from the plugin data folder (found under `%appdata%\com.covas-next.ui\plugin_data\` by plugin GUID)
- Restart COVAS — your browser will open for re-auth on first command

**Binding doesn't play immediately**
- Say the phrase again — first-attempt retries are occasionally needed

---

## What's NOT Possible

Due to Spotify API restrictions introduced in November 2024:
- Radio / recommendations (API deprecated for new apps)
- Endless smart queue (use artist or playlist playback instead)
- Related artists suggestions (API blocked)

---

## Files

```
Covasify/
  Covasify.py              # Main plugin
  manifest.json            # Plugin metadata
  deps/                    # Bundled Python dependencies
```

**Persistent data** (track bindings and OAuth token cache) is stored in COVAS:NEXT's plugin data folder by plugin GUID — not inside the plugin folder itself. Your bindings and login survive updates and reinstalls.

---

## Version History
**v4.1.0 — Improved track search accuracy. Added separate artist field for more precise matching, smarter scoring that heavily penalises covers, remixes, karaoke and live versions.**  
**v4.0.0** — Major token optimisation refactor by Lag0matic and AI
- Consolidated 15 tools down to 5 — ~65–70% reduction in per-turn LLM token cost
- Added ambient now-playing status — COVAS always knows the current track and play/pause state without a tool call
- Seek moved into `covasify_control` — one less tool in the LLM's context
- Removed background polling thread — no unnecessary Spotify API calls between commands
- Pause/resume state tracked locally at zero API cost

**v3.0.0** — Re-worked by Lag0matic and AI to function again

**v2.0.0** — Settings UI integration, credential management via COVAS:NEXT menu

**v1.0.0** — Initial release

---

## Credits

**Author**: D. Trintignant  
**COVAS:NEXT**: https://ratherrude.github.io/Elite-Dangerous-AI-Integration/  
**Spotify API**: Spotipy library
