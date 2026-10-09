"""IaC gates for the Keep side of a reviewed legacy cutover."""


def ownership(snapshot, team_id):
    entry = next((item for item in (snapshot or {}).get("bundle", {}).get("runtime_ownership", [])
                  if item["team_id"] == team_id), {})
    return {"team_id": team_id, "domain": "keep", "notifications": "keep", "legacy_snooze": "disabled", **entry}


def gate_reason(snapshot, team_id, component):
    owner = ownership(snapshot, team_id)[component]
    return None if owner == "keep" else "runtime_" + component + "_owned_by_" + owner


def projected_ownership(snapshot):
    teams = [item["id"] for item in snapshot["documents"]["access"]["teams"]] + [None]
    return [ownership(snapshot, team) for team in teams]
