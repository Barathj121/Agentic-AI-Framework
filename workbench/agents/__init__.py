"""Optional domain agents; importing the workbench does not load cloud SDKs."""

AGENTS = {
    "clarifi": {
        "name": "ClariFi financial investigation",
        "sector": "BFSI",
        "capabilities": ["insider_trading_indicators", "shell_company_indicators"],
        "entrypoint": "workbench.agents.clarifi.agent:ClariFiAgent",
    }
}


def run_agent(agent_id: str, payload: dict, *, approved=False, mode="vertex", model=None) -> dict:
    """Only locally registered implementations may run; no caller-provided imports."""
    from importlib import import_module

    if agent_id not in AGENTS:
        raise ValueError("Unknown registered agent")
    module, name = AGENTS[agent_id]["entrypoint"].split(":")
    implementation = getattr(import_module(module), name)
    return implementation(mode=mode, model=model).run(payload, approved=approved)
