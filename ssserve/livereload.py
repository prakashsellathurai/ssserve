from __future__ import annotations

import os
import threading
import time


INJECT_SCRIPT = """<script>
(function(){var v=%d;
function c(){var x=new XMLHttpRequest();
x.open("GET","/__ssserve/lr-check?v="+v,true);
x.onload=function(){try{var d=JSON.parse(x.responseText);if(d.reload){location.reload()}else{v=d.version}}catch(e){}};
x.send()}
setInterval(c,1000)})()
</script>"""


class LiveReload:
    def __init__(self, root_dir: str, interval: float = 1.0) -> None:
        self.root_dir = os.path.abspath(root_dir)
        self.interval = interval
        self._version = 0
        self._snapshot: dict[str, float] = {}
        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        self._changed = threading.Event()

    @property
    def version(self) -> int:
        return self._version

    def _walk(self) -> dict[str, float]:
        snap: dict[str, float] = {}
        try:
            for dirpath, dirnames, filenames in os.walk(self.root_dir):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if fn.startswith("."):
                        continue
                    fp = os.path.join(dirpath, fn)
                    try:
                        snap[fp] = os.stat(fp).st_mtime
                    except OSError:
                        pass
        except OSError:
            pass
        return snap

    def _compare_snapshots(self, old: dict[str, float], new: dict[str, float]) -> bool:
        if set(old.keys()) != set(new.keys()):
            return True
        for key in old:
            if old[key] != new[key]:
                return True
        return False

    def _watch(self) -> None:
        self._snapshot = self._walk()
        while self._running.is_set():
            time.sleep(self.interval)
            if not self._running.is_set():
                break
            current = self._walk()
            if self._compare_snapshots(self._snapshot, current):
                self._snapshot = current
                self._version += 1
                self._changed.set()

    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def wait_for_change(self, timeout: float | None = None) -> bool:
        result = self._changed.wait(timeout=timeout)
        if result:
            self._changed.clear()
        return result

    @staticmethod
    def inject_script(html: str, version: int) -> str:
        script = INJECT_SCRIPT % version
        idx = html.rfind("</body>")
        if idx == -1:
            return html + "\n" + script
        return html[:idx] + script + "\n" + html[idx:]
