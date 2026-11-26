"""Adaptador da Responses API para o ciclo de conversa e memória que o Robin já usa."""

import json

import httpx
from google.genai import types

from .chatgpt_auth import ChatGPTAuthError, RECONNECT, RESOURCE
from .gemini import GeminiError, GeminiService, WatchdogTimeout, _user_content

DEFAULT_CHATGPT_MODEL = "gpt-6.1-sol"
MEMORY_NAMESPACE = "robin_memory"


def _error(code: str, status: int = 0) -> GeminiError:
    if status == 401 or code in {"subscription_sharing_invalid_user", "chatpass_v2_scope_not_authorized"}:
        return GeminiError("chatgpt_auth", RECONNECT)
    if status == 429 or code == "subscription_sharing_usage_limit_exceeded":
        return GeminiError("chatgpt_quota", "A cota do ChatGPT foi atingida. Consulte Configurações → Uso no ChatGPT.")
    if status == 404 or code == "model_not_found":
        return GeminiError("model_unavailable", "Esse modelo não está disponível nesta conexão do ChatGPT.")
    if code == "subscription_sharing_usage_unavailable":
        return GeminiError("chatgpt_plan", "O plano conectado não está disponível para esta solicitação.")
    return GeminiError("chatgpt_request", "O ChatGPT não conseguiu concluir a solicitação. Tente novamente.")


def _events(response):
    """Lê mensagens SSE separadas por linha em branco, inclusive dados em múltiplas linhas."""
    data = []
    for line in response.iter_lines():
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            payload = "\n".join(data)
            data.clear()
            if payload != "[DONE]":
                yield json.loads(payload)
    if data and data != ["[DONE]"]:
        yield json.loads("\n".join(data))


def _items(history) -> list[dict]:
    items = []
    for content in history:
        for part in content.parts or []:
            if part.function_call:
                call = part.function_call
                items.append({"type": "function_call", "call_id": call.id, "name": call.name,
                              "namespace": MEMORY_NAMESPACE,
                              "arguments": json.dumps(call.args or {}, ensure_ascii=False)})
            elif part.function_response:
                result = part.function_response
                items.append({"type": "function_call_output", "call_id": result.id,
                              "output": json.dumps(result.response, ensure_ascii=False)})
            elif part.thought and part.thought_signature:
                items.append(json.loads(part.thought_signature))
            elif part.text and not part.thought:
                items.append({"role": "assistant" if content.role == "model" else "user", "content": part.text})
    return items


def _chunk(part: types.Part) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.ModelContent(parts=[part]))])


