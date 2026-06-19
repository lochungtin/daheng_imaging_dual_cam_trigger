import os
import queue
import threading
import time

import cv2
import gxipy as gx
from PIL import Image
from termcolor import colored
import json

CONFIG_PATH = "config.json"

def load_config():
    """Load config.json from the project root. Returns empty dict if missing or malformed."""
    if not os.path.exists(CONFIG_PATH):
        WHITE(f"[Config] No config file found at {CONFIG_PATH}, using defaults.")
        return {}
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
        WHITE(f"[Config] Loaded: {CONFIG_PATH}")
        return config
    except Exception as e:
        RED(f"[Config] Failed to parse {CONFIG_PATH}: {e}. Using defaults.")
        return {}


def get_rotation(config, cam_id):
    """Return the cv2 rotation code for a given camera, or None if no rotation set."""
    key = f"cam_{cam_id}"
    degrees = config.get(key, {}).get("rotation", 0)
    return {
        90:  cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }.get(degrees, None)


def apply_rotation(image, rotation_code):
    """Rotate a numpy image if a rotation code is set."""
    if rotation_code is None:
        return image
    return cv2.rotate(image, rotation_code)

# --- Session directory setup ---
SESSION_DIR = time.strftime("out/acquisition_%Y%m%d_%H%M")
SINGLES_CAM1 = os.path.join(SESSION_DIR, "singles", "cam_1")
SINGLES_CAM2 = os.path.join(SESSION_DIR, "singles", "cam_2")

for d in [SINGLES_CAM1, SINGLES_CAM2]:
    os.makedirs(d, exist_ok=True)

# --- Colored print helpers ---
WHITE  = lambda x: print(x)
CYAN   = lambda x: print(colored(x, color="cyan"))    # capture thread
YELLOW = lambda x: print(colored(x, color="yellow"))  # save worker
GREEN  = lambda x: print(colored(x, color="green"))   # loop controller
MAGENTA = lambda x: print(colored(x, color="magenta")) # preview
RED    = lambda x: print(colored(x, color="red"))      # errors


def make_loop_dirs(loop_n):
    """Create and return (cam1_dir, cam2_dir) for a new loop run."""
    cam1_dir = os.path.join(SESSION_DIR, "loops", "cam_1", f"loop_{loop_n}")
    cam2_dir = os.path.join(SESSION_DIR, "loops", "cam_2", f"loop_{loop_n}")
    os.makedirs(cam1_dir, exist_ok=True)
    os.makedirs(cam2_dir, exist_ok=True)
    return cam1_dir, cam2_dir


def flush_buffer(cam, cam_id):
    """Drain stale pre-filled SDK buffer frames after stream_on."""
    flushed = 0
    while True:
        try:
            cam.TriggerSoftware.send_command()
            stale = cam.data_stream[0].get_image(timeout=1)
            if stale is None:
                break
            flushed += 1
        except Exception:
            break
    CYAN(f"[Cam {cam_id}] Buffer flushed ({flushed} stale frame(s) discarded).")


def save_worker(save_queue, stop_event, config):
    """Dedicated thread: drain the save queue and write images to disk."""
    while not stop_event.is_set() or not save_queue.empty():
        try:
            cam_id, numpy_image, timestamp, save_dir = save_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            rotation_code = get_rotation(config, cam_id)

            # Rotate via cv2 (faster than PIL), then convert back to PIL for saving
            if rotation_code is not None:
                numpy_image = cv2.rotate(numpy_image, rotation_code)

            pil_image = Image.fromarray(numpy_image)
            file_name = os.path.join(save_dir, f"cam_{cam_id}_{timestamp}.tiff")
            pil_image.save(file_name, format="TIFF", compression="raw")
            YELLOW(f"[Save  ] Cam {cam_id} image written: {file_name}.")
        except Exception as e:
            RED(f"[Save  ] Cam {cam_id} save error: {e}")
        finally:
            save_queue.task_done()
            YELLOW(f"[Save  ] Cam {cam_id} buffer flushed. Queue depth: {save_queue.qsize()}.")


def capture_thread(cam, cam_id, trigger_event, stop_event, save_queue):
    """Capture thread: flush stale buffer, then grab and queue images on trigger."""
    try:
        cam.stream_on()
        flush_buffer(cam, cam_id)
        CYAN(f"[Cam {cam_id}] Stream started. Waiting for trigger...")

        while not stop_event.is_set():
            triggered = trigger_event.wait(timeout=0.1)
            if not triggered:
                continue

            trigger_event.clear()

            raw_image = cam.data_stream[0].get_image(timeout=10)
            if raw_image is None:
                RED(f"[Cam {cam_id}] Error: Failed to catch triggered image.")
                continue

            numpy_image = raw_image.get_numpy_array().copy()
            timestamp = time.time_ns()
            save_queue.put((cam_id, numpy_image, timestamp, cam.current_save_dir))
            CYAN(f"[Cam {cam_id}] Image captured and queued → {cam.current_save_dir}")

    except Exception as e:
        RED(f"[Cam {cam_id}] Exception occurred: {e}")
    finally:
        cam.stream_off()
        CYAN(f"[Cam {cam_id}] Stream stopped.")


