"""Wiring of every control component behind the console.

The runtime lives in :mod:`waterplant.runtime` so the offline rehearsal
package can share it without importing the console; this module re-exports it
for the existing console imports.
"""

from __future__ import annotations

from waterplant.runtime import Runtime

__all__ = ["Runtime"]
