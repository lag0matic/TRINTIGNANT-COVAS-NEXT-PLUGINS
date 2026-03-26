from typing_extensions import override
import json
import os
import sys
import string
import re
import time
import threading
from datetime import datetime
from pydantic import BaseModel
from typing import Optional
import webbrowser

from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse as urlparse

class SpotifyAuthCallbackHandler(BaseHTTPRequestHandler):
    auth_code = None

    def do_GET(self):
        parsed = urlparse.urlparse(self.path)
        params = urlparse.parse_qs(parsed.query)

        if "code" in params:
            SpotifyAuthCallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Spotify authentication successful!</h2>You may close this window.</body></html>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing code parameter")

    def log_message(self, format, *args):
        return  # silence HTTP server logs

def start_spotify_callback_server():
    server = HTTPServer(("127.0.0.1", 8888), SpotifyAuthCallbackHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server

# Set up deps path BEFORE importing spotipy
current_dir = os.path.dirname(os.path.abspath(__file__))
deps_path = os.path.join(current_dir, 'deps')
if deps_path not in sys.path:
    sys.path.insert(0, deps_path)

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from lib.PluginHelper import PluginHelper
from lib.PluginSettingDefinitions import PluginSettings, SettingsGrid, TextSetting, ToggleSetting
from lib.Logger import log
from lib.EventManager import Projection
from lib.PluginBase import PluginBase, PluginManifest
from lib.Event import Event
from pydantic import Field
from typing import List

# ============================================================================
# GENUI PROJECTION
# Exposes now-playing state to the GenUI overlay system.
# Updated directly whenever track info changes — no LLM turns required.
# Ask COVAS "show what's playing on the HUD" to render it.
# ============================================================================

class SpotifyStateModel(BaseModel):
    playing: bool = Field(default=False, description="Whether music is currently playing")
    track: str = Field(default="", description="Current track name")
    artist: str = Field(default="", description="Current artist name")
    album: str = Field(default="", description="Current album name")
    album_art_url: str = Field(default="", description="Album art image URL")
    progress: str = Field(default="", description="Playback progress e.g. 2:34 / 4:12")
    shuffle: bool = Field(default=False, description="Whether shuffle is enabled")
    repeat: str = Field(default="off", description="Repeat mode: off/track/context")

class SpotifyProjection(Projection[SpotifyStateModel]):
    StateModel = SpotifyStateModel

    def process(self, event: Event) -> None:
        pass  # Updated directly by Covasify, not from game events

# ============================================================================
# PARAM MODELS
# Consolidated from 7 models down to 4.
# ============================================================================

class EmptyParams(BaseModel):
    pass

class PlayParams(BaseModel):
    type: str               # "track", "artist", "artist_top", "album", "playlist"
    query: str
    artist: Optional[str] = None  # separate artist field for more accurate track search
    shuffle: Optional[bool] = None  # None = use sensible default per type

class ControlParams(BaseModel):
    command: str            # see covasify_control for full list; seek uses value_str
    value: Optional[int] = None
    value_str: Optional[str] = None  # used for seek (e.g. "2:30" or "150")

class LibraryParams(BaseModel):
    action: str             # "save", "remove"

class BindingsParams(BaseModel):
    action: str             # "bind", "play", "list", "remove", "clear"
    phrase: Optional[str] = None

# ============================================================================
# RELIABILITY CLIENT - Caching System (from Covinance v7.6)
# ============================================================================

class ReliabilityClient:
    """Caching wrapper for Spotify API calls - 1 hour cache TTL"""

    TTL_DEFAULT = 3600
    INFLIGHT_WAIT_TIMEOUT = 30

    def __init__(self):
        self.cache = {}
        self.lock = threading.RLock()
        self.in_flight = {}
        self.stats = {
            'cache_hits': 0,
            'cache_misses': 0,
            'inflight_hits': 0,
            'api_calls': 0,
            'errors': 0
        }

    def _make_cache_key(self, endpoint, params):
        param_str = json.dumps(params, sort_keys=True) if params else ""
        return f"{endpoint}:{param_str}"

    def get_cached_or_fetch(self, endpoint, params, fetch_fn):
        key = self._make_cache_key(endpoint, params)

        with self.lock:
            if key in self.cache:
                cached_data, cached_time, cached_ttl = self.cache[key]
                age = (datetime.now() - cached_time).total_seconds()
                if age < cached_ttl:
                    self.stats['cache_hits'] += 1
                    log('info', f'COVASIFY: Cache HIT for {endpoint} (age: {age:.1f}s)')
                    return cached_data

            if key in self.in_flight:
                event, result_holder = self.in_flight[key]
                log('info', f'COVASIFY: In-flight HIT for {endpoint}')

        if key in self.in_flight:
            event.wait(timeout=self.INFLIGHT_WAIT_TIMEOUT)
            with self.lock:
                if key in self.cache:
                    cached_data, cached_time, cached_ttl = self.cache[key]
                    age = (datetime.now() - cached_time).total_seconds()
                    if age < cached_ttl:
                        self.stats['inflight_hits'] += 1
                        return cached_data

        with self.lock:
            if key not in self.in_flight:
                event = threading.Event()
                result_holder = [None, None]
                self.in_flight[key] = (event, result_holder)
            else:
                event, result_holder = self.in_flight[key]

        if result_holder[0] is not None or result_holder[1] is not None:
            event.wait(timeout=self.INFLIGHT_WAIT_TIMEOUT)
            with self.lock:
                if key in self.cache:
                    return self.cache[key][0]
                if result_holder[1] is not None:
                    raise result_holder[1]

        ttl = self.TTL_DEFAULT
        last_error = None
        result = None

        with self.lock:
            self.stats['cache_misses'] += 1

        try:
            for attempt in range(3):
                try:
                    with self.lock:
                        self.stats['api_calls'] += 1
                    log('info', f'COVASIFY: Cache MISS - Fetching {endpoint} (attempt {attempt + 1}/3)')
                    result = fetch_fn(endpoint, params)

                    is_error = (isinstance(result, dict) and 'error' in result) or result is None

                    if is_error:
                        error_msg = result.get('error') if isinstance(result, dict) else 'None'
                        log('warning', f'COVASIFY: Not caching error for {endpoint}: {error_msg}')
                        with self.lock:
                            result_holder[0] = result
                        return result

                    with self.lock:
                        self.cache[key] = (result, datetime.now(), ttl)
                        result_holder[0] = result

                    return result

                except Exception as e:
                    last_error = e
                    if attempt < 2:
                        wait = 2 ** attempt
                        log('warning', f'COVASIFY: Retry in {wait}s...')
                        time.sleep(wait)
                    else:
                        log('error', f'COVASIFY: Failed after 3 attempts: {str(e)}')

            with self.lock:
                result_holder[1] = last_error
            raise last_error

        finally:
            with self.lock:
                if key in self.in_flight:
                    event, _ = self.in_flight[key]
                    event.set()
                    del self.in_flight[key]

    def get_stats(self):
        with self.lock:
            total = self.stats['cache_hits'] + self.stats['cache_misses']
            hit_rate = (self.stats['cache_hits'] / total * 100) if total > 0 else 0
            return {
                'cache_hit_rate': f"{hit_rate:.1f}%",
                'total_requests': total,
                'api_calls_saved': self.stats['cache_hits'] + self.stats['inflight_hits'],
                **self.stats
            }

# ============================================================================
# MAIN PLUGIN CLASS
# ============================================================================

class COVASIFYPlugin(PluginBase):
    settings_config = PluginSettings(
        key="COVASIFYPlugin",
        label="Covasify Spotify Integration",
        icon="music_note",
        grids=[
            SettingsGrid(
                key="spotify_credentials",
                label="Spotify API Credentials",
                fields=[
                    TextSetting(
                        key="client_id",
                        label="Client ID",
                        type="text",
                        readonly=False,
                        placeholder="Your Spotify Client ID",
                        default_value=""
                    ),
                    TextSetting(
                        key="client_secret",
                        label="Client Secret",
                        type="text",
                        readonly=False,
                        placeholder="Your Spotify Client Secret",
                        default_value=""
                    ),
                    TextSetting(
                        key="redirect_uri",
                        label="Redirect URI",
                        type="text",
                        readonly=False,
                        placeholder="http://127.0.0.1:8888/callback",
                        default_value="http://127.0.0.1:8888/callback"
                    )
                ]
            )
        ]
    )

    @override
    def get_settings_config(self):
        return self.settings_config

    def __init__(self, plugin_manifest: PluginManifest):
        super().__init__(plugin_manifest)
        self.reliability_client = ReliabilityClient()
        self.sp = None
        self.current_track_info = None
        self.settings = {}
        self.spotify_projection = SpotifyProjection()
        self._poll_thread = None
        self._poll_stop = threading.Event()

    def on_settings_changed(self, settings: dict):
        self.settings = settings

    def normalize_phrase(self, phrase: str) -> str:
        """Normalize phrase for matching: lowercase + strip punctuation + trim whitespace"""
        if not phrase:
            return ""
        cleaned = phrase.translate(str.maketrans('', '', string.punctuation))
        return ' '.join(cleaned.lower().split())

    # -------------------------------------------------------------------------
    # LIFECYCLE
    # -------------------------------------------------------------------------

    @override
    def on_chat_start(self, helper: PluginHelper):
        log('info', f"COVASIFY: Raw settings object = {self.settings}")
        try:
            credentials = self.load_credentials()
            if credentials:
                self.initialize_spotify(credentials)
                log('info', 'COVASIFY: Spotify initialized successfully')

            # 5 consolidated tools instead of 15
            helper.register_action(
                'covasify_play',
                "Play music on Spotify. type: track/artist/artist_top/album/playlist. For tracks, use query for song name and artist for artist name to improve accuracy.",
                PlayParams, self.covasify_play, 'global'
            )
            helper.register_action(
                'covasify_control',
                "Control Spotify: pause/resume/next/previous/restart, volume_up/down/set/mute/unmute, shuffle_on/off, repeat_track/context/off, seek (use value_str for position e.g. '2:30').",
                ControlParams, self.covasify_control, 'global'
            )
            helper.register_action(
                'covasify_library',
                "Manage liked songs. action: save/remove current track.",
                LibraryParams, self.covasify_library, 'global'
            )
            helper.register_action(
                'covasify_bindings',
                "Manage voice phrase bindings. action: bind/play/list/remove/clear. phrase required for bind/play/remove.",
                BindingsParams, self.covasify_bindings, 'global'
            )
            helper.register_action(
                'covasify_status',
                "Get current Spotify track details on demand.",
                EmptyParams, self.covasify_status, 'global'
            )

            log('info', 'COVASIFY: Actions registered successfully')

            # Register GenUI projection
            helper.register_projection(self.spotify_projection)
            log('info', 'COVASIFY: Spotify projection registered')

            # Start background polling to keep GenUI projection fresh
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(target=self._projection_poll_loop, daemon=True)
            self._poll_thread.start()
        except Exception as e:
            log('error', f'COVASIFY: Failed during chat start: {str(e)}')

    def register_projections(self, helper: PluginHelper):
        pass

    @override
    def register_sideeffects(self, helper: PluginHelper):
        pass

    @override
    def register_prompt_event_handlers(self, helper: PluginHelper):
        pass

    @override
    def register_status_generators(self, helper: PluginHelper):
        helper.register_status_generator(self.generate_spotify_status)

    @override
    def register_should_reply_handlers(self, helper: PluginHelper):
        pass

    @override
    def on_plugin_helper_ready(self, helper: PluginHelper):
        log('info', 'COVASIFY: Plugin helper is ready')

    @override
    def on_chat_stop(self, helper: PluginHelper):
        log('info', 'COVASIFY: Chat stopped')
        self._poll_stop.set()

    def _projection_poll_loop(self):
        """Poll Spotify every 10 seconds to keep the GenUI projection fresh.
        Only updates the projection — does not fire COVAS events or cost tokens."""
        while not self._poll_stop.wait(10.0):
            try:
                if self.sp:
                    self.update_current_track_info()
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # STATUS GENERATOR
    # Reads from local cache only — no Spotify API call, zero extra cost.
    # Pause/resume state is reflected here passively each turn.
    # -------------------------------------------------------------------------

    def generate_spotify_status(self, projected_states: dict) -> list[tuple[str, str]]:
        """Push current playback state into context each turn from local cache."""
        try:
            if not self.sp:
                return [("Spotify", "Not connected")]

            # Track info is now handled by the SpotifyProjection for GenUI.
            # Status only reports connection state to avoid conflicting with projection.
            state = "Paused" if self.current_track_info and self.current_track_info.get('state') == 'paused' else "Playing" if self.current_track_info else "Idle"
            bindings = self.load_bindings()
            binding_note = f" | {len(bindings)} binding(s)" if bindings else ""
            return [("Spotify", f"{state}{binding_note}")]

        except Exception as e:
            log('error', f'COVASIFY: Error generating status: {str(e)}')
            return [("Spotify", "Connected")]

    # -------------------------------------------------------------------------
    # CONSOLIDATED TOOL: covasify_play
    # Replaces: covasify_play_track, covasify_play_artist, covasify_play_top_tracks,
    #           covasify_play_album, covasify_play_playlist
    # -------------------------------------------------------------------------

    def covasify_play(self, args, projected_states) -> str:
        """Route play requests by type."""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify. Check credentials."

            play_type = (args.type or '').lower().strip()

            if play_type == 'track':
                return self._play_track(args.query, artist=args.artist)
            elif play_type == 'artist':
                return self._play_artist(args.query, shuffle=args.shuffle if args.shuffle is not None else True)
            elif play_type == 'artist_top':
                return self._play_artist_top(args.query)
            elif play_type == 'album':
                return self._play_album(args.query, shuffle=args.shuffle if args.shuffle is not None else False)
            elif play_type == 'playlist':
                return self._play_playlist(args.query, shuffle=args.shuffle if args.shuffle is not None else True)
            else:
                return f"COVASIFY: Unknown play type '{play_type}'. Use: track, artist, artist_top, album, playlist."

        except Exception as e:
            log('error', f'COVASIFY play error: {str(e)}')
            return f"COVASIFY: Play failed - {str(e)}"

    def _get_device_id(self) -> Optional[str]:
        """Get first available Spotify device ID."""
        devices = self.sp.devices()
        if not devices['devices']:
            return None
        return devices['devices'][0]['id']

    def _play_track(self, query: str, artist: str = None) -> str:
        if not query:
            return "COVASIFY: No search query provided."

        log('info', f'COVASIFY: Searching for track: {query}' + (f' by {artist}' if artist else ''))

        # Build search query — if artist provided, include it plainly
        # Spotify's field syntax (track: artist:) is unreliable for niche artists
        if artist:
            search_query = f'{query} {artist}'
        else:
            search_query = query

        def search_track(endpoint, params):
            return self.sp.search(q=params['q'], type='track', limit=10)

        results = self.reliability_client.get_cached_or_fetch(
            'spotify_search_track', {'q': search_query}, search_track
        )

        if not results['tracks']['items']:
            return f"COVASIFY: No tracks found for '{query}'."

        query_lower = query.lower()
        artist_lower = artist.lower() if artist else ''
        query_words = set(query_lower.split())

        def score_track(t):
            name = t['name'].lower()
            track_artist = t['artists'][0]['name'].lower()

            # Exact title match — must win decisively
            if name == query_lower:
                exact_name = 10
            elif name.startswith(query_lower + ' ') or name.startswith(query_lower + '('):
                exact_name = 6
            elif query_lower in name:
                exact_name = 2
            else:
                exact_name = 0

            # Artist match — if artist provided, this is critical
            artist_score = 0
            if artist_lower:
                if artist_lower == track_artist:
                    artist_score = 8
                elif artist_lower in track_artist or track_artist in artist_lower:
                    artist_score = 4
                else:
                    # Wrong artist entirely — heavy penalty
                    artist_score = -6

            # Penalise variants unless user asked for them
            variant_penalty = 0
            variants = ['acoustic', 'remix', 'live', 'cover', 'remaster', 'remastered',
                       'edit', 'instrumental', 'demo', 'radio edit', 'extended', 'karaoke',
                       'largo', 'reprise', 'version', 'mix', 'dub']
            if any(v in name for v in variants) and not any(v in query_lower for v in variants):
                variant_penalty = 8

            # Word overlap — only against track name, not artist
            # Prevents tracks with artist name in title from getting unfair overlap bonus
            name_words = set(name.split())
            overlap = len(query_words & name_words)

            # Penalise length difference
            length_diff = abs(len(name) - len(query_lower))

            return exact_name + artist_score + overlap - variant_penalty - (length_diff * 0.05)

        track = max(results['tracks']['items'], key=score_track)
        track_name = track['name']
        artist_name = track['artists'][0]['name']
        track_uri = track['uri']
        album_uri = track['album']['uri']

        device_id = self._get_device_id()
        if not device_id:
            return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."

        self.sp.transfer_playback(device_id, force_play=True)
        self.sp.start_playback(device_id=device_id, context_uri=album_uri, offset={"uri": track_uri})
        self.update_current_track_info()

        log('info', f'COVASIFY: Playing {track_name} by {artist_name}')
        return f"COVASIFY: Now playing '{track_name}' by {artist_name}."

    def _play_artist(self, query: str, shuffle: bool = True) -> str:
        if not query:
            return "COVASIFY: No artist provided."

        log('info', f'COVASIFY: Playing artist: {query}')

        def search_artist(endpoint, params):
            return self.sp.search(q=params['q'], type='artist', limit=1)

        artist_results = self.reliability_client.get_cached_or_fetch(
            'spotify_search_artist', {'q': query}, search_artist
        )

        if not artist_results['artists']['items']:
            return f"COVASIFY: Could not find artist '{query}'."

        artist = artist_results['artists']['items'][0]
        artist_name = artist['name']
        artist_uri = f"spotify:artist:{artist['id']}"

        device_id = self._get_device_id()
        if not device_id:
            return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."

        self.sp.start_playback(device_id=device_id, context_uri=artist_uri)
        if shuffle:
            self.sp.shuffle(True, device_id=device_id)

        self.update_current_track_info()
        log('info', f'COVASIFY: Playing {artist_name} (shuffle: {shuffle})')
        return f"COVASIFY: Playing {artist_name}{' (shuffled)' if shuffle else ''}."

    def _play_artist_top(self, query: str) -> str:
        if not query:
            return "COVASIFY: No artist provided."

        log('info', f'COVASIFY: Getting top tracks for: {query}')

        def search_artist(endpoint, params):
            return self.sp.search(q=params['q'], type='artist', limit=1)

        artist_results = self.reliability_client.get_cached_or_fetch(
            'spotify_search_artist', {'q': query}, search_artist
        )

        if not artist_results['artists']['items']:
            return f"COVASIFY: Could not find artist '{query}'."

        artist = artist_results['artists']['items'][0]
        artist_name = artist['name']
        artist_id = artist['id']

        top_tracks = self.sp.artist_top_tracks(artist_id)
        if not top_tracks['tracks']:
            return f"COVASIFY: No top tracks found for {artist_name}."

        track_uris = [track['uri'] for track in top_tracks['tracks']]

        device_id = self._get_device_id()
        if not device_id:
            return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."

        self.sp.start_playback(device_id=device_id, uris=track_uris)
        self.update_current_track_info()

        log('info', f'COVASIFY: Playing {len(track_uris)} top tracks by {artist_name}')
        return f"COVASIFY: Playing {len(track_uris)} most popular songs by {artist_name}."

    def _play_album(self, query: str, shuffle: bool = False) -> str:
        if not query:
            return "COVASIFY: No album name provided."

        log('info', f'COVASIFY: Searching for album: {query}')

        def search_album(endpoint, params):
            return self.sp.search(q=params['q'], type='album', limit=1)

        album_results = self.reliability_client.get_cached_or_fetch(
            'spotify_search_album', {'q': query}, search_album
        )

        if not album_results['albums']['items']:
            return f"COVASIFY: Could not find album '{query}'."

        album = album_results['albums']['items'][0]
        album_name = album['name']
        artist_name = album['artists'][0]['name']
        album_uri = album['uri']
        total_tracks = album['total_tracks']

        device_id = self._get_device_id()
        if not device_id:
            return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."

        self.sp.start_playback(device_id=device_id, context_uri=album_uri)
        if shuffle:
            self.sp.shuffle(True, device_id=device_id)

        self.update_current_track_info()
        log('info', f'COVASIFY: Playing album "{album_name}" by {artist_name}')
        return f"COVASIFY: Playing '{album_name}' by {artist_name} ({total_tracks} tracks){' (shuffled)' if shuffle else ''}."

    def _play_playlist(self, query: str, shuffle: bool = True) -> str:
        if not query:
            return "COVASIFY: No playlist name provided."

        log('info', f'COVASIFY: Searching for playlist: {query}')

        device_id = self._get_device_id()
        if not device_id:
            return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."

        query_lower = query.lower()

        # Handle Liked Songs specially
        if 'liked' in query_lower or 'saved' in query_lower or 'favorite' in query_lower:
            saved_tracks = self.sp.current_user_saved_tracks(limit=50)
            if not saved_tracks['items']:
                return "COVASIFY: No liked songs found."
            track_uris = [item['track']['uri'] for item in saved_tracks['items']]
            self.sp.start_playback(device_id=device_id, uris=track_uris)
            if shuffle:
                self.sp.shuffle(True, device_id=device_id)
            self.update_current_track_info()
            return f"COVASIFY: Playing your Liked Songs{' (shuffled)' if shuffle else ''}."

        def search_playlist(endpoint, params):
            return self.sp.search(q=params['q'], type='playlist', limit=5)

        results = self.reliability_client.get_cached_or_fetch(
            'spotify_search_playlist', {'q': query}, search_playlist
        )

        if not results['playlists']['items']:
            return f"COVASIFY: No playlists found for '{query}'."

        playlist = results['playlists']['items'][0]
        playlist_name = playlist['name']
        playlist_uri = playlist['uri']

        if not playlist_uri:
            return f"COVASIFY: Found playlist '{playlist_name}' but cannot access it."

        self.sp.start_playback(device_id=device_id, context_uri=playlist_uri)
        if shuffle:
            self.sp.shuffle(True, device_id=device_id)

        self.update_current_track_info()
        log('info', f'COVASIFY: Playing playlist {playlist_name}')
        return f"COVASIFY: Playing playlist '{playlist_name}'{' (shuffled)' if shuffle else ''}."

    # -------------------------------------------------------------------------
    # CONSOLIDATED TOOL: covasify_control
    # Replaces: covasify_control + covasify_seek (seek is now command="seek")
    # -------------------------------------------------------------------------

    def covasify_control(self, args, projected_states) -> str:
        """Control Spotify playback. Seek via command='seek' with value_str='MM:SS'."""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."

            command = args.command.lower()
            log('info', f'COVASIFY: Control command: {command}')

            if command in ['pause', 'stop']:
                self.sp.pause_playback()
                self._set_track_state('paused')
                return "COVASIFY: Playback paused."

            elif command in ['resume', 'play', 'unpause']:
                self.sp.start_playback()
                self._set_track_state('playing')
                return "COVASIFY: Playback resumed."

            elif command in ['next', 'skip', 'skip_forward', 'next_track']:
                self.sp.next_track()
                self.update_current_track_info()
                return "COVASIFY: Skipped to next track."

            elif command in ['previous', 'back', 'skip_back', 'previous_track']:
                self.sp.previous_track()
                self.update_current_track_info()
                return "COVASIFY: Skipped to previous track."

            elif command in ['restart', 'restart_track', 'restart_song', 'start_over', 'from_beginning']:
                current = self.sp.current_playback()
                if current and current.get('device'):
                    self.sp.seek_track(position_ms=0, device_id=current['device']['id'])
                    return "COVASIFY: Restarted current track from beginning."
                return "COVASIFY: No active playback to restart."

            elif command in ['volume_up', 'louder', 'increase_volume']:
                current = self.sp.current_playback()
                if current and current.get('device'):
                    new_volume = min(100, current['device']['volume_percent'] + 10)
                    self.sp.volume(new_volume)
                    return f"COVASIFY: Volume increased to {new_volume}%."
                return "COVASIFY: No active playback to adjust volume."

            elif command in ['volume_down', 'quieter', 'decrease_volume']:
                current = self.sp.current_playback()
                if current and current.get('device'):
                    new_volume = max(0, current['device']['volume_percent'] - 10)
                    self.sp.volume(new_volume)
                    return f"COVASIFY: Volume decreased to {new_volume}%."
                return "COVASIFY: No active playback to adjust volume."

            elif command in ['volume_set', 'set_volume']:
                value = max(0, min(100, args.value if args.value is not None else 50))
                self.sp.volume(value)
                return f"COVASIFY: Volume set to {value}%."

            elif command in ['mute', 'silence']:
                self.sp.volume(0)
                return "COVASIFY: Muted."

            elif command in ['unmute', 'unsilence']:
                self.sp.volume(50)
                return "COVASIFY: Unmuted to 50%."

            elif command in ['shuffle_on', 'enable_shuffle', 'shuffle']:
                self.sp.shuffle(True)
                return "COVASIFY: Shuffle enabled."

            elif command in ['shuffle_off', 'disable_shuffle', 'no_shuffle']:
                self.sp.shuffle(False)
                return "COVASIFY: Shuffle disabled."

            elif command in ['repeat_track', 'repeat_song', 'repeat_one']:
                self.sp.repeat('track')
                return "COVASIFY: Repeat track enabled."

            elif command in ['repeat_context', 'repeat_all', 'repeat_playlist']:
                self.sp.repeat('context')
                return "COVASIFY: Repeat all enabled."

            elif command in ['repeat_off', 'disable_repeat', 'no_repeat']:
                self.sp.repeat('off')
                return "COVASIFY: Repeat disabled."

            elif command in ['seek']:
                # Seek is now part of control — value_str holds the time input
                time_input = args.value_str
                if not time_input:
                    return "COVASIFY: No seek position provided. Use value_str e.g. '2:30' or '150'."
                return self._seek(time_input.strip())

            else:
                return f"COVASIFY: Unknown command '{command}'."

        except Exception as e:
            log('error', f'COVASIFY control error: {str(e)}')
            return f"COVASIFY: Control failed - {str(e)}"

    def _seek(self, time_input: str) -> str:
        """Seek to position in current track."""
        position_ms = self._parse_time_to_ms(time_input)
        if position_ms is None:
            return f"COVASIFY: Could not parse time '{time_input}'. Use format like '2:30' or '150' seconds."

        current = self.sp.current_playback()
        if not current or not current.get('item'):
            return "COVASIFY: No track currently playing to seek."

        track = current['item']
        duration_ms = track.get('duration_ms', 0)

        if position_ms > duration_ms:
            duration_min = duration_ms // 60000
            duration_sec = (duration_ms % 60000) // 1000
            return f"COVASIFY: Position exceeds track duration ({duration_min}:{duration_sec:02d})."

        self.sp.seek_track(position_ms, device_id=current['device']['id'])
        seek_min = position_ms // 60000
        seek_sec = (position_ms % 60000) // 1000
        return f"COVASIFY: Seeked to {seek_min}:{seek_sec:02d} in '{track['name']}'."

    def _parse_time_to_ms(self, time_input: str) -> Optional[int]:
        """Parse time string to milliseconds — handles MM:SS, H:MM:SS, or seconds."""
        try:
            time_input = time_input.strip().lower()
            time_input = re.sub(r'minutes?|seconds?|and', '', time_input).strip()

            if time_input.isdigit():
                return int(time_input) * 1000

            if ':' in time_input:
                parts = time_input.split(':')
                if len(parts) == 2:
                    return (int(parts[0]) * 60 + int(parts[1])) * 1000
                elif len(parts) == 3:
                    return (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000

            numbers = re.findall(r'\d+', time_input)
            if len(numbers) == 2:
                return (int(numbers[0]) * 60 + int(numbers[1])) * 1000
            elif len(numbers) == 1:
                return int(numbers[0]) * 1000

            return None
        except Exception as e:
            log('error', f'COVASIFY: Error parsing time {time_input}: {str(e)}')
            return None

    # -------------------------------------------------------------------------
    # CONSOLIDATED TOOL: covasify_library
    # Replaces: covasify_save_track, covasify_remove_track
    # -------------------------------------------------------------------------

    def covasify_library(self, args, projected_states) -> str:
        """Save or remove the current track from Liked Songs."""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."

            action = (args.action or '').lower()

            current = self.sp.current_playback()
            if not current or not current.get('item'):
                return "COVASIFY: No track currently playing."

            track = current['item']
            track_id = track['id']
            track_name = track['name']
            artist_name = track['artists'][0]['name']

            is_saved = self.sp.current_user_saved_tracks_contains([track_id])[0]

            if action == 'save':
                if is_saved:
                    return f"COVASIFY: '{track_name}' is already in your Liked Songs."
                self.sp.current_user_saved_tracks_add([track_id])
                log('info', f'COVASIFY: Saved "{track_name}"')
                return f"COVASIFY: Added '{track_name}' by {artist_name} to Liked Songs."

            elif action == 'remove':
                if not is_saved:
                    return f"COVASIFY: '{track_name}' is not in your Liked Songs."
                self.sp.current_user_saved_tracks_delete([track_id])
                log('info', f'COVASIFY: Removed "{track_name}"')
                return f"COVASIFY: Removed '{track_name}' by {artist_name} from Liked Songs."

            else:
                return f"COVASIFY: Unknown library action '{action}'. Use: save, remove."

        except Exception as e:
            log('error', f'COVASIFY library error: {str(e)}')
            return f"COVASIFY: Library action failed - {str(e)}"

    # -------------------------------------------------------------------------
    # CONSOLIDATED TOOL: covasify_bindings
    # Replaces: covasify_bind_track, covasify_play_bound, covasify_list_bindings,
    #           covasify_unbind, covasify_unbind_all
    # -------------------------------------------------------------------------

    def covasify_bindings(self, args, projected_states) -> str:
        """Manage phrase-to-track bindings."""
        try:
            if not self.sp and args.action not in ['list', 'clear']:
                return "COVASIFY: Not connected to Spotify."

            action = (args.action or '').lower()
            phrase = args.phrase or ''

            if action == 'bind':
                return self._bind_track(phrase)
            elif action == 'play':
                return self._play_bound(phrase)
            elif action == 'list':
                return self._list_bindings()
            elif action == 'remove':
                return self._unbind(phrase)
            elif action == 'clear':
                return self._unbind_all()
            else:
                return f"COVASIFY: Unknown bindings action '{action}'. Use: bind, play, list, remove, clear."

        except Exception as e:
            log('error', f'COVASIFY bindings error: {str(e)}')
            return f"COVASIFY: Bindings action failed - {str(e)}"

    def _bind_track(self, phrase: str) -> str:
        normalized_phrase = self.normalize_phrase(phrase)
        if not normalized_phrase:
            return "COVASIFY: No phrase provided."

        self.update_current_track_info()
        if not self.current_track_info:
            return "COVASIFY: No track currently playing to bind. Play a track first."

        bindings = self.load_bindings()
        bindings[normalized_phrase] = {
            'track_uri': self.current_track_info['track_uri'],
            'track_name': self.current_track_info['track_name'],
            'artist_name': self.current_track_info['artist_name'],
            'album_name': self.current_track_info['album_name']
        }

        if self.save_bindings(bindings):
            return f"COVASIFY: Bound '{self.current_track_info['track_name']}' by {self.current_track_info['artist_name']} to phrase '{phrase}'."
        return "COVASIFY: Failed to save binding."

    def _play_bound(self, phrase: str) -> str:
        normalized_phrase = self.normalize_phrase(phrase)
        if not normalized_phrase:
            return "COVASIFY: No phrase provided."

        bindings = self.load_bindings()
        if normalized_phrase not in bindings:
            return f"COVASIFY: No track bound to phrase '{phrase}'."

        binding = bindings[normalized_phrase]
        device_id = self._get_device_id()
        if not device_id:
            return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."

        self.sp.start_playback(device_id=device_id, uris=[binding['track_uri']])
        self.update_current_track_info()
        return f"COVASIFY: Playing '{binding['track_name']}' by {binding['artist_name']}."

    def _list_bindings(self) -> str:
        bindings = self.load_bindings()
        if not bindings:
            return "COVASIFY: No track bindings found."
        lines = [f"- '{p}' -> {info['track_name']} by {info['artist_name']}" for p, info in bindings.items()]
        return f"COVASIFY: {len(bindings)} binding(s):\n" + "\n".join(lines)

    def _unbind(self, phrase: str) -> str:
        normalized_phrase = self.normalize_phrase(phrase)
        if not normalized_phrase:
            return "COVASIFY: No phrase provided."

        bindings = self.load_bindings()
        if normalized_phrase not in bindings:
            return f"COVASIFY: No track bound to phrase '{phrase}'."

        track_name = bindings[normalized_phrase]['track_name']
        artist_name = bindings[normalized_phrase]['artist_name']
        del bindings[normalized_phrase]

        if self.save_bindings(bindings):
            return f"COVASIFY: Unbound '{track_name}' by {artist_name} from phrase '{phrase}'."
        return "COVASIFY: Failed to save updated bindings."

    def _unbind_all(self) -> str:
        bindings = self.load_bindings()
        count = len(bindings)
        if count == 0:
            return "COVASIFY: No track bindings to remove."
        if self.save_bindings({}):
            return f"COVASIFY: Removed all {count} track binding(s)."
        return "COVASIFY: Failed to clear bindings."

    # -------------------------------------------------------------------------
    # ON-DEMAND TOOL: covasify_status
    # Called by the LLM only when the user explicitly asks about the current track.
    # Makes a live Spotify API call for full detail (progress, album, etc.)
    # -------------------------------------------------------------------------

    def covasify_status(self, args, projected_states) -> str:
        """Get detailed info about the currently playing track on demand."""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."

            current = self.sp.current_playback()
            if not current or not current.get('item'):
                return "COVASIFY: No track currently playing."

            track = current['item']
            track_name = track['name']
            artists = ', '.join([a['name'] for a in track['artists']])
            album = track['album']['name']

            progress_ms = current.get('progress_ms', 0)
            duration_ms = track.get('duration_ms', 0)
            progress_min = progress_ms // 60000
            progress_sec = (progress_ms % 60000) // 1000
            duration_min = duration_ms // 60000
            duration_sec = (duration_ms % 60000) // 1000

            is_playing = current.get('is_playing', False)
            state = "Playing" if is_playing else "Paused"

            self.update_current_track_info()

            return (
                f"COVASIFY: {state} '{track_name}' by {artists} "
                f"from '{album}'. "
                f"Progress: {progress_min}:{progress_sec:02d} / {duration_min}:{duration_sec:02d}."
            )

        except Exception as e:
            log('error', f'COVASIFY status error: {str(e)}')
            return f"COVASIFY: Failed to get track info - {str(e)}"

    # -------------------------------------------------------------------------
    # INTERNAL HELPERS
    # -------------------------------------------------------------------------

    def _set_track_state(self, state: str):
        """Update local track state cache (playing/paused) without an API call."""
        if self.current_track_info:
            self.current_track_info['state'] = state

    def update_current_track_info(self):
        """Update local cache and GenUI projection after any play action."""
        try:
            if not self.sp:
                return
            current = self.sp.current_playback()
            if not current or not current.get('item'):
                self.current_track_info = None
                self.spotify_projection.state.playing = False
                self.spotify_projection.state.track = ''
                self.spotify_projection.state.artist = ''
                self.spotify_projection.state.album = ''
                self.spotify_projection.state.album_art_url = ''
                self.spotify_projection.state.progress = ''
                return

            track = current['item']
            is_playing = current.get('is_playing', True)
            artist_name = ', '.join([a['name'] for a in track['artists']])
            album_name = track['album']['name']

            # Album art — use largest available image
            images = track['album'].get('images', [])
            album_art_url = images[0]['url'] if images else ''

            # Progress
            duration_ms = track.get('duration_ms', 0)
            progress_ms = current.get('progress_ms', 0)
            def ms_to_str(ms):
                s = ms // 1000
                return f"{s // 60}:{s % 60:02d}"
            progress_str = f"{ms_to_str(progress_ms)} / {ms_to_str(duration_ms)}" if duration_ms else ''

            # Repeat mode
            repeat_map = {'track': 'track', 'context': 'context', 'off': 'off'}
            repeat = repeat_map.get(current.get('repeat_state', 'off'), 'off')

            self.current_track_info = {
                'track_uri': track['uri'],
                'track_name': track['name'],
                'artist_name': artist_name,
                'album_name': album_name,
                'track_id': track['id'],
                'state': 'playing' if is_playing else 'paused'
            }

            # Update GenUI projection
            self.spotify_projection.state.playing = is_playing
            self.spotify_projection.state.track = track['name']
            self.spotify_projection.state.artist = artist_name
            self.spotify_projection.state.album = album_name
            self.spotify_projection.state.album_art_url = album_art_url
            self.spotify_projection.state.progress = progress_str
            self.spotify_projection.state.shuffle = current.get('shuffle_state', False)
            self.spotify_projection.state.repeat = repeat

        except Exception as e:
            log('error', f'COVASIFY: Error updating track info: {str(e)}')
            self.current_track_info = None

    def get_plugin_folder_path(self) -> str:
        try:
            return os.path.dirname(os.path.abspath(__file__))
        except:
            try:
                appdata = os.getenv('APPDATA')
                if appdata:
                    return os.path.join(appdata, 'com.covas-next.ui', 'plugins', 'Covasify')
            except:
                pass
        return ""

    def load_credentials(self) -> dict:
        try:
            log('info', 'COVASIFY: Attempting to read credentials from Settings UI')
            client_id = self.settings.get('client_id')
            client_secret = self.settings.get('client_secret')
            redirect_uri = self.settings.get('redirect_uri')

            if client_id and client_secret:
                log('info', 'COVASIFY: Credentials loaded from Settings UI')
                return {
                    'CLIENT_ID': client_id,
                    'CLIENT_SECRET': client_secret,
                    'REDIRECT_URI': redirect_uri or 'http://127.0.0.1:8888/callback'
                }

            log('warning', 'COVASIFY: Settings UI incomplete — falling back to spotify_credentials.txt')

            plugin_folder = self.get_plugin_folder_path()
            cred_file = os.path.join(plugin_folder, 'spotify_credentials.txt')

            if not os.path.exists(cred_file):
                log('error', 'COVASIFY: No credentials found in Settings UI or spotify_credentials.txt')
                return None

            credentials = {}
            with open(cred_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if '=' in line and not line.startswith('#'):
                        key, value = line.split('=', 1)
                        credentials[key.strip()] = value.strip()

            if 'CLIENT_ID' in credentials and 'CLIENT_SECRET' in credentials:
                credentials.setdefault('REDIRECT_URI', 'http://127.0.0.1:8888/callback')
                return credentials

            log('error', 'COVASIFY: Invalid credentials format in spotify_credentials.txt')
            return None

        except Exception as e:
            log('error', f'COVASIFY: Error loading credentials: {str(e)}')
            return None

    def initialize_spotify(self, credentials: dict):
        try:
            log('info', f"COVASIFY: Initializing Spotify with Client ID: {credentials['CLIENT_ID'][:10]}...")
            plugin_folder = self.get_plugin_folder_path()
            cache_path = os.path.join(plugin_folder, '_spotify_cache')

            auth_manager = SpotifyOAuth(
                client_id=credentials['CLIENT_ID'],
                client_secret=credentials['CLIENT_SECRET'],
                redirect_uri=credentials.get('REDIRECT_URI', 'http://127.0.0.1:8888/callback'),
                scope='user-read-playback-state user-modify-playback-state user-read-currently-playing user-library-read user-library-modify user-top-read playlist-read-private playlist-read-collaborative',
                cache_path=cache_path,
                open_browser=False
            )

            token_info = auth_manager.get_cached_token()

            if not (token_info and not auth_manager.is_token_expired(token_info)):
                SpotifyAuthCallbackHandler.auth_code = None
                server = start_spotify_callback_server()
                log('info', "COVASIFY: Local OAuth callback server started on 127.0.0.1:8888")

                auth_url = auth_manager.get_authorize_url()
                webbrowser.open(auth_url)

                log('info', "COVASIFY: Waiting for Spotify authorization code...")
                timeout = 120
                elapsed = 0
                while SpotifyAuthCallbackHandler.auth_code is None and elapsed < timeout:
                    time.sleep(0.1)
                    elapsed += 0.1

                if SpotifyAuthCallbackHandler.auth_code is None:
                    log('error', 'COVASIFY: Timed out waiting for Spotify auth code')
                    server.shutdown()
                    return False

                code = SpotifyAuthCallbackHandler.auth_code
                server.shutdown()

                token_info = auth_manager.get_access_token(code, as_dict=True)
                if not token_info:
                    log('error', 'COVASIFY: Failed to exchange auth code for token')
                    return False
            else:
                log('info', 'COVASIFY: Valid cached token found, skipping OAuth flow')

            self.sp = spotipy.Spotify(auth_manager=auth_manager)
            user = self.sp.current_user()
            log('info', f"COVASIFY: Connected to Spotify as {user['display_name']}")
            return True

        except Exception as e:
            log('error', f'COVASIFY: Failed to initialize Spotify: {str(e)}')
            self.sp = None
            return False

    def get_bindings_file(self) -> str:
        return os.path.join(self.get_plugin_folder_path(), 'spotify_bindings.json')

    def load_bindings(self) -> dict:
        try:
            bindings_file = self.get_bindings_file()
            if os.path.exists(bindings_file):
                with open(bindings_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            log('error', f'COVASIFY: Error loading bindings: {str(e)}')
            return {}

    def save_bindings(self, bindings: dict) -> bool:
        try:
            with open(self.get_bindings_file(), 'w', encoding='utf-8') as f:
                json.dump(bindings, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            log('error', f'COVASIFY: Error saving bindings: {str(e)}')
            return False
