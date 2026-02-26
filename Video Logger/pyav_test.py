# minimal_open_test.py
import av
import time

DEVICE_NAME = "video=USB2.0 PC CAMERA"  # <-- try this first; if it fails, see suggestions below

def main():
    print("STEP 1: Enabling PyAV/FFmpeg debug logging...")
    av.logging.set_level(av.logging.VERBOSE)

    print(f"STEP 2: Attempting to open device: {DEVICE_NAME!r} via dshow (no options)")
    try:
        ic = av.open(DEVICE_NAME, format="dshow")
    except Exception as e:
        print("ERROR: Failed to open with no options.")
        print("EXCEPTION:", e)
        return
    print("STEP 3: Camera opened.")

    # Find video stream
    vstream = next((s for s in ic.streams if s.type == "video"), None)
    if vstream is None:
        print("ERROR: No video stream found.")
        ic.close()
        return
    print("STEP 4: Video stream acquired:", vstream)

    print("STEP 5: Reading frames for ~5 seconds...")
    start = time.time()
    frames = 0
    try:
        for packet in ic.demux(vstream):
            for frame in packet.decode():
                frames += 1
                print(f"  Frame {frames}: {frame.width}x{frame.height} fmt={frame.format.name}")
                if time.time() - start >= 5:
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("ERROR while reading/decoding frames:", e)

    print("STEP 6: Closing camera...")
    try:
        ic.close()
        print("STEP 7: Closed cleanly.")
    except Exception as e:
        print("ERROR closing camera:", e)

    print(f"FINAL: Total frames read: {frames}")

if __name__ == "__main__":
    main()