def start_capture_threads(cam1, cam2, trigger_evt1, trigger_evt2, save_queue):
    """Spin up a fresh stop event, two capture threads, return all three."""
    stop_evt = threading.Event()

    t1 = threading.Thread(
        target=capture_thread,
        args=(cam1, 1, trigger_evt1, stop_evt, save_queue),
        daemon=True,
    )
    t2 = threading.Thread(
        target=capture_thread,
        args=(cam2, 2, trigger_evt2, stop_evt, save_queue),
        daemon=True,
    )
    t1.start()
    t2.start()
    return t1, t2, stop_evt


def stop_capture_threads(t1, t2, stop_evt):
    """Signal and join the current capture threads."""
    stop_evt.set()
    t1.join()
    t2.join()


def load_overlay(session_dir, dir_arg, cam_id):
    """
    Resolve a user-supplied dir_arg ("singles" or "loop_N") to an actual
    path, find the image with the largest timestamp for the given cam_id,
    and return it as a BGR numpy array, or None if nothing is found.
    """
    if dir_arg == "singles":
        search_dir = os.path.join(session_dir, "singles", f"cam_{cam_id}")
    else:
        # expect format "loop_N"
        search_dir = os.path.join(session_dir, "loops", f"cam_{cam_id}", dir_arg)

    if not os.path.isdir(search_dir):
        RED(f"[Preview] Overlay path not found: {search_dir}")
        return None

    tiffs = [f for f in os.listdir(search_dir) if f.endswith(".tiff")]
    if not tiffs:
        RED(f"[Preview] No images found in: {search_dir}")
        return None

    # Filenames are cam_{id}_{timestamp}.tiff — sort by timestamp numerically
    def extract_timestamp(fname):
        try:
            return int(fname.replace(f"cam_{cam_id}_", "").replace(".tiff", ""))
        except ValueError:
            return 0

    latest = max(tiffs, key=extract_timestamp)
    path = os.path.join(search_dir, latest)

    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        RED(f"[Preview] Failed to load overlay image: {path}")
        return None

    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    MAGENTA(f"[Preview] Cam {cam_id} overlay loaded: {latest}")
    return img

def apply_tint(image, b, g, r):
    """
    Multiply each BGR channel by a scalar in [0.0, 1.0] to tint the image.
    b, g, r are the channel multipliers.
    """
    tinted = image.astype("float32")
    tinted[:, :, 0] *= b
    tinted[:, :, 1] *= g
    tinted[:, :, 2] *= r
    return tinted.clip(0, 255).astype("uint8")


def blend_overlay(frame, overlay):
    """
    Resize overlay to match frame if needed.
    Live frame is tinted magenta (full R, no G, full B).
    Overlay is tinted green (no R, full G, no B).
    Blended at equal 0.5 weight.
    """
    if overlay.shape[:2] != frame.shape[:2]:
        overlay = cv2.resize(overlay, (frame.shape[1], frame.shape[0]))

    magenta_frame = apply_tint(frame,   b=1.0, g=0.0, r=1.0)
    green_overlay = apply_tint(overlay, b=0.0, g=1.0, r=0.0)

    return cv2.addWeighted(magenta_frame, 1.0, green_overlay, 0.5, 0)

def preview_capture_thread(cam, cam_id, frame_buffer, frame_lock, stop_preview_event, config):
    """
    Grab frames continuously in free-run mode and push the latest into
    frame_buffer. Display is handled on the main thread.
    """
    rotation_code = get_rotation(config, cam_id)
    try:
        cam.TriggerMode.set(gx.GxSwitchEntry.OFF)
        cam.stream_on()
        MAGENTA(f"[Preview] Cam {cam_id} capture started.")

        while not stop_preview_event.is_set():
            raw_image = cam.data_stream[0].get_image(timeout=100)
            if raw_image is None:
                continue

            numpy_image = raw_image.get_numpy_array()
            if numpy_image is None:
                continue

            if numpy_image.ndim == 2:
                display = cv2.cvtColor(numpy_image, cv2.COLOR_GRAY2BGR)
            else:
                display = cv2.cvtColor(numpy_image, cv2.COLOR_RGB2BGR)

            display = apply_rotation(display, rotation_code)

            with frame_lock:
                frame_buffer[cam_id] = display

    except Exception as e:
        RED(f"[Preview] Cam {cam_id} capture error: {e}")
    finally:
        cam.stream_off()
        cam.TriggerMode.set(gx.GxSwitchEntry.ON)
        cam.TriggerSource.set(gx.GxTriggerSourceEntry.SOFTWARE)
        MAGENTA(f"[Preview] Cam {cam_id} stopped. Software trigger restored.")


