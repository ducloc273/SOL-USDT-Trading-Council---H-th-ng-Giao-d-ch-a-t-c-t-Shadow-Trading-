import json, time, uuid
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from council import run_council

api = FastAPI()
MODEL_ID = "ai-council"


def last_user_text(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c
    return ""


@api.get("/v1/models")
def models():
    return {"object": "list",
            "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]}


@api.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    question = last_user_text(body.get("messages", []))
    cid, ts = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())

    def chunk(delta, finish=None):
        return "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": ts,
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

    if body.get("stream"):
        async def gen():
            yield chunk({"role": "assistant", "content": ""})
            async for piece in run_council(question):
                yield chunk({"content": piece})
            yield chunk({}, "stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    text = "".join([p async for p in run_council(question)])
    return {"id": cid, "object": "chat.completion", "created": ts, "model": MODEL_ID,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}
