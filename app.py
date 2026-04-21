import math
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template, request, session

app = Flask(__name__)

# ============ CONFIG ============

app.secret_key = os.environ.get("SECRET_KEY") or (_ for _ in ()).throw(
    RuntimeError("SECRET_KEY env var must be set")
)

app.config.update(
    SESSION_COOKIE_NAME="ss_sid",
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") != "development",
    PERMANENT_SESSION_LIFETIME=86400 * 7,
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://erspvsdfwaqjtuhymubj.supabase.co")
ANON_KEY     = os.environ.get("SUPABASE_ANON_KEY", "")
LOC_ENDPOINT = os.environ.get("LOCATION_ENDPOINT", "https://location.splashin.app/api/v4/on-location")
DEVICE_UUID  = os.environ.get("DEVICE_UUID", "7D53AF20-66A8-4F42-B422-0C7ED8939911")
ORS_API_KEY  = os.environ.get("ORS_API_KEY", "")

# Shared HTTP session — connection pooling / keep-alive
_http = requests.Session()
_http.headers.update({"Content-Type": "application/json"})

# Thread pool for non-blocking route calculations (never blocks Flask workers)
_route_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="route")

# ============ PER-USER STATE ============

_user_states: dict[str, dict] = {}
_states_lock = threading.Lock()

MODES = {
    "walking": {"speed_ms": 1.4,  "variance": 0.4, "activity": "walking",    "accuracy": 8.0,  "jitter_m": 2.5, "post_interval": 5},
    "driving": {"speed_ms": 11.0, "variance": 4.0, "activity": "in_vehicle", "accuracy": 5.0,  "jitter_m": 1.0, "post_interval": 2},
    "still":   {"speed_ms": 0.0,  "variance": 0.0, "activity": "still",      "accuracy": 10.0, "jitter_m": 1.5, "post_interval": 30},
}


def _make_state() -> dict:
    return {
        "spoofing": False,
        "lat": None, "lon": None,
        "mode": "still",
        "speed": 0.0, "velocity": 0.0, "heading": 0.0,
        "battery": 0.72, "charging": False,
        "route": [], "route_index": 0,
        "log": [],
        "last_post": 0.0, "last_lat": None, "last_lon": None,
        # auth
        "access_token": "", "refresh_token": "",
        "user_id": "", "expo_token": "",
        "location_jwt": "", "location_refresh_token": "",
        # settings
        "saved_locations": {"home": {"lat": 0.0, "lon": 0.0, "name": "Home"}},
        "home_location": "home",
        "home_radius_m": 200,
        # routing status — polled by frontend to show live progress
        "route_status":     "idle",   # idle | calculating | ready | error
        "route_status_msg": "",
        "route_pending":    None,     # holds route until frontend acks it
        # internal
        "_thread": None,
        "_last_activity": time.monotonic(),
        "_stop": False,
    }


def _get_state(user_id: str) -> dict:
    with _states_lock:
        if user_id not in _user_states:
            _user_states[user_id] = _make_state()
        st = _user_states[user_id]
        st["_last_activity"] = time.monotonic()
        return st


def _remove_state(user_id: str):
    with _states_lock:
        st = _user_states.pop(user_id, None)
    if st:
        st["_stop"] = True


def _current_uid() -> str | None:
    return session.get("uid")


def _require_state():
    uid = _current_uid()
    return _get_state(uid) if uid else None

# ============ HELPERS ============

