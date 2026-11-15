"""Offline test models; never imported by a shipped runtime."""
from companion.extensions.synthesis import _VIRTUAL_PATH, _REVIEW_TASK, _json

def _scripted_models(document: str, keys: list[str]):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class Scripted(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            # No executor, tokenizer download, provider client, socket or sleep.
            message = self.responses[self.i]
            self.i += 1
            return ChatResult(generations=[ChatGeneration(message=message)])

    def tool(name, args, ident):
        return {"name": name, "args": args, "id": ident, "type": "tool_call"}

    parent = Scripted(responses=[
        AIMessage(content="", tool_calls=[tool("read_file", {"file_path": _VIRTUAL_PATH}, "r")]),
        AIMessage(content="", tool_calls=[
            tool("task", {"description": _REVIEW_TASK, "subagent_type": "coverage-review"}, "t")]),
        AIMessage(content=_json({"selected": keys})),
    ])
    child = Scripted(responses=[AIMessage(content=_json({"reviewed": keys}))])
    return parent, child


def review_model(responder):
    """Wrap a simple fake (``await responder.ainvoke(messages, **kwargs)`` -> ``.content``) as a real
    LangChain chat model for the Deep Agents pattern review.

    The fake still receives exactly the messages the agent sends to the provider, plus the bound
    model settings (e.g. ``max_tokens``). A JSON-object reply becomes the single structured-answer
    tool call a real provider returns under ``tool_choice="any"``; anything else is returned as plain
    text, which the review rejects. Fakes that raise simulate provider failures.
    """
    import json
    from typing import Any
    from uuid import uuid4
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.utils.function_calling import convert_to_openai_tool

    class ReviewModel(BaseChatModel):
        responder: Any
        model_name: str = 'scripted-review-model'

        @property
        def _llm_type(self) -> str:
            return 'scripted-review'

        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self.bind(tools=[convert_to_openai_tool(t) for t in tools], tool_choice=tool_choice, **kwargs)

        def _generate(self, *args, **kwargs):
            raise NotImplementedError('The pattern review is async-only.')

        async def _agenerate(self, messages, stop=None, run_manager=None, tools=(), tool_choice=None, **kwargs):
            reply = await self.responder.ainvoke(messages, **kwargs)
            content = getattr(reply, 'content', None)
            try:
                args = json.loads(content) if isinstance(content, str) else None
            except ValueError:
                args = None
            names = [t['function']['name'] for t in tools]
            if isinstance(args, dict) and len(names) == 1:
                message = AIMessage(content='', tool_calls=[{'name': names[0], 'args': args,
                                                             'id': 'call-' + uuid4().hex, 'type': 'tool_call'}])
            else:
                message = AIMessage(content=content if isinstance(content, str) else '')
            return ChatResult(generations=[ChatGeneration(message=message)])

    return ReviewModel(responder=responder)
