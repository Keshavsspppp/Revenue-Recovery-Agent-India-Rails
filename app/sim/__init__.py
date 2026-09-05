"""The simulator. May import app.domain; nothing in app.plan, app.diagnose, app.propose
or app.policy may import this package — tests/test_boundaries.py enforces it.

Leaking the true salary date into the planner is the easiest way to accidentally fake
your result, and that test is the guard.
"""
