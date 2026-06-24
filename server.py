import subprocess, os, glob, time, threading, io, json, math, shutil
from flask import Flask, send_file, jsonify, send_from_directory, request, session

try:
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import qrcode
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False

app = Flask(__name__, static_folder='.')
app.secret_key = os.urandom(24)  # used to sign the session cookie for PIN auth
app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 12  # 12 hours — covers a full event day

CONTROL_PANEL_PIN = '4729'  # change this to whatever PIN you want before the wedding

@app.errorhandler(Exception)
def handle_unexpected_error(e):
    log_error(f'Unhandled server error: {e}')
    return jsonify({'error': 'Internal server error'}), 500
BASE_PHOTO_DIR = os.path.expanduser('~/wedding-booth/photos')
os.makedirs(BASE_PHOTO_DIR, exist_ok=True)

def get_photo_dir():
    """Returns today's dated subfolder, creating it if needed."""
    dated = os.path.join(BASE_PHOTO_DIR, time.strftime('%Y-%m-%d'))
    os.makedirs(dated, exist_ok=True)
    return dated

def get_local_ip():
    """Best-effort detection of this machine's LAN IP address, so QR codes
    point somewhere a guest's phone can actually reach (not localhost)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

LOCAL_IP = get_local_ip()
SERVER_PORT = 5000

# ── Shared state ──
preview_lock = threading.Lock()
camera_lock = threading.Lock()   # held by ANY gphoto2 call — guarantees only one
                                  # process ever talks to the USB device at a time
latest_preview = None
capturing_photo = False
last_preview_success = 0
CAMERA_OFFLINE_THRESHOLD = 4.0  # seconds

shoot_pending = False   # control panel asked for a photo
shoot_acked = False     # booth screen has started the countdown/flash
idle_trigger_requested = False  # control panel manually requested the idle screen
last_photo = None  # {'date': ..., 'file': ...} for the most recent successful single-shot capture

# ── Single active booth viewer lock ──
# Only one browser tab/device is allowed to be "the" booth screen at a time.
# The first one to load gets a token; any other attempt is shown a locked
# screen requiring the control panel PIN to take over.
active_booth_token = None
active_booth_claimed_at = 0

# Burst mode (4-shot sequence) state
burst_pending = False        # control panel requested a burst
burst_total = 4
burst_current_index = 0      # 1-4 while in progress, 0 when idle
burst_acked_for_index = 0    # booth has fired the shutter for this shot index
burst_complete = False       # all 4 shots finished, ready for booth to show confirmation
burst_photos = []            # list of {date, file} for the completed burst

session_photo_count = 0    # resets to 0 each new day
session_date = time.strftime('%Y-%m-%d')

DEFAULT_SETTINGS = {
    'filter': 'none',
    'countdown': 3,
    'enabled': True,
    'message': '',
    'font': None,
    'liveview_enabled': True,
    'vignette_strength': 50,
    'orientation': 'portrait',
    'grid_title': "Haley & Gary's Wedding",
    'grid_font': None,
    'booth_title': 'Haley & Gary'
}

SETTINGS_FILE = os.path.expanduser('~/wedding-booth/settings.json')

def load_settings():
    """Loads saved settings from disk if present, falling back to defaults
    for any keys that are missing (e.g. after an app update adds new ones)."""
    merged = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                saved = json.load(f)
            merged.update(saved)
    except Exception as e:
        print(f'Could not load saved settings, using defaults: {e}')
    return merged

def save_settings():
    """Persists the current settings dict to disk so they survive a
    server restart."""
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print(f'Could not save settings: {e}')

settings = load_settings()

def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr

# ── SD card capacity check ──
def _read_property_value(property_path):
    try:
        code, out, err = run(['gphoto2', '--get-config', property_path])
        if code != 0:
            return None
        for line in out.splitlines():
            if line.strip().startswith('Current:'):
                return int(line.split(':', 1)[1].strip())
    except Exception as e:
        log_error(f'Could not read {property_path}: {e}')
    return None

def get_remaining_shots():
    """Returns the number of remaining shots on SLOT1 (used by the capture
    pre-check). Kept for backward compatibility with do_capture/do_burst_capture."""
    return _read_property_value('/main/other/d249')

def get_remaining_shots_all_slots():
    """Returns a dict with remaining shots for both card slots, e.g.
    {'slot1': 6, 'slot2': None} — None means that slot isn't readable
    (commonly because no card is inserted in that slot)."""
    return {
        'slot1': _read_property_value('/main/other/d249'),
        'slot2': _read_property_value('/main/other/d257'),
    }

# ── Disk space check (the Mac running the booth, not the camera's SD card) ──
DISK_LOW_THRESHOLD_GB = 2.0

def get_disk_space_gb():
    """Returns free space in GB on the drive holding the photos folder."""
    try:
        usage = shutil.disk_usage(BASE_PHOTO_DIR)
        return round(usage.free / (1024 ** 3), 2)
    except Exception as e:
        log_error(f'Could not read disk space: {e}')
        return None

# ── Error logging ──
LOG_FILE = os.path.expanduser('~/wedding-booth/errors.log')

def log_error(message):
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{timestamp}] {message}\n'
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line)
    except Exception as e:
        print('Failed to write to log file:', e)
    print(line.strip())  # still show in terminal too

# ── Background preview loop ──
def preview_loop():
    global latest_preview, capturing_photo, last_preview_success
    while True:
        if capturing_photo or not settings.get('liveview_enabled', True):
            time.sleep(0.2)
            continue

        # Try to grab the camera lock WITHOUT blocking — if a real capture
        # is in progress (or about to start), just skip this preview tick
        # rather than risk colliding with it on the USB device.
        acquired = camera_lock.acquire(blocking=False)
        if not acquired:
            time.sleep(0.1)
            continue
        try:
            tmp = os.path.join(BASE_PHOTO_DIR, '_preview_tmp.jpg')
            code, out, err = run(['gphoto2', '--capture-preview', '--filename', tmp, '--force-overwrite'])
            matches = glob.glob(os.path.join(BASE_PHOTO_DIR, '*preview*'))
            if code == 0 and matches:
                newest = max(matches, key=os.path.getmtime)
                with open(newest, 'rb') as f:
                    data = f.read()
                with preview_lock:
                    latest_preview = data
                last_preview_success = time.time()
        except:
            pass
        finally:
            camera_lock.release()
        time.sleep(0.01)

t = threading.Thread(target=preview_loop, daemon=True)
t.start()

# ── Orientation correction ──
# When the camera is physically mounted in portrait orientation, the booth
# rotates the feed 90° to display upright on a vertical monitor, then
# mirrors it for a natural selfie feel. Saved photos need the same
# treatment to match what guests saw on screen. In landscape mode, the
# camera is mounted normally — only the mirror is needed, no rotation.
def rotate_and_mirror_photo(image_path):
    if not PIL_AVAILABLE:
        return
    try:
        img = Image.open(image_path)
        if settings.get('orientation', 'portrait') == 'portrait':
            img = img.rotate(90, expand=True)
        img = ImageOps.mirror(img)
        img.save(image_path, quality=95)
    except Exception as e:
        log_error(f'Rotation error: {e}')

# ── Filter application (Pillow) ──
def apply_filter(image_path, filter_name):
    """Applies the chosen filter to the captured photo in place."""
    if not PIL_AVAILABLE or filter_name == 'none':
        return
    try:
        img = Image.open(image_path).convert('RGB')

        if filter_name == 'bw':
            img = ImageOps.grayscale(img).convert('RGB')

        elif filter_name == 'warm':
            r, g, b = img.split()
            r = r.point(lambda i: min(255, int(i * 1.12)))
            b = b.point(lambda i: int(i * 0.88))
            img = Image.merge('RGB', (r, g, b))
            img = ImageEnhance.Color(img).enhance(1.25)
            img = ImageEnhance.Brightness(img).enhance(1.04)

        elif filter_name == 'cool':
            r, g, b = img.split()
            b = b.point(lambda i: min(255, int(i * 1.12)))
            r = r.point(lambda i: int(i * 0.92))
            img = Image.merge('RGB', (r, g, b))
            img = ImageEnhance.Color(img).enhance(0.85)
            img = ImageEnhance.Brightness(img).enhance(1.04)

        elif filter_name == 'vignette':
            img = ImageEnhance.Contrast(img).enhance(1.1)
            img = ImageEnhance.Brightness(img).enhance(0.95)
            w, h = img.size

            strength = settings.get('vignette_strength', 50) / 100.0  # 0.0 - 1.0
            # innerRadius: fraction of the half-diagonal that stays fully bright
            # strength 0   -> innerRadius 0.95 (almost no vignette)
            # strength 1.0 -> innerRadius 0.25 (heavy vignette)
            inner_radius = 0.95 - (strength * 0.70)
            outer_radius = 1.05  # fully dark just past the corners

            import numpy as np
            cx, cy = w / 2.0, h / 2.0
            max_dist = math.sqrt(cx**2 + cy**2)

            y_idx, x_idx = np.indices((h, w))
            dist = np.sqrt((x_idx - cx) ** 2 + (y_idx - cy) ** 2) / max_dist  # 0 center -> ~1 corner

            # Smoothly map distance to brightness: 1.0 inside inner_radius,
            # fading linearly to 0.0 at outer_radius
            mask_arr = np.clip((outer_radius - dist) / (outer_radius - inner_radius), 0, 1)
            mask_arr = (mask_arr * 255).astype('uint8')
            mask = Image.fromarray(mask_arr, mode='L')

            black = Image.new('RGB', (w, h), (0, 0, 0))
            img = Image.composite(img, black, mask)

        elif filter_name == 'vintage':
            img = ImageEnhance.Color(img).enhance(0.7)
            img = ImageEnhance.Contrast(img).enhance(0.9)
            img = ImageEnhance.Brightness(img).enhance(0.96)
            r, g, b = img.split()
            r = r.point(lambda i: min(255, int(i * 1.08)))
            b = b.point(lambda i: int(i * 0.85))
            img = Image.merge('RGB', (r, g, b))

        img.save(image_path, quality=92)
    except Exception as e:
        log_error(f'Filter error ({filter_name}): {e}')

# ── Routes: pages ──
@app.route('/')
def index():
    return send_from_directory('.', 'booth.html')

@app.route('/api/booth-claim', methods=['POST'])
def booth_claim():
    """Called once when the booth page loads. If no one else currently
    holds the active session, this browser becomes the active viewer.
    If someone else already holds it, returns claimed=False so the
    booth.html JS shows its own locked overlay instead."""
    global active_booth_token, active_booth_claimed_at
    data = request.get_json() or {}
    token = data.get('token')
    if not token:
        return jsonify({'ok': False, 'error': 'Missing token'}), 400

    now = time.time()
    # Treat the previous holder as gone if it hasn't checked in for a while
    # (e.g. the tab was closed without a clean release) — this self-heals
    # without anyone needing the PIN if the old tab is genuinely gone.
    STALE_AFTER = 15  # seconds

    if active_booth_token is None or token == active_booth_token or (now - active_booth_claimed_at) > STALE_AFTER:
        active_booth_token = token
        active_booth_claimed_at = now
        return jsonify({'ok': True, 'claimed': True})

    return jsonify({'ok': True, 'claimed': False})

@app.route('/api/booth-heartbeat', methods=['POST'])
def booth_heartbeat():
    """The active booth tab calls this periodically to prove it's still
    the legitimate holder, refreshing the staleness timer."""
    global active_booth_claimed_at
    data = request.get_json() or {}
    token = data.get('token')
    if token == active_booth_token:
        active_booth_claimed_at = time.time()
        return jsonify({'ok': True, 'active': True})
    return jsonify({'ok': True, 'active': False})

@app.route('/api/booth-takeover', methods=['POST'])
def booth_takeover():
    """A second device that hit the locked screen can take over the active
    session, but only after providing the correct PIN."""
    global active_booth_token, active_booth_claimed_at
    data = request.get_json() or {}
    pin = str(data.get('pin', '')).strip()
    token = data.get('token')

    if pin != CONTROL_PANEL_PIN:
        return jsonify({'ok': False, 'error': 'Incorrect PIN'}), 401
    if not token:
        return jsonify({'ok': False, 'error': 'Missing token'}), 400

    active_booth_token = token
    active_booth_claimed_at = time.time()
    return jsonify({'ok': True})

@app.route('/api/booth-release', methods=['POST'])
def booth_release():
    """Called when the active booth tab is intentionally closed/navigated
    away, freeing up the slot immediately rather than waiting for staleness."""
    global active_booth_token
    data = request.get_json() or {}
    token = data.get('token')
    if token == active_booth_token:
        active_booth_token = None
    return jsonify({'ok': True})

def require_control_auth(f):
    """Decorator: blocks access unless the session has already passed the
    PIN check via /control-login. Applied to the control panel page itself
    and to every control-only API route, so the PIN can't be bypassed by
    calling the API directly."""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('control_authed'):
            return jsonify({'error': 'Not authorized'}), 401
        return f(*args, **kwargs)
    return wrapper

@app.route('/control')
def control():
    if not session.get('control_authed'):
        return send_from_directory('.', 'control-login.html')
    return send_from_directory('.', 'control.html')

@app.route('/api/control-login', methods=['POST'])
def control_login():
    data = request.get_json() or {}
    pin = str(data.get('pin', '')).strip()
    if pin == CONTROL_PANEL_PIN:
        session['control_authed'] = True
        session.permanent = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Incorrect PIN'}), 401

@app.route('/api/control-logout', methods=['POST'])
def control_logout():
    session.pop('control_authed', None)
    return jsonify({'ok': True})

# ── Routes: liveview ──
@app.route('/preview')
def preview():
    with preview_lock:
        data = latest_preview
    if data is None:
        return jsonify({'error': 'No preview yet'}), 503
    return send_file(io.BytesIO(data), mimetype='image/jpeg')

@app.route('/api/camera-status')
def camera_status():
    online = (time.time() - last_preview_success) < CAMERA_OFFLINE_THRESHOLD and last_preview_success > 0
    return jsonify({'online': online})

@app.route('/api/session-count')
def get_session_count():
    today = time.strftime('%Y-%m-%d')
    global session_photo_count, session_date
    if today != session_date:
        session_date = today
        session_photo_count = 0
    return jsonify({'count': session_photo_count, 'date': session_date})

@app.route('/api/sd-status')
@require_control_auth
def sd_status():
    LOW_THRESHOLD = 10
    slots = get_remaining_shots_all_slots()

    low_slots = []
    for slot_name, remaining in slots.items():
        if remaining is not None and remaining <= LOW_THRESHOLD:
            low_slots.append({'slot': slot_name, 'remaining': remaining})

    return jsonify({
        'slot1': slots['slot1'],
        'slot2': slots['slot2'],
        'low': len(low_slots) > 0,
        'low_slots': low_slots
    })

@app.route('/api/disk-status')
@require_control_auth
def disk_status():
    free_gb = get_disk_space_gb()
    return jsonify({
        'free_gb': free_gb,
        'low': free_gb is not None and free_gb <= DISK_LOW_THRESHOLD_GB
    })

@app.route('/api/trigger-idle', methods=['POST'])
@require_control_auth
def trigger_idle():
    """Control panel manually requests the booth screen show its idle/
    screensaver overlay (backup in case the screen needs resetting)."""
    global idle_trigger_requested
    idle_trigger_requested = True
    return jsonify({'ok': True})

@app.route('/api/idle-trigger-check')
def idle_trigger_check():
    """Booth screen polls this; once it acts on a pending trigger, it calls
    the consume endpoint below to clear the flag so it doesn't re-fire."""
    return jsonify({'triggered': idle_trigger_requested})

