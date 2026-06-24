"""
========================================
web/letters.py — 信件（letter）读写
========================================

- /api/letters：列出信件
- /api/letter (POST)：写信
- /letters：信件页（兼容入口）
- /api/letter/{id} (PATCH/DELETE)：编辑 / 删除信件

对外暴露：register(mcp)。
========================================
"""

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

try:
    from utils import strip_wikilinks  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import strip_wikilinks  # type: ignore


def register(mcp) -> None:

    @mcp.custom_route("/api/letters", methods=["GET"])
    async def api_letters(request: Request) -> Response:
        """List all letters, newest first. Supports ?author=user|claude filter."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        author = request.query_params.get("author", "").strip().lower()
        try:
            all_b = await sh.bucket_mgr.list_all(include_archive=False)
            letters = [b for b in all_b if b["metadata"].get("type") == "letter"]
            if author in ("user", "claude"):
                letters = [b for b in letters if b["metadata"].get("author") == author]
            letters.sort(
                key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""),
                reverse=True,
            )
            result = []
            for b in letters:
                m = b["metadata"]
                result.append({
                    "id": b["id"],
                    "author": m.get("author", ""),
                    "user_name": m.get("user_name", ""),
                    "title": m.get("title", "") or m.get("name", ""),
                    "date": m.get("letter_date") or m.get("created", "")[:10],
                    "created": m.get("created", ""),
                    "content": strip_wikilinks(b.get("content", "")),
                })
            return JSONResponse({"letters": result, "total": len(result)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/letter", methods=["POST"])
    async def api_letter_create(request: Request) -> Response:
        """Create a letter from the dashboard."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        author = (body.get("author") or "").strip().lower()
        content = (body.get("content") or "").strip()
        if author not in ("user", "claude"):
            return JSONResponse({"error": "author must be 'user' or 'claude'"}, status_code=400)
        if not content:
            return JSONResponse({"error": "content required"}, status_code=400)
        user_name = (body.get("user_name") or "").strip()
        title = (body.get("title") or "").strip()[:120]
        date = (body.get("date") or "").strip()
        extra = {"author": author}
        if user_name:
            extra["user_name"] = user_name
        if title:
            extra["title"] = title
        if date:
            extra["letter_date"] = date
        try:
            bid = await sh.bucket_mgr.create(
                content=content,
                tags=["__letter__"],
                importance=10,
                domain=["letter"],
                valence=0.5,
                arousal=0.3,
                name=(title[:60] or f"{author}_{date or 'letter'}"),
                bucket_type="letter",
                source_tool="letter",
            )
            await sh.bucket_mgr.update(bid, **extra)
            try:
                await sh.embedding_engine.generate_and_store(bid, content)
            except Exception:
                pass
            return JSONResponse({"ok": True, "id": bid})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/letters", methods=["GET"])
    async def letters_page(request: Request) -> Response:
        """Legacy alias: /letters 永久跳到 dashboard 的「信」分页。

        我把 letters 合并进 dashboard 的一个 tab 后，这条老路径只保留 301 软迁移，
        避免独立维护两套 HTML/JS。
        """
        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/#letters", status_code=301)


    @mcp.custom_route("/api/letter/{letter_id}", methods=["PATCH"])
    async def api_letter_edit(request: Request) -> Response:
        """Edit an existing letter (content / title / author / date / user_name)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        letter_id = request.path_params["letter_id"]
        bucket = await sh.bucket_mgr.get(letter_id)
        if not bucket or bucket["metadata"].get("type") != "letter":
            return JSONResponse({"error": "letter not found"}, status_code=404)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        updates: dict = {}
        if "content" in body and isinstance(body["content"], str) and body["content"].strip():
            updates["content"] = body["content"].strip()
        if "title" in body and isinstance(body["title"], str):
            updates["title"] = body["title"].strip()[:120]
        if "author" in body:
            a = str(body["author"]).strip().lower()
            if a in ("user", "claude"):
                updates["author"] = a
        if "user_name" in body and isinstance(body["user_name"], str):
            updates["user_name"] = body["user_name"].strip()
        if "date" in body and isinstance(body["date"], str):
            updates["letter_date"] = body["date"].strip()

        if not updates:
            return JSONResponse({"error": "nothing to update"}, status_code=400)

        try:
            ok = await sh.bucket_mgr.update(letter_id, **updates)
            if not ok:
                return JSONResponse({"error": "update failed"}, status_code=500)
            if "content" in updates:
                try:
                    await sh.embedding_engine.generate_and_store(letter_id, updates["content"])
                except Exception:
                    pass
                try:
                    sh.dehydrator.invalidate_cache(bucket["content"])
                except Exception:
                    pass
            return JSONResponse({"ok": True, "id": letter_id, "updated": list(updates.keys())})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/letter/{letter_id}", methods=["DELETE"])
    async def api_letter_delete(request: Request) -> Response:
        """Hard delete a letter. Requires ?confirm=true."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if request.query_params.get("confirm", "").lower() not in ("true", "1", "yes"):
            return JSONResponse({"error": "confirm=true required"}, status_code=400)
        letter_id = request.path_params["letter_id"]
        bucket = await sh.bucket_mgr.get(letter_id)
        if not bucket or bucket["metadata"].get("type") != "letter":
            return JSONResponse({"error": "letter not found"}, status_code=404)
        try:
            ok = await sh.bucket_mgr.delete(letter_id)
            if ok:
                sh.embedding_engine.delete_embedding(letter_id)
            return JSONResponse({"ok": ok, "deleted": ok})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
