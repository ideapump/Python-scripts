
import time
import pyautogui
from pynput import keyboard, mouse

# SETTINGS
IDLE_SECONDS = 240  # 4 minutes

last_activity = time.monotonic()

pyautogui.FAILSAFE = True


# Record keyboard activity
def on_key_activity(key):
    global last_activity
    last_activity = time.monotonic()


# Record mouse movement and clicks
def on_mouse_activity(*args):
    global last_activity
    last_activity = time.monotonic()


def keep_display_active():
    print("Workflow display helper running.")
    print("Waiting for 4 minutes of inactivity.")
    print("Press Ctrl+C to stop.")

    global last_activity

    while True:
        time.sleep(1)

        idle_time = time.monotonic() - last_activity

        if idle_time >= IDLE_SECONDS:

            print("Idle detected. Performing activity.")

            # Move mouse one pixel and return
            x, y = pyautogui.position()

            pyautogui.moveTo(x + 1, y, duration=0.1)
            pyautogui.moveTo(x, y, duration=0.1)

            # Briefly tap and release Shift
            pyautogui.keyDown("shift")
            time.sleep(0.1)
            pyautogui.keyUp("shift")

            # Reset timer
            last_activity = time.monotonic()

            print("Activity completed.")


# Start global input listeners
keyboard_listener = keyboard.Listener(
    on_press=on_key_activity
)

mouse_listener = mouse.Listener(
    on_move=on_mouse_activity,
    on_click=on_mouse_activity,
    on_scroll=on_mouse_activity
)

keyboard_listener.start()
mouse_listener.start()

try:
    keep_display_active()

except KeyboardInterrupt:
    print("\nStopping workflow display helper.")

finally:
    keyboard_listener.stop()
    mouse_listener.stop()
    pyautogui.keyUp("shift")
