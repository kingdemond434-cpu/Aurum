from golddesk.analyst import ANALYST_SYSTEM
from golddesk.constitution import AI_DISCOVERY_CONSTITUTION
from golddesk.live import LiveDesk


def test_ai_discovers_quant_measures_boundary_is_explicit_and_operational():
    joined = " ".join(AI_DISCOVERY_CONSTITUTION)
    assert "primary source of thesis generation" in joined
    assert "never invents a directional thesis" in joined
    assert "evidence, not votes or trades" in joined
    assert "No finite strategy-family whitelist" in joined
    assert "There is no finite strategy-family whitelist" in ANALYST_SYSTEM
    assert "post-hoc" in ANALYST_SYSTEM


def test_production_default_never_replaces_ai_with_rule_direction():
    assert LiveDesk.fallback_when_blind is False
