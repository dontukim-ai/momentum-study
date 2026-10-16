"""Preserve configured proxy/CA trust while refusing authenticated redirects."""

import ssl
import urllib.request
from urllib.parse import urlparse

from .contracts import AgentError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise AgentError("Authenticated API redirects are disabled")


def open_authenticated(request: urllib.request.Request, timeout: int):
    url = urlparse(request.full_url)
    allowed = {("api.openai.com", 443), ("openapivts.koreainvestment.com", 29443),
               ("openapi.koreainvestment.com", 9443)}
    if url.scheme != "https" or (url.hostname, url.port or 443) not in allowed:
        raise AgentError("Authenticated API destination is not allowed")
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return opener.open(request, timeout=timeout)
