"""Append-only evidence bundles in a private GCS bucket using ADC."""
import json
import os


def persist(data, report):
    bucket_name = os.environ.get("CLARIFI_EVIDENCE_BUCKET", "")
    if not bucket_name:
        return None
    from google.cloud import storage

    object_name = f"investigations/{report['case_id']}/{report['investigation_id']}.json"
    bundle = {"input": data.model_dump(mode="json"), "report": report}
    # A source hash alone cannot reconstruct evidence: retain the validated input
    # alongside the report. Bucket IAM, retention and residency are operator-owned.
    storage.Client().bucket(bucket_name).blob(object_name).upload_from_string(
        json.dumps(bundle, ensure_ascii=False, allow_nan=False),
        content_type="application/json", if_generation_match=0, timeout=30)
    return f"gs://{bucket_name}/{object_name}"
