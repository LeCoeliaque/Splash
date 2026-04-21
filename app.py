import json
import math
import os
import random
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template, request, session
from flask_session import Session

app = Flask(__name__)

# ============ CONFIG FROM ENV ============

app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))

# Flask-Session: server-side sessions stored in memory
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = "/tmp/flask_sessions"
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7  # 7 days

Session(app)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://erspvsdfwaqjtuhymubj.supabase.co")
ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
LOCATION_ENDPOINT = os.environ.get(
    "LOCATION_ENDPOINT", "https://location.splashin.app/api/v4/on-location"
)
DEVICE_UUID = os.environ.get("DEVICE_UUID", "7D53AF20-66A8-4F42-B422-0C7ED8939911")

# ============ PER-USER STATE STORE ============
# Keyed by session["user_id"]. Cleaned up on logout / TTL.
_user_states: dict[str, dict] = {}
_user_states_lock = threading.Lock()

DEFAULT_SAVED_LOCATIONS = {
    "home": {"lat": 0.0, "lon": 0.0, "name": "Home"},
}

MODES = {
    "walking": {"speed_ms": 1.4, "variance": 0.4, "activity": "walking",    "accuracy": 8.0,  "jitter_m": 2.5, "interval": 5},
    "driving": {"speed_ms": 11.0,"variance": 4.0, "activity": "in_vehicle", "accuracy": 5.0,  "jitter_m": 1.0, "interval": 3},
    "still":   {"speed_ms": 0,   "variance": 0,   "activity": "still",      "accuracy": 10.0, "jitter_m": 1.5, "interval": 30},
}


def make_user_state() -> dict:
    return {
        # movement
        "spoofing": False,
        "lat": None,
        "lon": None,
        "mode": "still",
        "speed": 0.0,
        "velocity": 0.0,
        "heading": 0.0,
        "battery": 0.72,
        "charging": False,
        "route": [],
        "route_index": 0,
        "status": "Idle",
        "log": [],
        "last_post": 0,
        "last_lat": None,
        "last_lon": None,
        "stopped_until": 0,
        "traffic_wave": None,
        # auth
        "access_token": "",
        "refresh_token": "",
        "user_id": "",
        "expo_token": "",
        "location_jwt": "",
        "location_refresh_token": "",
        # settings
        "saved_locations": dict(DEFAULT_SAVED_LOCATIONS),
        "home_location": "home",
        "home_radius_m": 200,
        # thread control
        "_thread_started": False,
        "_last_activity": time.time(),
    }


def get_state(user_id: str) -> dict:
    with _user_states_lock:
        if user_id not in _user_states:
            _user_states[user_id] = make_user_state()
        _user_states[user_id]["_last_activity"] = time.time()
        return _user_states[user_id]


def remove_state(user_id: str):
    with _user_states_lock:
        _user_states.pop(user_id, None)


def current_user_id() -> str | None:
    return session.get("user_id")


def require_state():
    uid = current_user_id()
    if not uid:
        return None
    return get_state(uid)

# ============ HELPERS ============

