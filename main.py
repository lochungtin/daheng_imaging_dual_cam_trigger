import os
import queue
import threading
import time

import gxipy as gx
from PIL import Image
from termcolor import colored

SESSION_DIR = time.strftime("out/acquisition_%Y%m%d_%H%M")
SINGLES_CAM1 = os.path.join(SESSION_DIR, "singles", "cam_1")
SINGLES_CAM2 = os.path.join(SESSION_DIR, "singles", "cam_2")

for d in [SINGLES_CAM1, SINGLES_CAM2]:
    os.makedirs(d, exist_ok=True)

WHITE = lambda x: print(x)
CYAN = lambda x: print(colored(x, color="cyan"))  # capture thread
YELLOW = lambda x: print(colored(x, color="yellow"))  # save worker
GREEN = lambda x: print(colored(x, color="green"))  # loop controller
RED = lambda x: print(colored(x, color="red"))  # errors


def make_loop_dirs(loop_n):
    cam1_dir = os.path.join(SESSION_DIR, "loops", "cam_1", f"loop_{loop_n}")
    cam2_dir = os.path.join(SESSION_DIR, "loops", "cam_2", f"loop_{loop_n}")
    os.makedirs(cam1_dir, exist_ok=True)
    os.makedirs(cam2_dir, exist_ok=True)
    return cam1_dir, cam2_dir


def flush_buffer(cam, cam_id):
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
            YELLOW(
                f"[Save  ] Cam {cam_id} buffer flushed. Queue depth: {save_queue.qsize()}."
            )


def capture_thread(cam, cam_id, trigger_event, stop_event, save_queue):
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


def fire_trigger(cam1, cam2, trigger_evt1, trigger_evt2, dir1, dir2):
    cam1.current_save_dir = dir1
    cam2.current_save_dir = dir2
    trigger_evt1.set()
    trigger_evt2.set()
    cam1.TriggerSoftware.send_command()
    cam2.TriggerSoftware.send_command()


def loop_acquisition(
    cam1, cam2, trigger_evt1, trigger_evt2, stop_evt, interval_ms, count, loop_n
):
    cam1_dir, cam2_dir = make_loop_dirs(loop_n)
    interval_s = interval_ms / 1000.0

    GREEN(f"[Loop {loop_n}] Starting: {count} acquisition(s) every {interval_ms} ms.")
    GREEN(
        f"[Loop {loop_n}] Saving to: loops/cam_1/loop_{loop_n} & loops/cam_2/loop_{loop_n}"
    )

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

    time.sleep(1.0)

    loop_thread = None
    loop_counter = 0

    WHITE("\nCommands:")
    WHITE("  [Enter]  — single trigger")
    WHITE("  l        — start looped acquisition")
    WHITE("  q        — quit")

    try:
        while True:
            user_input = input("\n> ").strip().lower()

            if user_input == "q":
                break

            elif user_input == "l":
                if loop_thread and loop_thread.is_alive():
                    RED(
                        "A loop is already running. Wait for it to finish or press 'q' to quit."
                    )
                    continue

                interval_ms = prompt_int(
                    "  Interval between acquisitions (ms)", min_val=1, default=1000
                )
                count = prompt_int("  Number of acquisitions", min_val=1, default=10)

                loop_counter += 1
                loop_thread = threading.Thread(
                    target=loop_acquisition,
                    args=(
                        cam1,
                        cam2,
                        trigger_evt1,
                        trigger_evt2,
                        stop_evt,
                        interval_ms,
                        count,
                        loop_counter,
                    ),
                    daemon=True,
                )
                loop_thread.start()

            elif user_input == "":
                WHITE("Triggering both cameras (single)...")
                fire_trigger(
                    cam1,
                    cam2,
                    trigger_evt1,
                    trigger_evt2,
                    SINGLES_CAM1,
                    SINGLES_CAM2,
                )

            else:
                RED("Unknown command. Use [Enter], 'l', or 'q'.")

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
