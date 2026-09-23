"""Keep credentials out of exported provider metadata and error messages."""
import re

PRIVATE_FIELDS = {"api_key", "apikey", "access_token", "insttoken", "authtoken",
                  "authorization", "cookie", "cookies", "x-els-apikey", "x-els-insttoken"}


def public_metadata(value, secrets=()):
    if isinstance(value, dict):
        return {key: public_metadata(item, secrets) for key, item in value.items()
                if str(key).casefold() not in PRIVATE_FIELDS}
    if isinstance(value, list):
        return [public_metadata(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return re.sub(r"([?&](?:api_key|apikey|access_token|insttoken|authtoken)=)[^&\s<>\"']*",
                      r"\1[redacted]", value, flags=re.I)
    return value


def safe_error(exc):
    code = getattr(exc, "code", None)
    return f"HTTP {code}" if isinstance(code, int) else type(exc).__name__