def ulog(st: dict, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    st["log"].append(entry)
    if len(st["log"]) > 200:
        st["log"] = st["log"][-200:]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    dlon = (lon2 - lon1) * p
    x = math.sin(dlon) * math.cos(lat2 * p)
    y = (math.cos(lat1 * p) * math.sin(lat2 * p) -
         math.sin(lat1 * p) * math.cos(lat2 * p) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def add_jitter(lat, lon, metres):
    dlat = random.gauss(0, metres) / 111320
    dlon = random.gauss(0, metres) / (111320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def near_home(st: dict) -> bool:
    home_key = st.get("home_location", "home")
    home = st["saved_locations"].get(home_key)
    if not home or st["lat"] is None:
        return False
    return haversine_m(st["lat"], st["lon"], home["lat"], home["lon"]) < st.get("home_radius_m", 200)


def is_night():
    hour = datetime.now().hour
    return hour >= 22 or hour < 7


def update_battery(st: dict):
    charging = is_night() or (near_home(st) and random.random() < 0.7)
    st["charging"] = charging
    if charging:
        st["battery"] = min(1.0, st["battery"] + random.uniform(0.001, 0.004))
    else:
        drain = 0.00005 * (2.0 if st["mode"] == "driving" else 1.0)
        st["battery"] = max(0.05, st["battery"] - random.uniform(0, drain))

# ============ TOKEN REFRESH ============

def refresh_access_token(st: dict) -> bool:
    if not st.get("refresh_token"):
        return False
    try:
        r = requests.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
            headers={"Content-Type": "application/json", "apikey": ANON_KEY},
            json={"refresh_token": st["refresh_token"]},
            timeout=10,
        )
        data = r.json()
        if "access_token" in data:
            st["access_token"] = data["access_token"]
            if "refresh_token" in data:
                st["refresh_token"] = data["refresh_token"]
            ulog(st, "🔑 Token refreshed")
            return True
        else:
            ulog(st, f"⚠️ Token refresh failed: {data.get('error_description', data)}")
            return False
    except Exception as e:
        ulog(st, f"❌ Token refresh error: {e}")
        return False


# ============ LOCATION TOKEN MANAGEMENT ============

def issue_location_token(st: dict) -> bool:
    """
    Call once after login (or after a Supabase token refresh) to get
    a fresh location JWT + location refresh token from the Splashin API.
    Uses the current Supabase access_token as the bearer.
    """
    if not st.get("access_token"):
        return False
    try:
        r = requests.get(
            "https://splashin.app/api/v3/auth/location/issue",
            headers={"Authorization": f"Bearer {st['access_token']}"},
            timeout=10,
        )
        data = r.json()
        # Accept whatever key name the API returns for the two tokens.
        # Common patterns: token/jwt/access_token  and  refresh_token/refreshToken
        jwt = (
            data.get("token")
            or data.get("jwt")
            or data.get("access_token")
            or data.get("locationToken")
        )
        rft = (
            data.get("refresh_token")
            or data.get("refreshToken")
            or data.get("locationRefreshToken")
        )
        if jwt:
            st["location_jwt"] = jwt
            if rft:
                st["location_refresh_token"] = rft
            ulog(st, "🗝️ Location token issued")
            return True
        else:
            ulog(st, f"⚠️ Location token issue failed: {data}")
            return False
    except Exception as e:
        ulog(st, f"❌ Location token issue error: {e}")
        return False


def refresh_location_token(st: dict) -> bool:
    """
    Silently refresh the location JWT using the location refresh token.
    Falls back to re-issuing from the Supabase access token if no
    refresh token is stored yet.
    """
    rft = st.get("location_refresh_token")

    if not rft:
        # No refresh token yet — issue a brand-new one instead
        return issue_location_token(st)

    try:
        r = requests.get(
            "https://splashin.app/api/v3/auth/location/refresh",
            headers={"refresh": rft},
            timeout=10,
        )
        data = r.json()
        jwt = (
            data.get("token")
            or data.get("jwt")
            or data.get("access_token")
            or data.get("locationToken")
        )
        new_rft = (
            data.get("refresh_token")
            or data.get("refreshToken")
            or data.get("locationRefreshToken")
        )
        if jwt:
            st["location_jwt"] = jwt
            if new_rft:
                st["location_refresh_token"] = new_rft
            ulog(st, "🔄 Location token refreshed")
            return True
        else:
            ulog(st, f"⚠️ Location token refresh failed: {data} — re-issuing…")
            return issue_location_token(st)
    except Exception as e:
        ulog(st, f"❌ Location token refresh error: {e} — re-issuing…")
        return issue_location_token(st)


def reregister_device(st: dict):
    if not st.get("access_token") or not st.get("user_id"):
        return
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/get_and_update_profile",
            headers={
                "apikey": ANON_KEY,
                "Authorization": f"Bearer {st['access_token']}",
                "Content-Type": "application/json",
            },
            json={
                "uid": st["user_id"],
                "exp_tkn": st.get("expo_token", ""),
                "apns_tkn": "null", "apns_loc_tkn": "null", "fcm_tkn": "null",
                "app_vsn": "3.7.4.0", "device_vsn": "26.4.1",
                "device_type": "Apple iPhone17,1",
            },
            timeout=10,
        )
        ulog(st, f"📱 Device re-registered: {r.status_code}")
    except Exception as e:
        ulog(st, f"❌ Device register error: {e}")

# ============ ROUTING ============

def simplify_route(route, step=3):
    if not route:
        return route
    return route[::step] + [route[-1]]


def get_route(start_lat, start_lon, end_lat, end_lon, mode="walking"):
    profile = "car" if mode == "driving" else "foot"
    try:
        url = (
            f"http://router.project-osrm.org/route/v1/{profile}/"
            f"{start_lon},{start_lat};{end_lon},{end_lat}"
            f"?overview=simplified&geometries=geojson"
        )
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("code") == "Ok":
            coords = data["routes"][0]["geometry"]["coordinates"]
            route = [(c[1], c[0]) for c in coords]
            return simplify_route(route, step=3)
    except Exception as e:
        pass

    # fallback straight-line
    steps = 30
    route = [
        (
            start_lat + (end_lat - start_lat) * i / steps,
            start_lon + (end_lon - start_lon) * i / steps,
        )
        for i in range(steps + 1)
    ]
    return simplify_route(route, step=4)

# ============ LOCATION POST ============

def post_location(st: dict):
    # Prefer the dedicated location JWT (always kept fresh by the loop).
    # Fall back to the Supabase access token only as a last resort.
    jwt = st.get("location_jwt") or st.get("access_token")
    if not jwt:
        ulog(st, "⚠️ No token available for location post — skipping")
        return

    try:
        update_battery(st)

        j_lat, j_lon = add_jitter(
            st.get("lat", 0.0),
            st.get("lon", 0.0),
            MODES[st["mode"]]["jitter_m"]
        )

        payload = {
            "user_id": st["user_id"],
            "latitude": round(j_lat, 7),
            "longitude": round(j_lon, 7),
            "accuracy": MODES[st["mode"]]["accuracy"] + random.uniform(-0.5, 0.5),
            "speed": round(st.get("speed", 0.0), 2),
            "heading": round(st.get("heading", 0.0), 1),
            "is_moving": st.get("speed", 0.0) > 0.3,
            "activity": MODES[st["mode"]]["activity"],
            "activity_confidence": random.randint(85, 100),
            "battery_level": round(st.get("battery", 0.72), 3),
            "battery_is_charging": st.get("charging", False),
            "location_updated_at": now_iso(),
            "last_updated_at": now_iso(),
            "activity_updated_at": now_iso(),
            "uuid": DEVICE_UUID,
            "heartbeat_at": now_iso(),
            "cu": True,
        }

        r = requests.post(
            LOCATION_ENDPOINT,
            json=payload,
            headers={
                "Authorization": f"Bearer {jwt}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )

        ulog(st,
            f"📡 {st['mode']} → {j_lat:.5f},{j_lon:.5f} "
            f"spd={st.get('speed', 0):.1f} bat={st.get('battery', 0):.0%} → {r.status_code}"
        )

    except Exception as e:
        ulog(st, f"❌ Post error: {e}")

# ============ MOVEMENT LOOP (per-user thread) ============

def should_post(st: dict, interval: int) -> bool:
    return time.time() - st["last_post"] >= interval


def mark_post(st: dict, lat, lon):
    st["last_post"] = time.time()
    st["last_lat"] = lat
    st["last_lon"] = lon


def moved_enough(st: dict, lat, lon) -> bool:
    if st["last_lat"] is None:
        return True
    return haversine_m(lat, lon, st["last_lat"], st["last_lon"]) > 2.0


def movement_loop(user_id: str):
    SMOOTHING = 0.15
    TIME_SCALE = 0.6
    TOKEN_REFRESH_INTERVAL = 3300        # Supabase access token — every 55 min
    LOCATION_TOKEN_REFRESH_INTERVAL = 3000  # Location JWT — every 50 min (slightly ahead)
    DEVICE_REGISTER_INTERVAL = 120
    IDLE_TIMEOUT = 3600  # 1 hour without activity = kill thread

    last_token_refresh = time.time()
    last_location_token_refresh = time.time()
    last_device_register = time.time()

    while True:
        # Check if state still exists (user logged out)
        with _user_states_lock:
            st = _user_states.get(user_id)

        if st is None:
            return  # state was removed; exit thread

        # Idle timeout: stop thread if user hasn't touched the app
        if time.time() - st.get("_last_activity", 0) > IDLE_TIMEOUT:
            st["spoofing"] = False
            ulog(st, "💤 Session idle — spoofing paused")
            # Don't kill the thread; just sleep and wait for activity
            time.sleep(30)
            continue

        # Periodic Supabase token refresh — then immediately re-issue location token
        if time.time() - last_token_refresh >= TOKEN_REFRESH_INTERVAL:
            if refresh_access_token(st):
                # New Supabase token → get a fresh location JWT straight away
                issue_location_token(st)
                last_location_token_refresh = time.time()
            last_token_refresh = time.time()

        # Periodic location token refresh (independent of Supabase refresh)
        if time.time() - last_location_token_refresh >= LOCATION_TOKEN_REFRESH_INTERVAL:
            refresh_location_token(st)
            last_location_token_refresh = time.time()

        # Periodic device re-register
        if time.time() - last_device_register >= DEVICE_REGISTER_INTERVAL:
            reregister_device(st)
            last_device_register = time.time()

        if not st.get("spoofing") or st.get("lat") is None:
            time.sleep(1)
            continue

        mode = MODES[st["mode"]]
        route = st.get("route", [])

        if not route or st.get("route_index", 0) >= len(route) - 1:
            st["mode"] = "still"
            st["speed"] = 0.0
            st["velocity"] = 0.0

            if should_post(st, 8):
                post_location(st)
                mark_post(st, st["lat"], st["lon"])

            time.sleep(1)
            continue

        # Speed model
        target = mode["speed_ms"] + random.uniform(-mode["variance"], mode["variance"])
        target = max(0.0, target)

        st["velocity"] = (
            st["velocity"] * (1 - SMOOTHING) +
            target * SMOOTHING
        )

        speed = st["velocity"]
        st["speed"] = speed

        dist_to_cover = speed * TIME_SCALE

        cur_lat, cur_lon = st["lat"], st["lon"]
        moved = False

        while dist_to_cover > 0.5 and st["route_index"] < len(route) - 1:
            nxt = route[st["route_index"] + 1]
            seg = haversine_m(cur_lat, cur_lon, nxt[0], nxt[1])

            if seg <= dist_to_cover:
                dist_to_cover -= seg
                st["route_index"] += 1
                cur_lat, cur_lon = nxt
                moved = True
            else:
                frac = dist_to_cover / seg
                hdg = bearing(cur_lat, cur_lon, nxt[0], nxt[1])
                st["heading"] += (hdg - st["heading"]) * 0.2
                cur_lat += (nxt[0] - cur_lat) * frac
                cur_lon += (nxt[1] - cur_lon) * frac
                moved = True
                dist_to_cover = 0

        # Lane drift
        drift = 0.000002
        cur_lat += random.uniform(-drift, drift)
        cur_lon += random.uniform(-drift, drift)

        st["lat"] = cur_lat
        st["lon"] = cur_lon

        if moved and moved_enough(st, cur_lat, cur_lon):
            if should_post(st, 2 if st["mode"] == "driving" else 3):
                post_location(st)
                mark_post(st, cur_lat, cur_lon)

        time.sleep(1)


def ensure_user_thread(user_id: str):
    """Start a movement thread for this user if one isn't running."""
    st = get_state(user_id)
    if not st.get("_thread_started"):
        t = threading.Thread(target=movement_loop, args=(user_id,), daemon=True)
        t.start()
        st["_thread_started"] = True

# ============ SEED LOCATION ============

def fetch_real_location(st: dict) -> bool:
    try:
        headers = {
            "apikey": ANON_KEY,
            "Authorization": f"Bearer {st['access_token']}",
        }
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/user_location"
            f"?user_id=eq.{st['user_id']}&select=latitude,longitude,battery_level",
            headers=headers,
            timeout=10,
        )
        data = r.json()
        if isinstance(data, list) and data:
            row = data[0]
            st["lat"] = float(row.get("latitude", 0.0))
            st["lon"] = float(row.get("longitude", 0.0))
            st["battery"] = float(row.get("battery_level", 0.72))
            ulog(st, f"📍 Seeded from Supabase: {st['lat']:.5f}, {st['lon']:.5f}")
            return True
    except Exception as e:
        ulog(st, f"⚠️ Supabase seed failed: {e}")

    try:
        r = requests.get("https://ipapi.co/json/", timeout=5)
        data = r.json()
        st["lat"] = float(data.get("latitude", 0.0))
        st["lon"] = float(data.get("longitude", 0.0))
        ulog(st, f"📍 Seeded from IP: {st['lat']:.5f}, {st['lon']:.5f}")
        return True
    except Exception as e:
        ulog(st, f"❌ IP seed failed: {e}")

    return False

# ============ CLEANUP THREAD ============

def cleanup_loop():
    """Periodically remove stale user states (no activity for 2 hours)."""
    while True:
        time.sleep(600)
        cutoff = time.time() - 7200
        with _user_states_lock:
            stale = [uid for uid, st in _user_states.items() if st.get("_last_activity", 0) < cutoff]
            for uid in stale:
                del _user_states[uid]

threading.Thread(target=cleanup_loop, daemon=True).start()

# ============ FLASK ROUTES ============

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    uid = current_user_id()
    if not uid:
        return jsonify({"logged_in": False})

    st = get_state(uid)
    return jsonify({
        "logged_in": True,
        "spoofing": st["spoofing"],
        "lat": st["lat"],
        "lon": st["lon"],
        "mode": st["mode"],
        "speed": round(st["speed"], 2),
        "heading": round(st["heading"], 1),
        "battery": round(st["battery"], 3),
        "charging": st["charging"],
        "route_progress": st["route_index"],
        "route_total": len(st["route"]),
        "status": st["status"],
        "log": st["log"][-50:],
        "near_home": near_home(st),
        "saved_locations": st["saved_locations"],
    })


@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.json or {}
    email = body.get("email", "")
    password = body.get("password", "")

    try:
        r = requests.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"Content-Type": "application/json", "apikey": ANON_KEY},
            json={"email": email, "password": password},
            timeout=10,
        )
        data = r.json()
        if "access_token" in data:
            user_id = data["user"]["id"]
            session["user_id"] = user_id

            st = get_state(user_id)
            st["access_token"] = data["access_token"]
            st["refresh_token"] = data["refresh_token"]
            st["user_id"] = user_id

            # Pull expo_token from user metadata if present
            user_meta = data.get("user", {}).get("user_metadata", {})
            if user_meta.get("expo_token"):
                st["expo_token"] = user_meta["expo_token"]

            fetch_real_location(st)
            issue_location_token(st)   # get location JWT before first post
            ensure_user_thread(user_id)
            reregister_device(st)
            ulog(st, f"✅ Logged in as {email}")
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": data.get("error_description", "Login failed")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    uid = current_user_id()
    if uid:
        st = get_state(uid)
        st["spoofing"] = False
        remove_state(uid)
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    st = require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    st["spoofing"] = not st["spoofing"]
    ulog(st, f"{'▶️ Spoofing ON' if st['spoofing'] else '⏸️ Spoofing OFF'}")
    return jsonify({"spoofing": st["spoofing"]})