@app.route('/api/idle-trigger-consume', methods=['POST'])
def idle_trigger_consume():
    global idle_trigger_requested
    idle_trigger_requested = False
    return jsonify({'ok': True})

@app.route('/api/restart-camera', methods=['POST'])
@require_control_auth
def restart_camera():
    """Software-side reset of the camera connection. Since each gphoto2 call
    in this app already opens and closes its own session (there's no
    persistent connection to 'restart'), this instead: pauses the live
    preview loop completely, waits for the USB bus to fully release, then
    verifies the camera responds again with a real command before reporting
    success — rather than just calling --auto-detect, which only lists
    devices and doesn't recover a stuck camera."""
    global last_preview_success, capturing_photo
    try:
        # Force the preview loop to stop attempting captures
        capturing_photo = True
        with camera_lock:
            time.sleep(1.5)  # let any in-flight USB transaction fully release

            # Verify the camera actually responds now, not just that it's listed
            code, out, err = run(['gphoto2', '--get-config', '/main/status/focusindication'])

        last_preview_success = 0  # force offline state on both screens until a preview succeeds
        capturing_photo = False

        if code == 0:
            return jsonify({'ok': True, 'message': 'Camera connection verified and reset'})
        else:
            log_error(f'Camera restart: camera still not responding: {err.strip()}')
            return jsonify({'ok': False, 'error': 'Camera is not responding. Check the USB cable and camera power.'}), 500
    except Exception as e:
        capturing_photo = False
        log_error(f'Camera restart error: {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500

# ── Routes: capture flow ──
# Control panel calls /api/shoot-request -> tells booth screen to start its countdown
# Booth screen polls /api/shoot-pending, sees True, runs countdown + flash on screen
# Booth screen calls /api/shoot-ack EXACTLY when the countdown hits zero —
#   this is what actually fires the shutter, so the photo captures the right pose
# The slow part (camera processing + USB transfer, ~4s) then happens in the
#   background. The booth screen does NOT wait for this — it returns to live
#   view immediately. The photo simply appears in the gallery a few seconds later.

shoot_ack_lock = threading.Lock()

@app.route('/api/shoot-request', methods=['POST'])
@require_control_auth
def shoot_request():
    global shoot_pending, shoot_acked
    if capturing_photo:
        return jsonify({'ok': False, 'error': 'Already capturing'}), 409
    shoot_pending = True
    shoot_acked = False
    return jsonify({'ok': True})

@app.route('/api/shoot-pending')
def shoot_pending_check():
    return jsonify({'pending': shoot_pending and not shoot_acked})

@app.route('/api/shoot-ack', methods=['POST'])
def shoot_ack():
    # Booth screen calls this the moment its countdown hits zero.
    # This is the real shutter trigger — fire it now, then let the
    # slow download happen in the background.
    # Guarded so that even if multiple booth tabs/windows are open and
    # all call this at once, only ONE capture is ever triggered per shot.
    global shoot_acked
    with shoot_ack_lock:
        if shoot_acked:
            # Someone else already acked this shot — no-op, don't capture again
            return jsonify({'ok': True, 'duplicate': True})
        shoot_acked = True
        thread = threading.Thread(target=do_capture)
        thread.start()
    return jsonify({'ok': True})

@app.route('/api/capture-status')
def capture_status():
    return jsonify({'capturing': capturing_photo, 'pending': shoot_pending and not shoot_acked})

# ── Burst mode (4-shot sequence) ──
# Control panel calls /api/burst-request -> sets burst_pending, burst_current_index = 1
# Booth screen polls /api/burst-status repeatedly. For each shot:
#   - sees current_index increase, runs its countdown + flash
#   - calls /api/burst-ack to fire that shot's shutter (server captures in background)
#   - waits for capturing_photo to clear, then polls again — server auto-advances
#     current_index for the next shot once the previous one finishes
# After shot 4 completes, burst_complete is set so booth shows the confirmation,
# and control panel's button re-enables.

burst_lock = threading.Lock()

@app.route('/api/burst-request', methods=['POST'])
@require_control_auth
def burst_request():
    global burst_pending, burst_current_index, burst_complete, burst_photos
    if capturing_photo or burst_pending:
        return jsonify({'ok': False, 'error': 'Already busy'}), 409
    burst_pending = True
    burst_current_index = 1
    burst_complete = False
    burst_photos = []
    return jsonify({'ok': True})

@app.route('/api/burst-status')
def burst_status():
    return jsonify({
        'pending': burst_pending,
        'current_index': burst_current_index,
        'total': burst_total,
        'complete': burst_complete,
        'capturing': capturing_photo
    })

@app.route('/api/burst-ack', methods=['POST'])
def burst_ack():
    """Booth screen calls this the moment its countdown hits zero for the
    CURRENT shot in the burst. Fires the shutter for that shot only."""
    global burst_acked_for_index
    data = request.get_json() or {}
    shot_index = data.get('index')

    with burst_lock:
        if shot_index != burst_current_index or capturing_photo:
            # Stale/duplicate ack (e.g. multiple tabs) — ignore safely
            return jsonify({'ok': True, 'duplicate': True})
        burst_acked_for_index = shot_index
        thread = threading.Thread(target=do_burst_capture, args=(shot_index,))
        thread.start()
    return jsonify({'ok': True})

@app.route('/api/burst-reset', methods=['POST'])
def burst_reset():
    """Called once the booth has shown its confirmation and is ready to
    return to normal live view, clearing burst state for next time."""
    global burst_pending, burst_current_index, burst_complete, burst_photos
    burst_pending = False
    burst_current_index = 0
    burst_complete = False
    burst_photos = []
    return jsonify({'ok': True})

def do_burst_capture(shot_index):
    global capturing_photo, burst_current_index, burst_complete, burst_pending
    global session_photo_count, session_date, burst_photos, last_photo

    today = time.strftime('%Y-%m-%d')
    if today != session_date:
        session_date = today
        session_photo_count = 0

    capturing_photo = True
    try:
        with camera_lock:
            remaining = get_remaining_shots()
            if remaining is not None and remaining <= 0:
                log_error('Burst capture aborted: SD card is full (0 shots remaining)')
                burst_pending = False
                return

            photo_dir = get_photo_dir()
            filename = os.path.join(photo_dir, time.strftime('%H%M%S') + f'_burst{shot_index}.jpg')

            code, out, err = None, None, None
            for attempt in range(2):
                code, out, err = run([
                    'gphoto2', '--capture-image-and-download',
                    '--filename', filename,
                    '--force-overwrite'
                ])
                if code == 0 and os.path.exists(filename):
                    break
                if attempt == 0:
                    log_error(f'Burst shot {shot_index} attempt 1 failed, retrying: {err.strip()}')
                    time.sleep(0.5)

        if code == 0 and os.path.exists(filename):
            rotate_and_mirror_photo(filename)
            active_filter = settings.get('filter', 'none')
            apply_filter(filename, active_filter)
            session_photo_count += 1
            photo_ref = {'date': os.path.basename(photo_dir), 'file': os.path.basename(filename)}
            burst_photos.append(photo_ref)
            last_photo = photo_ref
        else:
            log_error(f'Burst shot {shot_index} failed after retry: {err.strip() if err else "unknown error"}')
    finally:
        capturing_photo = False
        if shot_index >= burst_total:
            burst_complete = True
            burst_pending = False
        else:
            burst_current_index = shot_index + 1

def do_capture():
    global capturing_photo, shoot_pending, shoot_acked, session_photo_count, session_date, last_photo

    # Reset session count if it's a new day
    today = time.strftime('%Y-%m-%d')
    if today != session_date:
        session_date = today
        session_photo_count = 0

    capturing_photo = True   # tells preview loop to stop trying immediately
    try:
        with camera_lock:
            # Check SD card capacity before attempting capture
            remaining = get_remaining_shots()
            if remaining is not None and remaining <= 0:
                log_error('Capture aborted: SD card is full (0 shots remaining)')
                return

            photo_dir = get_photo_dir()
            filename = os.path.join(photo_dir, time.strftime('%H%M%S') + '.jpg')

            # Auto-retry: try up to 2 times total if the first attempt fails
            code, out, err = None, None, None
            for attempt in range(2):
                code, out, err = run([
                    'gphoto2', '--capture-image-and-download',
                    '--filename', filename,
                    '--force-overwrite'
                ])
                if code == 0 and os.path.exists(filename):
                    break
                if attempt == 0:
                    log_error(f'Capture attempt 1 failed, retrying: {err.strip()}')
                    time.sleep(0.5)

        if code == 0 and os.path.exists(filename):
            rotate_and_mirror_photo(filename)
            active_filter = settings.get('filter', 'none')
            apply_filter(filename, active_filter)
            session_photo_count += 1
            last_photo = {'date': os.path.basename(photo_dir), 'file': os.path.basename(filename)}
        else:
            log_error(f'Capture failed after retry: {err.strip() if err else "unknown error"}')
    finally:
        capturing_photo = False
        shoot_pending = False
        shoot_acked = False


@app.route('/photos/<date>/<filename>')
def photo(date, filename):
    return send_from_directory(os.path.join(BASE_PHOTO_DIR, date), filename)

# ── QR code download flow ──
# After a photo is captured, the booth screen shows a QR code linking to
# /download/<date>/<filename> — a simple mobile-friendly page guests can
# open on their own phone to save the photo, no app or login needed.

@app.route('/api/last-photo')
def api_last_photo():
    if last_photo is None:
        return jsonify({'photo': None})
    return jsonify({'photo': last_photo})

@app.route('/api/qr-code/<date>/<filename>')
def qr_code(date, filename):
    if not QRCODE_AVAILABLE:
        return jsonify({'error': 'qrcode package not installed on server'}), 500

    download_url = f'http://{LOCAL_IP}:{SERVER_PORT}/download/{date}/{filename}'
    img = qrcode.make(download_url, border=2, box_size=8)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')

@app.route('/download/<date>/<filename>')
def download_page(date, filename):
    photo_url = f'/photos/{date}/{filename}'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Your Photo</title>
<style>
  body {{
    margin: 0; padding: 24px;
    background: #1C1714; color: #FAF7F2;
    font-family: -apple-system, 'Cormorant Garamond', serif;
    display: flex; flex-direction: column; align-items: center;
    min-height: 100vh; box-sizing: border-box;
  }}
  h1 {{ font-size: 1.3rem; font-weight: 400; letter-spacing: .04em; margin-bottom: 20px; text-align: center; }}
  img {{ max-width: 100%; border-radius: 8px; box-shadow: 0 8px 30px rgba(0,0,0,.5); margin-bottom: 24px; }}
  a.save-btn {{
    background: #B89B6A; color: #1C1714; text-decoration: none;
    padding: 14px 36px; border-radius: 6px; font-weight: 700;
    letter-spacing: .08em; text-transform: uppercase; font-size: .85rem;
  }}
  p {{ color: rgba(255,255,255,.4); font-size: .8rem; margin-top: 20px; text-align: center; }}
</style>
</head>
<body>
  <h1>Your Wedding Photo</h1>
  <img src="{photo_url}" alt="Your photo" />
  <a class="save-btn" href="{photo_url}" download="{filename}">Save to Phone</a>
  <p>Press and hold the photo, or use the Save button, to download it to your device.</p>
</body>
</html>"""

# ── Routes: control panel API ──
@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(settings)

@app.route('/api/settings', methods=['POST'])
@require_control_auth
def post_settings():
    data = request.get_json()
    settings.update(data)
    save_settings()
    return jsonify({'ok': True})

@app.route('/api/photos', methods=['GET'])
@require_control_auth
def get_photos():
    # Collect photos from all dated subfolders, newest dates first
    all_files = []
    for date_dir in sorted(glob.glob(os.path.join(BASE_PHOTO_DIR, '????-??-??')), reverse=True):
        date = os.path.basename(date_dir)
        files = sorted(glob.glob(os.path.join(date_dir, '*.jpg')), reverse=True)
        for f in files:
            if 'preview' not in os.path.basename(f) and 'grid_' not in os.path.basename(f):
                all_files.append({'date': date, 'file': os.path.basename(f)})
    return jsonify({'photos': all_files})

# ── Grid creation ──
def create_grid_image(photo_refs, title_text, font_data=None):
    """photo_refs: list of 4 {date, file} dicts. Returns the new grid filename
    (date, file) on success, raises on failure."""
    if not PIL_AVAILABLE:
        raise RuntimeError('Pillow is not installed — cannot create grid')
    if len(photo_refs) != 4:
        raise ValueError('Grid requires exactly 4 photos')

    imgs = []
    for ref in photo_refs:
        path = os.path.join(BASE_PHOTO_DIR, ref['date'], ref['file'])
        if not os.path.exists(path):
            raise FileNotFoundError(f"Photo not found: {ref['date']}/{ref['file']}")
        imgs.append(Image.open(path).convert('RGB'))

    # Resize all 4 to the same cell size (based on the smallest photo, capped for sanity)
    cell_w = min(img.width for img in imgs)
    cell_h = min(img.height for img in imgs)
    cell_w = min(cell_w, 1200)
    cell_h = min(cell_h, 1200)
    imgs = [ImageOps.fit(img, (cell_w, cell_h), Image.LANCZOS) for img in imgs]

    gutter = max(8, cell_w // 80)  # thin border between cells, scales with size
    canvas_w = cell_w * 2 + gutter * 3
    canvas_h = cell_h * 2 + gutter * 3

    canvas = Image.new('RGB', (canvas_w, canvas_h), (255, 255, 255))
    positions = [
        (gutter, gutter),
        (gutter * 2 + cell_w, gutter),
        (gutter, gutter * 2 + cell_h),
        (gutter * 2 + cell_w, gutter * 2 + cell_h),
    ]
    for img, pos in zip(imgs, positions):
        canvas.paste(img, pos)

    # Draw the title text centered in the middle gutter intersection
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(canvas)

    font_size = max(20, cell_w // 14)
    font = None
    try:
        if font_data and font_data.get('local_path') and os.path.exists(font_data['local_path']):
            font = ImageFont.truetype(font_data['local_path'], font_size)
    except Exception as e:
        log_error(f'Grid font load failed, using default: {e}')
    if font is None:
        try:
            font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Georgia.ttf', font_size)
        except Exception:
            font = ImageFont.load_default()

    # Small white plaque behind the text so it's readable over the gutter
    bbox = draw.textbbox((0, 0), title_text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    cx, cy = canvas_w // 2, canvas_h // 2
    pad = 16
    draw.rectangle(
        [cx - text_w // 2 - pad, cy - text_h // 2 - pad, cx + text_w // 2 + pad, cy + text_h // 2 + pad],
        fill=(255, 255, 255)
    )
    draw.text((cx - text_w // 2, cy - text_h // 2 - bbox[1]), title_text, fill=(20, 20, 20), font=font)

    out_dir = get_photo_dir()
    out_filename = 'grid_' + time.strftime('%H%M%S') + '.jpg'
    out_path = os.path.join(out_dir, out_filename)
    canvas.save(out_path, quality=92)
    return {'date': os.path.basename(out_dir), 'file': out_filename}

@app.route('/api/create-grid', methods=['POST'])
@require_control_auth
def api_create_grid():
    data = request.get_json()
    photo_refs = data.get('photos')  # list of {date, file}, or omit to use last 4
    title_text = data.get('title', settings.get('grid_title', ''))
    font_data = settings.get('grid_font')

    try:
        if not photo_refs:
            # "Use last 4" — gather most recent 4 across all dates
            all_files = []
            for date_dir in sorted(glob.glob(os.path.join(BASE_PHOTO_DIR, '????-??-??')), reverse=True):
                date = os.path.basename(date_dir)
                files = sorted(glob.glob(os.path.join(date_dir, '*.jpg')), reverse=True)
                for f in files:
                    if 'preview' not in os.path.basename(f) and 'grid_' not in os.path.basename(f):
                        all_files.append({'date': date, 'file': os.path.basename(f)})
            if len(all_files) < 4:
                return jsonify({'error': f'Need at least 4 photos, only found {len(all_files)}'}), 400
            photo_refs = all_files[:4]

        if len(photo_refs) != 4:
            return jsonify({'error': 'Exactly 4 photos are required for a grid'}), 400

        result = create_grid_image(photo_refs, title_text, font_data)
        return jsonify({'ok': True, **result})
    except Exception as e:
        log_error(f'Grid creation failed: {e}')
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    if not PIL_AVAILABLE:
        log_error('WARNING: Pillow is not installed — filters will not be applied to captured photos. Run: pip install Pillow')
    print("\n  Wedding Booth running!")
    print("  Booth screen  ->  http://localhost:5000")
    print("  Control panel ->  http://localhost:5000/control")
    print(f"  Error log     ->  {LOG_FILE}\n")
    app.run(host='0.0.0.0', port=5000, threaded=True)
