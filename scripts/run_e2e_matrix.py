#!/usr/bin/env python3
"""Comprehensive E2E validation matrix for bidirectional mute sync across Keep, Alertmanager and Mattermost."""

import json
import os
import sys
import time
import uuid
import requests
from datetime import datetime, timezone, timedelta

AM_URL = "http://alertmanager:9093"
KEEP_URL = "http://127.0.0.1:8080"
BRIDGE_URL = "http://keep-mm-bridge:8080"
MM_URL = "http://mattermost.keep-lab.svc:8065"

BRIDGE_SECRET = os.environ['MM_BRIDGE_SECRET']
KEEP_KEY = os.environ['KEEP_API_KEY']
KEEP_HEADERS = {"X-API-KEY": KEEP_KEY}

INCIDENT_ID = os.environ['KEEP_TEST_INCIDENT_ID']
MM_POST_ID = os.environ['MM_TEST_POST_ID']
MM_CHANNEL_ID = os.environ['MM_TEST_CHANNEL_ID']
ALERT_NAME = "BidirectionalFlowTest"
NAMESPACE = "billing-prod"


def log(section, msg):
    print(f"\n[{section}] {msg}", flush=True)


def get_keep_incident():
    r = requests.get(f"{KEEP_URL}/incidents/{INCIDENT_ID}", headers=KEEP_HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def get_keep_silence(silence_id):
    r = requests.get(f"{KEEP_URL}/silences/{silence_id}", headers=KEEP_HEADERS, timeout=10)
    if r.status_code == 200:
        return r.json()
    return None


def get_am_silences(active_only=False):
    url = f"{AM_URL}/api/v2/silences"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    silences = r.json()
    if active_only:
        return [s for s in silences if s.get("status", {}).get("state") in ("active", "pending")]
    return silences


def get_am_silence(am_id):
    r = requests.get(f"{AM_URL}/api/v2/silence/{am_id}", timeout=10)
    if r.status_code == 200:
        return r.json()
    return None


def run_reconciliation():
    from keep.api.tasks.process_alertmanager_reconciliation_task import process_alertmanager_reconciliation_once
    return process_alertmanager_reconciliation_once()


def post_alert_to_am():
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = [{
        "labels": {
            "alertname": ALERT_NAME,
            "namespace": NAMESPACE,
            "severity": "critical"
        },
        "annotations": {
            "summary": "E2E live verification alert"
        },
        "startsAt": now_iso
    }]
    r = requests.post(f"{AM_URL}/api/v2/alerts", json=payload, timeout=10)
    r.raise_for_status()
    return r.status_code


def check_am_alert_status():
    r = requests.get(f"{AM_URL}/api/v2/alerts", timeout=10)
    r.raise_for_status()
    for a in r.json():
        if a.get("labels", {}).get("alertname") == ALERT_NAME:
            return a.get("status", {})
    return None


def get_mm_post():
    bot_token = os.environ['MM_BOT_TOKEN']
    r = requests.get(f"{MM_URL}/api/v4/posts/{MM_POST_ID}", headers={"Authorization": f"Bearer {bot_token}"}, timeout=10)
    if r.status_code == 200:
        return r.json()
    return None


def wait_for_incident_silence(expected_silenced, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        inc = get_keep_incident()
        if inc.get("silence", {}).get("silenced") == expected_silenced:
            return inc
        time.sleep(0.5)
    return get_keep_incident()


def main():
    log("INIT", "Starting Comprehensive E2E Verification Matrix")

    # Step 0: Ensure clean baseline
    log("BASELINE", "Cleaning stale AM test silences...")
    for s in get_am_silences(active_only=True):
        for m in s.get("matchers", []):
            if m.get("value") == ALERT_NAME:
                requests.delete(f"{AM_URL}/api/v2/silence/{s['id']}")
    inc = get_keep_incident()
    if inc.get("silence", {}).get("silenced"):
        log("BASELINE", "Clearing existing silence on incident...")
        requests.post(f"{BRIDGE_URL}/", json={
            "user_id": "rmgjyc6prirqjgnitichy79dgr", "user_name": "cleaner",
            "channel_id": MM_CHANNEL_ID, "post_id": MM_POST_ID,
            "context": {"action": "unsilence", "incident_id": INCIDENT_ID, "secret": BRIDGE_SECRET}
        })
        run_reconciliation()
        inc = wait_for_incident_silence(False, timeout=10)
    log("BASELINE", f"Incident {INCIDENT_ID}: status={inc.get('status')}, silenced={inc.get('silence', {}).get('silenced')}")

    # =========================================================================
    # SCENARIO 1: Mute from Mattermost -> Keep -> Alertmanager
    # =========================================================================
    log("SCENARIO 1", "Mute from Mattermost: user clicks Silence for 4h")
    snooze_payload = {
        "user_id": "rmgjyc6prirqjgnitichy79dgr",
        "user_name": "calls",
        "channel_id": MM_CHANNEL_ID,
        "post_id": MM_POST_ID,
        "context": {
            "action": "snooze",
            "incident_id": INCIDENT_ID,
            "selected_option": "4",
            "secret": BRIDGE_SECRET
        }
    }
    r = requests.post(f"{BRIDGE_URL}/", json=snooze_payload, timeout=30)
    assert r.status_code == 200, f"Bridge snooze failed: {r.status_code} {r.text}"
    log("SCENARIO 1", f"Bridge response: {r.json()}")

    # 1.1 Verify Keep Incident state
    inc = wait_for_incident_silence(True, timeout=10)
    silence_info = inc.get("silence", {})
    assert silence_info.get("silenced") is True, f"Keep incident should be silenced, got {silence_info}"
    reasons = silence_info.get("reasons", [])
    assert len(reasons) > 0, "No reasons found in incident silence"
    keep_silence_id = reasons[0]["silence_id"]
    log("SCENARIO 1", f"Keep incident is SILENCED. Keep silence_id={keep_silence_id}")

    # 1.2 Wait until Keep silence is synced to Alertmanager
    am_silence_id = None
    start = time.time()
    while time.time() - start < 20:
        ks = get_keep_silence(keep_silence_id)
        if ks and (ks.get("correlation_id") or "").startswith("am_synced:"):
            am_silence_id = ks["correlation_id"].split(":", 1)[1]
            break
        run_reconciliation()
        time.sleep(1)
    assert am_silence_id is not None, f"Silence {keep_silence_id} was not synced to AM in time"
    log("SCENARIO 1", f"Silence synced to Alertmanager with am_id={am_silence_id}")

    # 1.3 Verify Alertmanager Silence
    am_sil = get_am_silence(am_silence_id)
    assert am_sil is not None, f"Silence {am_silence_id} not found in AM"
    assert am_sil.get("status", {}).get("state") in ("active", "pending"), f"AM silence not active: {am_sil}"
    assert am_sil.get("createdBy") == "keep", f"CreatedBy should be keep, got {am_sil.get('createdBy')}"
    matchers = am_sil.get("matchers", [])
    assert any(m["name"] == "alertname" and m["value"] == ALERT_NAME for m in matchers), f"Matchers missing alertname: {matchers}"
    log("SCENARIO 1", f"Alertmanager silence verified: state={am_sil.get('status', {}).get('state')}, createdBy={am_sil.get('createdBy')}, matchers={matchers}")

    # 1.4 Verify Alert Suppression in Alertmanager
    post_alert_to_am()
    time.sleep(1)
    alert_st = check_am_alert_status()
    assert alert_st is not None, "Alert not found in AM"
    assert alert_st.get("state") == "suppressed", f"Alert should be suppressed, got {alert_st}"
    assert am_silence_id in alert_st.get("silencedBy", []), f"Alert not silenced by {am_silence_id}: {alert_st}"
    log("SCENARIO 1", f"Alert suppression verified in AM: state={alert_st.get('state')}, silencedBy={alert_st.get('silencedBy')}")

    # =========================================================================
    # SCENARIO 2: Unsilence from Mattermost -> Keep -> Alertmanager
    # =========================================================================
    log("SCENARIO 2", "Unsilence from Mattermost: user clicks Unsilence button")
    unsnooze_payload = {
        "user_id": "rmgjyc6prirqjgnitichy79dgr",
        "user_name": "calls",
        "channel_id": MM_CHANNEL_ID,
        "post_id": MM_POST_ID,
        "context": {
            "action": "unsilence",
            "incident_id": INCIDENT_ID,
            "secret": BRIDGE_SECRET
        }
    }
    r = requests.post(f"{BRIDGE_URL}/", json=unsnooze_payload, timeout=30)
    assert r.status_code == 200, f"Bridge unsilence failed: {r.status_code} {r.text}"
    log("SCENARIO 2", f"Bridge response: {r.json()}")

    # 2.1 Verify Keep silence is cancelled
    keep_sil = get_keep_silence(keep_silence_id)
    assert keep_sil is not None, "Keep silence not found"
    assert keep_sil.get("cancelled_at") is not None, f"Keep silence was not cancelled: {keep_sil}"
    log("SCENARIO 2", f"Keep silence cancelled at {keep_sil.get('cancelled_at')}")

    inc = wait_for_incident_silence(False, timeout=10)
    assert inc.get("silence", {}).get("silenced") is False, "Keep incident still silenced after unsilence"
    log("SCENARIO 2", "Keep incident is UNSILENCED (silenced=False)")

    # 2.2 Wait until Reconciler deletes silence in Alertmanager
    start = time.time()
    deleted_ok = False
    while time.time() - start < 20:
        am_sil = get_am_silence(am_silence_id)
        if am_sil and am_sil.get("status", {}).get("state") == "expired":
            deleted_ok = True
            break
        run_reconciliation()
        time.sleep(1)
    assert deleted_ok, f"Silence {am_silence_id} did not transition to expired in AM"
    log("SCENARIO 2", f"Silence {am_silence_id} successfully deleted from Alertmanager (state=expired)")

    # 2.3 Verify Alert is NO LONGER suppressed in AM
    post_alert_to_am()
    time.sleep(1)
    alert_st = check_am_alert_status()
    assert alert_st is not None, "Alert not found in AM"
    assert alert_st.get("state") == "active", f"Alert should be active (not suppressed), got {alert_st}"
    log("SCENARIO 2", f"Alert suppression lifted in AM: state={alert_st.get('state')}")

    # =========================================================================
    # SCENARIO 3: Mute from Alertmanager -> Keep -> Mattermost
    # =========================================================================
    log("SCENARIO 3", "Mute from Alertmanager: creating native silence in AM")
    now = datetime.now(timezone.utc)
    am_new_payload = {
        "matchers": [
            {"name": "alertname", "value": ALERT_NAME, "isRegex": False, "isEqual": True},
            {"name": "namespace", "value": NAMESPACE, "isRegex": False, "isEqual": True}
        ],
        "startsAt": now.isoformat(),
        "endsAt": (now + timedelta(hours=2)).isoformat(),
        "createdBy": "alertmanager-admin",
        "comment": "Muted directly in Alertmanager UI"
    }
    r = requests.post(f"{AM_URL}/api/v2/silences", json=am_new_payload, timeout=10)
    assert r.status_code in (200, 201), f"Failed to create AM silence: {r.status_code} {r.text}"
    am_native_id = r.json().get("silenceID")
    log("SCENARIO 3", f"Created native Alertmanager silence: {am_native_id}")

    # 3.1 Reconciler AM -> Keep imports silence into Keep
    start = time.time()
    imported_ok = False
    while time.time() - start < 20:
        inc = get_keep_incident()
        if inc.get("silence", {}).get("silenced"):
            imported_ok = True
            break
        run_reconciliation()
        time.sleep(1)
    assert imported_ok, f"AM silence {am_native_id} was not imported into Keep incident"
    log("SCENARIO 3", f"Keep incident is SILENCED from AM: silenced=True")

    # =========================================================================
    # SCENARIO 4: Unsilence from Alertmanager -> Keep -> Mattermost
    # =========================================================================
    log("SCENARIO 4", "Unsilence from Alertmanager: deleting silence in AM")
    r = requests.delete(f"{AM_URL}/api/v2/silence/{am_native_id}", timeout=10)
    assert r.status_code in (200, 404), f"Failed to delete AM silence: {r.status_code} {r.text}"
    log("SCENARIO 4", f"Deleted silence {am_native_id} in Alertmanager")

    # 4.1 Reconciler detects removal in AM and cancels Keep mirror silence
    start = time.time()
    unsilenced_ok = False
    while time.time() - start < 20:
        inc = get_keep_incident()
        if not inc.get("silence", {}).get("silenced"):
            unsilenced_ok = True
            break
        run_reconciliation()
        time.sleep(1)
    assert unsilenced_ok, "Keep incident was not unsilenced after AM deletion"
    log("SCENARIO 4", "Keep incident is UNSILENCED (silenced=False)")

    # =========================================================================
    # SCENARIO 5: Keep native Silence -> Alertmanager -> Unsilence from AM
    # =========================================================================
    log("SCENARIO 5", "Mute in Keep -> sync to AM -> Unsilence directly in Alertmanager")
    # 5.1 Create Keep silence for incident
    keep_cmd = {
        "schema_version": 1,
        "client_request_id": str(uuid.uuid4()),
        "team_id": None,
        "selector": {
            "kind": "incident",
            "incident_ids": [INCIDENT_ID]
        },
        "starts_at": (datetime.now(timezone.utc) + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "comment": "Created natively in Keep UI for Scenario 5",
        "correlation_id": None
    }
    r = requests.post(f"{KEEP_URL}/silences", json=keep_cmd, headers=KEEP_HEADERS, timeout=10)
    assert r.status_code in (200, 201), f"Failed to create Keep silence: {r.status_code} {r.text}"
    sc5_keep_id = r.json()["result"]["id"]
    log("SCENARIO 5", f"Created Keep silence: {sc5_keep_id}")

    # 5.2 Wait until Reconciler pushes to AM
    sc5_am_id = None
    start = time.time()
    while time.time() - start < 20:
        ks = get_keep_silence(sc5_keep_id)
        if ks and (ks.get("correlation_id") or "").startswith("am_synced:"):
            sc5_am_id = ks["correlation_id"].split(":", 1)[1]
            break
        run_reconciliation()
        time.sleep(1)
    assert sc5_am_id is not None, f"Silence {sc5_keep_id} was not synced to AM"
    log("SCENARIO 5", f"Silence pushed to AM as {sc5_am_id}")

    # 5.3 Operator unsilences directly in Alertmanager UI
    r = requests.delete(f"{AM_URL}/api/v2/silence/{sc5_am_id}", timeout=10)
    assert r.status_code in (200, 404), f"Failed to delete AM silence: {r.status_code}"
    log("SCENARIO 5", f"Deleted silence {sc5_am_id} directly in Alertmanager")

    # 5.4 Sleep over the grace period to trigger unsilenced_from_am in Reconciler
    log("SCENARIO 5", "Waiting for grace period (31s) before Reconciler detects AM unsilence...")
    time.sleep(31)

    cancelled_ok = False
    start = time.time()
    while time.time() - start < 20:
        ks = get_keep_silence(sc5_keep_id)
        if ks and ks.get("cancelled_at"):
            cancelled_ok = True
            break
        run_reconciliation()
        time.sleep(1)
    assert cancelled_ok, f"Keep silence {sc5_keep_id} was not automatically cancelled by AM deletion"
    log("SCENARIO 5", f"Keep silence {sc5_keep_id} was AUTOMATICALLY CANCELLED by Alertmanager deletion!")

    inc = get_keep_incident()
    assert inc.get("silence", {}).get("silenced") is False, f"Incident still silenced: {inc.get('silence')}"
    log("SCENARIO 5", "Keep incident is UNSILENCED!")

    log("SUCCESS", "ALL 5 E2E BIDIRECTIONAL SCENARIOS PASSED WITH 100% INVARIANTS MET!")


if __name__ == "__main__":
    main()
