"""Delegated authentication never uses forwarded identities or legacy impersonation."""

import json
import math
import threading
import time

import jwt
import requests
from fastapi import Request

from keep.api.bl.silences_bl import fail
from keep.api.core.silence_integrations import IntegrationConfigurationError, get_silence_integrations
from keep.api.models.silence import SilenceActor
from keep.api.routes.silences import reject_impersonation
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.rbac import get_role_by_role_name


class ActorProofVerifier:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def _keys(self, profile, *, refresh=False):
        cache_key = json.dumps(profile, sort_keys=True)
        now = time.monotonic()
        with self.lock:
            cached = self.cache.get(cache_key)
            if cached and now - cached[0] < profile.get("jwks_cache_seconds", 300):
                if not refresh or now - cached[0] < 1:
                    return cached[1]
            try:
                with requests.get(profile["jwks_url"], timeout=profile.get("jwks_timeout_seconds", 5),
                                  allow_redirects=False, stream=True) as response:
                    if response.status_code != 200:
                        raise ValueError()
                    content = bytearray()
                    for chunk in response.iter_content(8192):
                        content.extend(chunk)
                        if len(content) > 524288:
                            raise ValueError()
                    document = json.loads(content)
                    keys = document["keys"]
                    if not isinstance(keys, list) or not 1 <= len(keys) <= 100:
                        raise ValueError()
                self.cache[cache_key] = (now, keys)
                return keys
            except (requests.RequestException, ValueError, KeyError, TypeError):
                # Expired keys are not used when the trusted key source is unavailable.
                fail(503, "actor_verification_unavailable", "Actor verification unavailable")

    def verify(self, token, profiles):
        if not token:
            fail(401, "actor_proof_required", "Actor proof is required")
        if len(token) > 32768:
            fail(401, "invalid_actor_proof", "Invalid actor proof")
        try:
            header = jwt.get_unverified_header(token)
            untrusted = jwt.decode(token, options={"verify_signature": False})
            if header.get("crit") or not isinstance(header.get("kid"), str) or not 1 <= len(header["kid"]) <= 256:
                raise ValueError()
            candidates = [profile for profile in profiles if untrusted.get("iss") == profile["issuer"]
                          and header.get("alg") in profile["algorithms"]]
            if not candidates:
                raise ValueError()
        except (jwt.PyJWTError, ValueError, TypeError, AttributeError):
            fail(401, "invalid_actor_proof", "Invalid actor proof")
        for profile in candidates:
            keys = self._keys(profile)
            matching = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]]
            if not matching:
                keys = self._keys(profile, refresh=True)
                matching = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]]
            try:
                if len(matching) != 1:
                    raise ValueError()
                key = matching[0]
                if key.get("use", "sig") != "sig" or key.get("alg", header["alg"]) != header["alg"]:
                    raise ValueError()
                if "key_ops" in key and "verify" not in key["key_ops"]:
                    raise ValueError()
                public_key = jwt.PyJWK.from_dict(key, algorithm=header["alg"]).key
                if header["alg"] == "RS256" and public_key.key_size < 2048:
                    raise ValueError()
                if header["alg"] == "ES256" and public_key.curve.name != "secp256r1":
                    raise ValueError()
                claims = jwt.decode(token, public_key, algorithms=profile["algorithms"],
                    issuer=profile["issuer"], audience=profile["audience"],
                    leeway=profile.get("clock_skew_seconds", 30),
                    options={"require": ["iss", "aud", "sub", "iat", "exp"]})
                if claims["aud"] not in (profile["audience"], [profile["audience"]]):
                    raise ValueError()
                if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 256:
                    raise ValueError()
                for name in ("iat", "exp", "nbf"):
                    if name in claims and (type(claims[name]) not in (int, float) or not math.isfinite(claims[name])):
                        raise ValueError()
                if claims["exp"] <= claims["iat"] or claims.get("nbf", claims["iat"]) > claims["exp"]:
                    raise ValueError()
                if time.time() - claims["iat"] > profile.get("max_age_seconds", 300) + profile.get("clock_skew_seconds", 30):
                    raise ValueError()
                party = claims.get(profile.get("party_claim", "azp"))
                if party not in profile["authorized_parties"]:
                    raise ValueError()
                if any(claims.get(name) != expected for name, expected in profile["required_claims"].items()):
                    raise ValueError()
                # These explicit grant markers cannot represent an interactive user.
                if claims.get("gty") == "client-credentials" or claims.get("grant_type") == "client_credentials":
                    raise ValueError()
                username = claims.get("preferred_username", "")
                if isinstance(username, str) and username.startswith("service-account-"):
                    raise ValueError()
                groups = claims.get(profile.get("groups_claim", "groups"))
                if not isinstance(groups, list) or len(groups) > 1000 or any(
                    not isinstance(group, str) or not group.startswith("/") or len(group) > 1024 for group in groups
                ):
                    raise ValueError()
                principal = claims.get(profile["username_claim"]) if profile.get("username_claim") else claims["sub"]
                if not isinstance(principal, str) or not 1 <= len(principal) <= 256 or principal.startswith("service-account-"):
                    raise ValueError()
                # Server-owned value replaces any same-named incoming claim.
                return {**claims, "__keep_actor_username": principal}, groups
            except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue
        fail(401, "invalid_actor_proof", "Invalid actor proof")


actor_proof_verifier = ActorProofVerifier()


def integration_identity(scope, *, delegated=False):
    def authenticate(request: Request):
        reject_impersonation(request)
        try:
            settings = get_silence_integrations()
            if settings is None:
                fail(401, "unauthenticated", "Integration client is not registered")
            client = settings.authenticate_client(request.headers.get("X-API-KEY"))
        except IntegrationConfigurationError:
            fail(503, "integration_configuration_unavailable", "Integration configuration unavailable")
        if client is None:
            fail(401, "unauthenticated", "Invalid integration credential")
        if scope not in client["scopes"]:
            fail(403, "forbidden", "Insufficient integration permissions")
        team_ids = frozenset(client["team_ids"])
        entity = AuthenticatedEntity(tenant_id=settings.tenant_id, email=client["id"],
            api_key_name=client["id"], role="viewer", integration_client_id=client["id"],
            integration_origin=client["origin"], service_scopes=frozenset(client["scopes"]),
            delegated_visible_teams=team_ids, delegated_writable_teams=frozenset())
        entity.integration_configuration_digest = settings.digest
        if delegated:
            claims, groups = actor_proof_verifier.verify(request.headers.get("X-Keep-Actor-Token"),
                [settings.profiles[reference] for reference in client["proof_profile_refs"]])
            roles = settings.policy.roles_for_groups(set(groups))
            role = next((role for role in ("admin", "responder", "noc", "viewer") if role in roles), None)
            if role is None or not get_role_by_role_name(role).has_scopes([scope]):
                fail(403, "forbidden", "Operator is not permitted to perform this command")
            members = settings.policy.teams_for_groups(set(groups))
            visible = team_ids if role == "admin" or settings.policy.visibility == "all" else team_ids & settings.policy.visible_teams(members)
            writable = team_ids if role == "admin" else team_ids & members
            entity.role = role
            entity.email = claims["__keep_actor_username"]
            entity.delegated_visible_teams = visible
            entity.delegated_writable_teams = writable
            entity.verified_silence_actor = SilenceActor(kind="user", subject=claims["sub"],
                issuer=claims["iss"], display_name=entity.email)
        return entity
    return authenticate