class _ResponsesChat:
    def __init__(self, account, model, config, history):
        self._account = account
        self._profile = account.profile_tag()
        self._model = model
        self._config = config
        self._history = list(history)

    def get_history(self, *, curated: bool):
        return list(self._history)

    def record_history(self, *, user_input, model_output, is_valid, **_options):
        if is_valid:
            self._history.extend([user_input, *model_output])

    def send_message_stream(self, message):
        user = _user_content(message)
        config = self._config
        body = {"model": self._model, "instructions": config.system_instruction,
                "input": _items([*self._history, user]), "store": False, "stream": True,
                "include": ["reasoning.encrypted_content"]}
        tools_off = config.tool_config and config.tool_config.function_calling_config.mode == "NONE"
        if config.tools and not tools_off:
            functions = [
                {"type": "function", "name": tool.name, "description": tool.description,
                 "parameters": tool.parameters_json_schema, "strict": False}
                for group in config.tools for tool in group.function_declarations or []
            ]
            body["tools"] = [{"type": "namespace", "name": MEMORY_NAMESPACE,
                              "description": "Ferramentas disponíveis para o turno no Robin.", "tools": functions}]
        access = self._account.token(expected_profile=self._profile)
        with httpx.stream("POST", RESOURCE + "/responses", json=body, timeout=60,
                          headers={"Authorization": f"Bearer {access}"}) as response:
            if response.is_error:
                response.read()
                try:
                    code = (response.json().get("error") or {}).get("code", "")
                except ValueError:
                    code = ""
                error = _error(code, response.status_code)
                if error.code == "chatgpt_auth":
                    self._account.invalidate(access)
                raise error
            parts = []
            for event in _events(response):
                kind = event.get("type")
                if kind in {"response.output_text.delta", "response.refusal.delta"} and event.get("delta"):
                    part = types.Part.from_text(text=event["delta"])
                    parts.append(part)
                    yield _chunk(part)
                elif kind in {"response.failed", "response.incomplete", "error"}:
                    error = event.get("response", {}).get("error") or event
                    failure = _error(error.get("code", ""))
                    if failure.code == "chatgpt_auth":
                        self._account.invalidate(access)
                    raise failure
                elif kind in {"response.reasoning_summary_text.delta", "response.in_progress",
                              "response.output_item.added"} and (kind != "response.output_item.added"
                              or event.get("item", {}).get("type") == "reasoning"):
                    yield _chunk(types.Part(thought=True))
                elif kind == "response.completed":
                    ordered = []
                    for item in event["response"].get("output", []):
                        if item.get("type") == "reasoning" and item.get("encrypted_content"):
                            part = types.Part(thought=True, thought_signature=json.dumps(item).encode())
                            ordered.append(part)
                            yield _chunk(part)
                        elif item.get("type") == "function_call":
                            if item.get("namespace", MEMORY_NAMESPACE) != MEMORY_NAMESPACE or tools_off or not config.tools:
                                raise GeminiError("chatgpt_tool", "O ChatGPT solicitou uma ferramenta indisponível.")
                            args = json.loads(item["arguments"])
                            if not isinstance(args, dict):
                                raise GeminiError("chatgpt_tool", "O ChatGPT retornou argumentos inválidos.")
                            part = types.Part(function_call=types.FunctionCall(id=item["call_id"], name=item["name"], args=args))
                            ordered.append(part)
                            yield _chunk(part)
                        elif item.get("type") == "message":
                            for content in item.get("content", []):
                                text = content.get("text") or content.get("refusal")
                                if text:
                                    part = types.Part.from_text(text=text)
                                    ordered.append(part)
                                    if not parts:
                                        yield _chunk(part)
                    self.record_history(user_input=user,
                                        model_output=[types.ModelContent(parts=ordered or parts)], is_valid=True)
                    return
        raise GeminiError("chatgpt_stream", "A conexão com o ChatGPT terminou antes de concluir a resposta.")


class _ResponsesClient:
    def __init__(self, account):
        self._account = account
        self.chats = self

    def create(self, *, model, config, history):
        return _ResponsesChat(self._account, model, config, history)


class ChatGPTService(GeminiService):
    """Mantém o mesmo recorder, ações e eventos do chat, com transporte próprio."""
    _fallback_model = None
    _provider_name = "ChatGPT"
    _no_connection = RECONNECT

    def __init__(self, account, **kwargs):
        self._account = account
        super().__init__(api_key="chatgpt", rate_limit=0, manage_embeddings=False, **kwargs)

    def _make_client(self, _api_key):
        return _ResponsesClient(self._account)

    def _stream_error(self, error, model, *, private=False):
        if isinstance(error, GeminiError):
            return error
        if isinstance(error, ChatGPTAuthError):
            return GeminiError("chatgpt_auth", str(error))
        if isinstance(error, WatchdogTimeout):
            return GeminiError("timeout", "O ChatGPT demorou para responder.")
        if isinstance(error, (httpx.TimeoutException, httpx.NetworkError)):
            return GeminiError("offline", "O Robin não conseguiu se conectar ao ChatGPT.")
        return GeminiError("chatgpt_request", "Não foi possível concluir a resposta do ChatGPT.")

    def list_models(self) -> list[str]:
        try:
            token = self._account.token()
            response = httpx.get(RESOURCE + "/models", headers={"Authorization": f"Bearer {token}"}, timeout=10)
            response.raise_for_status()
            models = [item["slug"] for item in response.json().get("models", [])
                      if item.get("visibility") == "list" and item.get("slug")]
            return list(dict.fromkeys([*models, self.model]))
        except (ChatGPTAuthError, httpx.HTTPError, ValueError, OSError):
            return [self.model]
