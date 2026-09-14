"""Shared MQTT connection configuration with optional verified TLS."""

from dataclasses import dataclass
import os
import ssl


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MQTTSettings:
    host: str
    port: int
    transport: str
    username: str = ""
    password: str = ""
    tls: bool = False
    ca_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None

    @classmethod
    def from_env(
        cls,
        prefix: str,
        defaults: "MQTTSettings | None" = None,
    ) -> "MQTTSettings":
        default_host = defaults.host if defaults else "home-logger"
        default_port = defaults.port if defaults else 8081
        port = int(os.environ.get(f"{prefix}_MQTT_PORT", str(default_port)))
        default_transport = (
            defaults.transport if defaults
            else ("tcp" if port == 1883 else "websockets")
        )
        return cls(
            host=os.environ.get(f"{prefix}_MQTT_HOST", default_host),
            port=port,
            transport=os.environ.get(f"{prefix}_MQTT_TRANSPORT", default_transport),
            username=os.environ.get(
                f"{prefix}_MQTT_USER",
                defaults.username if defaults else "",
            ),
            password=os.environ.get(
                f"{prefix}_MQTT_PASS",
                defaults.password if defaults else "",
            ),
            tls=_env_bool(
                f"{prefix}_MQTT_TLS",
                defaults.tls if defaults else False,
            ),
            ca_cert=os.environ.get(
                f"{prefix}_MQTT_CA_CERT",
                defaults.ca_cert if defaults else None,
            ),
            client_cert=os.environ.get(
                f"{prefix}_MQTT_CLIENT_CERT",
                defaults.client_cert if defaults else None,
            ),
            client_key=os.environ.get(
                f"{prefix}_MQTT_CLIENT_KEY",
                defaults.client_key if defaults else None,
            ),
        )

    def configure_client(self, client) -> None:
        if self.username:
            client.username_pw_set(self.username, self.password)
        if not self.tls:
            return
        if bool(self.client_cert) != bool(self.client_key):
            raise ValueError(
                "MQTT client certificate and key must be configured together"
            )
        client.tls_set(
            ca_certs=self.ca_cert,
            certfile=self.client_cert,
            keyfile=self.client_key,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        client.tls_insecure_set(False)
