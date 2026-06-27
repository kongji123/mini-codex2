# Visual Action Loop

Use this reference for browser or computer-use tasks where rendered UI state matters.

1. Observe before acting.
2. State the current UI state and target state.
3. Choose the most stable available action surface:
   - selector/text action
   - accessibility/DOM action
   - coordinate action when visual evidence is sufficient
4. Execute one meaningful action.
5. Observe again.
6. Verify whether the UI changed as expected.
7. Record screenshot/browser evidence when it proves success or explains failure.

Do not rely on HTTP status alone for JavaScript UI flows. Use real browser evidence when routing, forms, overlays, media, or client-side state are involved.
