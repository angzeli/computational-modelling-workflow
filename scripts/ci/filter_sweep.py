"""Bound sweep console output; keep recent starts outside the test interpreter."""
from collections import deque
import json


RECENT_ROWS = 16


class SweepOutput:
    def __init__(self, write):
        self.write = write
        self.pending = b''
        self.starts = deque(maxlen=RECENT_ROWS)
        self.active_test = None
        self.diagnosed = False

    def feed(self, data):
        self.pending += data
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            self.line(line + b'\n')

    def finish(self):
        if self.pending:
            self.line(self.pending)
            self.pending = b''

    def line(self, line):
        if b'CMW_TEST_CASE_STARTED ' in line:
            self.active_test = line
            return
        if b'CMW_TEST_CASE_' in line:
            return
        if b'OWNERSHIP_FAULT_ROW ' in line:
            try:
                row = json.loads(line.split(b'OWNERSHIP_FAULT_ROW ', 1)[1])
            except (ValueError, TypeError):
                self.write(line)  # Never hide malformed diagnostic evidence.
                return
            if row.get('event') == 'START':
                self.starts.append(row)
                return
            if row.get('event') == 'END':
                return
        if b'OWNERSHIP_FAULT_SCENARIO ' in line:
            return  # Timings remain in the final summary and JSON evidence.
        if b'OWNERSHIP_FAULT_WATCHDOG ' in line or b'Timeout (' in line:
            if not self.diagnosed:
                if self.active_test:
                    self.write(self.active_test)
                self.write(b'OWNERSHIP_FAULT_RECENT ' + json.dumps(list(self.starts)).encode() + b'\n')
                self.diagnosed = True
        self.write(line)
