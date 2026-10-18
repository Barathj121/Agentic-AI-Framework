"""Authenticated, explicit Cloud Run handoff; never falls back to local execution."""
from __future__ import annotations

import hashlib
import json
import os
import re
from urllib.parse import urlsplit

import httpx

from .schemas import InvestigationInput


class RemoteClariFiError(RuntimeError):
    """Safe to show without revealing credentials, source rows or upstream errors."""


def service_url() -> str:
    value = os.environ.get("CLARIFI_API_URL", "").rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname
            or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)?\.run\.app", parsed.hostname)
            or parsed.netloc != parsed.hostname or parsed.path or parsed.query or parsed.fragment):
        raise RemoteClariFiError("Configure CLARIFI_API_URL with the Cloud Run service's HTTPS URL.")
    return value


def identity_token(audience: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import fetch_id_token

    return fetch_id_token(Request(), audience)


def _request(method: str, path: str, **kwargs):
    try:
        url = service_url()
        token = identity_token(url)
        # No redirects or retries: a failed response may follow a completed run.
        with httpx.Client(timeout=httpx.Timeout(310, connect=15), follow_redirects=False) as client:
            response = client.request(method, url + path,
                                      headers={"Authorization": "Bearer " + token,
                                               "Content-Type": "application/json"}, **kwargs)
        if response.status_code != 200:
            raise RemoteClariFiError(
                f"Cloud investigation service returned HTTP {response.status_code}. "
                "Check service access, model quota and backend logs. No local fallback was run.")
        return response.json()
    except RemoteClariFiError:
        raise
    except Exception:
        raise RemoteClariFiError(
            "Cloud investigation service could not be reached or returned an invalid response. "
            "Check service identity and connectivity. The request was not retried; "
            "check investigation evidence before submitting again.") from None


def health() -> dict:
    return _request("GET", "/healthz")


def investigate(payload, *, approved=False, model=None) -> dict:
    if approved is not True:
        raise ValueError("Explicit investigation approval is required")
    source = InvestigationInput.model_validate(payload)
    expected_model = os.environ.get("CLARIFI_REMOTE_MODEL", "")
    if not model or model != expected_model:
        raise RemoteClariFiError("Cloud model configuration changed; review and approve the investigation again.")
    body = json.dumps({"data": source.model_dump(mode="json"), "approved": True, "model": model})
    if len(body.encode()) > 5 * 1024 * 1024:
        raise ValueError("Investigation exceeds 5 MiB; narrow the scope")
    report = _request("POST", "/investigations", content=body.encode())
    digest = hashlib.sha256(json.dumps(source.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    if (not isinstance(report, dict) or report.get("status") != "COMPLETED"
            or report.get("agent") != "clarifi" or report.get("case_id") != source.case_id
            or report.get("input_sha256") != digest
            or not isinstance(report.get("provenance"), dict)
            or report.get("provenance", {}).get("model") != model
            or report.get("provenance", {}).get("ai_used") is not True
            or not isinstance(report.get("evidence_uri"), str)
            or not report["evidence_uri"].startswith("gs://")):
        raise RemoteClariFiError("Cloud report did not match the approved input, model or evidence contract.")
    return report


def draft(payload, screening, *, approved=False, model=None):
    if approved is not True:
        raise ValueError('Explicit AI drafting approval is required')
    source = InvestigationInput.model_validate(payload)
    from .screening import input_hash
    if screening.get('status') != 'SCREENED' or screening.get('input_sha256') != input_hash(source):
        raise RemoteClariFiError('Screening changed; screen and review this input again.')
    if not model or model != os.environ.get('CLARIFI_REMOTE_MODEL', ''):
        raise RemoteClariFiError('Cloud model configuration changed; review and approve the draft again.')
    body = json.dumps({'data': source.model_dump(mode='json'), 'approved': True, 'model': model})
    if len(body.encode()) > 5 * 1024 * 1024:
        raise ValueError('Investigation exceeds 5 MiB; narrow the scope')
    report = _request('POST', '/drafts', content=body.encode())
    if (not isinstance(report, dict) or report.get('status') != 'COMPLETED'
            or report.get('agent') != 'clarifi' or report.get('case_id') != source.case_id
            or report.get('input_sha256') != input_hash(source)
            or report.get('findings') != screening['findings']
            or report.get('provenance', {}).get('workflow') != 'scores_first'
            or report.get('provenance', {}).get('model') != model
            or report.get('provenance', {}).get('ai_used') is not True
            or not isinstance(report.get('evidence_uri'), str) or not report['evidence_uri'].startswith('gs://')):
        raise RemoteClariFiError('AI draft does not match the reviewed screening, model or evidence contract.')
    # Keep the local screening lineage, independently of server recomputation.
    report['provenance']['screening_id'] = screening['investigation_id']
    return report
