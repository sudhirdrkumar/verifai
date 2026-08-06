#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import Counter


def main() -> int:
    data = json.load(open("/tmp/out_diag.json", "r", encoding="utf-8"))

    keys = [
        "opinion_but_open_claims__drraghvendra",
        "opinion_but_open_claims__draghvendra",
        "opinion_but_open_claims__raghvendra",
    ]
    for key in keys:
        rows = data.get(key, [])
        counts = Counter([r[2] for r in rows])  # status index
        print(f"\n{key} count={len(rows)} status_counts={dict(counts)}")
        for r in rows[:10]:
            # id, external_claim_id, status, assigned_doctor_id, report_export_status, tagging,
            # opinion_preview, documents, last_status_event_status, last_status_event_at, updated_at
            last_event_status = r[8] if len(r) > 8 else ""
            last_event_at = r[9] if len(r) > 9 else ""
            updated_at = r[10] if len(r) > 10 else (r[8] if len(r) > 8 else "")
            mismatch = (
                "MISMATCH"
                if last_event_status and str(last_event_status).strip().lower() != str(r[2]).strip().lower()
                else ""
            )
            print(
                f"  claim={r[1]} status={r[2]} last_event_status={last_event_status} "
                f"last_event_at={last_event_at} export={r[4]} tag={r[5]} docs={r[7]} updated={updated_at} {mismatch}"
            )

    print("\npending_display_by_doctor:")
    print(data.get("pending_display_by_doctor"))

    print("\nworkflow_event_type_counts_for_ragh_open_claims:")
    print(data.get("workflow_event_type_counts_for_ragh_open_claims"))

    rows_meta = data.get("ragh_claims_current_state_with_upload_meta", [])
    print(f"\nragh_claims_current_state_with_upload_meta total={len(rows_meta)}")
    for r in rows_meta[:20]:
        print(f"  {r}")

    events = data.get("sample_workflow_events_for_ragh", [])
    print(f"\nsample_workflow_events_for_ragh total={len(events)}")
    for e in events[:40]:
        claim_id, event_type, payload_text, occurred_at = e
        payload_short = (payload_text or "").replace("\n", " ")[:220]
        print(
            f"  claim_id={claim_id} event={event_type} at={occurred_at} payload={payload_short}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
