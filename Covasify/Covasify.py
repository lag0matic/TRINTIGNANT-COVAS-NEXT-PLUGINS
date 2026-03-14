from typing_extensions import override
import json
import os
import sys
import string  # For punctuation stripping
import re  # For time parsing
import time
import threading
from datetime import datetime
from pydantic import BaseModel
from typing import Optional
import webbrowser


from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
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

# Set up deps path BEFORE importing spotipy (like Songbird does with deps)
current_dir = os.path.dirname(os.path.abspath(__file__))
deps_path = os.path.join(current_dir, 'deps')
if deps_path not in sys.path:
    sys.path.insert(0, deps_path)

# Now import spotipy at module level
import spotipy
from spotipy.oauth2 import SpotifyOAuth

from lib.PluginHelper import PluginHelper
from lib.PluginSettingDefinitions import PluginSettings, SettingsGrid, TextSetting, ToggleSetting
from lib.Logger import log
from lib.EventManager import Projection
from lib.PluginBase import PluginBase, PluginManifest
from lib.Event import Event
class EmptyParams(BaseModel):
    pass

class QueryParams(BaseModel):
    query: str

class QueryShuffleParams(BaseModel):
    query: str
    shuffle: Optional[bool] = True

class QueryNoShuffleParams(BaseModel):
    query: str
    shuffle: Optional[bool] = False

class ControlParams(BaseModel):
    command: str
    value: Optional[int] = None

class SeekParams(BaseModel):
    time_input: str

class PhraseParams(BaseModel):
    phrase: str
# ============================================================================
# RELIABILITY CLIENT - Caching System (from Covinance v7.6)
# ============================================================================

class ReliabilityClient:
    """Caching wrapper for Spotify API calls - 1 hour cache TTL"""
    
    TTL_DEFAULT = 3600  # 1 hour
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

