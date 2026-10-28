"""
Combined PKG test: launch retip → navigate to garden → Frida thread dump → detect hang.
Hang detection: look for XamShowDirtyDiscErrorUI in any thread stack.

Usage:
  python test_hang.py backup     # test original PKG
  python test_hang.py modified   # test injected PKG

Optional path flags (defaults = the author's original hardcoded layout):
  --game-dir <path>   retip install dir (default: <path/to/retip-1.13.0-windows>)
  --deploy <path>     PKG deploy target (default: <game-dir>/assets/Beta/packages/1150.pkg)
  --backup <path>     vanilla backup PKG (default: <deploy>.backup)
  --pkg <path>        custom SOURCE PKG to deploy (mode=modified)
"""
import ctypes, time, sys, os, json, shutil, subprocess, frida
from ctypes import wintypes

u = ctypes.windll.user32
k = ctypes.windll.kernel32
gdi = ctypes.windll.gdi32

# XInput controller support
try:
    xinput = ctypes.windll.xinput1_4
    HAS_XINPUT = True
except:
    try:
        xinput = ctypes.windll.xinput9_1_0
        HAS_XINPUT = True
    except:
        HAS_XINPUT = False

class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort),
                ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]

class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint),
                ("Gamepad", XINPUT_GAMEPAD)]

# Button constants
XINPUT_GAMEPAD_DPAD_UP = 0x0001
XINPUT_GAMEPAD_DPAD_DOWN = 0x0002
XINPUT_GAMEPAD_DPAD_LEFT = 0x0004
XINPUT_GAMEPAD_DPAD_RIGHT = 0x0008
XINPUT_GAMEPAD_START = 0x0010
XINPUT_GAMEPAD_BACK = 0x0020
XINPUT_GAMEPAD_A = 0x1000
XINPUT_GAMEPAD_B = 0x2000

def controller_press(button, duration_ms=100):
    """Press a controller button using XInput."""
    if not HAS_XINPUT:
        return False
    state = XINPUT_STATE()
    for controller in range(4):
        result = xinput.XInputGetState(controller, ctypes.byref(state))
        if result == 0:  # Controller connected
            # Press button
            state.Gamepad.wButtons = button
            xinput.XInputSetState(controller, ctypes.byref(state))
            time.sleep(duration_ms / 1000.0)
            # Release button
            state.Gamepad.wButtons = 0
            xinput.XInputSetState(controller, ctypes.byref(state))
            time.sleep(0.1)
            return True
    return False

def controller_dpad_down(times=1, delay=0.3):
    """Press D-pad down multiple times."""
    for i in range(times):
        controller_press(XINPUT_GAMEPAD_DPAD_DOWN, 80)
        time.sleep(delay)

def controller_a():
    """Press A button (confirm/select)."""
    controller_press(XINPUT_GAMEPAD_A, 80)
TOOLS = os.path.dirname(os.path.abspath(__file__))
# Defaults to the standard Goopie install under the current user's profile;
# override with --game-dir / --deploy / --backup.
DEFAULT_GAME = (
    r"C:\Users\{}\AppData\Local\Goopie\Games\retip-1.13.0-windows".format(
        os.environ.get("USERNAME", "user")))
GAME = DEFAULT_GAME
PKG = os.path.join(GAME, "assets", "Beta", "packages", "1150.pkg")
BAK = PKG + ".backup"
MOD = PKG + ".modified"
_COORDS = os.path.join(TOOLS, "game_coords.json")
if not os.path.exists(_COORDS):
    sys.exit(f"file not found: {_COORDS} — place game_coords.json next to test_hang.py")
COORDS = json.load(open(_COORDS))

