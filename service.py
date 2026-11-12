#!/usr/bin/env python3
"""
Windows service wrapper for ArtechCMS.

Runs app.py as a child process so the whole Flask app + scheduler run in the
background with no console/login. If app.py exits (e.g. the online-update
restart, exit code 3), the service relaunches it automatically.

Install/manage with install-service.bat / uninstall-service.bat, or manually:
    python service.py --startup auto install
    python service.py start | stop | restart | remove
"""
import os
import sys
import subprocess

import win32serviceutil
import win32service
import win32event

BASE = os.path.dirname(os.path.abspath(__file__))


def _python():
    """Prefer the venv's windowless python so no console window appears."""
    for p in (os.path.join(BASE, ".venv", "Scripts", "pythonw.exe"),
              os.path.join(BASE, ".venv", "Scripts", "python.exe")):
        if os.path.exists(p):
            return p
    return sys.executable


class ArtechCMSService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ArtechCMS"
    _svc_display_name_ = "ArtechCMS - Quan ly ghi am Artech"
    _svc_description_ = ("Tu dong tai file ghi am tu cac may ghi am Artech va "
                         "phuc vu giao dien web (http://localhost:8787).")

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.proc = None
        self.running = True

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.running = False
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        py = _python()
        app = os.path.join(BASE, "app.py")
        flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
        while self.running:
            try:
                self.proc = subprocess.Popen([py, app], cwd=BASE, creationflags=flags)
            except Exception:
                # back off, then retry
                if win32event.WaitForSingleObject(self.stop_event, 5000) == win32event.WAIT_OBJECT_0:
                    break
                continue
            # wait until a stop is requested or the child exits (update -> relaunch)
            while self.running:
                if win32event.WaitForSingleObject(self.stop_event, 2000) == win32event.WAIT_OBJECT_0:
                    self.running = False
                    break
                if self.proc.poll() is not None:
                    break                                      # child exited -> outer loop relaunches
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(ArtechCMSService)
