import av
import time
import cv2

DEVICE_NAME = "video=USB2.0 PC CAMERA"  # <-- leave as-is if it matched your working test

def main():
    print("STEP 1: Enabling PyAV/FFmpeg debug logging...")
    av.logging.set_level(av.logging.VERBOSE)

    print(f"STEP 2: Opening device {DEVICE_NAME!r} via dshow...")
    try:
        # Start permissive: no options, since your device opened fine in that mode.
        ic = av.open(DEVICE_NAME, format="dshow")
    except Exception as e:
        print("ERROR: Failed to open camera.")
        print("EXCEPTION:", e)
        return
    print("STEP 3: Camera opened.")

    # Get the video stream
    vstream = next((s for s in ic.streams if s.type == "video"), None)
    if vstream is None:
        print("ERROR: No video stream found.")
        ic.close()
        return
    print("STEP 4: Video stream acquired:", vstream)

    print("STEP 5: Creating preview window...")
    cv2.namedWindow("Preview (press Q to quit)", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Preview (press Q to quit)", 800, 600)

    print("STEP 6: Reading/Displaying frames for ~5 seconds...")
    start = time.time()
    frames = 0

    try:
        for packet in ic.demux(vstream):
            for frame in packet.decode():
                frames += 1

                # Convert PyAV frame to numpy BGR for OpenCV display
                # Your camera outputs yuyv422; converting to bgr24 is efficient and standard for cv2.imshow
                bgr = frame.to_ndarray(format="bgr24")

                # Show the frame
                cv2.imshow("Preview (press Q to quit)", bgr)

                # Handle key events; press 'q' or 'Q' to exit early
                key = cv2.waitKey(1) & 0xFF
                if key in (ord('q'), ord('Q')):
                    print("STEP 7: 'Q' pressed, exiting preview loop.")
                    raise KeyboardInterrupt

                # Stop after ~5 seconds
                if time.time() - start >= 5:
                    print("STEP 7: 5 seconds reached, exiting preview loop.")
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("ERROR while decoding/displaying frames:", e)

    print("STEP 8: Cleaning up (closing camera & window)...")
    try:
        ic.close()
    except Exception as e:
        print("WARN: Error closing camera:", e)
    cv2.destroyAllWindows()

    print(f"FINAL: Total frames displayed: {frames}")

if __name__ == "__main__":
    main()