def _log(st: dict, msg: str):
    ts  = datetime.now().strftime("%H:%M:%S")
    log = st["log"]
    log.append(f"[{ts}] {msg}")
    if len(log) > 150:
        del log[:50]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000.0
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _bearing(lat1, lon1, lat2, lon2) -> float:
    p    = math.pi / 180
    dlon = (lon2 - lon1) * p
    x    = math.sin(dlon) * math.cos(lat2 * p)
    y    = math.cos(lat1 * p) * math.sin(lat2 * p) - math.sin(lat1 * p) * math.cos(lat2 * p) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _jitter(lat, lon, metres):
    dlat = random.gauss(0, metres) / 111_320
    dlon = random.gauss(0, metres) / (111_320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def _near_home(st: dict) -> bool:
    home = st["saved_locations"].get(st.get("home_location", "home"))
    if not home or st["lat"] is None:
        return False
    return _haversine(st["lat"], st["lon"], home["lat"], home["lon"]) < st.get("home_radius_m", 200)


def _update_battery(st: dict):
    charging = (datetime.now().hour >= 22 or datetime.now().hour < 7) or (
        _near_home(st) and random.random() < 0.7
    )
    st["charging"] = charging
    if charging:
        st["battery"] = min(1.0, st["battery"] + random.uniform(0.001, 0.004))
    else:
        drain = 0.00005 * (2.0 if st["mode"] == "driving" else 1.0)
        st["battery"] = max(0.05, st["battery"] - random.uniform(0, drain))

# ============ TOKEN HELPERS ============

def _refresh_supabase_token(st: dict) -> bool:
    if not st.get("refresh_token"):
        return False
    try:
        r = _http.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": ANON_KEY},
            json={"refresh_token": st["refresh_token"]},
            timeout=10,
        )
        data = r.json()
        if "access_token" in data:
            st["access_token"]  = data["access_token"]
            st["refresh_token"] = data.get("refresh_token", st["refresh_token"])
            _log(st, "🔑 Supabase token refreshed")
            return True
        _log(st, f"⚠️ Token refresh failed: {data.get('error_description', data)}")
    except Exception as e:
        _log(st, f"❌ Token refresh error: {e}")
    return False


def _issue_location_token(st: dict) -> bool:
    if not st.get("access_token"):
        return False
    try:
        r = _http.get(
            "https://splashin.app/api/v3/auth/location/issue",
            headers={"Authorization": f"Bearer {st['access_token']}"},
            timeout=10,
        )
        data = r.json()
        jwt = data.get("token") or data.get("jwt") or data.get("access_token") or data.get("locationToken")
        rft = data.get("refresh_token") or data.get("refreshToken") or data.get("locationRefreshToken")
        if jwt:
            st["location_jwt"] = jwt
            if rft:
                st["location_refresh_token"] = rft
            _log(st, "🗝️ Location token issued")
            return True
        _log(st, f"⚠️ Location token issue failed: {data}")
    except Exception as e:
        _log(st, f"❌ Location token issue error: {e}")
    return False


def _refresh_location_token(st: dict) -> bool:
    rft = st.get("location_refresh_token")
    if not rft:
        return _issue_location_token(st)
    try:
        r = _http.get(
            "https://splashin.app/api/v3/auth/location/refresh",
            headers={"refresh": rft},
            timeout=10,
        )
        data = r.json()
        jwt     = data.get("token") or data.get("jwt") or data.get("access_token") or data.get("locationToken")
        new_rft = data.get("refresh_token") or data.get("refreshToken") or data.get("locationRefreshToken")
        if jwt:
            st["location_jwt"] = jwt
            if new_rft:
                st["location_refresh_token"] = new_rft
            _log(st, "🔄 Location token refreshed")
            return True
        _log(st, "⚠️ Location token refresh failed — re-issuing")
    except Exception as e:
        _log(st, f"❌ Location token refresh error: {e} — re-issuing")
    return _issue_location_token(st)


def _reregister_device(st: dict):
    if not st.get("access_token") or not st.get("user_id"):
        return
    try:
        r = _http.post(
            f"{SUPABASE_URL}/rest/v1/rpc/get_and_update_profile",
            headers={"apikey": ANON_KEY, "Authorization": f"Bearer {st['access_token']}"},
            json={
                "uid": st["user_id"],
                "exp_tkn": st.get("expo_token", ""),
                "apns_tkn": "null", "apns_loc_tkn": "null", "fcm_tkn": "null",
                "app_vsn": "3.7.4.0", "device_vsn": "26.4.1",
                "device_type": "Apple iPhone17,1",
            },
            timeout=10,
        )
        _log(st, f"📱 Device re-registered: {r.status_code}")
    except Exception as e:
        _log(st, f"❌ Device register error: {e}")