def start_preview(cam1, cam2, config, dir_arg=None):
    overlay1 = load_overlay(SESSION_DIR, dir_arg, 1) if dir_arg else None
    overlay2 = load_overlay(SESSION_DIR, dir_arg, 2) if dir_arg else None

    frame_buffer = {}
    frame_lock = threading.Lock()
    stop_preview_evt = threading.Event()

    p1 = threading.Thread(
        target=preview_capture_thread,
        args=(cam1, 1, frame_buffer, frame_lock, stop_preview_evt, config),
        daemon=True,
    )
    p2 = threading.Thread(
        target=preview_capture_thread,
        args=(cam2, 2, frame_buffer, frame_lock, stop_preview_evt, config),
        daemon=True,
    )

    p1.start()
    p2.start()

    MAGENTA("[Preview] Both cameras live. Press 'q' or close a window to stop.")
    if dir_arg:
        MAGENTA(f"[Preview] Overlay source: {dir_arg}")

    cv2.namedWindow("cam_1", cv2.WINDOW_NORMAL)
    cv2.namedWindow("cam_2", cv2.WINDOW_NORMAL)

    overlays = {1: overlay1, 2: overlay2}

    while not stop_preview_evt.is_set():
        with frame_lock:
            frames = dict(frame_buffer)

        for cam_id, frame in frames.items():
            if overlays.get(cam_id) is not None:
                display = blend_overlay(frame, overlays[cam_id])
            else:
                display = apply_tint(frame, b=1.0, g=0.0, r=1.0)
            cv2.imshow(f"cam_{cam_id}", display)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            stop_preview_evt.set()
            break

        for win in ["cam_1", "cam_2"]:
            if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
                stop_preview_evt.set()
                break

    cv2.destroyAllWindows()
    p1.join()
    p2.join()

    MAGENTA("[Preview] Preview closed. Flushing buffers before resuming acquisition...")
    for cam, cam_id in [(cam1, 1), (cam2, 2)]:
        cam.stream_on()
        flush_buffer(cam, cam_id)
        cam.stream_off()
    MAGENTA("[Preview] Ready for acquisition.")


def blend_overlay(frame, overlay):
    """
    Resize overlay to match frame dimensions if needed, then alpha-blend
    at 0.5 opacity onto the live frame.
    """
    if overlay.shape[:2] != frame.shape[:2]:
        overlay = cv2.resize(overlay, (frame.shape[1], frame.shape[0]))
    return cv2.addWeighted(frame, 1.0, overlay, 0.5, 0)


def fire_trigger(cam1, cam2, trigger_evt1, trigger_evt2, dir1, dir2):
    """Attach destination directory to each camera, then fire both triggers."""
    cam1.current_save_dir = dir1
    cam2.current_save_dir = dir2
    trigger_evt1.set()
    trigger_evt2.set()
    cam1.TriggerSoftware.send_command()
    cam2.TriggerSoftware.send_command()


def loop_acquisition(
    cam1, cam2, trigger_evt1, trigger_evt2, stop_evt, interval_ms, count, loop_n
):
    """Run a looped acquisition sequence in a background thread."""
    cam1_dir, cam2_dir = make_loop_dirs(loop_n)
    interval_s = interval_ms / 1000.0

    GREEN(f"[Loop {loop_n}] Starting: {count} acquisition(s) every {interval_ms} ms.")
    GREEN(f"[Loop {loop_n}] Saving to: loops/cam_1/loop_{loop_n} & loops/cam_2/loop_{loop_n}")

    for i in range(count):
        if stop_evt.is_set():
            GREEN(f"[Loop {loop_n}] Aborted by stop signal.")
            break

        GREEN(f"[Loop {loop_n}] Acquisition {i + 1}/{count}...")
        fire_trigger(cam1, cam2, trigger_evt1, trigger_evt2, cam1_dir, cam2_dir)

        if i < count - 1:
            deadline = time.monotonic() + interval_s
            while time.monotonic() < deadline:
                if stop_evt.is_set():
                    RED(f"[Loop {loop_n}] Aborted during interval sleep.")
                    return
                time.sleep(0.05)

    GREEN(f"[Loop {loop_n}] Sequence complete.")


