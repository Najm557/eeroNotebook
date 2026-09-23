"""The application holds no host address and no model server address.

Requirement 3.8. All inference reaches the backend through the Inference
Gateway, so the one address pointing at the host belongs to the gateway service
and to nothing else. Task 2.4 verified that against the running container; this
file is what keeps it true after the next edit to the compose project, where the
tempting "fix" for an inference problem is to point the application straight at
the model server and quietly lose the boundary.

The check reads deploy/docker-compose.yml rather than a live container, so it
runs anywhere. The application's other source of addresses is the stored
provider credential, which is data rather than configuration and cannot be
asserted here.
"""

from pathlib import Path

import yaml  # type: ignore[import-untyped]

DEPLOY_COMPOSE = Path(__file__).parent.parent / "deploy" / "docker-compose.yml"

APP_SERVICE = "eeronotebook-app"
GATEWAY_SERVICE = "eeronotebook-inference"

# What "a host address or a model server address" looks like in this
# deployment: the runtime's host alias, the Dev Server's LAN address (task 1.3),
# and Ollama's port.
HOST_ADDRESS_MARKERS = (
    "host.docker.internal",
    "host-gateway",
    "10.17.8.52",
    ":11434",
)


def _compose() -> dict:
    return yaml.safe_load(DEPLOY_COMPOSE.read_text(encoding="utf-8"))


def _service(name: str) -> dict:
    services = _compose()["services"]
    assert name in services, f"{name} is not defined in {DEPLOY_COMPOSE}"
    return services[name]


def _environment(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        # Compose accepts a list of KEY=VALUE strings as well as a mapping.
        pairs = (item.partition("=") for item in env)
        return {name.strip(): raw.strip() for name, _, raw in pairs}
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def _markers_in(text: str) -> list[str]:
    return [marker for marker in HOST_ADDRESS_MARKERS if marker in text]


class TestApplicationHoldsNoHostAddress:
    """Requirement 3.8, read off the deployment's own configuration."""

    def test_app_environment_names_no_host_or_model_server_address(self):
        for name, value in _environment(_service(APP_SERVICE)).items():
            found = _markers_in(value)
            assert not found, (
                f"{APP_SERVICE} environment {name} carries {found}, which "
                "addresses the host directly instead of going through "
                f"{GATEWAY_SERVICE} (Requirement 3.8)"
            )

    def test_app_container_is_given_no_route_to_the_host(self):
        """Without the host alias the application cannot reach the backend at
        all, so the boundary holds even if something is misconfigured."""
        assert not _service(APP_SERVICE).get("extra_hosts"), (
            f"{APP_SERVICE} declares extra_hosts, which would give it a route "
            "to the host that only the gateway is meant to have"
        )

    def test_the_gateway_is_the_only_service_addressing_the_backend(self):
        services = _compose()["services"]
        for name, service in services.items():
            if name == GATEWAY_SERVICE:
                continue
            found = _markers_in(yaml.safe_dump(service))
            assert not found, f"{name} carries {found}; only {GATEWAY_SERVICE} may"

    def test_the_gateway_does_address_the_backend(self):
        """Guards the three checks above against passing vacuously - they would
        all hold in a compose file with no inference backend at all."""
        gateway = yaml.safe_dump(_service(GATEWAY_SERVICE))
        assert _markers_in(gateway), (
            f"{GATEWAY_SERVICE} names no backend address, so the checks above "
            "prove nothing about where inference goes"
        )