# ============ ROUTING ============

ORS_PROFILES = {"walking": "foot-walking", "driving": "driving-car"}


def _straight_line(slat, slon, elat, elon, steps=20):
    return [
        (slat + (elat - slat) * i / steps, slon + (elon - slon) * i / steps)
        for i in range(steps + 1)
    ]


def _simplify(route, step=3):
    if not route:
        return route
    effective = max(1, min(step, len(route) // 10)) if len(route) > 10 else 1
    simplified = route[::effective]
    if simplified[-1] != route[-1]:
        simplified.append(route[-1])
    return simplified


def _get_route_sync(slat, slon, elat, elon, mode) -> tuple[list, str]:
    """Blocking route fetch — always called from thread pool, never from Flask."""

    # 1. OpenRouteService
    if ORS_API_KEY:
        try:
            r = _http.post(
                f"https://api.openrouteservice.org/v2/directions/{ORS_PROFILES.get(mode,'foot-walking')}/geojson",
                headers={"Authorization": ORS_API_KEY},
                json={"coordinates": [[slon, slat], [elon, elat]],
                      "instructions": False, "geometry_simplify": False},
                timeout=10,
            )
            features = (r.json().get("features") or [])
            if features:
                coords = features[0]["geometry"]["coordinates"]
                if coords:
                    return _simplify([(c[1], c[0]) for c in coords], step=2), "ORS"
        except Exception as e:
            print(f"❌ ORS: {e}")

    # 2. OSRM (two mirrors)
    op = "car" if mode == "driving" else "foot"
    for base in [
        f"http://router.project-osrm.org/route/v1/{op}",
        f"https://routing.openstreetmap.de/routed-{op}/route/v1/{op}",
    ]:
        try:
            r = _http.get(
                f"{base}/{slon},{slat};{elon},{elat}?overview=full&geometries=geojson&steps=false",
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("code") == "Ok":
                    coords = data["routes"][0]["geometry"]["coordinates"]
                    if coords:
                        return _simplify([(c[1], c[0]) for c in coords], step=3), "OSRM"
        except requests.exceptions.Timeout:
            print(f"⏱️ OSRM timeout: {base}")
        except Exception as e:
            print(f"❌ OSRM {base}: {e}")

    # 3. Straight-line last resort
    dist  = _haversine(slat, slon, elat, elon)
    steps = max(10, min(60, int(dist / 50)))
    return _straight_line(slat, slon, elat, elon, steps), "straight-line"


def _calc_route_bg(user_id: str, slat, slon, elat, elon, mode: str):
    """Runs in thread pool — writes result straight into user state."""
    with _states_lock:
        st = _user_states.get(user_id)
    if not st:
        return
    try:
        route, source = _get_route_sync(slat, slon, elat, elon, mode)
        st["route"]           = route
        st["route_index"]     = 0
        st["mode"]            = mode
        st["route_pending"]   = route          # frontend draws this on next poll
        st["route_status"]     = "ready"
        st["route_status_msg"] = f"{source} · {len(route)} pts"
        _log(st, f"✅ Route ready ({source}): {len(route)} waypoints")
    except Exception as e:
        st["route_status"]     = "error"
        st["route_status_msg"] = str(e)
        _log(st, f"❌ Route error: {e}")

# ============ LOCATION POST ============

def _post_location(st: dict):
    jwt = st.get("location_jwt") or st.get("access_token")
    if not jwt:
        _log(st, "⚠️ No token — skipping post")
        return
    try:
        _update_battery(st)
        j_lat, j_lon = _jitter(st["lat"], st["lon"], MODES[st["mode"]]["jitter_m"])
        now = _now_iso()
        payload = {
            "user_id":             st["user_id"],
            "latitude":            round(j_lat, 7),
            "longitude":           round(j_lon, 7),
            "accuracy":            MODES[st["mode"]]["accuracy"] + random.uniform(-0.5, 0.5),
            "speed":               round(st["speed"], 2),
            "heading":             round(st["heading"], 1),
            "is_moving":           st["speed"] > 0.3,
            "activity":            MODES[st["mode"]]["activity"],
            "activity_confidence": random.randint(85, 100),
            "battery_level":       round(st["battery"], 3),
            "battery_is_charging": st["charging"],
            "location_updated_at": now,
            "last_updated_at":     now,
            "activity_updated_at": now,
            "uuid":                DEVICE_UUID,
            "heartbeat_at":        now,
            "cu":                  True,
        }
        r = _http.post(LOC_ENDPOINT, json=payload,
                       headers={"Authorization": f"Bearer {jwt}"}, timeout=10)
        _log(st, f"📡 {st['mode']} → {j_lat:.5f},{j_lon:.5f} "
                 f"spd={st['speed']:.1f} bat={st['battery']:.0%} → {r.status_code}")
    except Exception as e:
        _log(st, f"❌ Post error: {e}")

# ============ MOVEMENT LOOP ============

_TOKEN_REFRESH_S     = 3300
_LOC_TOKEN_REFRESH_S = 3000
_DEVICE_REGISTER_S   = 120
_IDLE_PAUSE_S        = 1800
_SMOOTHING           = 0.15
_TIME_SCALE          = 0.6


def _movement_loop(user_id: str, st: dict):
    last_token_refresh     = time.monotonic()
    last_loc_token_refresh = time.monotonic()
    last_device_register   = time.monotonic()

    while not st["_stop"]:
        now = time.monotonic()

        if now - last_token_refresh >= _TOKEN_REFRESH_S:
            if _refresh_supabase_token(st):
                _issue_location_token(st)
                last_loc_token_refresh = now
            last_token_refresh = now

        if now - last_loc_token_refresh >= _LOC_TOKEN_REFRESH_S:
            _refresh_location_token(st)
            last_loc_token_refresh = now

        if now - last_device_register >= _DEVICE_REGISTER_S:
            _reregister_device(st)
            last_device_register = now

        if time.monotonic() - st["_last_activity"] > _IDLE_PAUSE_S:
            if st["spoofing"]:
                st["spoofing"] = False
                _log(st, "💤 Idle — spoofing paused")
            time.sleep(15)
            continue

        if not st["spoofing"] or st["lat"] is None:
            time.sleep(1)
            continue

        mode  = MODES[st["mode"]]
        route = st["route"]

        if not route or st["route_index"] >= len(route) - 1:
            if st["mode"] != "still":
                st["mode"] = "still"; st["speed"] = 0.0; st["velocity"] = 0.0
            if time.time() - st["last_post"] >= 8:
                _post_location(st)
                st["last_post"] = time.time()
                st["last_lat"]  = st["lat"]
                st["last_lon"]  = st["lon"]
            time.sleep(1)
            continue

        target     = max(0.0, mode["speed_ms"] + random.uniform(-mode["variance"], mode["variance"]))
        st["velocity"] = st["velocity"] * (1 - _SMOOTHING) + target * _SMOOTHING
        st["speed"]    = st["velocity"]

        dist_left      = st["velocity"] * _TIME_SCALE
        cur_lat, cur_lon = st["lat"], st["lon"]
        moved          = False

        while dist_left > 0.5 and st["route_index"] < len(route) - 1:
            nxt = route[st["route_index"] + 1]
            seg = _haversine(cur_lat, cur_lon, nxt[0], nxt[1])
            if seg <= dist_left:
                dist_left -= seg; st["route_index"] += 1
                cur_lat, cur_lon = nxt; moved = True
            else:
                frac = dist_left / seg
                hdg  = _bearing(cur_lat, cur_lon, nxt[0], nxt[1])
                st["heading"] += (hdg - st["heading"]) * 0.2
                cur_lat += (nxt[0] - cur_lat) * frac
                cur_lon += (nxt[1] - cur_lon) * frac
                moved = True; dist_left = 0

        cur_lat += random.uniform(-0.000002, 0.000002)
        cur_lon += random.uniform(-0.000002, 0.000002)
        st["lat"], st["lon"] = cur_lat, cur_lon

        if moved:
            ll = st["last_lat"]
            if (ll is None or _haversine(cur_lat, cur_lon, ll, st["last_lon"]) > 2.0) \
                    and time.time() - st["last_post"] >= mode["post_interval"]:
                _post_location(st)
                st["last_post"] = time.time()
                st["last_lat"]  = cur_lat
                st["last_lon"]  = cur_lon

        time.sleep(1)


def _ensure_thread(user_id: str):
    st = _get_state(user_id)
    t  = st.get("_thread")
    if t is None or not t.is_alive():
        st["_stop"] = False
        t = threading.Thread(target=_movement_loop, args=(user_id, st),
                             daemon=True, name=f"mv-{user_id[:8]}")
        t.start()
        st["_thread"] = t

# ============ SEED LOCATION ============

def _seed_location(st: dict) -> bool:
    try:
        r = _http.get(
            f"{SUPABASE_URL}/rest/v1/user_location"
            f"?user_id=eq.{st['user_id']}&select=latitude,longitude,battery_level",
            headers={"apikey": ANON_KEY, "Authorization": f"Bearer {st['access_token']}"},
            timeout=8,
        )
        rows = r.json()
        if isinstance(rows, list) and rows:
            row = rows[0]
            st["lat"]     = float(row.get("latitude", 0.0))
            st["lon"]     = float(row.get("longitude", 0.0))
            st["battery"] = float(row.get("battery_level", 0.72))
            _log(st, f"📍 Seeded from Supabase: {st['lat']:.5f}, {st['lon']:.5f}")
            return True
    except Exception as e:
        _log(st, f"⚠️ Supabase seed failed: {e}")
    try:
        r    = _http.get("https://ipapi.co/json/", timeout=5)
        data = r.json()
        st["lat"] = float(data.get("latitude", 0.0))
        st["lon"] = float(data.get("longitude", 0.0))
        _log(st, f"📍 Seeded from IP: {st['lat']:.5f}, {st['lon']:.5f}")
        return True
    except Exception as e:
        _log(st, f"❌ IP seed failed: {e}")
    return False

# ============ CLEANUP ============

def _cleanup_loop():
    while True:
        time.sleep(300)
        cutoff = time.monotonic() - 7200
        with _states_lock:
            stale = [uid for uid, s in _user_states.items()
                     if s.get("_last_activity", 0) < cutoff]
        for uid in stale:
            _remove_state(uid)

threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup").start()

# ============ FLASK ROUTES ============

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    uid = _current_uid()
    if not uid:
        return jsonify({"logged_in": False})
    st    = _get_state(uid)
    route = st.get("route") or []

    # Send pending route once, then clear it
    pending = st.pop("route_pending", None)

    return jsonify({
        "logged_in":        True,
        "spoofing":         st["spoofing"],
        "lat":              st["lat"],
        "lon":              st["lon"],
        "mode":             st["mode"],
        "speed":            round(st["speed"], 2),
        "heading":          round(st["heading"], 1),
        "battery":          round(st["battery"], 3),
        "charging":         st["charging"],
        "route_total":      len(route),
        "route_progress":   min(st["route_index"], len(route)),
        "log":              st["log"][-50:],
        "near_home":        _near_home(st),
        "saved_locations":  st["saved_locations"],
        "route_status":     st.get("route_status", "idle"),
        "route_status_msg": st.get("route_status_msg", ""),
        "route":            pending,   # only non-null when a fresh route just arrived
    })


@app.route("/api/login", methods=["POST"])
def api_login():
    body     = request.get_json(silent=True) or {}
    email    = body.get("email", "")
    password = body.get("password", "")
    try:
        r = _http.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": ANON_KEY},
            json={"email": email, "password": password},
            timeout=10,
        )
        data = r.json()
        if "access_token" not in data:
            return jsonify({"ok": False, "error": data.get("error_description", "Login failed")})

        user_id = data["user"]["id"]
        session.clear()
        session["uid"]     = user_id
        session.permanent  = True

        st = _get_state(user_id)
        st["access_token"]  = data["access_token"]
        st["refresh_token"] = data["refresh_token"]
        st["user_id"]       = user_id
        meta = data.get("user", {}).get("user_metadata", {})
        if meta.get("expo_token"):
            st["expo_token"] = meta["expo_token"]

        _seed_location(st)
        _issue_location_token(st)
        _reregister_device(st)
        _ensure_thread(user_id)
        _log(st, f"✅ Logged in as {email}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    uid = _current_uid()
    if uid:
        _remove_state(uid)
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    st = _require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    st["spoofing"] = not st["spoofing"]
    _log(st, "▶️ Spoofing ON" if st["spoofing"] else "⏸️ Spoofing OFF")
    return jsonify({"spoofing": st["spoofing"]})


@app.route("/api/goto", methods=["POST"])
def api_goto():
    """
    Returns immediately with {ok, status:"calculating"}.
    Route calculated in background thread pool.
    Frontend polls /api/state for route_status == "ready" | "error".
    """
    st = _require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    body = request.get_json(silent=True) or {}
    try:
        dest_lat = float(body["lat"])
        dest_lon = float(body["lon"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "lat/lon required"}), 400
    mode = body.get("mode", "walking")
    if mode not in MODES:
        return jsonify({"ok": False, "error": f"unknown mode: {mode}"}), 400
    if st["lat"] is None:
        return jsonify({"ok": False, "error": "No current position"})

    uid = _current_uid()
    st["route_status"]     = "calculating"
    st["route_status_msg"] = f"Calculating {mode} route…"
    st["route"]            = []
    st["route_pending"]    = None

    _route_executor.submit(_calc_route_bg, uid, st["lat"], st["lon"], dest_lat, dest_lon, mode)
    return jsonify({"ok": True, "status": "calculating"})


@app.route("/api/still", methods=["POST"])
def api_still():
    st = _require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    st["mode"]             = "still"
    st["route"]            = []
    st["speed"]            = 0.0
    st["route_status"]     = "idle"
    st["route_status_msg"] = ""
    _log(st, "⏸️ Stopped")
    return jsonify({"ok": True})


@app.route("/api/teleport", methods=["POST"])
def api_teleport():
    st = _require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    body = request.get_json(silent=True) or {}
    try:
        st["lat"] = float(body["lat"])
        st["lon"] = float(body["lon"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "lat/lon required"}), 400
    st["route"]            = []
    st["mode"]             = "still"
    st["route_status"]     = "idle"
    st["route_status_msg"] = ""
    _log(st, f"✈️ Teleported → {st['lat']:.5f},{st['lon']:.5f}")
    return jsonify({"ok": True})


@app.route("/api/save_location", methods=["POST"])
def api_save_location():
    st = _require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    body = request.get_json(silent=True) or {}
    try:
        name = str(body["name"]).lower().strip()
        lat  = float(body["lat"])
        lon  = float(body["lon"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "name/lat/lon required"}), 400
    st["saved_locations"][name] = {"lat": lat, "lon": lon, "name": body["name"]}
    _log(st, f"💾 Saved '{name}' at {lat:.5f},{lon:.5f}")
    return jsonify({"ok": True})


@app.route("/api/delete_location", methods=["POST"])
def api_delete_location():
    st = _require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    name = ((request.get_json(silent=True) or {}).get("name") or "").lower().strip()
    st["saved_locations"].pop(name, None)
    return jsonify({"ok": True})


@app.route("/api/config", methods=["GET"])
def api_get_config():
    st = _require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    return jsonify({
        "home_location": st.get("home_location", "home"),
        "home_radius_m": st.get("home_radius_m", 200),
        "expo_token":    st.get("expo_token", ""),
        "location_jwt":  st.get("location_jwt", ""),
    })


@app.route("/api/config", methods=["POST"])
def api_set_config():
    st = _require_state()
    if not st:
        return jsonify({"error": "not logged in"}), 401
    body = request.get_json(silent=True) or {}
    for k in ("home_location", "home_radius_m", "expo_token", "location_jwt"):
        if k in body:
            st[k] = body[k]
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("🟢 Splashin Switch → http://localhost:5000")
    app.run(debug=False, port=5000)