def prompt_int(prompt, min_val=1, default=None):
    """Prompt the user for a positive integer with optional default."""
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if raw == "" and default is not None:
            return default
        try:
            val = int(raw)
            if val >= min_val:
                return val
            WHITE(f"  Please enter a value >= {min_val}.")
        except ValueError:
            WHITE("  Invalid input — please enter an integer.")


def main():
    config = load_config()

    device_manager = gx.DeviceManager()
    dev_num, dev_info_list = device_manager.update_device_list()

    if dev_num < 2:
        RED(f"Error: Found only {dev_num} camera(s). Two cameras are required.")
        return

    WHITE(f"Found {dev_num} cameras. Opening the first two devices...")
    WHITE(f"Session directory: {SESSION_DIR}")

    cam1 = device_manager.open_device_by_sn(dev_info_list[0]["sn"])
    cam2 = device_manager.open_device_by_sn(dev_info_list[1]["sn"])

    cam1.current_save_dir = SINGLES_CAM1
    cam2.current_save_dir = SINGLES_CAM2

    for i, cam in enumerate([cam1, cam2], start=1):
        cam.TriggerMode.set(gx.GxSwitchEntry.ON)
        cam.TriggerSource.set(gx.GxTriggerSourceEntry.SOFTWARE)
        WHITE(f"Camera {i} configured for Software Trigger.")

    trigger_evt1 = threading.Event()
    trigger_evt2 = threading.Event()

    save_queue = queue.Queue()

    save_stop_evt = threading.Event()
    t_save = threading.Thread(
        target=save_worker,
        args=(save_queue, save_stop_evt, config),
        daemon=True,
    )
    t_save.start()

    t1, t2, stop_evt = start_capture_threads(
        cam1, cam2, trigger_evt1, trigger_evt2, save_queue
    )

    time.sleep(1.0)

    loop_thread = None
    loop_counter = 0

    WHITE("\nCommands:")
    WHITE("  [Enter]      — single trigger")
    WHITE("  l            — start looped acquisition")
    WHITE("  p            — preview camera streams")
    WHITE("  p singles    — preview with latest single-shot overlay")
    WHITE("  p loop_N     — preview with latest image from loop N as overlay")
    WHITE("  q            — quit")

    try:
        while True:
            user_input = input("\n> ").strip().lower()

            if user_input == "q":
                break

            elif user_input.startswith("p"):
                if loop_thread and loop_thread.is_alive():
                    RED("[Preview] Cannot preview while a loop is running.")
                    continue

                parts = user_input.split()
                dir_arg = parts[1] if len(parts) > 1 else None

                if dir_arg is not None:
                    valid = dir_arg == "singles" or (
                        dir_arg.startswith("loop_") and dir_arg[5:].isdigit()
                    )
                    if not valid:
                        RED(f"[Preview] Invalid directory '{dir_arg}'. Use 'singles' or 'loop_N'.")
                        continue

                stop_capture_threads(t1, t2, stop_evt)
                start_preview(cam1, cam2, config, dir_arg=dir_arg)
                t1, t2, stop_evt = start_capture_threads(
                    cam1, cam2, trigger_evt1, trigger_evt2, save_queue
                )
                time.sleep(1.0)

            elif user_input == "l":
                if loop_thread and loop_thread.is_alive():
                    RED("A loop is already running. Wait for it to finish or press 'q' to quit.")
                    continue

                interval_ms = prompt_int(
                    "  Interval between acquisitions (ms)", min_val=1, default=1000
                )
                count = prompt_int("  Number of acquisitions", min_val=1, default=10)

                loop_counter += 1
                loop_thread = threading.Thread(
                    target=loop_acquisition,
                    args=(
                        cam1, cam2,
                        trigger_evt1, trigger_evt2,
                        stop_evt,
                        interval_ms, count,
                        loop_counter,
                    ),
                    daemon=True,
                )
                loop_thread.start()

            elif user_input == "":
                WHITE("Triggering both cameras (single)...")
                fire_trigger(
                    cam1, cam2,
                    trigger_evt1, trigger_evt2,
                    SINGLES_CAM1, SINGLES_CAM2,
                )

            else:
                RED("Unknown command. Use [Enter], 'l', 'p', or 'q'.")

    except KeyboardInterrupt:
        RED("\nInterrupted — shutting down...")
    finally:
        stop_capture_threads(t1, t2, stop_evt)
        if loop_thread:
            loop_thread.join(timeout=5)
        save_stop_evt.set()
        save_queue.join()
        WHITE("[Save  ] All queued images have been written to disk.")
        t_save.join()
        cam1.close_device()
        cam2.close_device()
        WHITE("Cameras closed safely. Program exited.")


if __name__ == "__main__":
    main()