@app.route("/api/goto", methods=["POST"])
def api_goto():
    st = require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    body = request.json or {}
    dest_lat = float(body["lat"])
    dest_lon = float(body["lon"])
    mode = body.get("mode", "walking")
    if st["lat"] is None:
        return jsonify({"ok": False, "error": "No current position"})
    ulog(st, f"🗺️ Routing {mode} to {dest_lat:.5f},{dest_lon:.5f}...")
    route = get_route(st["lat"], st["lon"], dest_lat, dest_lon, mode)
    st["route"] = route
    st["route_index"] = 0
    st["mode"] = mode
    ulog(st, f"✅ Route loaded: {len(route)} waypoints")
    return jsonify({"ok": True, "waypoints": len(route), "route": route})


@app.route("/api/still", methods=["POST"])
def api_still():
    st = require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    st["mode"] = "still"
    st["route"] = []
    st["speed"] = 0
    ulog(st, "⏸️ Stopped")
    return jsonify({"ok": True})


@app.route("/api/teleport", methods=["POST"])
def api_teleport():
    st = require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    body = request.json or {}
    st["lat"] = float(body["lat"])
    st["lon"] = float(body["lon"])
    st["route"] = []
    st["mode"] = "still"
    ulog(st, f"✈️ Teleported to {st['lat']:.5f},{st['lon']:.5f}")
    return jsonify({"ok": True})


