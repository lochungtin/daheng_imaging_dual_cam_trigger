import os
import queue
import threading
import time

import cv2
import gxipy as gx
from PIL import Image
from termcolor import colored

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


def save_worker(save_queue, stop_event):
    """Dedicated thread: drain the save queue and write images to disk."""
    while not stop_event.is_set() or not save_queue.empty():
        try:
            cam_id, numpy_image, timestamp, save_dir = save_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
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


def preview_thread(cam, cam_id, stop_preview_event):
    """
    Temporarily switches the camera to continuous (free-run) mode, streams
    frames into an OpenCV window, then restores software trigger mode on exit.
    Close the window or press 'q' inside it to stop.
    """
    window_name = f"cam_{cam_id}"
    MAGENTA(f"[Preview] Cam {cam_id} starting. Press 'q' in the preview window to stop.")

    try:
        # --- Switch to continuous mode for free-running preview ---
        cam.TriggerMode.set(gx.GxSwitchEntry.OFF)
        cam.stream_on()

        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

        while not stop_preview_event.is_set():
            raw_image = cam.data_stream[0].get_image(timeout=100)
            if raw_image is None:
                continue

            numpy_image = raw_image.get_numpy_array()
            if numpy_image is None:
                continue

            # Convert to BGR for OpenCV display (handles mono and colour)
            if numpy_image.ndim == 2:
                display = cv2.cvtColor(numpy_image, cv2.COLOR_GRAY2BGR)
            else:
                display = cv2.cvtColor(numpy_image, cv2.COLOR_RGB2BGR)

            cv2.imshow(window_name, display)

            # 'q' inside the window stops this camera's preview
            if cv2.waitKey(1) & 0xFF == ord("q"):
                stop_preview_event.set()
                break

            # Also stop if the window was closed with the X button
            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                stop_preview_event.set()
                break

    except Exception as e:
        RED(f"[Preview] Cam {cam_id} error: {e}")
    finally:
        cam.stream_off()
        cv2.destroyWindow(window_name)

        # --- Restore software trigger mode ---
        cam.TriggerMode.set(gx.GxSwitchEntry.ON)
        cam.TriggerSource.set(gx.GxTriggerSourceEntry.SOFTWARE)

        MAGENTA(f"[Preview] Cam {cam_id} stopped. Software trigger restored.")


def start_preview(cam1, cam2):
    """
    Launch preview threads for both cameras. Blocks until both previews
    are closed, then flushes buffers to prepare for triggered acquisition.
    """
    stop_preview_evt = threading.Event()

    p1 = threading.Thread(
        target=preview_thread,
        args=(cam1, 1, stop_preview_evt),
        daemon=True,
    )
    p2 = threading.Thread(
        target=preview_thread,
        args=(cam2, 2, stop_preview_evt),
        daemon=True,
    )

    p1.start()
    p2.start()

    MAGENTA("[Preview] Both cameras live. Close either window or press 'q' to stop.")

    p1.join()
    p2.join()

    MAGENTA("[Preview] Preview closed. Flushing buffers before resuming acquisition...")

    # Re-flush after preview to clear any frames left in the buffer
    for cam, cam_id in [(cam1, 1), (cam2, 2)]:
        cam.stream_on()
        flush_buffer(cam, cam_id)
        cam.stream_off()

    MAGENTA("[Preview] Ready for acquisition.")


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
    device_manager = gx.DeviceManager()
    dev_num, dev_info_list = device_manager.update_device_list()

    if dev_num < 2:
        RED(f"Error: Found only {dev_num} camera(s). Two cameras are required.")
        return

    WHITE(f"Found {dev_num} cameras. Opening the first two devices...")
    WHITE(f"Session directory: {SESSION_DIR}")

    cam1 = device_manager.open_device_by_sn(dev_info_list[0]["sn"])
    cam2 = device_manager.open_device_by_sn(dev_info_list[1]["sn"])

    # Initialise save dir attributes to singles by default
    cam1.current_save_dir = SINGLES_CAM1
    cam2.current_save_dir = SINGLES_CAM2

    for i, cam in enumerate([cam1, cam2], start=1):
        cam.TriggerMode.set(gx.GxSwitchEntry.ON)
        cam.TriggerSource.set(gx.GxTriggerSourceEntry.SOFTWARE)
        WHITE(f"Camera {i} configured for Software Trigger.")

    trigger_evt1 = threading.Event()
    trigger_evt2 = threading.Event()
    stop_evt = threading.Event()

    save_queue = queue.Queue()

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
    t_save = threading.Thread(
        target=save_worker,
        args=(save_queue, stop_evt),
        daemon=True,
    )

    t1.start()
    t2.start()
    t_save.start()

    # Give capture threads time to complete buffer flush before accepting input
    time.sleep(1.0)

    loop_thread = None
    loop_counter = 0

    WHITE("\nCommands:")
    WHITE("  [Enter]  — single trigger")
    WHITE("  l        — start looped acquisition")
    WHITE("  p        — preview camera streams")
    WHITE("  q        — quit")

    try:
        while True:
            user_input = input("\n> ").strip().lower()

            if user_input == "q":
                break

            elif user_input == "p":
                if loop_thread and loop_thread.is_alive():
                    RED("[Preview] Cannot preview while a loop is running.")
                    continue
                # Pause capture threads by setting stop_evt, join, then relaunch after preview
                stop_evt.set()
                t1.join()
                t2.join()
                stop_evt.clear()

                start_preview(cam1, cam2)

                # Relaunch capture threads after preview closes
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
        stop_evt.set()
        if loop_thread:
            loop_thread.join(timeout=5)
        t1.join()
        t2.join()
        save_queue.join()
        WHITE("[Save  ] All queued images have been written to disk.")
        t_save.join()
        cam1.close_device()
        cam2.close_device()
        WHITE("Cameras closed safely. Program exited.")


if __name__ == "__main__":
    main()