# --- Frida script: dump all thread stacks + hook animation loading ---
FRIDA_JS = r"""
  'use strict';
  // Guest-to-host mapping: host = guest + 0x100000000
  // Known guest functions:
  var GUEST_FUNCS = {
    '0x821d1890': 'statsCommonGetStatsAidFromTag',
    '0x821d2120': 'rex_meLoadAsset',
    '0x821d0208': 'rex_meFindBundleWithAid',
    '0x821d0290': 'rex_meLoadAssetFromPackage',
    '0x821d03d8': 'rex_meFindFromPackage',
    '0x821d0460': 'rex_assetDbInsertX',
    '0x821ce210': 'rex_assetDbPackageManagerInit',
    '0x82240ed8': 'rex_locMainGetBundlesPath',
    '0x821d0130': 'rex_meGetPackageTimestamp',
    '0x821d0358': 'rex_meFreeAssetFromPackage',
    '0x825c5838': 'XuiClass_Scene_Pinata_ModifyAbility',
    '0x8232c048': 'crate_capacity',
    '0x82558d28': 'pinata_surface_preference',
  };
  function resolveAddr(addr) {
    var s = addr.toString();
    // Check if this is a host address in guest region (0x182000000+)
    try {
      var hostVal = addr.compare(ptr('0x182000000'));
      if (hostVal >= 0) {
        var guestAddr = addr.sub(ptr('0x100000000'));
        var gHex = '0x' + guestAddr.toString(16);
        var known = GUEST_FUNCS[gHex];
        if (known) return s + " GUEST!" + known + " (" + gHex + ")";
        return s + " guest(" + gHex + ")";
      }
    } catch(e) {}
    // Check if host address is in retip.exe
    try {
      var sym = DebugSymbol.fromAddress(addr);
      if (sym && sym.name && sym.name !== addr.toString()) {
        return s + " " + sym.name;
      }
    } catch(e) {}
    try {
      var m = Process.getModuleByAddress(addr);
      if (m) {
        var off = addr.sub(m.base);
        return s + " " + m.name + "+" + off.toString();
      }
    } catch(e) {}
    return s;
  }
  rpc.exports = {
    dump: function() {
      const threads = Process.enumerateThreads();
      const out = [];
      for (const t of threads) {
        let frames = [];
        try {
          const bt = Thread.backtrace(t.context, Backtracer.ACCURATE);
          frames = bt.map(a => resolveAddr(a));
        } catch(e) {
          try {
            const bt = Thread.backtrace(t.context, Backtracer.FUZZY);
            frames = bt.map(a => resolveAddr(a));
          } catch(e2) {
            frames = ['<bt fail: ' + e2 + '>'];
          }
        }
        out.push({tid: t.id, state: t.state, frames: frames});
      }
      return out;
    },
    scanAnim: function() {
      // Scan all readable memory for "animation" magic
      var results = [];
      var ranges = Process.enumerateRanges('r--');
      var needle = [0x61, 0x6e, 0x69, 0x6d]; // "anim"
      for (var i = 0; i < ranges.length; i++) {
        var r = ranges[i];
        if (r.size > 50 * 1024 * 1024) continue; // skip > 50MB
        try {
          var buf = Memory.readByteArray(r.base, Math.min(r.size, 2000000));
          if (!buf) continue;
          var data = new Uint8Array(buf);
          for (var j = 0; j < data.length - 20; j++) {
            if (data[j]===0x61 && data[j+1]===0x6e && data[j+2]===0x69 && data[j+3]===0x6d) {
              var s = '';
              for (var k = 0; k < 10 && j+k < data.length; k++) s += String.fromCharCode(data[j+k]);
              if (s === 'animation') {
                var addr = r.base.add(j);
                // Read QBS header
                var hv = j + 0x7C;
                function be32(off) { return (data[off]<<24)|(data[off+1]<<16)|(data[off+2]<<8)|data[off+3]; }
                function be16(off) { return (data[off]<<8)|data[off+1]; }
                if (hv + 0x28 < data.length) {
                  var info = {
                    addr: addr.toString(),
                    range_base: r.base.toString(),
                    range_size: r.size,
                    offset: j,
                    mode_off: be32(hv+0x0C),
                    base_off: be32(hv+0x10),
                    bc_off: be32(hv+0x14),
                    sizeA: be16(hv+0x18),
                    sizeB: be16(hv+0x1A),
                    bpf: be16(hv+0x1C),
                    nch: be16(hv+0x22),
                    nfr: be16(hv+0x24)
                  };
                  // Dump mode bytes
                  var modeHex = '';
                  for (var m = 0; m < Math.min(info.sizeB, 52); m++) modeHex += data[j+info.mode_off+m].toString(16).padStart(2,'0') + ' ';
                  info.mode_bytes = modeHex;
                  // Dump bases
                  var baseHex = '';
                  for (var b = 0; b < Math.min(info.sizeA, 80); b += 4) {
                    baseHex += ((data[j+info.base_off+b]<<24)|(data[j+info.base_off+b+1]<<16)|(data[j+info.base_off+b+2]<<8)|data[j+info.base_off+b+3]) + ' ';
                  }
                  info.bases = baseHex;
                  // Dump bitcounts
                  var bcHex = '';
                  for (var c = 0; c < Math.min(info.sizeA, 30); c++) bcHex += data[j+info.bc_off+c].toString(16).padStart(2,'0') + ' ';
                  info.bitcounts = bcHex;
                  results.push(info);
                  if (results.length >= 5) return results;
                }
              }
            }
          }
        } catch(e) {}
      }
      return results;
    },
    pkgopened: function() {
      return { opened: targetPkgOpened || false, filecount: fileOpenCount || 0 };
    }
  };

// Garden-loaded signal: hook kernel32 CreateFileW for "1150.pkg"
// CreateFileW is a real Windows API — address is always correct regardless of build.
// 1150.pkg is opened at FILE_OPEN #80 when loading into the crocodile garden.
var k32 = Process.findModuleByName("kernel32.dll");
var targetPkgOpened = false;
var fileOpenCount = 0;
if (k32) {
    var createFileAddr = k32.getExportByName("CreateFileW");
    if (createFileAddr) {
        Interceptor.attach(createFileAddr, {
            onEnter: function(args) {
                try {
                    var path = args[0].readUtf16String();
                    if (path && path.indexOf(".pkg") !== -1) {
                        fileOpenCount++;
                        if (path.indexOf("1150") !== -1) {
                            targetPkgOpened = true;
                            send("[GARDEN_SIGNAL] 1150.pkg opened (file #" + fileOpenCount + ")");
                        }
                    }
                } catch(e) {}
            }
        });
    }
}

// Hook stbi_zlib_decode_buffer to dump animation data the game sees
var rexr = Process.findModuleByName("rexruntimerd.dll");
if (rexr) {
    send("[FRIDA] rexruntimerd.dll found");
    var zlibAddr = rexr.getExportByName("stbi_zlib_decode_buffer");
    if (zlibAddr) {
        send("[FRIDA] stbi_zlib_decode_buffer hooked at " + zlibAddr);
        var zlibCount = 0;
        Interceptor.attach(zlibAddr, {
            onEnter: function(args) {
                this.outBuf = args[0];
                this.outLen = args[1].toInt32();
                this.inLen = args[3].toInt32();
                zlibCount++;
                if (zlibCount <= 3) {
                    send("[zlib#" + zlibCount + "] inLen=" + this.inLen + " outLen=" + this.outLen);
                }
            },
            onLeave: function(retval) {
                if (this.inLen > 5000) {
                    zlibCount++;
                    try {
                        var outSize = this.outLen;
                        // Read decompressed data (cap at 600KB)
                        var readSize = Math.min(outSize, 600000);
                        var data = new Uint8Array(this.outBuf.readByteArray(readSize));
                        // Look for "animation" magic
                        for (var i = 0; i < data.length - 20; i++) {
                            if (data[i]===0x61 && data[i+1]===0x6e && data[i+2]===0x69 && data[i+3]===0x6d) {
                                var s = '';
                                for (var j = 0; j < 10 && i+j < data.length; j++) s += String.fromCharCode(data[i+j]);
                                if (s === 'animation') {
                                    send("[ANIM] found at offset " + i + " in decompressed buffer (size=" + outSize + ")");
                                    var hv = i + 0x7C;
                                    function be32(off) { return (data[off]<<24)|(data[off+1]<<16)|(data[off+2]<<8)|data[off+3]; }
                                    function be16(off) { return (data[off]<<8)|data[off+1]; }
                                    var mode_off = be32(hv+0x0C);
                                    var base_off = be32(hv+0x10);
                                    var bc_off = be32(hv+0x14);
                                    var sizeA = be16(hv+0x18);
                                    var sizeB = be16(hv+0x1A);
                                    var bpf = be16(hv+0x1C);
                                    var nch = be16(hv+0x22);
                                    var nfr = be16(hv+0x24);
                                    send("[ANIM] mode_off=" + mode_off + " base_off=" + base_off + " bc_off=" + bc_off);
                                    send("[ANIM] sizeA=" + sizeA + " sizeB=" + sizeB + " bpf=" + bpf + " nch=" + nch + " nfr=" + nfr);
                                    // Dump mode bytes
                                    var modeHex = '';
                                    for (var m = 0; m < Math.min(sizeB, 52); m++) modeHex += data[i+mode_off+m].toString(16).padStart(2,'0') + ' ';
                                    send("[ANIM] MODE: " + modeHex);
                                    // Dump bases (first 20 as int32 BE)
                                    var baseHex = '';
                                    for (var b = 0; b < Math.min(sizeA, 80); b += 4) {
                                        baseHex += ((data[i+base_off+b]<<24)|(data[i+base_off+b+1]<<16)|(data[i+base_off+b+2]<<8)|data[i+base_off+b+3]) + ' ';
                                    }
                                    send("[ANIM] BASES: " + baseHex);
                                    // Dump bitcounts (first 30)
                                    var bcHex = '';
                                    for (var c = 0; c < Math.min(sizeA, 30); c++) bcHex += data[i+bc_off+c].toString(16).padStart(2,'0') + ' ';
                                    send("[ANIM] BC: " + bcHex);
                                }
                            }
                        }
                    } catch(e) {
                        send("[zlib#" + zlibCount + "] in=" + this.inLen + " ERR: " + e);
                    }
                }
            }
        });
    }
}
"""

