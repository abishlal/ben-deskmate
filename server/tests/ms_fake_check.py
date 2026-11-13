"""Exercises the Microsoft To Do and OneNote tools against a small in-memory stand-in for Graph.

This checks the tools' own logic and the requests they send. It does not prove that the real
Graph service accepts them; that needs a real sign-in.

docker compose exec server python tests/ms_fake_check.py
"""
import asyncio
import json

import httpx

from app import ms_tools, msgraph, tools

lists = [{"id": "L1", "displayName": "Tasks", "wellknownListName": "defaultList"},
         {"id": "L2", "displayName": "Work", "wellknownListName": "none"}]
tasks: dict[str, dict] = {"L1": {}, "L2": {"T0": {"id": "T0", "title": "Existing work task", "status": "notStarted", "importance": "high"}}}
pages: dict[str, dict] = {"P0": {"id": "P0", "title": "Meeting notes", "lastModifiedDateTime": "2026-11-09T10:00:00Z",
                                 "html": "<html><head><title>Meeting notes</title></head><body><p>Ship &amp; test</p><div>Friday</div></body></html>"}}
log: list[str] = []


def handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer fake"
    path, method = request.url.path.removeprefix("/v1.0"), request.method
    log.append(f"{method} {path}")
    parts = path.strip("/").split("/")
    if parts[:3] == ["me", "todo", "lists"]:
        if len(parts) == 3:
            return httpx.Response(200, json={"value": lists})
        bucket = tasks[parts[3]]
        if len(parts) == 5 and method == "GET":
            assert request.headers["prefer"] == 'outlook.timezone="UTC"'
            return httpx.Response(200, json={"value": [t for t in bucket.values() if t["status"] != "completed"]})
        if len(parts) == 5 and method == "POST":
            task = {"id": f"T{len(bucket) + 1}", "status": "notStarted", **json.loads(request.content)}
            bucket[task["id"]] = task
            return httpx.Response(201, json=task)
        if method == "PATCH":
            bucket[parts[5]].update(json.loads(request.content))
            return httpx.Response(200, json=bucket[parts[5]])
        if method == "DELETE":
            del bucket[parts[5]]
            return httpx.Response(204)
    if parts[:3] == ["me", "onenote", "pages"]:
        if len(parts) == 3 and method == "GET":
            flt = request.url.params.get("$filter", "")
            found = [p for p in pages.values() if not flt or flt.split("'")[1] in p["title"].lower()]
            return httpx.Response(200, json={"value": [{k: v for k, v in p.items() if k != "html"} for p in found]})
        if len(parts) == 3 and method == "POST":
            assert request.headers["content-type"] == "text/html"
            body = request.content.decode()
            page = {"id": f"P{len(pages)}", "title": body.split("<title>")[1].split("</title>")[0],
                    "lastModifiedDateTime": "2026-11-10T00:00:00Z", "html": body}
            pages[page["id"]] = page
            return httpx.Response(201, json=page)
        if parts[-1] == "content" and method == "GET":
            return httpx.Response(200, text=pages[parts[3]]["html"])
        if parts[-1] == "content" and method == "PATCH":
            change = json.loads(request.content)[0]
            assert change["target"] == "body" and change["action"] == "append"
            pages[parts[3]]["html"] = pages[parts[3]]["html"].replace("</body>", change["content"] + "</body>")
            return httpx.Response(204)
        if method == "DELETE":
            del pages[parts[3]]
            return httpx.Response(204)
    return httpx.Response(404, json={"error": {"message": f"fake Graph has no {method} {path}"}})


async def call(tool, **args) -> str:
    result = await tool.handler(args)
    return ("ERROR " if result.get("is_error") else "") + result["content"][0]["text"]


async def main() -> None:
    msgraph._http = httpx.AsyncClient(base_url="https://graph.microsoft.com/v1.0", transport=httpx.MockTransport(handler))
    msgraph._access_token = lambda: "fake"

    names = [t.name for t in tools.active_tools(tools.Mode("graph", "graph"))]
    mixed = [t.name for t in tools.active_tools(tools.Mode("local", "desktop", True))]
    assert len(set(mixed)) == len(mixed), mixed
    assert {"list_events", "add_event", "list_note_sections", "add_todo", "search_history", "set_preference"} <= set(mixed)
    from app import planner, worklog
    assert len(names) == len(set(names)) == len(tools.ALL_TOOLS) + len(planner.TOOLS) + len(worklog.TOOLS), names
    print("tool set when signed in:", ", ".join(names))

    print(await call(ms_tools.add_todo, title="Send weekly report", due="2026-11-10T18:00", priority="high"))
    sent = tasks["L1"]["T1"]
    assert sent["dueDateTime"] == {"dateTime": "2026-11-10T12:30:00", "timeZone": "UTC"}, sent  # 18:00 IST
    assert sent["isReminderOn"] is True and sent["importance"] == "high"
    print(await call(ms_tools.add_todo, title="Review PR", list="work"))
    print(await call(ms_tools.add_todo, title="x", list="nope"))
    print(await call(ms_tools.list_todos))
    print(await call(ms_tools.update_todo, id="#t1", done=True))
    assert tasks["L1"]["T1"]["status"] == "completed"
    print(await call(ms_tools.update_todo, id="t99", done=True))
    print(await call(ms_tools.delete_todo, id="t2"))
    print(await call(ms_tools.list_todos))

    print(await call(ms_tools.search_notes, query="Meeting"))
    print(await call(ms_tools.read_note, id="n1"))
    print(await call(ms_tools.update_note, id="n1", body="Also: book room <B>"))
    print(await call(ms_tools.read_note, id="n1"))
    print(await call(ms_tools.save_note, title="Groceries & more", body="Milk\nEggs"))
    assert "<title>Groceries &amp; more</title>" in pages["P1"]["html"] and "<p>Milk</p><p>Eggs</p>" in pages["P1"]["html"]
    print(await call(ms_tools.delete_note, id="n2"))

    msgraph._access_token = lambda: None
    print(await call(ms_tools.list_todos))
    print(f"\n{len(log)} requests sent to the fake Graph; all assertions passed")


asyncio.run(main())
