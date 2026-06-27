# Action Repair

Use this reference when a computer/browser action fails.

Repair order:

1. Read the tool result and failure kind.
2. Re-observe the current UI state.
3. Check for common causes:
   - page still loading
   - selector stale or ambiguous
   - element hidden or disabled
   - overlay/modal intercepting input
   - auth/session missing
   - route/service changed
   - browser console or network error
4. Try a different stable action target if evidence supports it.
5. Reset to a known state if the UI drifted.
6. Stop and report a concrete blocker when repeated actions fail without new evidence.