@app.route("/api/save_location", methods=["POST"])
def api_save_location():
    st = require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    body = request.json or {}
    name = body["name"].lower().strip()
    lat = float(body["lat"])
    lon = float(body["lon"])
    st["saved_locations"][name] = {"lat": lat, "lon": lon, "name": body["name"]}
    ulog(st, f"💾 Saved '{name}' at {lat:.5f},{lon:.5f}")
    return jsonify({"ok": True})


@app.route("/api/delete_location", methods=["POST"])
def api_delete_location():
    st = require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    name = (request.json or {}).get("name", "").lower()
    if name in st["saved_locations"]:
        del st["saved_locations"][name]
    return jsonify({"ok": True})


@app.route("/api/config", methods=["GET"])
def api_get_config():
    st = require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    return jsonify({
        "home_location": st.get("home_location", "home"),
        "home_radius_m": st.get("home_radius_m", 200),
        "expo_token": st.get("expo_token", ""),
        "location_jwt": st.get("location_jwt", ""),
    })


@app.route("/api/config", methods=["POST"])
def api_set_config():
    st = require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    body = request.json or {}
    for k in ("home_location", "home_radius_m", "expo_token", "location_jwt"):
        if k in body:
            st[k] = body[k]
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("🟢 Splashin Switch starting on http://localhost:5000")
    os.makedirs("/tmp/flask_sessions", exist_ok=True)
    app.run(debug=False, port=5000)