def kill():
    # Close crash report dialog first if present
    h_crash = u.FindWindowW(None, "retip - crash report")
    if h_crash:
        u.PostMessageW(h_crash, 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(0.5)
    subprocess.run(["taskkill", "/F", "/IM", "retip.exe"], capture_output=True)
    time.sleep(1)

def find():
    return u.FindWindowW(None, 'retip [rexglue-v0.8.1.116-dev.gbdc6312-RelWithDebInfo]')

def get_pid():
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq retip.exe", "/NH"],
                       capture_output=True, text=True)
    for line in r.stdout.split("\n"):
        if "retip.exe" in line.lower():
            return int(line.split()[1])
    return None

def ensure_foreground(h, timeout=3.0):
    """Force window to foreground and VERIFY it. Returns True if confirmed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        u.SetForegroundWindow(h)
        time.sleep(0.2)
        # Also bring to top of Z-order
        u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002)  # HWND_TOPMOST then NOTOPMOST
        u.SetWindowPos(h, -2, 0, 0, 0, 0, 0x0001 | 0x0002)  # HWND_NOTOPMOST
        time.sleep(0.2)
        if u.GetForegroundWindow() == h:
            return True
        time.sleep(0.3)
    return False

def clk(h, x, y):
    if not ensure_foreground(h):
        print(f"  WARNING: could not bring window to foreground!")
        return False
    u.SetCursorPos(x, y); time.sleep(0.2)
    # Verify window still in foreground before clicking
    if u.GetForegroundWindow() != h:
        print(f"  WARNING: window lost focus before click!")
        return False
    u.mouse_event(2,0,0,0,0); time.sleep(0.1); u.mouse_event(4,0,0,0,0)
    time.sleep(0.2)
    return True

def key_s(h):
    if not ensure_foreground(h):
        print(f"  WARNING: could not bring window to foreground!")
        return False
    # Verify window still in foreground before keypress
    if u.GetForegroundWindow() != h:
        print(f"  WARNING: window lost focus before keypress!")
        return False
    u.keybd_event(0x53, 0, 0, 0)
    time.sleep(0.2)
    u.keybd_event(0x53, 0, 2, 0)
    time.sleep(0.4)
    return True

def get_log_dir():
    return os.path.join(GAME, "logs")

def get_latest_log():
    logs = sorted(os.listdir(get_log_dir()), key=lambda f: os.path.getmtime(os.path.join(get_log_dir(), f)), reverse=True)
    for f in logs:
        if f.startswith("retip_") and f.endswith(".log"):
            return os.path.join(get_log_dir(), f)
    return None

def check_log(log_path):
    """Read log file and return status dict."""
    with open(log_path, "r", errors="replace") as f:
        content = f.read()
    return {
        "dirty_disc": "XamShowDirtyDiscErrorUI" in content,
        "crash": "[CRASH]" in content,
        "crash_detail": next((l.strip() for l in content.split("\n") if "[CRASH]" in l), ""),
    }

def screenshot(name):
    from PIL import ImageGrab
    path = os.path.join(TOOLS, "screenshots", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ImageGrab.grab().save(path)
    return path

def get_pixel(x, y):
    """Read single pixel via gdi32.GetPixel. No screenshot, no tokens."""
    hdc = u.GetDC(0)
    c = gdi.GetPixel(hdc, x, y)
    u.ReleaseDC(0, hdc)
    return c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF

# Button centers from color sampling; calibration values in game_coords.json
GARDEN_BUTTONS = {
    "yellow": (1766, 352),
    "blue":   (1745, 379),
    "green":  (1745, 413),
    "red":    (1760, 440),
}

# --- Signal 1: GetPixel at known screen positions (fast, no capture) ---
def is_garden_loaded():
    """Detect garden by reading 4 known button pixels via GetPixel."""
    checks = {
        "yellow": lambda r,g,b: r > 200 and g > 180 and b < 100,
        "blue":   lambda r,g,b: r < 80  and g < 150 and b > 180,
        "green":  lambda r,g,b: r < 120 and g > 180 and b < 120,
        "red":    lambda r,g,b: r > 180 and g < 80  and b < 80,
    }
    colors = {}
    for name, (bx, by) in GARDEN_BUTTONS.items():
        try:
            r, g, b = get_pixel(bx, by)
            colors[name] = checks[name](r, g, b)
        except:
            colors[name] = False
    return sum(colors.values()) >= 3, colors

# --- Signal 2: HSV color blob detection via PrintWindow (position-independent) ---
# ROI within captured window image — buttons always here relative to window
COLOR_ROI = (720, 300, 170, 170)  # x, y, w, h

COLOR_BUTTONS = {
    "Y": {"hsv": (26, 202, 248),  "hue_tol": 12, "sat_tol": 80, "val_tol": 80, "expected": (95, 27)},
    "X": {"hsv": (106, 179, 233), "hue_tol": 15, "sat_tol": 80, "val_tol": 80, "expected": (77, 55)},
    "A": {"hsv": (48, 186, 219),  "hue_tol": 15, "sat_tol": 80, "val_tol": 80, "expected": (76, 88)},
    "B": {"hsv": (128, 196, 222), "hue_tol": 15, "sat_tol": 80, "val_tol": 80, "expected": (91, 118)},
}

def _grab_window_raw(hwnd):
    """Capture game window via PrintWindow. Returns numpy BGR image or None."""
    import numpy as np
    class BMIH(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]
    rect = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    hwnd_dc = u.GetWindowDC(hwnd)
    mem_dc = gdi.CreateCompatibleDC(hwnd_dc)
    bmp = gdi.CreateCompatibleBitmap(hwnd_dc, w, h)
    gdi.SelectObject(mem_dc, bmp)
    u.PrintWindow(hwnd, mem_dc, 3)
    bmi = BMIH()
    bmi.biSize = ctypes.sizeof(BMIH)
    bmi.biWidth = w; bmi.biHeight = -h; bmi.biPlanes = 1
    bmi.biBitCount = 32; bmi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    gdi.DeleteObject(bmp); gdi.DeleteDC(mem_dc); u.ReleaseDC(hwnd, hwnd_dc)
    img = np.frombuffer(buf.raw, dtype=np.uint8).reshape((h, w, 4))
    return img[:, :, [2, 1, 0]]  # BGRA -> BGR

def is_garden_loaded_color(hwnd):
    """Detect garden via HSV color blobs in fixed ROI. Returns (bool, dict)."""
    import numpy as np
    import cv2
    frame = _grab_window_raw(hwnd)
    if frame is None:
        return False, {}
    rx, ry, rw, rh = COLOR_ROI
    if frame.shape[0] < ry + rh or frame.shape[1] < rx + rw:
        return False, {}
    roi = frame[ry:ry+rh, rx:rx+rw]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    kernel = np.ones((5, 5), np.uint8)
    results = {}
    for name, btn in COLOR_BUTTONS.items():
        h0, s0, v0 = btn["hsv"]
        ht, st, vt = btn["hue_tol"], btn["sat_tol"], btn["val_tol"]
        if name == "B":
            mask = cv2.inRange(hsv, np.array([0, s0-st, v0-vt]), np.array([10+ht, 255, 255])) | \
                   cv2.inRange(hsv, np.array([170, s0-st, v0-vt]), np.array([180, 255, 255]))
        else:
            lo = np.array([max(0, h0-ht), max(0, s0-st), max(0, v0-vt)])
            hi = np.array([min(180, h0+ht), min(255, s0+st), min(255, v0+vt)])
            mask = cv2.inRange(hsv, lo, hi)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        found = False
        for c in contours:
            area = cv2.contourArea(c)
            if area < 300 or area > 3000:
                continue
            M = cv2.moments(c)
            if M["m00"] == 0:
                continue
            cx, cy = int(M["m10"]/M["m00"]), int(M["m01"]/M["m00"])
            perim = cv2.arcLength(c, True)
            if perim == 0:
                continue
            circ = 4 * np.pi * area / (perim ** 2)
            if circ < 0.55:
                continue
            exp = btn["expected"]
            dist = ((cx-exp[0])**2 + (cy-exp[1])**2) ** 0.5
            if dist < 60:
                found = True
                break
        results[name] = found
    return sum(results.values()) >= 3, results

def navigate(h, fail_nav=False):
    c = COORDS["center"]
    time.sleep(2)
    # Click to reach Garden Select Menu
    clk(h, c["x"], c["y"]); time.sleep(3)
    clk(h, c["x"], c["y"]); time.sleep(3)
    clk(h, c["x"], c["y"]); time.sleep(4)
    if fail_nav:
        print("  [FAIL-NAV] Skipping S key — staying on main menu")
        return
    # At Garden Select Menu — press S directly to move to Load Garden
    # DO NOT click center first — that selects New Garden!
    u.SetForegroundWindow(h); time.sleep(0.5)
    key_s(h); time.sleep(0.5)
    # Click to select Load Garden
    clk(h, c["x"], c["y"]); time.sleep(3)
    clk(h, c["x"], c["y"]); time.sleep(3)

def dump_stacks(pid):
    """Attach Frida, dump all thread stacks, return list of frame strings."""
    session = frida.attach(pid)
    script = session.create_script(FRIDA_JS)
    script.load()
    threads = script.exports_sync.dump()
    session.detach()
    return threads

def detect_hang(threads):
    """Look for XamShowDirtyDiscErrorUI or guest wait patterns in stacks."""
    all_frames = []
    for t in threads:
        all_frames.extend(t["frames"])

    # Only error dialog patterns indicate hang
    # Guest wait addresses (0x7ff73c5eafef, 0x7ff73c7aefe8) appear in NORMAL loading too
    dirty_disc = any("XamShowDirtyDisc" in f for f in all_frames)
    dialog = any("DispatchDialog" in f or "MessageBoxDialog" in f for f in all_frames)

    return {
        "dirty_disc": dirty_disc,
        "dialog": dialog,
        "hang": dirty_disc or dialog,
    }

def force_foreground(h):
    """Force window to foreground using AttachThreadInput bypass."""
    if u.IsIconic(h):
        u.ShowWindow(h, 9)
    fg = u.GetForegroundWindow()
    fg_tid = u.GetWindowThreadProcessId(fg, None)
    our_tid = k.GetCurrentThreadId()
    u.AttachThreadInput(our_tid, fg_tid, True)
    u.BringWindowToTop(h)
    u.SetForegroundWindow(h)
    u.AttachThreadInput(our_tid, fg_tid, False)
    u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
    time.sleep(0.2)
    u.SetWindowPos(h, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
    return u.GetForegroundWindow() == h

def get_window_center(h):
    rect = wintypes.RECT()
    u.GetWindowRect(h, ctypes.byref(rect))
    return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2

def wait_for_menu(h, timeout=30):
    """Poll log for XamUserGetSigninState, then click immediately."""
    log_path = get_latest_log()
    if not log_path:
        return False, 0
    start = time.time()
    while time.time() - start < timeout:
        try:
            with open(log_path, "r", errors="replace") as f:
                if "XamUserGetSigninState" in f.read():
                    time.sleep(0.5)
                    force_foreground(h)
                    time.sleep(0.2)
                    u.mouse_event(2,0,0,0,0); time.sleep(0.05); u.mouse_event(4,0,0,0,0)
                    return True, time.time() - start
        except:
            pass
        time.sleep(0.2)
    return False, time.time() - start

def fast_clk(h, x, y):
    """Fast click using force_foreground."""
    if not force_foreground(h):
        return False
    u.SetCursorPos(x, y); time.sleep(0.1)
    if u.GetForegroundWindow() != h:
        return False
    u.mouse_event(2,0,0,0,0); time.sleep(0.05); u.mouse_event(4,0,0,0,0)
    time.sleep(0.1)
    return True

def fast_key_s(h):
    """Fast S key press."""
    if not force_foreground(h):
        return False
    u.keybd_event(0x53, 0, 0, 0)
    time.sleep(0.05)
    u.keybd_event(0x53, 0, 2, 0)
    time.sleep(0.1)
    return True

def fast_navigate(h):
    """S + 2 clicks to Load Garden. Uses window center."""
    cx, cy = get_window_center(h)
    force_foreground(h); time.sleep(0.3)
    fast_key_s(h)
    fast_clk(h, cx, cy); time.sleep(1)
    fast_clk(h, cx, cy); time.sleep(1)

def _opt(name, default=None):
    """Return the value following `name` in sys.argv, else default."""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 >= len(sys.argv):
            sys.exit(f"missing value for {name}")
        return sys.argv[i + 1]
    return default

def main():
    global GAME, PKG, BAK, MOD
    GAME = _opt("--game-dir", DEFAULT_GAME)
    PKG = _opt("--deploy", os.path.join(GAME, "assets", "Beta", "packages", "1150.pkg"))
    BAK = _opt("--backup", PKG + ".backup")
    MOD = PKG + ".modified"
    if not os.path.isdir(GAME):
        sys.exit(f"game dir not found: {GAME} — pass --game-dir "
                 f"<path/to/retip-1.13.0-windows>")
    mode = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "backup"
    if mode not in ("backup", "modified"):
        print("Usage: test_hang.py [backup|modified] [--pkg <path>] [--fail-nav] "
              "[--game-dir <path>] [--deploy <path>] [--backup <path>]")
        return
    fail_nav = "--fail-nav" in sys.argv

    # Support --pkg to specify a custom file
    custom_pkg = None
    if "--pkg" in sys.argv:
        idx = sys.argv.index("--pkg")
        if idx + 1 < len(sys.argv):
            custom_pkg = sys.argv[idx + 1]

    if mode == "backup":
        src = BAK
    elif custom_pkg:
        src = os.path.abspath(custom_pkg)
    else:
        src = MOD

    # Compute source hash
    import hashlib
    def sha256(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest().upper()

    src_hash = sha256(src) if os.path.exists(src) else "FILE NOT FOUND"
    bak_hash = sha256(BAK) if os.path.exists(BAK) else "NONE"

    print(f"========================================")
    print(f"  MODE:     {mode}")
    print(f"  SOURCE:   {src}")
    print(f"  SHA256:   {src_hash}")
    print(f"  BACKUP:   {BAK}")
    print(f"  BA HASH:  {bak_hash}")
    print(f"  DEPLOY TO: {PKG}")
    print(f"========================================")

    if not os.path.exists(src):
        print(f"ERROR: Source file not found: {src}")
        return

    kill()
    print(f"\nDeploying...")
    shutil.copy2(src, PKG)

    # Verify deployment
    dep_hash = sha256(PKG)
    if dep_hash == src_hash:
        print(f"  Deployed OK (hash matches source)")
    else:
        print(f"  WARNING: Deployed hash MISMATCH!")
        print(f"    Expected: {src_hash}")
        print(f"    Got:      {dep_hash}")
        return

    # Show what changed vs backup
    if mode != "backup" and os.path.exists(BAK):
        print(f"\nDiff vs backup PKG:")
        with open(src, "rb") as f1, open(BAK, "rb") as f2:
            d1, d2 = f1.read(), f2.read()
        if len(d1) == len(d2):
            diffs = sum(1 for a, b in zip(d1, d2) if a != b)
            print(f"  {diffs} bytes differ (same size {len(d1)})")
        else:
            print(f"  Size differs: {len(d1)} vs {len(d2)}")

    print(f"\nLaunching retip...")
    os.startfile(os.path.join(GAME, "retip.exe"))

    # Wait for window
    h = None
    for attempt in range(15):
        time.sleep(2)
        h = find()
        if h:
            break
    if not h:
        print("FAIL: window not found"); kill(); return

    # Move to right half FIRST
    u.SetWindowPos(h, 0, COORDS["window"]["x"], COORDS["window"]["y"],
                   COORDS["window"]["w"], COORDS["window"]["h"], 0x40)
    time.sleep(0.5)
    force_foreground(h)
    cx, cy = get_window_center(h)
    u.SetCursorPos(cx, cy)

    # Poll log for menu signal (replaces fixed 12s wait)
    print("  Polling for menu...")
    success, elapsed = wait_for_menu(h, timeout=30)
    if not success:
        print(f"  FAIL: no menu signal after {elapsed:.1f}s")
        kill(); return
    print(f"  Menu signal at {elapsed:.1f}s — click 1 done")

    # Attach Frida
    pid = get_pid()
    frida_session = None
    frida_logs = []
    if pid:
        try:
            frida_session = frida.attach(pid)
            frida_script = frida_session.create_script(FRIDA_JS)
            def on_frida_msg(msg, data):
                if msg["type"] == "send":
                    payload = msg["payload"]
                    frida_logs.append(payload)
                    print(f"    [FRIDA] {payload}")
                elif msg["type"] == "error":
                    print(f"    [FRIDA ERR] {msg.get('description', msg)}")
            frida_script.on("message", on_frida_msg)
            frida_script.load()
            print(f"  Frida attached to PID {pid}")
        except Exception as e:
            print(f"  Frida attach failed: {e}")

    # Click 2 and 3
    time.sleep(1)
    fast_clk(h, cx, cy)
    print("  Click 2 done")

    time.sleep(1)
    fast_clk(h, cx, cy)
    print("  Click 3 done")

    # Navigate: S + 2 clicks
    print("  Navigating...")
    fast_navigate(h)
    print("  Navigation done")

    # Poll for garden load: MULTIPLE SIGNALS required.
    # Signal 1: Frida CreateFileW (1150.pkg opened)
    # Signal 2: GetPixel at known screen coords (fast)
    # Signal 3: HSV color blob detection via PrintWindow (position-independent)
    # Require at least 2 of 3 signals to confirm garden.
    print("  Polling for garden (3 signals, need 2+)...")
    start = time.time()
    result = "TIMEOUT"
    pkg_signal = False
    while time.time() - start < 30:
        try:
            # Signal 1: Frida pkg open
            if frida_session and not pkg_signal:
                try:
                    pkg_info = frida_script.exports_sync.pkgopened()
                    if pkg_info and pkg_info.get("opened", False):
                        pkg_signal = True
                        print(f"  [SIG1] GARDEN_SIGNAL: 1150.pkg opened at {time.time()-start:.1f}s")
                except:
                    pass

            # Signal 2: GetPixel
            pixel_loaded, pixel_colors = is_garden_loaded()

            # Signal 3: HSV blob detection
            try:
                blob_loaded, blob_colors = is_garden_loaded_color(h)
            except Exception:
                blob_loaded, blob_colors = False, {}

            # Count active signals
            active = sum([pkg_signal, pixel_loaded, blob_loaded])

            if active >= 2:
                result = "PASS"
                p_str = "".join("Y" if v else "n" for v in pixel_colors.values())
                b_str = "".join("Y" if v else "n" for v in blob_colors.values())
                print(f"  CONFIRMED at {time.time()-start:.1f}s: {active}/3 signals active")
                print(f"    pkg={pkg_signal} pixel={pixel_loaded}({p_str}) color={blob_loaded}({b_str})")
                break

            # Periodic status
            elapsed = int(time.time() - start)
            if elapsed > 0 and elapsed % 5 == 0:
                print(f"    [{elapsed}s] signals={active}/3 pkg={pkg_signal} pixel={pixel_loaded} color={blob_loaded}")
        except Exception as e:
            pass
        time.sleep(0.3)

    # POST-GARDEN WATCH: garden loads BEFORE animation plays.
    # Poll for 15s: process death, log [CRASH], window unresponsiveness.
    if result == "PASS":
        print("  Watching for post-load crashes/hangs (15s)...")
        watch_start = time.time()
        fail_reason = ""
        fail_detail = ""
        while time.time() - watch_start < 15:
            # 1. Process death
            pid_check = get_pid()
            if not pid_check:
                fail_reason = "CRASH"
                fail_detail = "retip.exe died"
                break
            # 2. Log [CRASH]
            log_path = get_latest_log()
            if log_path:
                try:
                    status = check_log(log_path)
                    if status["crash"]:
                        fail_reason = "CRASH"
                        fail_detail = status["crash_detail"]
                        break
                except:
                    pass
            # 3. Window responsiveness (hang detection)
            h_check = find()
            if h_check:
                result_ping = ctypes.c_ulong(0)
                ret = u.SendMessageTimeoutW(h_check, 0, 0, 0, 0x2, 1000,
                                            ctypes.byref(result_ping))
                if ret == 0:
                    fail_reason = "HANG"
                    fail_detail = "window unresponsive (SendMessageTimeout)"
                    break
            else:
                h_crash = u.FindWindowW(None, "retip - crash report")
                if h_crash:
                    fail_reason = "CRASH"
                    fail_detail = "crash report dialog detected"
                    break
            time.sleep(0.5)
        if fail_reason:
            result = fail_reason
            print(f"  {fail_reason} DETECTED after garden load: {fail_detail}")
            # CAPTURE RUNTIME STATE via Frida before killing
            if frida_session:
                try:
                    print("\n  === RUNTIME STATE CAPTURE ===")
                    anims = frida_script.exports_sync.scan_anim()
                    if anims:
                        for idx, a in enumerate(anims):
                            print(f"\n  Animation #{idx} at {a['addr']}:")
                            print(f"    nch={a['nch']} nfr={a['nfr']} bpf={a['bpf']} sizeA={a['sizeA']} sizeB={a['sizeB']}")
                            print(f"    mode_off={a['mode_off']} base_off={a['base_off']} bc_off={a['bc_off']}")
                            print(f"    MODE: {a['mode_bytes']}")
                            print(f"    BASES: {a['bases']}")
                            print(f"    BC: {a['bitcounts']}")
                    else:
                        print("  No animation data found in memory")
                    threads = frida_script.exports_sync.dump()
                    for t in threads:
                        has_guest = any(
                            f.startswith("0x182") or f.startswith("0x183") or f.startswith("0x184")
                            for f in t["frames"])
                        if has_guest or t["state"] == "waiting":
                            print(f"\n  Thread {t['tid']} [{t['state']}]:")
                            for f in t["frames"][:10]:
                                if f.startswith("0x182") or f.startswith("0x183") or f.startswith("0x184"):
                                    print(f"    {f} <<< GUEST")
                                else:
                                    print(f"    {f}")
                except Exception as e:
                    print(f"  Frida capture failed: {e}")
        else:
            print(f"  No crash/hang in 15s post-load window")

    # Auto-close crash report dialog if present
    h_crash = u.FindWindowW(None, "retip - crash report")
    if h_crash:
        print("  Closing crash report dialog...")
        u.PostMessageW(h_crash, 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(1)
        # Force kill if still open
        if u.FindWindowW(None, "retip - crash report"):
            u.DestroyWindow(h_crash)

    # Check for crash/hang
    pid_check = get_pid()
    if result == "CRASH":
        print("RESULT: FAIL — CRASH (post-load)")
    elif result == "HANG":
        print("RESULT: FAIL — HANG (post-load, window unresponsive)")
    elif not pid_check:
        print("RESULT: FAIL — CRASH (retip.exe not running)")
    elif result == "TIMEOUT":
        # Report CreateFileW status
        if pkg_signal:
            print(f"\n  1150.pkg WAS opened but garden UI never appeared")
            print("  RESULT: FAIL — HANG (pkg loaded, garden never rendered)")
        elif frida_session:
            try:
                pkg_info = frida_script.exports_sync.pkgopened()
                if pkg_info and pkg_info.get("opened", False):
                    print(f"\n  1150.pkg WAS opened (file #{pkg_info.get('filecount','?')}) but detection timed out")
                else:
                    print(f"\n  1150.pkg NEVER opened ({pkg_info.get('filecount',0) if pkg_info else 0} total .pkg files opened)")
                    print("  Game never loaded garden data — navigation failed or hang during menu")
            except:
                pass
        # Scan memory for animation data the game loaded
        print("\n  === MEMORY SCAN (looking for animation data) ===")
        if frida_session:
            try:
                anims = frida_script.exports_sync.scan_anim()
                if anims:
                    for idx, a in enumerate(anims):
                        print(f"\n  Animation #{idx} at {a['addr']} (range {a['range_base']} size {a['range_size']} offset {a['offset']}):")
                        print(f"    nch={a['nch']} nfr={a['nfr']} bpf={a['bpf']} sizeA={a['sizeA']} sizeB={a['sizeB']}")
                        print(f"    mode_off={a['mode_off']} base_off={a['base_off']} bc_off={a['bc_off']}")
                        print(f"    MODE: {a['mode_bytes']}")
                        print(f"    BASES: {a['bases']}")
                        print(f"    BC: {a['bitcounts']}")
                else:
                    print("  No animation data found in memory")
            except Exception as e:
                print(f"  Memory scan failed: {e}")

        # Dump thread stacks via Frida to see exactly where we're stuck
        print("\n  === THREAD DUMP (Frida) ===")
        if frida_session:
            try:
                threads = frida_script.exports_sync.dump()
                # Show threads with GUEST REGION frames (0x182000000+) or unusual stacks
                for t in threads:
                    # Guest region = 0x182000000 - 0x185000000 (verified via check_guestmap.py)
                    has_guest_region = any(
                        f.startswith("0x182") or f.startswith("0x183") or f.startswith("0x184")
                        or "guest(0x82" in f or "guest(0x83" in f or "guest(0x84" in f
                        for f in t["frames"])
                    has_garbage = any("0x100000000" == f.strip() for f in t["frames"][:2])
                    # Only show threads with guest region or garbage frames
                    if has_guest_region or has_garbage:
                        print(f"\n  Thread {t['tid']} [{t['state']}]:")
                        for f in t["frames"][:12]:
                            if f.startswith("0x182") or f.startswith("0x183") or f.startswith("0x184"):
                                print(f"    {f} <<< GUEST-REGION")
                            elif "0x100000000" == f.strip():
                                print(f"    {f} <<< GUEST-BASE")
                            else:
                                print(f"    {f}")
            except Exception as e:
                print(f"  Frida dump failed: {e}")

        # Also check window responsiveness + log
        h_check = find()
        responsive = True
        if h_check:
            result_ping = ctypes.c_ulong(0)
            ret = u.SendMessageTimeoutW(h_check, 0, 0, 0, 0x2, 2000,
                                        ctypes.byref(result_ping))
            responsive = (ret != 0)
        log_path = get_latest_log()
        log_status = check_log(log_path) if log_path else {"dirty_disc": False, "crash": False, "crash_detail": ""}
        if log_status["crash"]:
            print(f"\nLog: {log_status['crash_detail'][:120]}")
            print("RESULT: FAIL — CRASH")
        elif log_status["dirty_disc"]:
            print("RESULT: FAIL — HANG (dirty disc)")
        elif not responsive:
            print("RESULT: FAIL — HANG (window unresponsive)")
        else:
            print("RESULT: FAIL — TIMEOUT (no garden, window responsive)")
    else:
        if result == "CRASH":
            print(f"RESULT: FAIL — CRASH (post-load)")
        elif result == "HANG":
            print(f"RESULT: FAIL — HANG (post-load)")
        else:
            print(f"RESULT: PASS (garden visible)")

    # Print Frida logs
    if frida_logs:
        print(f"\nFrida logs ({len(frida_logs)}):")
        for line in frida_logs:
            print(f"  {line}")
    if frida_session:
        try:
            frida_session.detach()
        except:
            pass

    kill()
    shutil.copy2(BAK, PKG)
    print("Done.")

if __name__ == "__main__":
    main()
