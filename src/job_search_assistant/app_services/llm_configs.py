"""S3a configuration commands. No LLM client or connectivity checks."""

from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from job_search_assistant.core.errors import ValidationError
from job_search_assistant.core.idempotency import hash_request
from job_search_assistant.core.secrets import SecretStore


def text_field(value, limit=500):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 for char in value)):
        raise ValidationError("Invalid configuration field.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValidationError("Invalid configuration text.") from None
    return value.strip()


class LLMConfigService:
    def __init__(self, store, secrets: SecretStore):
        self._store, self._secrets = store, secrets

    def configs(self):
        with self._store.transaction() as tx:
            return [self._public(row) for row in tx.configs()]

    def selection(self):
        with self._store.transaction() as tx:
            return tx.selection()

    def _public(self, row):
        return {**row, "has_key": self._secrets.get(row["id"]) is not None}

    def command(self, action, payload, *, context, config_id=None):
        required = {"idempotency_key"}
        optional = set()
        if action == "create":
            required |= {"name", "api_url", "model"}
            optional = {"api_key"}
        elif action == "update":
            optional = {"name", "api_url", "model", "api_key"}
        elif action == "select":
            required |= {"config_id"}
        elif action != "delete":
            raise ValidationError("Unknown configuration action.")
        if (not isinstance(payload, dict) or required - payload.keys()
                or payload.keys() - required - optional):
            raise ValidationError("Unknown or missing configuration fields.")
        values = dict(payload)
        key = text_field(values.pop("idempotency_key"), 100)
        if config_id is not None:
            config_id = text_field(config_id, 100)
        for field in ("name", "api_url", "model"):
            if field in values:
                values[field] = text_field(values[field])
        if "api_url" in values:
            try:
                url = urlsplit(values["api_url"])
                valid = (url.scheme in ("http", "https") and url.hostname and url.port != 0
                         and not url.username and not url.password and not url.query
                         and not url.fragment and not any(c.isspace() for c in url.geturl()))
            except ValueError:
                valid = False
            if not valid:
                raise ValidationError("API URL must be HTTP(S), without credentials or query.")
        if "api_key" in values:
            secret = values["api_key"]
            if secret != "":
                text_field(secret, 8192)
                # Reject full AND partial masks, including the UI's fixed bullet mask.
                if any(char in secret for char in "*•●…"):
                    raise ValidationError("A mask cannot be used as a credential.")
        if action == "select" and values["config_id"] is not None:
            values["config_id"] = text_field(values["config_id"], 100)
        digest = hash_request({"id": config_id, "values": values, "actor": context.actor_id})
        identifier = "selection" if action == "select" else config_id or str(uuid4())
        with self._store.transaction() as tx:
            reservation, replay = tx.reserve(action, key, digest, context)
            if replay:
                return reservation.response
            if action == "select":
                before = tx.selection()
                tx.select(values["config_id"])
                result = tx.selection()
            else:
                before = None if action == "create" else self._public(tx.get(identifier))
                if action == "delete" or "api_key" in values:
                    old_secret = self._secrets.get(identifier)
                    tx.undo_secret = lambda: self._restore(identifier, old_secret)
                    if action == "delete" or values["api_key"] == "":
                        self._secrets.delete(identifier)
                    else:
                        self._secrets.set(identifier, values["api_key"])
                if action == "delete":
                    tx.delete(identifier)
                    result = {"id": identifier, "deleted": True}
                else:
                    now = datetime.now(UTC).isoformat()
                    row = ({k: v for k, v in before.items() if k != "has_key"}
                           if before else {"id": identifier, "created_at": now})
                    row.update({k: v for k, v in values.items() if k != "api_key"})
                    row["updated_at"] = now
                    tx.save(row)
                    result = self._public(row)
            tx.complete(reservation, action, identifier, before, result, context)
        return result

    def _restore(self, identifier, value):
        if value is None:
            self._secrets.delete(identifier)
        else:
            self._secrets.set(identifier, value)
