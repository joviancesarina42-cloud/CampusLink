"""Bounded subprocess logs; drain output even if the log volume is unavailable."""
from __future__ import annotations

import logging.handlers
import subprocess
import threading
from pathlib import Path


class BinaryRotatingLog(logging.handlers.RotatingFileHandler):
    def append(self, data):
        # Rotation is based on bytes, including output without line breaks.
        if self.stream is None:
            self.stream = self._open()
        if self.stream.tell() + len(data) > self.maxBytes:
            self.doRollover()
        self.stream.buffer.write(data)
        self.stream.flush()


def drain_log(pipe, path, max_bytes=4 * 1024 * 1024):
    handler = None
    try:
        path = Path(path)
        if path.exists() and path.stat().st_size > max_bytes:
            with path.open('rb+') as existing:
                existing.seek(-max_bytes, 2)
                tail = existing.read(max_bytes)
                existing.seek(0)
                existing.write(tail)
                existing.truncate()
        handler = BinaryRotatingLog(path, maxBytes=max_bytes, backupCount=2, encoding='utf-8')
        read = getattr(pipe, 'read1', pipe.read)
        while data := read(65536):
            try:
                handler.append(data)
            except OSError:
                # Logging must not block a network subprocess when the disk is full.
                pass
    except OSError:
        read = getattr(pipe, 'read1', pipe.read)
        while read(65536):
            pass
    finally:
        if handler:
            handler.close()
        pipe.close()


def start_logged(args, path, **kwargs):
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    thread = threading.Thread(target=drain_log, args=(process.stdout, path), daemon=True)
    thread.start()
    return process