# Main plugin class
class COVASIFYPlugin(PluginBase):
    # SETTINGS MUST BE HERE — CLASS LEVEL, NOT IN __init__
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
        # Set in on_chat_start via helper.get_plugin_data_path(); used for bindings + OAuth cache
        self._plugin_data_path = None

    def on_settings_changed(self, settings: dict):
        self.settings = settings


    def normalize_phrase(self, phrase: str) -> str:
        """Normalize phrase for matching: lowercase + strip punctuation + trim whitespace"""
        if not phrase:
            return ""
        # Remove punctuation
        cleaned = phrase.translate(str.maketrans('', '', string.punctuation))
        # Remove extra whitespace and lowercase
        return ' '.join(cleaned.lower().split())
    
    def _get_data_path(self) -> str:
        """Preferred path for persistent data (bindings, OAuth cache). Survives plugin updates."""
        return self._plugin_data_path or self.get_plugin_folder_path()

    @override
    def on_chat_start(self, helper: PluginHelper):
        self._plugin_data_path = helper.get_plugin_data_path(self.plugin_manifest)
        log('info', 'COVASIFY: Chat started')

        try:
            credentials = self.load_credentials()
            if credentials:
                self.initialize_spotify(credentials)
                log('info', 'COVASIFY: Spotify initialized successfully')

            helper.register_action('covasify_test', "Test Covasify functionality", EmptyParams, self.covasify_test, 'global')
            helper.register_action('covasify_play_track', "Search for a track on Spotify and play it.", QueryParams, self.covasify_play_track, 'global')
            helper.register_action(
                'covasify_control',
                "Control Spotify playback: pause, resume, next, previous, restart, stop, volume_up, volume_down, volume_set, mute, unmute, shuffle_on, shuffle_off, repeat_track, repeat_context, repeat_off.",
                ControlParams, self.covasify_control, 'global'
            )
            helper.register_action('covasify_seek', "Seek to a position in the current track. Accepts 'MM:SS', 'H:MM:SS', or total seconds.", SeekParams, self.covasify_seek, 'global')
            helper.register_action('covasify_current', "Get info about the currently playing Spotify track.", EmptyParams, self.covasify_current, 'global')
            helper.register_action('covasify_play_playlist', "Play a Spotify playlist by name, or 'liked songs' for saved tracks.", QueryShuffleParams, self.covasify_play_playlist, 'global')
            helper.register_action('covasify_play_artist', "Play music from an artist on Spotify.", QueryShuffleParams, self.covasify_play_artist, 'global')
            helper.register_action('covasify_play_top_tracks', "Play an artist's top 10 most popular tracks.", QueryParams, self.covasify_play_top_tracks, 'global')
            helper.register_action('covasify_play_album', "Play a complete album on Spotify.", QueryNoShuffleParams, self.covasify_play_album, 'global')
            helper.register_action('covasify_save_track', "Save the currently playing track to Liked Songs.", EmptyParams, self.covasify_save_track, 'global')
            helper.register_action('covasify_remove_track', "Remove the currently playing track from Liked Songs.", EmptyParams, self.covasify_remove_track, 'global')
            helper.register_action('covasify_bind_track', "Bind the currently playing track to a custom voice phrase.", PhraseParams, self.covasify_bind_track, 'global')
            helper.register_action('covasify_play_bound', "Play the track bound to a given phrase.", PhraseParams, self.covasify_play_bound, 'global')
            helper.register_action('covasify_list_bindings', "List all phrase-to-track bindings.", EmptyParams, self.covasify_list_bindings, 'global')
            helper.register_action('covasify_unbind', "Remove a specific phrase binding.", PhraseParams, self.covasify_unbind, 'global')
            helper.register_action('covasify_unbind_all', "Remove all phrase bindings.", EmptyParams, self.covasify_unbind_all, 'global')
            helper.register_action('covasify_cache_stats', "Show Covasify cache statistics.", EmptyParams, self.covasify_cache_stats, 'global')
            helper.register_status_generator(self.generate_binding_status)
            log('info', 'COVASIFY: Actions and status generator registered successfully')
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
        # Status generator registered in on_chat_start per plugin development docs
        pass
    
    def covasify_cache_stats(self, args, projected_states) -> str:
        """
        Get cache performance statistics.
        
        Shows cache hit rate, total requests, API calls saved, etc.
        
        Returns: Cache performance metrics
        """
        try:
            stats = self.reliability_client.get_stats()
            
            return (
                f"COVASIFY: Cache Performance\n"
                f"Hit Rate: {stats['cache_hit_rate']}\n"
                f"Total Requests: {stats['total_requests']}\n"
                f"API Calls Saved: {stats['api_calls_saved']}\n"
                f"Cache Hits: {stats['cache_hits']}\n"
                f"Cache Misses: {stats['cache_misses']}\n"
                f"In-Flight Hits: {stats['inflight_hits']}"
            )
        except Exception as e:
            log('error', f'COVASIFY: Error getting cache stats: {str(e)}')
            return f"COVASIFY: Error retrieving cache statistics: {str(e)}"
    
    def generate_binding_status(self, projected_states: dict[str, dict]) -> list[tuple[str, str]]:
        """Generate status about track bindings for COVAS context"""
        try:
            bindings = self.load_bindings()
            
            if not bindings:
                return [("Covasify Bindings", "No tracks bound to phrases")]
            
            # List bindings concisely
            binding_count = len(bindings)
            binding_phrases = list(bindings.keys())
            
            if binding_count <= 3:
                phrase_list = ", ".join([f"'{p}'" for p in binding_phrases])
                return [("Covasify Bindings", f"{binding_count} track(s): {phrase_list}")]
            else:
                # Just show count for many bindings
                return [("Covasify Bindings", f"{binding_count} bindings active")]
                
        except Exception as e:
            log('error', f'COVASIFY: Error generating binding status: {str(e)}')
            return [("Covasify Bindings", "System available")]

    @override
    def register_should_reply_handlers(self, helper: PluginHelper):
        pass
    
    @override
    def on_plugin_helper_ready(self, helper: PluginHelper):
        log('info', 'COVASIFY: Plugin helper is ready')
    
    @override
    def on_chat_stop(self, helper: PluginHelper):
        log('info', 'COVASIFY: Chat stopped')

    def get_plugin_folder_path(self) -> str:
        """Get the path to the plugin folder (same structure as Songbird)"""
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            return current_dir
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
            # Settings are FLAT — not nested under "spotify_credentials"
            client_id = self.settings.get('client_id')
            client_secret = self.settings.get('client_secret')
            redirect_uri = self.settings.get('redirect_uri')

            # If UI settings are complete, use them
            if client_id and client_secret:
                log('info', 'COVASIFY: Credentials loaded from Settings UI')
                return {
                    'CLIENT_ID': client_id,
                    'CLIENT_SECRET': client_secret,
                    'REDIRECT_URI': redirect_uri or 'http://127.0.0.1:8888/callback'
                }

            log('warning', 'COVASIFY: Settings UI incomplete — falling back to spotify_credentials.txt')

            # --- Fallback to file ---
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
                log('info', 'COVASIFY: Credentials loaded from spotify_credentials.txt (fallback)')
                credentials.setdefault('REDIRECT_URI', 'http://127.0.0.1:8888/callback')
                return credentials

            log('error', 'COVASIFY: Invalid credentials format in spotify_credentials.txt')
            return None

        except Exception as e:
            log('error', f'COVASIFY: Error loading credentials: {str(e)}')
            return None


    def initialize_spotify(self, credentials: dict):
        try:
            log('info', "COVASIFY: Initializing Spotify")
            data_path = self._get_data_path()
            try:
                os.makedirs(data_path, exist_ok=True)
            except OSError:
                pass
            cache_path = os.path.join(data_path, '_spotify_cache')

            auth_manager = SpotifyOAuth(
                client_id=credentials['CLIENT_ID'],
                client_secret=credentials['CLIENT_SECRET'],
                redirect_uri=credentials.get('REDIRECT_URI', 'http://127.0.0.1:8888/callback'),
                scope='user-read-playback-state user-modify-playback-state user-read-currently-playing user-library-read user-library-modify user-top-read playlist-read-private playlist-read-collaborative',
                cache_path=cache_path,
                open_browser=False
            )

            # Check if we already have a valid cached token
            token_info = auth_manager.get_cached_token()

            if not (token_info and not auth_manager.is_token_expired(token_info)):
                # Need fresh auth - reset class-level auth code first
                SpotifyAuthCallbackHandler.auth_code = None

                server = start_spotify_callback_server()
                log('info', "COVASIFY: Local OAuth callback server started on 127.0.0.1:8888")

                auth_url = auth_manager.get_authorize_url()
                log('info', f"COVASIFY: Opening browser for Spotify auth: {auth_url}")
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
                log('info', f"COVASIFY: Received Spotify auth code: {code[:10]}...")
                server.shutdown()
                log('info', "COVASIFY: OAuth callback server shut down")

                token_info = auth_manager.get_access_token(code, as_dict=True)
                if not token_info:
                    log('error', 'COVASIFY: Failed to exchange auth code for token')
                    return False
            else:
                log('info', 'COVASIFY: Valid cached token found, skipping OAuth flow')
            log('info', "COVASIFY: Access token obtained and cached")

            # Create Spotify client
            self.sp = spotipy.Spotify(auth_manager=auth_manager)
            user = self.sp.current_user()
            log('info', f"COVASIFY: Connected to Spotify as {user['display_name']}")

            return True

        except Exception as e:
            log('error', f'COVASIFY: Failed to initialize Spotify: {str(e)}')
            self.sp = None
            return False

    def update_current_track_info(self):
        """Update cached info about currently playing track for binding"""
        try:
            if not self.sp:
                return
            
            current = self.sp.current_playback()
            if not current or not current.get('item'):
                self.current_track_info = None
                return
            
            track = current['item']
            self.current_track_info = {
                'track_uri': track['uri'],
                'track_name': track['name'],
                'artist_name': ', '.join([artist['name'] for artist in track['artists']]),
                'album_name': track['album']['name'],
                'track_id': track['id']
            }
            
        except Exception as e:
            log('error', f'COVASIFY: Error updating current track info: {str(e)}')
            self.current_track_info = None

    def covasify_test(self, args, projected_states) -> str:
        """Test function (like Songbird's songbird_test)"""
        try:
            log('info', 'COVASIFY: Running test')
            
            version = self.plugin_manifest.version
            name = self.plugin_manifest.name
            
            # Check if Spotify client is initialized
            if self.sp:
                return f"COVASIFY Test: {name} v{version} - Active and connected to Spotify."
            else:
                plugin_folder = self.get_plugin_folder_path()
                return f"COVASIFY Test: {name} v{version} - Active but not connected. Check credentials at: {plugin_folder}"
                
        except Exception as e:
            log('error', f'COVASIFY test error: {str(e)}')
            return f"COVASIFY: Test failed - {str(e)}"

    def covasify_play_track(self, args, projected_states) -> str:
        """Search for and play a track on Spotify (like Songbird's play_sound)"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify. Check credentials."
            
            query = args.query
            if not query:
                return "COVASIFY: No search query provided."
            
            log('info', f'COVASIFY: Searching for track: {query}')
            
            # Search for track with caching - fetch multiple for better matching
            def search_track(endpoint, params):
                return self.sp.search(q=params['q'], type='track', limit=10)
            
            results = self.reliability_client.get_cached_or_fetch(
                'spotify_search_track',
                {'q': query},
                search_track
            )
            
            if not results['tracks']['items']:
                return f"COVASIFY: No tracks found for '{query}'."
            
            # Pick best match by scoring each result against the query
            query_lower = query.lower()
            query_words = set(query_lower.split())

            def score_track(t):
                name = t['name'].lower()
                artist = t['artists'][0]['name'].lower()
                combined = f"{name} {artist}"
                combined_words = set(combined.split())
                overlap = len(query_words & combined_words)
                exact = 2 if query_lower in combined else 0
                length_diff = abs(len(name) - len(query_lower))
                return overlap + exact - (length_diff * 0.05)

            track = max(results['tracks']['items'], key=score_track)
            track_name = track['name']
            artist_name = track['artists'][0]['name']
            track_uri = track['uri']
            album_uri = track['album']['uri']
            log('info', f"COVASIFY: Best match: '{track_name}' by {artist_name}")
            
            # Get available devices
            devices = self.sp.devices()
            if not devices['devices']:
                return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."
            
            device_id = devices['devices'][0]['id']
            self.sp.transfer_playback(device_id, force_play=True)
            self.sp.start_playback(
                device_id=device_id,
                context_uri=album_uri,
                offset={"uri": track_uri}
)

            
            # Update current track info for binding
            self.update_current_track_info()
            
            log('info', f'COVASIFY: Playing {track_name} by {artist_name}')
            return f"COVASIFY: Now playing {track_name} by {artist_name}."
            
        except Exception as e:
            log('error', f'COVASIFY play_track error: {str(e)}')
            return f"COVASIFY: Failed to play track - {str(e)}"

    def covasify_control(self, args, projected_states) -> str:
        """Control Spotify playback (like Songbird's songbird_control)"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."
            
            command = args.command.lower()
            
            log('info', f'COVASIFY: Control command: {command}')
            
            # Normalize command variations
            if command in ['pause', 'stop']:
                self.sp.pause_playback()
                log('info', 'COVASIFY: Paused playback')
                return "COVASIFY: Playback paused."
                
            elif command in ['resume', 'play', 'unpause']:
                self.sp.start_playback()
                log('info', 'COVASIFY: Resumed playback')
                return "COVASIFY: Playback resumed."
                
            elif command in ['next', 'skip', 'skip_forward', 'next_track']:
                self.sp.next_track()
                # Update current track info
                self.update_current_track_info()
                log('info', 'COVASIFY: Skipped to next track')
                return "COVASIFY: Skipped to next track."
                
            elif command in ['previous', 'back', 'skip_back', 'previous_track']:
                self.sp.previous_track()
                # Update current track info
                self.update_current_track_info()
                log('info', 'COVASIFY: Skipped to previous track')
                return "COVASIFY: Skipped to previous track."
                
            elif command in ['restart', 'restart_track', 'restart_song', 'start_over', 'from_beginning']:
                current = self.sp.current_playback()
                if current and current.get('device'):
                    device_id = current['device']['id']
                    self.sp.seek_track(position_ms=0, device_id=device_id)
                    log('info', 'COVASIFY: Restarted current track')
                    return "COVASIFY: Restarted current track from beginning."
                else:
                    return "COVASIFY: No active playback to restart."
                
            elif command in ['volume_up', 'louder', 'increase_volume']:
                current = self.sp.current_playback()
                if current and current.get('device'):
                    current_volume = current['device']['volume_percent']
                    new_volume = min(100, current_volume + 10)
                    self.sp.volume(new_volume)
                    log('info', f'COVASIFY: Volume increased to {new_volume}%')
                    return f"COVASIFY: Volume increased to {new_volume}%."
                return "COVASIFY: No active playback to adjust volume."
                
            elif command in ['volume_down', 'quieter', 'decrease_volume']:
                current = self.sp.current_playback()
                if current and current.get('device'):
                    current_volume = current['device']['volume_percent']
                    new_volume = max(0, current_volume - 10)
                    self.sp.volume(new_volume)
                    log('info', f'COVASIFY: Volume decreased to {new_volume}%')
                    return f"COVASIFY: Volume decreased to {new_volume}%."
                return "COVASIFY: No active playback to adjust volume."
                
            elif command in ['volume_set', 'set_volume']:
                value = args.value if args.value is not None else 50
                value = max(0, min(100, value))
                self.sp.volume(value)
                log('info', f'COVASIFY: Volume set to {value}%')
                return f"COVASIFY: Volume set to {value}%."
                
            elif command in ['mute', 'silence']:
                self.sp.volume(0)
                log('info', 'COVASIFY: Muted')
                return "COVASIFY: Muted."
                
            elif command in ['unmute', 'unsilence']:
                self.sp.volume(50)
                log('info', 'COVASIFY: Unmuted to 50%')
                return "COVASIFY: Unmuted to 50%."
                
            elif command in ['shuffle_on', 'enable_shuffle', 'shuffle']:
                self.sp.shuffle(True)
                log('info', 'COVASIFY: Shuffle enabled')
                return "COVASIFY: Shuffle enabled."
                
            elif command in ['shuffle_off', 'disable_shuffle', 'no_shuffle']:
                self.sp.shuffle(False)
                log('info', 'COVASIFY: Shuffle disabled')
                return "COVASIFY: Shuffle disabled."
                
            elif command in ['repeat_track', 'repeat_song', 'repeat_one']:
                self.sp.repeat('track')
                log('info', 'COVASIFY: Repeat track enabled')
                return "COVASIFY: Repeat track enabled."
                
            elif command in ['repeat_context', 'repeat_all', 'repeat_playlist']:
                self.sp.repeat('context')
                log('info', 'COVASIFY: Repeat context enabled')
                return "COVASIFY: Repeat all enabled."
                
            elif command in ['repeat_off', 'disable_repeat', 'no_repeat']:
                self.sp.repeat('off')
                log('info', 'COVASIFY: Repeat disabled')
                return "COVASIFY: Repeat disabled."
                
            else:
                return f"COVASIFY: Unknown command '{command}'."
                
        except Exception as e:
            log('error', f'COVASIFY control error: {str(e)}')
            return f"COVASIFY: Control failed - {str(e)}"

    def covasify_current(self, args, projected_states) -> str:
        """Get information about currently playing track (like Songbird tracking current sound)"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."
            
            current = self.sp.current_playback()
            
            if not current or not current.get('item'):
                return "COVASIFY: No track currently playing."
            
            track = current['item']
            track_name = track['name']
            artists = ', '.join([artist['name'] for artist in track['artists']])
            album = track['album']['name']
            
            # Get progress info
            progress_ms = current.get('progress_ms', 0)
            duration_ms = track.get('duration_ms', 0)
            progress_min = progress_ms // 60000
            progress_sec = (progress_ms % 60000) // 1000
            duration_min = duration_ms // 60000
            duration_sec = (duration_ms % 60000) // 1000
            
            # Update current track info for binding
            self.update_current_track_info()
            
            log('info', f'COVASIFY: Current track - {track_name} by {artists}')
            
            return f"COVASIFY: Now playing '{track_name}' by {artists} from the album '{album}'. Progress: {progress_min}:{progress_sec:02d} / {duration_min}:{duration_sec:02d}."
            
        except Exception as e:
            log('error', f'COVASIFY current track error: {str(e)}')
            return f"COVASIFY: Failed to get current track info - {str(e)}"

    def covasify_seek(self, args, projected_states) -> str:
        """Seek to specific position in track - handles multiple time formats"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."
            
            time_input = args.time_input.strip()
            
            if not time_input:
                return "COVASIFY: No time position provided."
            
            log('info', f'COVASIFY: Seek request: {time_input}')
            
            # Parse time input to milliseconds
            position_ms = self._parse_time_to_ms(time_input)
            
            if position_ms is None:
                return f"COVASIFY: Could not parse time '{time_input}'. Use format like '2:30' or '150' seconds."
            
            # Get current playback to check track duration
            current = self.sp.current_playback()
            
            if not current or not current.get('item'):
                return "COVASIFY: No track currently playing to seek."
            
            track = current['item']
            track_name = track['name']
            duration_ms = track.get('duration_ms', 0)
            
            # Check if position is within track duration
            if position_ms > duration_ms:
                duration_min = duration_ms // 60000
                duration_sec = (duration_ms % 60000) // 1000
                return f"COVASIFY: Position {time_input} exceeds track duration ({duration_min}:{duration_sec:02d})."
            
            # Seek to position
            device_id = current['device']['id']
            self.sp.seek_track(position_ms, device_id=device_id)
            
            # Format position for response
            seek_min = position_ms // 60000
            seek_sec = (position_ms % 60000) // 1000
            
            log('info', f'COVASIFY: Seeked to {seek_min}:{seek_sec:02d} in {track_name}')
            return f"COVASIFY: Seeked to {seek_min}:{seek_sec:02d} in {track_name}."
            
        except Exception as e:
            log('error', f'COVASIFY seek error: {str(e)}')
            return f"COVASIFY: Failed to seek - {str(e)}"

    def _parse_time_to_ms(self, time_input: str) -> int:
        """Parse time string to milliseconds - handles MM:SS, H:MM:SS, or seconds"""
        try:
            time_input = time_input.strip().lower()
            
            # Remove common words
            time_input = time_input.replace('minutes', '').replace('minute', '')
            time_input = time_input.replace('seconds', '').replace('second', '')
            time_input = time_input.replace('and', '').strip()
            
            # Check if it's just a number (seconds)
            if time_input.isdigit():
                return int(time_input) * 1000
            
            # Check for MM:SS or H:MM:SS format
            if ':' in time_input:
                parts = time_input.split(':')
                
                if len(parts) == 2:
                    # MM:SS format
                    minutes = int(parts[0])
                    seconds = int(parts[1])
                    return (minutes * 60 + seconds) * 1000
                    
                elif len(parts) == 3:
                    # H:MM:SS format
                    hours = int(parts[0])
                    minutes = int(parts[1])
                    seconds = int(parts[2])
                    return (hours * 3600 + minutes * 60 + seconds) * 1000
            
            # Try to extract numbers (e.g., "2 minutes 30 seconds")
            numbers = re.findall(r'\d+', time_input)
            
            if len(numbers) == 2:
                # Assume first is minutes, second is seconds
                minutes = int(numbers[0])
                seconds = int(numbers[1])
                return (minutes * 60 + seconds) * 1000
            elif len(numbers) == 1:
                # Just one number - assume seconds
                return int(numbers[0]) * 1000
            
            return None
            
        except Exception as e:
            log('error', f'COVASIFY: Error parsing time {time_input}: {str(e)}')
            return None

    def covasify_play_playlist(self, args, projected_states) -> str:
        """Play a Spotify playlist"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."
            
            query = args.query.lower()
            shuffle = args.shuffle
            
            if not query:
                return "COVASIFY: No playlist name provided."
            
            log('info', f'COVASIFY: Searching for playlist: {query}')
            
            # Get available devices
            devices = self.sp.devices()
            if not devices['devices']:
                return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."
            
            device_id = devices['devices'][0]['id']
            
            # Handle "Liked Songs" specially
            if 'liked' in query or 'saved' in query or 'favorite' in query:
                saved_tracks = self.sp.current_user_saved_tracks(limit=50)
                if not saved_tracks['items']:
                    return "COVASIFY: No liked songs found."
                
                track_uris = [item['track']['uri'] for item in saved_tracks['items']]
                
                self.sp.start_playback(device_id=device_id, uris=track_uris)
                if shuffle:
                    self.sp.shuffle(True, device_id=device_id)
                
                # Update current track info
                self.update_current_track_info()
                
                log('info', 'COVASIFY: Playing Liked Songs')
                return f"COVASIFY: Playing your Liked Songs{' (shuffled)' if shuffle else ''}."
            
            # Search for playlist by name with caching
            def search_playlist(endpoint, params):
                return self.sp.search(q=params['q'], type='playlist', limit=5)
            
            results = self.reliability_client.get_cached_or_fetch(
                'spotify_search_playlist',
                {'q': query},
                search_playlist
            )
            
            if not results['playlists']['items']:
                return f"COVASIFY: No playlists found for '{query}'."
            
            # Get first matching playlist
            playlist = results['playlists']['items'][0]
            playlist_name = playlist['name']
            playlist_uri = playlist['uri']
            
            # Check if playlist_uri is valid
            if not playlist_uri:
                return f"COVASIFY: Found playlist '{playlist_name}' but cannot access it."
            
            self.sp.start_playback(device_id=device_id, context_uri=playlist_uri)
            if shuffle:
                self.sp.shuffle(True, device_id=device_id)
            
            # Update current track info
            self.update_current_track_info()
            
            log('info', f'COVASIFY: Playing playlist {playlist_name}')
            return f"COVASIFY: Playing playlist '{playlist_name}'{' (shuffled)' if shuffle else ''}."
            
        except Exception as e:
            log('error', f'COVASIFY play_playlist error: {str(e)}')
            return f"COVASIFY: Failed to play playlist - {str(e)}"

    def covasify_play_artist(self, args, projected_states) -> str:
        """Play music from an artist using artist context - lets Spotify handle the queue naturally"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."
            
            query = args.query
            shuffle = args.shuffle
            
            if not query:
                return "COVASIFY: No artist provided."
            
            log('info', f'COVASIFY: Playing artist: {query}')
            
            # Get available devices
            devices = self.sp.devices()
            if not devices['devices']:
                return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."
            
            device_id = devices['devices'][0]['id']
            
            # Search for artist with caching
            def search_artist(endpoint, params):
                return self.sp.search(q=params['q'], type='artist', limit=1)
            
            artist_results = self.reliability_client.get_cached_or_fetch(
                'spotify_search_artist',
                {'q': query},
                search_artist
            )
            
            if not artist_results['artists']['items']:
                return f"COVASIFY: Could not find artist '{query}'."
            
            artist = artist_results['artists']['items'][0]
            artist_name = artist['name']
            artist_id = artist['id']
            artist_uri = f"spotify:artist:{artist_id}"
            
            log('info', f'COVASIFY: Found artist {artist_name} with ID {artist_id}')
            
            # Play from artist context - Spotify handles queue naturally
            self.sp.start_playback(device_id=device_id, context_uri=artist_uri)
            
            # Enable shuffle if requested
            if shuffle:
                self.sp.shuffle(True, device_id=device_id)
            
            # Update current track info
            self.update_current_track_info()
            
            log('info', f'COVASIFY: Playing {artist_name} from artist context (shuffle: {shuffle})')
            return f"COVASIFY: Playing {artist_name}{' (shuffled)' if shuffle else ''} - Spotify will queue their music naturally."
            
        except Exception as e:
            log('error', f'COVASIFY play_artist error: {str(e)}')
            return f"COVASIFY: Failed to play artist - {str(e)}"

    def covasify_play_top_tracks(self, args, projected_states) -> str:
        """Play an artist's most popular songs"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."
            
            query = args.query
            if not query:
                return "COVASIFY: No artist provided."
            
            log('info', f'COVASIFY: Getting top tracks for: {query}')
            
            # Get available devices
            devices = self.sp.devices()
            if not devices['devices']:
                return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."
            
            device_id = devices['devices'][0]['id']
            
            # Search for artist with caching
            def search_artist(endpoint, params):
                return self.sp.search(q=params['q'], type='artist', limit=1)
            
            artist_results = self.reliability_client.get_cached_or_fetch(
                'spotify_search_artist',
                {'q': query},
                search_artist
            )
            
            if not artist_results['artists']['items']:
                return f"COVASIFY: Could not find artist '{query}'."
            
            artist = artist_results['artists']['items'][0]
            artist_name = artist['name']
            artist_id = artist['id']
            
            log('info', f'COVASIFY: Found artist {artist_name} with ID {artist_id}')
            
            # Get top tracks
            top_tracks = self.sp.artist_top_tracks(artist_id)
            
            if not top_tracks['tracks']:
                return f"COVASIFY: No top tracks found for {artist_name}."
            
            track_uris = [track['uri'] for track in top_tracks['tracks']]
            
            # Play top tracks
            self.sp.start_playback(device_id=device_id, uris=track_uris)
            
            # Update current track info
            self.update_current_track_info()
            
            log('info', f'COVASIFY: Playing {len(track_uris)} top tracks by {artist_name}')
            return f"COVASIFY: Playing {len(track_uris)} most popular songs by {artist_name}."
            
        except Exception as e:
            log('error', f'COVASIFY play_top_tracks error: {str(e)}')
            return f"COVASIFY: Failed to play top tracks - {str(e)}"

    def covasify_play_album(self, args, projected_states) -> str:
        """Play a complete album on Spotify"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."
            
            query = args.query
            shuffle = args.shuffle  # Default false - most people want album order
            
            if not query:
                return "COVASIFY: No album name provided."
            
            log('info', f'COVASIFY: Searching for album: {query}')
            
            # Get available devices
            devices = self.sp.devices()
            log('info', f"COVASIFY: Available devices: {devices['devices']}")


            if not devices['devices']:
                return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."
            
            device_id = devices['devices'][0]['id']
            
            # Search for album with caching
            def search_album(endpoint, params):
                return self.sp.search(q=params['q'], type='album', limit=1)
            
            album_results = self.reliability_client.get_cached_or_fetch(
                'spotify_search_album',
                {'q': query},
                search_album
            )
            
            if not album_results['albums']['items']:
                return f"COVASIFY: Could not find album '{query}'."
            
            album = album_results['albums']['items'][0]
            album_name = album['name']
            artist_name = album['artists'][0]['name']
            album_uri = album['uri']
            total_tracks = album['total_tracks']
            
            log('info', f'COVASIFY: Found album "{album_name}" by {artist_name} ({total_tracks} tracks)')
            
            # Play album from context
            self.sp.start_playback(device_id=device_id, context_uri=album_uri)
            
            # Apply shuffle if requested
            if shuffle:
                self.sp.shuffle(True, device_id=device_id)
            
            # Update current track info
            self.update_current_track_info()
            
            log('info', f'COVASIFY: Playing album "{album_name}" by {artist_name} (shuffle: {shuffle})')
            return f"COVASIFY: Playing album '{album_name}' by {artist_name} ({total_tracks} tracks){' (shuffled)' if shuffle else ''}."
            
        except Exception as e:
            log('error', f'COVASIFY play_album error: {str(e)}')
            return f"COVASIFY: Failed to play album - {str(e)}"

    def covasify_save_track(self, args, projected_states) -> str:
        """Save currently playing track to Liked Songs"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."
            
            log('info', 'COVASIFY: Attempting to save current track')
            
            # Get currently playing track
            current = self.sp.current_playback()
            
            if not current or not current.get('item'):
                return "COVASIFY: No track currently playing to save."
            
            track = current['item']
            track_id = track['id']
            track_name = track['name']
            artist_name = track['artists'][0]['name']
            
            # Check if already saved
            is_saved = self.sp.current_user_saved_tracks_contains([track_id])
            
            if is_saved[0]:
                log('info', f'COVASIFY: Track "{track_name}" already in library')
                return f"COVASIFY: '{track_name}' by {artist_name} is already in your Liked Songs."
            
            # Save track to library
            self.sp.current_user_saved_tracks_add([track_id])
            
            log('info', f'COVASIFY: Saved "{track_name}" by {artist_name} to library')
            return f"COVASIFY: Added '{track_name}' by {artist_name} to your Liked Songs."
            
        except Exception as e:
            log('error', f'COVASIFY save_track error: {str(e)}')
            return f"COVASIFY: Failed to save track - {str(e)}"

    def covasify_remove_track(self, args, projected_states) -> str:
        """Remove currently playing track from Liked Songs"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."
            
            log('info', 'COVASIFY: Attempting to remove current track from library')
            
            # Get currently playing track
            current = self.sp.current_playback()
            
            if not current or not current.get('item'):
                return "COVASIFY: No track currently playing to remove."
            
            track = current['item']
            track_id = track['id']
            track_name = track['name']
            artist_name = track['artists'][0]['name']
            
            # Check if saved
            is_saved = self.sp.current_user_saved_tracks_contains([track_id])
            
            if not is_saved[0]:
                log('info', f'COVASIFY: Track "{track_name}" not in library')
                return f"COVASIFY: '{track_name}' by {artist_name} is not in your Liked Songs."
            
            # Remove track from library
            self.sp.current_user_saved_tracks_delete([track_id])
            
            log('info', f'COVASIFY: Removed "{track_name}" by {artist_name} from library')
            return f"COVASIFY: Removed '{track_name}' by {artist_name} from your Liked Songs."
            
        except Exception as e:
            log('error', f'COVASIFY remove_track error: {str(e)}')
            return f"COVASIFY: Failed to remove track - {str(e)}"

    def get_bindings_file(self) -> str:
        """Get path to bindings JSON file (uses plugin data path when available so bindings survive updates)."""
        base = self._get_data_path()
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            pass
        return os.path.join(base, 'spotify_bindings.json')

    def load_bindings(self) -> dict:
        """Load track bindings from file"""
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
        """Save track bindings to file"""
        try:
            bindings_file = self.get_bindings_file()
            with open(bindings_file, 'w', encoding='utf-8') as f:
                json.dump(bindings, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            log('error', f'COVASIFY: Error saving bindings: {str(e)}')
            return False

    def covasify_bind_track(self, args, projected_states) -> str:
        """Bind currently playing track to a custom phrase"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."
            
            phrase = args.phrase
            if not phrase:
                return "COVASIFY: No phrase provided."
            
            normalized_phrase = self.normalize_phrase(phrase)
            
            if not normalized_phrase:
                return "COVASIFY: Invalid phrase provided."
            
            log('info', f'COVASIFY: Binding request for phrase: "{phrase}" (normalized: "{normalized_phrase}")')
            
            # Update current track info
            self.update_current_track_info()
            
            if not self.current_track_info:
                return "COVASIFY: No track currently playing to bind. Play a track first."
            
            # Load existing bindings
            bindings = self.load_bindings()
            
            # Add new binding with normalized phrase as key
            bindings[normalized_phrase] = {
                'track_uri': self.current_track_info['track_uri'],
                'track_name': self.current_track_info['track_name'],
                'artist_name': self.current_track_info['artist_name'],
                'album_name': self.current_track_info['album_name']
            }
            
            # Save bindings
            if self.save_bindings(bindings):
                log('info', f'COVASIFY: Bound "{self.current_track_info["track_name"]}" to phrase "{normalized_phrase}"')
                return f"COVASIFY: Bound '{self.current_track_info['track_name']}' by {self.current_track_info['artist_name']} to phrase '{phrase}'."
            else:
                return "COVASIFY: Failed to save binding."
            
        except Exception as e:
            log('error', f'COVASIFY bind_track error: {str(e)}')
            return f"COVASIFY: Failed to bind track - {str(e)}"

    def covasify_play_bound(self, args, projected_states) -> str:
        """Play a track bound to a custom phrase"""
        try:
            if not self.sp:
                return "COVASIFY: Not connected to Spotify."
            
            phrase = args.phrase
            if not phrase:
                return "COVASIFY: No phrase provided."
            
            # Normalize phrase for matching
            normalized_phrase = self.normalize_phrase(phrase)
            
            log('info', f'COVASIFY: Playing bound track for phrase: "{phrase}" (normalized: "{normalized_phrase}")')
            
            # Load bindings
            bindings = self.load_bindings()
            
            # Check if phrase exists (bindings are already normalized when saved)
            if normalized_phrase not in bindings:
                return f"COVASIFY: No track bound to phrase '{phrase}'. Use 'list bindings' to see available phrases."
            
            binding = bindings[normalized_phrase]
            track_uri = binding['track_uri']
            track_name = binding['track_name']
            artist_name = binding['artist_name']
            
            # Get available devices
            devices = self.sp.devices()
            if not devices['devices']:
                return "COVASIFY: No active Spotify devices found. Open Spotify on a device first."
            
            device_id = devices['devices'][0]['id']
            
            # Play the bound track
            self.sp.start_playback(device_id=device_id, uris=[track_uri])
            
            # Update current track info
            self.update_current_track_info()
            
            log('info', f'COVASIFY: Playing bound track: {track_name}')
            return f"COVASIFY: Playing '{track_name}' by {artist_name}."
            
        except Exception as e:
            log('error', f'COVASIFY play_bound error: {str(e)}')
            return f"COVASIFY: Failed to play bound track - {str(e)}"

    def covasify_list_bindings(self, args, projected_states) -> str:
        """List all track bindings"""
        try:
            log('info', 'COVASIFY: Listing bindings')
            
            bindings = self.load_bindings()
            
            if not bindings:
                return "COVASIFY: No track bindings found. Use 'bind this to [phrase]' to create bindings."
            
            binding_list = []
            for phrase, info in bindings.items():
                track_name = info['track_name']
                artist_name = info['artist_name']
                binding_list.append(f"- '{phrase}' -> {track_name} by {artist_name}")
            
            result = f"COVASIFY: Found {len(bindings)} track bindings:\n" + "\n".join(binding_list)
            
            log('info', f'COVASIFY: Listed {len(bindings)} bindings')
            return result
            
        except Exception as e:
            log('error', f'COVASIFY list_bindings error: {str(e)}')
            return f"COVASIFY: Failed to list bindings - {str(e)}"

    def covasify_unbind(self, args, projected_states) -> str:
        """Remove a specific track binding"""
        try:
            phrase = args.phrase
            if not phrase:
                return "COVASIFY: No phrase provided."
            
            # Normalize phrase for matching
            normalized_phrase = self.normalize_phrase(phrase)
            
            log('info', f'COVASIFY: Unbind request for phrase: "{phrase}" (normalized: "{normalized_phrase}")')
            
            # Load bindings
            bindings = self.load_bindings()
            
            # Check if phrase exists
            if normalized_phrase not in bindings:
                return f"COVASIFY: No track bound to phrase '{phrase}'."
            
            track_name = bindings[normalized_phrase]['track_name']
            artist_name = bindings[normalized_phrase]['artist_name']
            
            # Remove binding
            del bindings[normalized_phrase]
            
            # Save updated bindings
            if self.save_bindings(bindings):
                log('info', f'COVASIFY: Unbound phrase "{normalized_phrase}"')
                return f"COVASIFY: Unbound '{track_name}' by {artist_name} from phrase '{phrase}'."
            else:
                return "COVASIFY: Failed to save updated bindings."
            
        except Exception as e:
            log('error', f'COVASIFY unbind error: {str(e)}')
            return f"COVASIFY: Failed to unbind - {str(e)}"

    def covasify_unbind_all(self, args, projected_states) -> str:
        """Remove all track bindings"""
        try:
            log('info', 'COVASIFY: Unbind all request')
            
            # Load current bindings to count them
            bindings = self.load_bindings()
            count = len(bindings)
            
            if count == 0:
                return "COVASIFY: No track bindings to remove."
            
            # Clear all bindings
            if self.save_bindings({}):
                log('info', f'COVASIFY: Removed all {count} bindings')
                return f"COVASIFY: Removed all {count} track bindings."
            else:
                return "COVASIFY: Failed to clear bindings."
            
        except Exception as e:
            log('error', f'COVASIFY unbind_all error: {str(e)}')
            return f"COVASIFY: Failed to unbind all - {str(e)}"