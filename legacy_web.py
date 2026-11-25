"""Plugin Page request and response helpers for AstrBot's Quart dashboard."""

from quart import g, jsonify
from quart import request as quart_request


class _DashboardRequest:
    @property
    def username(self):
        return getattr(g, "username", None)

    @property
    def query(self):
        return quart_request.args

    async def json(self, default=None):
        value = await quart_request.get_json(silent=True)
        return default if value is None else value


request = _DashboardRequest()


def json_response(data, *, status_code=200):
    response = jsonify(data)
    response.status_code = status_code
    return response


def error_response(message, *, status_code=400):
    return json_response(
        {"status": "error", "message": message, "data": None}, status_code=status_code
    )
