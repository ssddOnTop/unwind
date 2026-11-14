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
