import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

_FLW_BASE = "https://api.flutterwave.com/v3"


class FlutterwaveError(Exception):
    pass


def _headers():
    return {
        "Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def _call(method, path, payload=None):
    url = _FLW_BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            msg = body.get("message", "Flutterwave API error")
            # `errors`/`data.complete_message` carry the actionable detail
            # (bad bank code, insufficient float, disabled transfers). Losing it
            # is what makes a failed withdrawal impossible to diagnose later.
            detail = body.get("data")
            if isinstance(detail, dict):
                extra = detail.get("complete_message") or detail.get("error")
                if extra and extra not in msg:
                    msg = f"{msg}: {extra}"
        except Exception:
            msg = f"Flutterwave API error (HTTP {e.code})"
        logger.warning("Flutterwave %s %s failed (HTTP %s): %s", method, path, e.code, msg)
        raise FlutterwaveError(msg)
    except Exception as e:
        logger.warning("Flutterwave %s %s unreachable: %s", method, path, e)
        raise FlutterwaveError(f"Could not reach Flutterwave: {e}")


def verify_transaction(tx_ref):
    encoded = urllib.parse.quote(tx_ref)
    return _call("GET", f"/transactions/verify_by_reference?tx_ref={encoded}")


def initiate_transfer(amount, bank_code, account_number, account_name, reference):
    return _call("POST", "/transfers", {
        "account_bank": bank_code,
        "account_number": account_number,
        "amount": float(amount),
        "narration": "Axira Withdrawal",
        "currency": "NGN",
        "reference": reference,
        "beneficiary_name": account_name,
    })


def get_banks():
    return _call("GET", "/banks/NG")


def resolve_account(account_number, bank_code):
    return _call("POST", "/accounts/resolve", {
        "account_number": account_number,
        "account_bank": bank_code,
    })
