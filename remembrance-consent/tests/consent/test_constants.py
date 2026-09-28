from types import MappingProxyType

import pytest

from app.consent.constants import (
    ACTION_PURPOSE,
    BANNED_PURPOSES,
    RIGHTS_ACTIONS,
    SYNTHESIS_ACTIONS,
    USE_ACTIONS,
    BannedPurpose,
    ConsentAction,
    DeclaredPurpose,
    PurposeScope,
    is_banned_purpose,
    normalize_purpose,
    parse_declared_purpose,
)


def test_denylist_is_exactly_the_four_banned_purposes_and_immutable():
    assert BANNED_PURPOSES == {"FINANCIAL", "PROPERTY_CLAIM", "LEGAL_REPRESENTATION", "COMMERCIAL_IMPERSONATION"}
    assert isinstance(BANNED_PURPOSES, frozenset)
    assert {p.value for p in BannedPurpose} == BANNED_PURPOSES


def test_banned_purposes_can_never_be_granted_or_declared():
    assert not BANNED_PURPOSES & {p.value for p in PurposeScope}
    assert not BANNED_PURPOSES & {p.value for p in DeclaredPurpose}


def test_action_table_is_total_and_read_only():
    assert set(ACTION_PURPOSE) == set(ConsentAction)
    assert isinstance(ACTION_PURPOSE, MappingProxyType)
    with pytest.raises(TypeError):
        ACTION_PURPOSE[ConsentAction.VIEW_MEMORIAL] = DeclaredPurpose.ERASURE  # type: ignore[index]
    assert USE_ACTIONS | RIGHTS_ACTIONS == set(ConsentAction)
    assert not USE_ACTIONS & RIGHTS_ACTIONS
    assert SYNTHESIS_ACTIONS < USE_ACTIONS
    # Use purposes are exactly the grantable scopes.
    assert {ACTION_PURPOSE[a].value for a in USE_ACTIONS} == {p.value for p in PurposeScope}


@pytest.mark.parametrize(
    "code",
    [
        "FINANCIAL",
        "financial",
        "  Financial  ",
        "FINAN​CIAL",  # zero-width space
        "ＦＩＮＡＮＣＩＡＬ",  # full-width letters
        "property-claim",
        "Property Claim",
        "legal.representation",
        "LEGAL_REPRESENTATION",
        "commercial/impersonation",
        "COMMERCIAL_IMPERSONATION_V2",
        "financial_transfer",
        "urgent-financial-matter",
    ],
)
def test_banned_variants_are_detected(code):
    assert is_banned_purpose(code)


@pytest.mark.parametrize("code", ["MEMORIAL_VIEW", "voice synthesis", "PROPERTY", "LEGAL", "IMPERSONATION", "COMMERCIAL", ""])
def test_non_banned_codes(code):
    assert not is_banned_purpose(code)


def test_normalization_and_parsing():
    assert normalize_purpose(" voice-synthesis ") == "VOICE_SYNTHESIS"
    assert normalize_purpose("--data..portability--") == "DATA_PORTABILITY"
    assert parse_declared_purpose("script generation") is DeclaredPurpose.SCRIPT_GENERATION
    assert parse_declared_purpose("FINANCIAL") is None
    assert parse_declared_purpose("anything else") is None
