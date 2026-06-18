import subprocess, os, glob, time, threading, io, json
from flask import Flask, send_file, jsonify, send_from_directory, request

try:
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

app = Flask(__name__, static_folder='.')

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

settings = {
    'filter': 'none',
    'countdown': 3,
    'enabled': True,
    'message': '',
    'font': None,
    'liveview_enabled': True
}

def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr

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
        time.sleep(0.05)

t = threading.Thread(target=preview_loop, daemon=True)
t.start()

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
            from PIL import ImageDraw
            # Map strength 1-100 to inner ellipse inset: weak=large ellipse, strong=small ellipse
            strength = settings.get('vignette_strength', 50) / 100.0
            inset = 0.5 - (strength * 0.45)  # ranges from 0.05 (strong) to 0.5 (subtle)
            mask = Image.new('L', (w, h), 0)
            draw = ImageDraw.Draw(mask)
            draw.ellipse([w * inset, h * inset, w * (1 - inset), h * (1 - inset)], fill=255)
            blur_radius = int(min(w, h) * (0.15 + strength * 0.25))
            mask = mask.filter(ImageFilter.GaussianBlur(radius=blur_radius))
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

@app.route('/control')
def control():
    return send_from_directory('.', 'control.html')

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

# ── Routes: capture flow ──
# Control panel calls /api/shoot-request -> tells booth screen to start its countdown
# Booth screen polls /api/shoot-pending, sees True, runs countdown + flash on screen
# Booth screen calls /api/shoot-ack EXACTLY when the countdown hits zero —
#   this is what actually fires the shutter, so the photo captures the right pose
# The slow part (camera processing + USB transfer, ~4s) then happens in the
#   background. The booth screen does NOT wait for this — it returns to live
#   view immediately. The photo simply appears in the gallery a few seconds later.

@app.route('/api/shoot-request', methods=['POST'])
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
    global shoot_acked
    shoot_acked = True
    thread = threading.Thread(target=do_capture)
    thread.start()
    return jsonify({'ok': True})

@app.route('/api/capture-status')
def capture_status():
    return jsonify({'capturing': capturing_photo, 'pending': shoot_pending and not shoot_acked})

def do_capture():
    global capturing_photo, shoot_pending, shoot_acked
    capturing_photo = True   # tells preview loop to stop trying immediately
    try:
        # Block here (briefly) until any in-flight preview tick finishes,
        # then hold the lock for the entire real capture so nothing else
        # can touch the camera's USB connection until we're done.
        with camera_lock:
            photo_dir = get_photo_dir()  # resolves to today's date folder
            filename = os.path.join(photo_dir, time.strftime('%H%M%S') + '.jpg')
            code, out, err = run([
                'gphoto2', '--capture-image-and-download',
                '--filename', filename,
                '--force-overwrite'
            ])

        if code == 0 and os.path.exists(filename):
            active_filter = settings.get('filter', 'none')
            apply_filter(filename, active_filter)
        else:
            log_error(f'Capture failed: {err.strip()}')
    finally:
        capturing_photo = False
        shoot_pending = False
        shoot_acked = False


@app.route('/photos/<date>/<filename>')
def photo(date, filename):
    return send_from_directory(os.path.join(BASE_PHOTO_DIR, date), filename)

# ── Routes: control panel API ──
@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(settings)

@app.route('/api/settings', methods=['POST'])
def post_settings():
    data = request.get_json()
    settings.update(data)
    return jsonify({'ok': True})

@app.route('/api/photos', methods=['GET'])
def get_photos():
    # Collect photos from all dated subfolders, newest dates first
    all_files = []
    for date_dir in sorted(glob.glob(os.path.join(BASE_PHOTO_DIR, '????-??-??')), reverse=True):
        date = os.path.basename(date_dir)
        files = sorted(glob.glob(os.path.join(date_dir, '*.jpg')), reverse=True)
        for f in files:
            if 'preview' not in os.path.basename(f):
                all_files.append({'date': date, 'file': os.path.basename(f)})
    return jsonify({'photos': all_files})

if __name__ == '__main__':
    if not PIL_AVAILABLE:
        log_error('WARNING: Pillow is not installed — filters will not be applied to captured photos. Run: pip install Pillow')
    print("\n  Wedding Booth running!")
    print("  Booth screen  ->  http://localhost:5000")
    print("  Control panel ->  http://localhost:5000/control")
    print(f"  Error log     ->  {LOG_FILE}\n")
    app.run(host='0.0.0.0', port=5000, threaded=True)
