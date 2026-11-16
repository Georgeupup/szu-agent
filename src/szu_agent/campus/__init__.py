"""校园服务层：统一认证登录 + 各子系统适配器。不依赖任何外部程序。"""

from __future__ import annotations

from .errors import AGENT_HINTS, CampusError
from .session import SessionStore, agent_home


def make_campus(*, mock: bool, store: SessionStore | None = None, contact=None):
    store = store or SessionStore(mock=mock)
    if mock:
        from .mock import MockCampus

        return MockCampus(store, contact)
    from .http import CampusHttp
    from .service import RealCampus

    return RealCampus(CampusHttp(store), contact)


def make_cas(*, mock: bool, store: SessionStore | None = None, realm: str = "main"):
    """realm="webvpn"：登录学校 WebVPN（事务中心等系统用），见 cas.CasClient。"""
    store = store or SessionStore(mock=mock)
    if mock:
        from .mock import MockCas

        return MockCas(store, realm=realm)
    from .cas import CasClient

    return CasClient(store, realm=realm)


def make_mfa(*, mock: bool, store: SessionStore | None = None):
    store = store or SessionStore(mock=mock)
    if mock:
        from .mock import MockMfa

        return MockMfa(store)
    from .mfa import MfaClient

    return MfaClient(store)


__all__ = ["make_mfa", "AGENT_HINTS", "CampusError", "SessionStore", "agent_home", "make_campus", "make_cas"]
