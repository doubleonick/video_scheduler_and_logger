import threading
import time
import keyboard  # pip install keyboard

start_flag = False
stop_flag = False

def on_r_press(event):
    global start_flag, stop_flag

    if not start_flag:
        print("[KEY] Start detected ('R')")
        start_flag = True
    else:
        print("[KEY] Stop detected ('R')")
        stop_flag = True

def main():
    global start_flag, stop_flag

    # register callback
    keyboard.on_press_key("r", on_r_press)

    print("Press R to start recording...")
    while not start_flag:
        time.sleep(0.05)

    print(">> RECORDING STARTED")
    start_time = time.time()

    # Simulated recording loop
    while not stop_flag and time.time() - start_time < 10:
        print(" recording frame...")
        time.sleep(0.5)

    print(">> RECORDING STOPPED")

if __name__ == "__main__":
    main()
