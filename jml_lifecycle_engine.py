import os
import json
import time
import uuid
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv

load_dotenv()

TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
DOMAIN = "limra.co.in"

if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
    raise SystemExit("[ERROR] Missing TENANT_ID, CLIENT_ID, or CLIENT_SECRET in .env")

USER_ADMIN_ROLE_TEMPLATE_ID = "fe930be7-5e62-47db-91af-98c3a49a38b1"

def get_token() -> str:
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    resp = requests.post(url, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]

def graph_request(method: str, endpoint: str, token: str, payload: dict = None, retries: int = 3) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"https://graph.microsoft.com/v1.0{endpoint}"
    for attempt in range(1, retries + 1):
        resp = requests.request(method, url, headers=headers, json=payload, timeout=20)
        if resp.status_code == 404 and "SubjectNotFound" in resp.text and attempt < retries:
            print(f"    [~] Waiting 8s for Entra -> PIM directory replication (attempt {attempt}/{retries})...")
            time.sleep(8)
            continue
        if resp.status_code >= 400:
            print(f"[ERROR] {method} {endpoint} -> {resp.status_code}: {resp.text}")
            resp.raise_for_status()
        return resp.json() if resp.text else {"status": resp.status_code}

def run_jml_simulation():
    token = get_token()
    audit_events = []
    now = datetime.now(timezone.utc)
    suffix = str(uuid.uuid4())[:6]
    upn = f"jml.hire.{suffix}@{DOMAIN}"

    # --- STAGE 1: JOINER ---
    print(f"[1/3] JOINER: Provisioning {upn}...")
    user_payload = {
        "accountEnabled": True,
        "displayName": f"JML Demo Hire ({suffix})",
        "mailNickname": f"jmlhire{suffix}",
        "userPrincipalName": upn,
        "usageLocation": "IN",
        "department": "Finance",
        "jobTitle": "Junior IAM Analyst",
        "passwordProfile": {
            "forceChangePasswordNextSignIn": True,
            "password": f"Limra!{uuid.uuid4().hex[:10]}#26",
        },
    }
    user = graph_request("POST", "/users", token, user_payload)
    user_id = user["id"]
    audit_events.append({
        "stage": "JOINER",
        "timestampUTC": datetime.now(timezone.utc).isoformat(),
        "upn": upn,
        "userId": user_id,
        "action": "Created Entra ID user with usageLocation=IN, department=Finance",
    })

    print("    [~] Waiting 10s for Entra ID object propagation across microservices...")
    time.sleep(10)

    # --- STAGE 2: MOVER ---
    print(f"[2/3] MOVER: Promoting {upn} to Identity Operations & assigning PIM Eligible User Administrator...")
    graph_request("PATCH", f"/users/{user_id}", token, {
        "department": "Identity & Access Management",
        "jobTitle": "IAM Operations Engineer",
    })

    pim_payload = {
        "action": "adminAssign",
        "justification": "Automated JML Mover policy: JIT User Administrator eligibility",
        "roleDefinitionId": USER_ADMIN_ROLE_TEMPLATE_ID,
        "directoryScopeId": "/",
        "principalId": user_id,
        "scheduleInfo": {
            "startDateTime": datetime.now(timezone.utc).isoformat(),
            "expiration": {
                "type": "afterDuration",
                "duration": "P30D",
            },
        },
    }
    pim_res = graph_request(
        "POST",
        "/roleManagement/directory/roleEligibilityScheduleRequests",
        token,
        pim_payload,
    )
    audit_events.append({
        "stage": "MOVER",
        "timestampUTC": datetime.now(timezone.utc).isoformat(),
        "upn": upn,
        "action": "Updated department to IAM; granted 30-day PIM Eligible User Administrator role",
        "pimRequestId": pim_res.get("id"),
        "pimStatus": pim_res.get("status"),
    })

    # --- STAGE 3: LEAVER ---
    print(f"[3/3] LEAVER: Offboarding {upn} (Remove PIM eligibility, disable account, revoke refresh tokens)...")
    pim_revoke_payload = {
        "action": "adminRemove",
        "justification": "Automated JML Leaver offboarding: Revoke all privileged eligibility",
        "roleDefinitionId": USER_ADMIN_ROLE_TEMPLATE_ID,
        "directoryScopeId": "/",
        "principalId": user_id,
    }
    pim_revoke = graph_request(
        "POST",
        "/roleManagement/directory/roleEligibilityScheduleRequests",
        token,
        pim_revoke_payload,
    )
    time.sleep(5)
    graph_request("PATCH", f"/users/{user_id}", token, {"accountEnabled": False})
    graph_request("POST", f"/users/{user_id}/revokeSignInSessions", token)

    audit_events.append({
        "stage": "LEAVER",
        "timestampUTC": datetime.now(timezone.utc).isoformat(),
        "upn": upn,
        "action": "Removed PIM eligibility, disabled account, revoked active refresh tokens (revokeSignInSessions)",
        "pimRevokeStatus": pim_revoke.get("status"),
    })

    log_output = {
        "pipeline": "Automated-JML-RBAC-Engine",
        "tenantDomain": DOMAIN,
        "executedAtUTC": now.isoformat(),
        "events": audit_events,
    }
    with open("jml_execution_audit.json", "w", encoding="utf-8") as f:
        json.dump(log_output, f, indent=2)

    print("[+] JML full lifecycle simulation complete! Saved jml_execution_audit.json")

if __name__ == "__main__":
    run_jml_simulation()