"""The same model-facing tools can be called in process or through HTTP."""

from .control import Rejected, check_schema


def obj(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


STR = {"type": "string"}
SCHEMAS = {
    "list_capabilities": ("List available device capabilities and their argument contracts.", obj({})),
    "observe": ("Read visible device state and obtain a fresh execution context.",
                obj({"capability": STR}, ["capability"])),
    "execute": ("Submit a device operation; acceptance does not mean task success. Reuse request_id for retries.",
                obj({"request_id": STR, "capability": STR, "observation_id": STR,
                     "arguments": {"type": "object"},
                     "timeout_s": {"type": "number", "minimum": 0.01, "maximum": 3600}},
                    ["request_id", "capability", "observation_id", "arguments"])),
    "execution_status": ("Read progress and whether controller execution has settled.",
                         obj({"execution_id": STR}, ["execution_id"])),
    "wait_execution": ("Wait for completion or an uncertain stop; wait timeout does not cancel.",
                       obj({"execution_id": STR, "timeout_s": {"type": "number", "minimum": 0.01,
                                                                  "maximum": 60}}, ["execution_id"])),
    "cancel_execution": ("Request cancellation; wait for settled=true before transferring control.",
                         obj({"execution_id": STR}, ["execution_id"])),
}


class RuntimeTools:
    def __init__(self, runtime, owner="astra"):
        self.runtime, self.owner = runtime, owner

    def schemas(self):
        return [{"type": "function", "function": {"name": name, "description": description,
                                                  "parameters": schema}}
                for name, (description, schema) in SCHEMAS.items()]

    async def call(self, name, arguments):
        if name not in SCHEMAS:
            raise Rejected("unknown_tool")
        check_schema(arguments, SCHEMAS[name][1])
        if name == "list_capabilities":
            return {"capabilities": self.runtime.catalog(), "topology": self.runtime.topology()}
        if name == "observe":
            return await self.runtime.observe(**arguments)
        if name == "execute":
            return await self.runtime.submit(owner=self.owner, **arguments)
        if name == "execution_status":
            return self.runtime.status(**arguments)
        if name == "wait_execution":
            return await self.runtime.wait(**arguments)
        return await self.runtime.cancel(owner=self.owner, **arguments)


def create_app(runtime, *, owner="astra", token=None, max_body_bytes=2_097_152):
    """Optional FastAPI facade. Local tool calls do not need HTTP or this dependency."""
    from fastapi import FastAPI, HTTPException, Request
    import secrets
    bridge = RuntimeTools(runtime, owner)
    from . import __version__
    app = FastAPI(title="Robot Runtime", version=__version__)

    @app.middleware("http")
    async def body_limit(request, call_next):
        from fastapi.responses import JSONResponse
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > max_body_bytes:
                return JSONResponse({"detail": "request_too_large"}, status_code=413)
            body.extend(chunk)
        request._body = bytes(body)
        return await call_next(request)

    def authorize(request):
        if token and not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "invalid authorization")
    app.state.authorize = authorize

    @app.get("/livez")
    async def live():
        return {"status": "alive", "version": __version__}

    @app.get("/readyz")
    async def ready(request: Request):
        authorize(request)
        if runtime.closing or runtime.recovery_pending:
            raise HTTPException(503, "runtime_not_ready")
        try:
            for cap, driver in runtime.capabilities.values():
                if cap.backend != "coordinated":
                    import asyncio
                    await asyncio.wait_for(driver.observe(), timeout=1)
        except Exception as exc:
            raise HTTPException(503, "device_observations_not_ready") from exc
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics(request: Request):
        from fastapi.responses import PlainTextResponse
        authorize(request)
        health = runtime.health()
        values = {"robot_runtime_active_executions": health["active_executions"],
                  "robot_runtime_recovery_pending": len(runtime.recovery_pending),
                  "robot_runtime_quarantined_resources": sum(r["state"] == "quarantined" for r in health["resources"].values())}
        return PlainTextResponse("".join(f"# TYPE {k} gauge\n{k} {v}\n" for k, v in values.items()),
                                 media_type="text/plain; version=0.0.4")

    @app.get("/health")
    async def health(request: Request):
        authorize(request)
        return runtime.health()

    @app.get("/tools")
    async def tools(request: Request):
        authorize(request)
        return {"tools": bridge.schemas(), "capabilities": runtime.catalog(), "topology": runtime.topology()}

    @app.post("/tools/{name}")
    async def call(name: str, request: Request):
        authorize(request)
        try:
            arguments = await request.json()
            return await bridge.call(name, arguments)
        except Rejected as exc:
            raise HTTPException(409, {"code": str(exc), "request_dispatched": False}) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "invalid request") from exc
    return app


def create_system_app(system, *, token=None, **kwargs):
    from fastapi import HTTPException, Request
    app = create_app(system.runtime, token=token, **kwargs)

    @app.post("/tasks")
    async def start_task(request: Request):
        app.state.authorize(request)
        try:
            body = await request.json()
            check_schema(body, obj({"task": STR, "request_id": STR, "plan": {"type": "object"},
                                   "deadline_s": {"type": "number", "minimum": 0.01, "maximum": 86400}}, ["task"]))
            return system.start(**body)
        except Rejected as exc:
            status = 429 if str(exc) == "task_capacity_exceeded" else 409 if str(exc) == "idempotency_conflict" else 422
            raise HTTPException(status, {"code": str(exc)}) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/recovery")
    async def recovery(request: Request):
        app.state.authorize(request)
        return {"executions": [system.runtime.status(e) for e in system.runtime.recovery_pending]}

    @app.post("/recovery/{execution_id}/reconcile")
    async def reconcile(execution_id: str, request: Request):
        app.state.authorize(request)
        try:
            return await system.runtime.reconcile(execution_id)
        except (Rejected, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/tasks/{task_id}/resume")
    async def resume(task_id: str, request: Request):
        app.state.authorize(request)
        try:
            return system.resume(task_id)
        except Rejected as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/tasks/{task_id}")
    async def task_status(task_id: str, request: Request):
        app.state.authorize(request)
        try:
            return system.status(task_id)
        except Rejected as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, request: Request):
        app.state.authorize(request)
        try:
            await system.cancel(task_id)
            return {"task_id": task_id, "cancel_requested": True}
        except Rejected as exc:
            raise HTTPException(409, str(exc)) from exc
    return app
