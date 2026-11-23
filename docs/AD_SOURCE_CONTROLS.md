# Per-store Sponsored Products observation source

The owner console stores one explicit SP observation choice per company store.
A store with no selection stays on Lingxing. This does not replace Lingxing
sales, settlement, account-health or ordinary daily operating reports, change
other stores, edit advertising accounts, or change existing credentials.

1. Set `SPARKX_AI_TOKEN` in the server's private environment, not in a browser.
   Restrict `SPARKX_PROFILE_IDS` to the approved profile list. Keep these values
   separate from any Lingxing credentials.
2. Open the selected store's **商品推广（SP）观测来源** section. Check the exact
   Amazon Marketplace ID, currency, and report timezone against the
   store/provider account; enter that shop's approved SparkX Profile ID.
3. Click **核验 SparkX 只读范围**. This is the only button that explicitly runs
   the outbound read check. It verifies the single approved profile from the
   authenticated server and requires a completed representative advertising
   sample. Failed or contradictory evidence does not create a verification
   receipt. Verification alone does not switch any source.
4. Select **SparkX（仅观测）** and click **保存观测来源**. After the switch, for
   this store only, subsequent enabled hourly tasks collect SparkX SP evidence
   separately; legacy automatic advertising alerts are held and **automatic
   SparkX Feishu advertising alerts are not enabled**. Refresh the same store
   page to see metadata for the latest observations of this selection, when
   available. The same panel distinguishes a paused store or an incomplete
   latest recorded attempt from historical successful observations.
   A saved setting is not proof that any observation has completed.
5. To roll back, select **领星** and save, even after a SparkX token revocation.
   No network probe or credentials change is needed for rollback. New eligible
   tasks can use Lingxing, but no old queued alert is rearmed or replayed.

The dedicated state file defaults to
`/app/data/advertising-source-controls.json` (override with
`ADVERTISING_SOURCE_CONTROLS_FILE`). Owner mutations require authenticated
same-origin requests and compare-and-swap revision protection. The file is
atomically replaced under a lock with private permissions. Keep the data
mount persistent and back up this file with its neighboring `.initialized`
marker. The marker is created when a source is actually changed, not when
only testing a binding. After a source change, a missing state file blocks
the lane and does not implicitly restore Lingxing. Restore the correct saved state if the file is
lost; do not delete the marker to reset the selection. The state file contains a
local token fingerprint, not a reusable token; neither fingerprint nor token
is returned to the browser. Credential/profile revocation or a scope or
metadata mismatch blocks SparkX without an implicit fallback. A new binding
for an active SparkX selection requires rolling back first. A removed store
cannot use the configured SparkX binding.

No live data source or Feishu message should be tested without explicit
operator approval. Validate exact profile and store identity offline first,
then use one isolated, approved store when ready.
