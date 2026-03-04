from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest


class DeltaDefiAuth(AuthBase):

    def __init__(self, api_key: str):
        self.api_key: str = api_key

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        headers = {}
        if request.headers is not None:
            headers.update(request.headers)
        headers["X-API-KEY"] = self.api_key
        request.headers = headers
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        return request  # WS auth is via query param ?api_key=

    def ws_auth_query_param(self) -> str:
        return f"api_key={self.api_key}"
