"""OAuth2 token endpoint (password + client-credentials grants) issuing RS256 JWTs, JWKS
publication, and RBAC enforcement.

The API is its own authorisation server for the hackathon; because tokens are standard RS256
JWTs verified against a JWKS, swapping in Keycloak/Auth0/Cognito (OIDC) only changes the issuer
URL and JWKS location (ADR-0005).
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache

import bcrypt
import jwt
from cryptography.hazmat.primitives import serialization
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from voltpath.config import settings
from voltpath.infra import ensure_jwt_key

bearer = HTTPBearer(auto_error=False)
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt(rounds=10))


@dataclass(frozen=True)
class Principal:
    sub: str
    user_id: int | None
    tenant_id: int | None
    role: str
    permissions: frozenset[str] = field(default_factory=frozenset)
    name: str = ""
    actor_type: str = "user"   # user | agent | service

    def can(self, perm: str) -> bool:
        return perm in self.permissions

    def as_agent(self) -> Principal:
        """The copilot acts with the *caller's* permissions (never more), tagged for audit."""
        return Principal(self.sub, self.user_id, self.tenant_id, self.role, self.permissions, self.name, "agent")


class Keys:
    def __init__(self):
        path = ensure_jwt_key()
        self.private = serialization.load_pem_private_key(path.read_bytes(), password=None)
        self.public = self.private.public_key()
        nums = self.public.public_numbers()
        der = self.public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        import hashlib

        self.kid = hashlib.sha256(der).hexdigest()[:16]
        self.jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid,
                    "n": _b64u(nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")),
                    "e": _b64u(nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big"))}


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@lru_cache
def keys() -> Keys:
    return Keys()


def issue_token(*, sub: str, user_id: int | None, tenant_id: int | None, role: str, name: str) -> tuple[str, int]:
    s = settings()
    now = int(time.time())
    claims = {"iss": s.jwt_issuer, "aud": s.jwt_audience, "sub": sub, "iat": now, "nbf": now,
              "exp": now + s.jwt_ttl_s, "jti": uuid.uuid4().hex, "tid": tenant_id, "uid": user_id, "role": role,
              "name": name}
    k = keys()
    return jwt.encode(claims, k.private, algorithm="RS256", headers={"kid": k.kid}), s.jwt_ttl_s


def decode_token(token: str) -> dict:
    s = settings()
    try:
        return jwt.decode(token, keys().public, algorithms=["RS256"], audience=s.jwt_audience, issuer=s.jwt_issuer,
                          options={"require": ["exp", "iat", "sub", "aud", "iss"]})
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(401, "token expired", headers={"WWW-Authenticate": "Bearer"}) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"}) from exc


def check_password(password: str, hashed: str | None) -> bool:
    if hashed is None:
        bcrypt.checkpw(password.encode(), _DUMMY_HASH)  # constant-ish time for unknown users
        return False
    return bcrypt.checkpw(password.encode(), hashed.encode())


async def principal_from_token(request: Request, token: str) -> Principal:
    from voltpath.api.deps import R

    claims = decode_token(token)
    perms = R.role_permissions.get(claims["role"], frozenset())
    return Principal(claims["sub"], claims.get("uid"), claims.get("tid"), claims["role"], perms,
                     claims.get("name", ""))


async def current_principal(request: Request,
                            creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Bearer"})
    p = await principal_from_token(request, creds.credentials)
    request.state.principal = p
    return p


def require(*perms: str):
    """Dependency factory: 403 unless the principal has every listed permission."""

    async def dep(p: Principal = Depends(current_principal)) -> Principal:
        missing = [x for x in perms if not p.can(x)]
        if missing:
            raise HTTPException(403, f"missing permission: {', '.join(missing)}")
        if p.tenant_id is None:
            raise HTTPException(403, "no tenant context")
        return p

    return dep
