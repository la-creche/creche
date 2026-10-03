"""The quiet check (contract 01 §3.15): code decides whether a cron firing
wakes the family, before any session exists.

    timer -> agent-trigger fire <family>
               |
               |  quiet: in the family file?  no --------------------> fire
               v
             gate.py    attendance: a wake still live? ---- yes -------> quiet
               |        records.py: last firing ended ok? --> last good wake
               |        pep.py, AS the family: survey_board, job_status
               |        records.py: the daily call made today?
               v
             decide.py  every reason to wake, against the last good wake
               |
               |  none -> quiet, nothing started
               '  any  -> fire, then state.py keeps what the gate saw

`decide.py` is pure. The three readers each sit behind a Protocol, so the
gate is tested with fakes and nothing under `tests/` reaches a live service.
"""
