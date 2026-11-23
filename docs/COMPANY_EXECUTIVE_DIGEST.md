# Private 08:30 Beijing company digest

The executive digest is off by default. It uses the existing warehouse only;
it never triggers Lingxing reads, backfills, historical replays, or budget
changes. It does not replace or change the separately configured daily report
(e.g. the existing 09:00 notification group) or hourly advertising alerts.

## Scope and first delivery

An owner must first approve **both** the full intended list of company stores
and the single recipient for this aggregate. Do not reuse a daily report group,
Feishu administrator, ad subscriber, or a list of currently visible/ready shops
as a substitute. Verify the recipient's Open ID using the **same Feishu app**
that will deliver their private message, and confirm they are authorized to
receive this company scope. No routing fallback or mentions are added.

Add this dedicated section to the existing server-side `FEISHU_ROUTING_FILE`
after approval, keeping real values out of source control:

```json
{
  "company_executive_digest": {
    "open_id": "ou_REPLACE_WITH_VERIFIED_ID",
    "app_id": "cli_REPLACE_WITH_VERIFIED_APPLICATION_ID",
    "generation": "0123456789abcdef0123456789abcdef",
    "verified_at": "2026-01-01T00:00:00+00:00",
    "store_ids": ["101", "102"]
  }
}
```

Use a fresh random 32-character lowercase hexadecimal generation at every
authorization or recipient change, with the actual verification time. The
explicit approved store set must match `COLLECTION_SCOPE_FILE` exactly; adding
or omitting even one store blocks the private lane. Every store must currently
be authorized and configured for `America/Los_Angeles` in both the warehouse and
store-timezone configuration. Pausing any included store's notification stops
the combined message rather than sending a partial report. Authorization and
its generation are checked again immediately before the message POST.
The verified application ID must also match both current effective Feishu
credentials and the actual adapter which acquired the token. Secrets do not
enter the binding, frozen card, or send receipt. Duplicate JSON keys in route,
scope, timezone or credentials files fail closed rather than selecting a winner.

Set `FEISHU_COMPANY_EXECUTIVE_DIGEST_START_DATE` to the first approved
Beijing delivery date, `YYYY-MM-DD`, and only then set
`FEISHU_COMPANY_EXECUTIVE_DIGEST_ENABLED=true`. The scheduler also requires its
usual application transport, Feishu enablement and dry-run-off gates. Restart
the scheduler when enabling so it registers the new tasks. The first attempt is
scheduled at **08:30 Beijing time**; minute recovery and outbox retry are
bounded by **08:40 exclusive**. Recovery is for a missed enqueue tick or an
unavailable route, not for replacing a frozen card when more data arrives.
Missing the window means no catch-up. One
persistent outbox key per Beijing date prevents duplicates after restart or a
route change. Ambiguous delivery is retained for manual verification, not
retried automatically. To stop the lane, disable it or remove its private
binding; pending messages are not migrated or replayed. No schema or data
migration is required.

## What the digest means

- The latest cohort is the most recent **completed** Los Angeles report day
  at 08:30 Beijing, normally two calendar dates before the Beijing send date.
  It shows sales, order volume, SP/SB/SD spend, TACOS (same-date ad spend /
  total sales), and PPC as effective CPC (same-date mixed SP/SB/SD spend /
  clicks, which may include impression charges; not the per-click bill rate).
- Order volume is the normalized ASIN-grain order sum, not distinct customer
  orders. No cross-currency money total or FX conversion is shown. The
  all-store order count is displayed only when all configured stores have
  complete order evidence.
- ACOS (mature SP/SB/SD spend / their attributed sales) and CVR (mature SP
  click-attributed ad orders / SP clicks) appear only under their separately
  dated mature cohort, exactly 15 marketplace dates before the latest closed
  date. The extra date allows a routine prior 18:30 Beijing collection to clear
  the 14-day attribution and 12-hour reporting cushion when it captured all
  needed partitions. The latest closed day's ACOS/CVR are deliberately absent.
  [Amazon Ads attribution](https://advertising.amazon.com/help/GX7KDKHMWQYMJ385)
  distinguishes click-only SP from standard SB/SD conversions that may also
  include views. The warehouse does not prove a click-only company-wide CVR
  across all three types. Mature captures require the complete attribution
  window plus a conservative 12 hours for late reported conversions; stale
  earlier captures, even if marked complete, cannot establish a mature value.
- Ratios are weighted by summing matching numerators and denominators first.
  A currency bucket omits a metric when any of its stores lacks evidence for
  it. If any store's currency cannot be established from its sales facts, no
  currency subtotals are shown. Empty/missing data and zero denominators are
  not interpreted as zero. Explicit reported zeros remain zeros.
- Scope means exactly the owner-approved configured shops, not every shop
  listed in an upstream account. Coverage counts are evidence counts rather
  than a claim about provider freshness. Only an offline/mocked delivery test
  can be claimed until an explicitly approved private Feishu canary confirms
  the live route.

For a trusted operator, `preview_company_digest(db, settings, now=aware_clock)`
in `jx_alert.notifications.company_executive` returns the aggregate and card
from a read-only session without requiring delivery settings, enqueuing or
sending. Keep previewed business data private.
